"""Read-only, secret-safe index of durable stage-run sessions.

The history UI deliberately treats ``session.json`` files as untrusted input.
It walks only the documented directory depth, never follows symlinks, opens
files with ``O_NOFOLLOW``, and exposes a small allowlisted presentation model.
No method in this module saves a session or initializes a data/LLM provider.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tradingagents.graph.stage_session import PIPELINE_STAGES, StageSession, default_runs_dir

MAX_SESSION_BYTES = 5 * 1024 * 1024
MAX_INDEXED_SESSIONS = 10_000
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
_HISTORY_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_STATUS_VALUES = {"running", "completed", "failed", "partial", "not_started"}
_STAGE_STATUS_VALUES = {"not_run", "completed", "unavailable", "failed"}

_PROVENANCE_SCALARS = {
    "provider",
    "vendor",
    "tool",
    "kind",
    "ticker",
    "analysis_date",
    "status",
    "reason",
    "sample_size",
    "unique_authors",
    "count",
    "article_count",
    "observation_count",
    "window_start",
    "window_end",
    "as_of",
    "published_at",
    "period_start",
    "period_end",
    "fetch_id",
    "fetch_ids",
    "point_in_time_quality",
    "attempted_vendors",
    "vendor_chain",
    "input_stages",
    "actual_vendor_observed",
    "stale",
    "stale_indicators",
    "analysis_mode",
    "analysis_cutoff",
    "completed_at",
    "unavailable_at",
    "failed_at",
    "category",
    "source_series",
    "source_provider",
    "source_url",
    "canonical_url",
    "url",
}
_PROVENANCE_CONTAINERS = {
    "retail_social_signal",
    "media_tone",
    "official_disclosures",
    "editorial_media",
    "vn_macro",
    "sources",
    "source_results",
}


@dataclass(frozen=True)
class _Signature:
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class _CacheEntry:
    relative_path: str
    signature: _Signature | None
    item: dict[str, Any] | None


def _redact_credentials(value: str) -> str:
    # Import lazily so dashboard can construct the repository without a module
    # cycle, while keeping one canonical credential sanitizer for all UI data.
    from tradingagents.ui.dashboard import _redact_public_text

    sanitized = _redact_public_text(value)
    sanitized = re.sub(
        r"(?i)\bsk-(?:proj-|ant-|svcacct-)?[A-Za-z0-9_-]{16,}",
        "[redacted]",
        sanitized,
    )
    sanitized = re.sub(
        r"\bAIza[0-9A-Za-z_-]{20,}",
        "[redacted]",
        sanitized,
    )
    return sanitized


def _canonical_url(value: str) -> str | None:
    try:
        parsed = urlsplit(_redact_credentials(value))
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = f"{hostname}:{port}" if port is not None else hostname
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))[:2_000]


def _redact_text(value: str, *, limit: int = 100_000) -> str:
    sanitized = _redact_credentials(value)

    # Old reports can contain signed/tracking URLs whose parameter names are
    # unknown to today's sanitizer. History only needs a stable citation, so
    # remove every query and fragment while preserving the public origin/path.
    def sanitize_url(match: re.Match[str]) -> str:
        token = match.group(0)
        suffix = ""
        while token and token[-1] in ".,;:!?)]}":
            suffix = token[-1] + suffix
            token = token[:-1]
        return (_canonical_url(token) or "[redacted URL]") + suffix

    sanitized = re.sub(r"(?i)https?://[^\s<>'\"`]+", sanitize_url, sanitized)
    sanitized = re.sub(
        r"(?i)(?<![\w/:])/(?:Users|home|private|tmp|var|opt|etc|root|Volumes)/[^\s<>'\"`|,;)\]}]+",
        "[redacted path]",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)(?<!\w)~/[^\s<>'\"`|,;)\]}]+",
        "[redacted path]",
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)\b[A-Z]:\\[^\s<>'\"`|,;)\]}]+",
        "[redacted path]",
        sanitized,
    )

    return sanitized[:limit]


def _aware_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {field}")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ValueError(f"invalid {field}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"invalid {field}")
    return parsed


def _safe_url(value: str) -> str | None:
    return _canonical_url(value)


def _public_scalar(value: Any, *, url: bool = False) -> Any:
    if isinstance(value, str):
        return _safe_url(value) if url else _redact_text(value, limit=2_000)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _public_provenance(value: Any, *, depth: int = 0) -> Any:
    """Recursively expose only provenance fields, never raw evidence/profile data."""
    if depth > 6:
        return None
    if isinstance(value, dict):
        public: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key == "warnings":
                entries = item if isinstance(item, (list, tuple)) else [item]
                public[key] = [_redact_text(str(entry), limit=500) for entry in entries[:25]]
            elif key in _PROVENANCE_CONTAINERS:
                nested = _public_provenance(item, depth=depth + 1)
                if nested is not None and nested != [] and nested != {}:
                    public[key] = nested
            elif key in _PROVENANCE_SCALARS:
                if isinstance(item, (list, tuple)):
                    values = [_public_scalar(entry) for entry in item[:100]]
                    public[key] = [entry for entry in values if entry is not None]
                else:
                    sanitized = _public_scalar(
                        item, url=key in {"url", "source_url", "canonical_url"}
                    )
                    if sanitized is not None:
                        public[key] = sanitized
        return public
    if isinstance(value, (list, tuple)):
        return [
            item
            for item in (_public_provenance(entry, depth=depth + 1) for entry in value[:100])
            if item is not None and item != [] and item != {}
        ]
    return _public_scalar(value)


def _overall_status(stage_status: Mapping[str, str], active: bool) -> str:
    if active:
        return "running"
    if stage_status.get("risk") == "completed":
        return "completed"
    if any(value == "failed" for value in stage_status.values()):
        return "failed"
    if any(value in {"completed", "unavailable"} for value in stage_status.values()):
        return "partial"
    return "not_started"


class SessionHistoryRepository:
    """Index and present session files without exposing their filesystem paths."""

    def __init__(
        self,
        runs_dir: str | Path | None = None,
        *,
        active_run_ids: Callable[[], Iterable[str]] | Iterable[str] | None = None,
    ) -> None:
        self.runs_dir = Path(runs_dir) if runs_dir is not None else default_runs_dir()
        self.runs_dir = self.runs_dir.expanduser()
        self._active_run_ids = active_run_ids
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_lock = threading.RLock()

    def list_history(self, params: Mapping[str, str] | None = None) -> dict[str, Any]:
        filters = self._validate_filters(params or {})
        entries, skipped = self._scan()
        active = self._active_ids()
        items = [self._with_status(entry.item, active) for entry in entries if entry.item]

        query = filters["query"].casefold()
        if query:
            items = [
                item
                for item in items
                if query
                in " ".join(
                    str(item.get(key) or "") for key in ("ticker", "company_name", "run_id")
                ).casefold()
            ]
        if filters["mode"]:
            items = [item for item in items if item["analysis_mode"] == filters["mode"]]
        if filters["status"]:
            items = [item for item in items if item["status"] == filters["status"]]
        if filters["from"]:
            items = [item for item in items if item["analysis_date"] >= filters["from"]]
        if filters["to"]:
            items = [item for item in items if item["analysis_date"] <= filters["to"]]

        items.sort(
            key=lambda item: (
                _aware_timestamp(item["updated_at"], "updated_at").timestamp(),
                item["run_id"],
            ),
            reverse=True,
        )
        total = len(items)
        page = filters["page"]
        page_size = filters["page_size"]
        start = (page - 1) * page_size
        total_pages = max(1, math.ceil(total / page_size))
        return {
            "items": deepcopy(items[start : start + page_size]),
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "skipped_invalid": skipped,
        }

    def get_history(self, history_id: str) -> dict[str, Any] | None:
        if not isinstance(history_id, str) or not _HISTORY_ID_RE.fullmatch(history_id):
            return None
        entries, _ = self._scan()
        entry = next(
            (
                candidate
                for candidate in entries
                if candidate.item and candidate.item["history_id"] == history_id
            ),
            None,
        )
        if entry is None or entry.signature is None:
            return None
        try:
            session = self._secure_load(Path(entry.relative_path), entry.signature)
            self._validate_session(session, Path(entry.relative_path))
            return self._build_detail(session, history_id, self._active_ids())
        except Exception:  # noqa: BLE001 - one corrupt file never breaks the index
            return None

    def _active_ids(self) -> set[str]:
        try:
            values = (
                self._active_run_ids() if callable(self._active_run_ids) else self._active_run_ids
            )
            return {str(value) for value in (values or ())}
        except Exception:  # pragma: no cover - defensive callback isolation
            return set()

    @staticmethod
    def _validate_filters(params: Mapping[str, str]) -> dict[str, Any]:
        allowed = {"query", "mode", "status", "from", "to", "page", "page_size"}
        unexpected = sorted(set(params) - allowed)
        if unexpected:
            raise ValueError("Unsupported history filter: " + ", ".join(unexpected))
        query = str(params.get("query") or "").strip()
        if len(query) > 64:
            raise ValueError("History query must be at most 64 characters.")
        mode = str(params.get("mode") or "").strip().lower()
        if mode and mode not in {"close", "live"}:
            raise ValueError("History mode must be 'close' or 'live'.")
        status_value = str(params.get("status") or "").strip().lower()
        if status_value and status_value not in _STATUS_VALUES:
            raise ValueError("History status is invalid.")

        dates: dict[str, str] = {}
        for key in ("from", "to"):
            value = str(params.get(key) or "").strip()
            if value:
                try:
                    parsed = date.fromisoformat(value)
                except ValueError:
                    raise ValueError(f"History {key} must use YYYY-MM-DD.") from None
                if parsed.isoformat() != value:
                    raise ValueError(f"History {key} must use YYYY-MM-DD.")
                dates[key] = value
            else:
                dates[key] = ""
        if dates["from"] and dates["to"] and dates["from"] > dates["to"]:
            raise ValueError("History 'from' must not be after 'to'.")

        def positive_integer(key: str, default: int, maximum: int | None = None) -> int:
            raw = params.get(key)
            if raw in {None, ""}:
                return default
            try:
                value = int(str(raw))
            except ValueError:
                raise ValueError(f"History {key} must be a positive integer.") from None
            if value < 1 or (maximum is not None and value > maximum):
                suffix = f" no greater than {maximum}" if maximum else ""
                raise ValueError(f"History {key} must be positive{suffix}.")
            return value

        return {
            "query": query,
            "mode": mode,
            "status": status_value,
            **dates,
            "page": positive_integer("page", 1),
            "page_size": positive_integer("page_size", DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE),
        }

    def _scan(self) -> tuple[list[_CacheEntry], int]:
        with self._cache_lock:
            previous = dict(self._cache)
        next_cache: dict[str, _CacheEntry] = {}
        skipped = 0
        for candidates, (path, relative_path) in enumerate(self._iter_session_entries()):
            if candidates >= MAX_INDEXED_SESSIONS:
                break
            relative = relative_path.as_posix()
            try:
                signature = self._signature(path)
                cached = previous.get(relative)
                if cached is not None and cached.signature == signature:
                    entry = cached
                else:
                    session = self._secure_load(relative_path, signature)
                    self._validate_session(session, relative_path)
                    item = self._build_item(session, relative)
                    entry = _CacheEntry(relative, signature, item)
            except Exception:  # noqa: BLE001 - isolate malformed/untrusted sessions
                signature = None
                with suppress(OSError, ValueError):
                    signature = self._signature(path)
                entry = _CacheEntry(relative, signature, None)
            if entry.item is None:
                skipped += 1
            next_cache[relative] = entry

        with self._cache_lock:
            self._cache = next_cache
        valid = [entry for entry in next_cache.values() if entry.item is not None]
        return valid, skipped

    def _iter_session_entries(self) -> Iterable[tuple[Path, Path]]:
        root = self.runs_dir
        try:
            ticker_entries = os.scandir(root)
        except (OSError, ValueError):
            return
        yielded = 0
        with ticker_entries:
            for ticker_entry in ticker_entries:
                try:
                    if not ticker_entry.is_dir(follow_symlinks=False):
                        continue
                    date_entries = os.scandir(ticker_entry.path)
                except (OSError, ValueError):
                    continue
                with date_entries:
                    for date_entry in date_entries:
                        try:
                            if not date_entry.is_dir(follow_symlinks=False):
                                continue
                            run_entries = os.scandir(date_entry.path)
                        except (OSError, ValueError):
                            continue
                        with run_entries:
                            for run_entry in run_entries:
                                try:
                                    if not run_entry.is_dir(follow_symlinks=False):
                                        continue
                                    files = os.scandir(run_entry.path)
                                except (OSError, ValueError):
                                    continue
                                with files:
                                    for file_entry in files:
                                        if file_entry.name != "session.json":
                                            continue
                                        relative = Path(
                                            ticker_entry.name,
                                            date_entry.name,
                                            run_entry.name,
                                            "session.json",
                                        )
                                        yield Path(file_entry.path), relative
                                        yielded += 1
                                        if yielded >= MAX_INDEXED_SESSIONS:
                                            return

    @staticmethod
    def _signature(path: Path) -> _Signature:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SESSION_BYTES:
            raise ValueError("invalid session file")
        return _Signature(info.st_mtime_ns, info.st_size)

    def _secure_load(self, relative: Path, expected: _Signature) -> StageSession:
        parts = relative.parts
        if (
            len(parts) != 4
            or parts[-1] != "session.json"
            or any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in parts)
        ):
            raise ValueError("invalid session path")
        nofollow = getattr(os, "O_NOFOLLOW", None)
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if nofollow is None or directory_flag is None:  # pragma: no cover - POSIX target
            raise OSError("secure no-follow file opens are unavailable")
        common_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
        directories: list[int] = []
        descriptor = -1
        try:
            directories.append(os.open(self.runs_dir, common_flags | directory_flag))
            for component in parts[:-1]:
                directories.append(
                    os.open(
                        component,
                        common_flags | directory_flag,
                        dir_fd=directories[-1],
                    )
                )
            descriptor = os.open(parts[-1], common_flags, dir_fd=directories[-1])
            info = os.fstat(descriptor)
            actual = _Signature(info.st_mtime_ns, info.st_size)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > MAX_SESSION_BYTES
                or actual != expected
            ):
                raise ValueError("session changed while indexing")
            # Copy at most MAX+1 bytes from the already-open descriptor before
            # running the canonical migration loader. Loading /dev/fd directly
            # would let a concurrent append grow beyond the checked fstat size.
            # The original path is never reopened.
            chunks: list[bytes] = []
            remaining = MAX_SESSION_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > MAX_SESSION_BYTES:
                raise ValueError("session is too large")
            temporary_name = ""
            try:
                with tempfile.NamedTemporaryFile(mode="wb", delete=False) as handle:
                    temporary_name = handle.name
                    os.chmod(temporary_name, 0o600)
                    handle.write(payload)
                    handle.flush()
                return StageSession.load(temporary_name)
            finally:
                if temporary_name:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary_name)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            for directory in reversed(directories):
                os.close(directory)

    @staticmethod
    def _validate_session(session: StageSession, relative: Path) -> None:
        parts = relative.parts
        if len(parts) != 4 or parts[-1] != "session.json":
            raise ValueError("invalid session path")
        ticker, analysis_date, run_id, _ = parts
        if (
            session.ticker != ticker
            or session.analysis_date != analysis_date
            or session.run_id != run_id
        ):
            raise ValueError("session identity does not match its path")
        parsed_date = date.fromisoformat(analysis_date)
        if parsed_date.isoformat() != analysis_date:
            raise ValueError("invalid analysis date")
        _aware_timestamp(session.created_at, "created_at")
        _aware_timestamp(session.updated_at, "updated_at")
        if (
            not isinstance(session.state, dict)
            or not isinstance(session.stage_status, dict)
            or not isinstance(session.stage_metadata, dict)
            or not isinstance(session.completed_stages, list)
        ):
            raise ValueError("invalid session state")
        if set(session.stage_status) - set(PIPELINE_STAGES):
            raise ValueError("invalid stage status")
        for stage in PIPELINE_STAGES:
            if session.stage_status.get(stage, "not_run") not in _STAGE_STATUS_VALUES:
                raise ValueError("invalid stage status")

    @staticmethod
    def _company_name(state: Mapping[str, Any]) -> str | None:
        for key in ("company_name", "organization_name", "company_short_name"):
            value = state.get(key)
            if isinstance(value, str) and value.strip():
                return _redact_text(value.strip(), limit=200)
        context = state.get("instrument_context")
        if isinstance(context, str):
            match = re.search(
                r"\bCompany\s*:\s*(.{1,200}?)(?=;\s*|\.\s+Do not\b|$)",
                context,
                re.IGNORECASE,
            )
            if match:
                return _redact_text(match.group(1).strip(), limit=200)
        return None

    def _build_item(self, session: StageSession, relative: str) -> dict[str, Any]:
        from tradingagents.ui.dashboard import (
            _markdown_field,
            _price_target_summary,
            _recommendation,
            _report_text,
        )

        final_decision = _report_text(session.state, "final_trade_decision")
        recommendation, rating = _recommendation(final_decision)
        target_summary = _price_target_summary(final_decision)
        rating = _redact_text(rating, limit=120) if rating else None
        completed = sum(session.stage_status.get(stage) == "completed" for stage in PIPELINE_STAGES)
        return self._redact_history_paths(
            {
                "history_id": hashlib.sha256(relative.encode("utf-8")).hexdigest(),
                "run_id": _redact_text(session.run_id, limit=128),
                "ticker": _redact_text(session.ticker, limit=32),
                "company_name": self._company_name(session.state),
                "analysis_date": session.analysis_date,
                "analysis_mode": session.analysis_mode,
                "analysis_cutoff": session.analysis_cutoff,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "status": _overall_status(session.stage_status, False),
                "completed_stages": completed,
                "total_stages": len(PIPELINE_STAGES),
                "summary": {
                    "recommendation": recommendation,
                    "detailed_rating": rating,
                    "confidence": None,
                    "confidence_source": None,
                    **target_summary,
                    "time_horizon": _markdown_field(final_decision, "Time Horizon"),
                    "risk": None,
                },
            }
        )

    @staticmethod
    def _with_status(item: dict[str, Any], active: set[str]) -> dict[str, Any]:
        result = deepcopy(item)
        if result["run_id"] in active:
            result["status"] = "running"
        return result

    def _build_detail(
        self, session: StageSession, history_id: str, active: set[str]
    ) -> dict[str, Any]:
        from tradingagents.ui.dashboard import _progress_rows, build_dashboard_result

        presented = build_dashboard_result(session)
        base = self._build_item(
            session,
            Path(session.ticker, session.analysis_date, session.run_id, "session.json").as_posix(),
        )
        base["history_id"] = history_id
        base = self._with_status(base, active)
        state = session.state
        debate = state.get("investment_debate_state")
        risk = state.get("risk_debate_state")
        debate = debate if isinstance(debate, dict) else {}
        risk = risk if isinstance(risk, dict) else {}

        def report(value: Any) -> str | None:
            return _redact_text(value.strip()) if isinstance(value, str) and value.strip() else None

        stage_sources: dict[str, Any] = {}
        for stage in PIPELINE_STAGES:
            metadata = session.stage_metadata.get(stage)
            if not isinstance(metadata, dict):
                continue
            public = _public_provenance(metadata)
            if public:
                stage_sources[stage] = public

        base.update(
            {
                "progress": _progress_rows(session.stage_status, None),
                "tabs": deepcopy(presented["tabs"]),
                "final_analysis": presented["final_analysis"],
                "sections": {
                    "technical": report(state.get("market_report")),
                    "fundamentals": report(state.get("fundamentals_report")),
                    "sentiment": report(state.get("sentiment_report")),
                    "news": report(state.get("news_report")),
                },
                "plans": {
                    "investment": report(state.get("investment_plan")),
                    "trader": report(state.get("trader_investment_plan")),
                },
                "debates": {
                    "bull": report(debate.get("bull_history")),
                    "bear": report(debate.get("bear_history")),
                    "aggressive": report(risk.get("aggressive_history")),
                    "neutral": report(risk.get("neutral_history")),
                    "conservative": report(risk.get("conservative_history")),
                },
                "sources": {
                    "sentiment": _public_provenance(state.get("sentiment_source_metadata")) or {},
                    "news": _public_provenance(state.get("news_source_metadata")) or {},
                    "stages": stage_sources,
                },
            }
        )
        return self._redact_history_paths(base)

    def _redact_history_paths(self, value: Any) -> Any:
        roots = {
            str(self.runs_dir),
            str(self.runs_dir.absolute()),
        }

        def visit(item: Any) -> Any:
            if isinstance(item, str):
                sanitized = item
                for root in sorted((root for root in roots if root), key=len, reverse=True):
                    sanitized = sanitized.replace(root, "[redacted runs directory]")
                return _redact_text(sanitized)
            if isinstance(item, list):
                return [visit(entry) for entry in item]
            if isinstance(item, dict):
                return {str(key): visit(entry) for key, entry in item.items()}
            return item

        return visit(value)


__all__ = ["SessionHistoryRepository"]
