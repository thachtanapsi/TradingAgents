"""Durable, versioned state for modular TradingAgents runs.

The stage runner writes one ``session.json`` per run.  The public JSON shape is
deliberately small and stable: run identity and LLM/data transport are immutable,
while stage outputs can be replaced and their downstream dependants invalidated.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 6
UTC = timezone.utc
VIETNAM_TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
ANALYST_STAGES = ("market", "sentiment", "news", "fundamentals")
PIPELINE_STAGES = (*ANALYST_STAGES, "research", "trader", "risk")
STAGE_STATUS_VALUES = ("not_run", "completed", "unavailable", "failed")
DOWNSTREAM_STAGES = {
    "market": ("research", "trader", "risk"),
    "sentiment": ("research", "trader", "risk"),
    "news": ("research", "trader", "risk"),
    "fundamentals": ("research", "trader", "risk"),
    "research": ("trader", "risk"),
    "trader": ("risk",),
    "risk": (),
}
UPSTREAM_STAGES = {
    "market": (),
    "sentiment": (),
    "news": (),
    "fundamentals": (),
    "research": ANALYST_STAGES,
    "trader": ("research",),
    "risk": (*ANALYST_STAGES, "research", "trader"),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _now_vietnam() -> datetime:
    """Injectable wall clock used only when creating a fresh close run."""
    return datetime.now(VIETNAM_TIMEZONE)


def _parse_analysis_date(value: str) -> date:
    """Parse the public run date without accepting an embedded timestamp."""
    if not isinstance(value, str):
        raise ValueError("analysis_date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError("analysis_date must use YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise ValueError("analysis_date must use YYYY-MM-DD")
    return parsed


def _parse_aware_cutoff(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            raise ValueError("analysis_cutoff must be a timezone-aware timestamp")
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            raise ValueError(
                "analysis_cutoff must be a timezone-aware ISO timestamp"
            ) from None
    else:
        raise ValueError("analysis_cutoff must be a timezone-aware timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("analysis_cutoff must be timezone-aware")
    return parsed.astimezone(VIETNAM_TIMEZONE)


def _normalize_analysis_identity(
    analysis_date: str,
    analysis_mode: str,
    analysis_cutoff: datetime | str | None,
) -> tuple[str, str]:
    """Validate and canonicalize the immutable close/live point-in-time identity."""
    parsed_date = _parse_analysis_date(analysis_date)
    mode = str(analysis_mode or "").strip().lower()
    if mode not in {"close", "live"}:
        raise ValueError("analysis_mode must be 'close' or 'live'")

    expected_close = datetime.combine(parsed_date, time(15, 0), VIETNAM_TIMEZONE)
    if analysis_cutoff is None:
        if mode == "live":
            raise ValueError("analysis_cutoff is required for live analysis")
        cutoff = expected_close
    else:
        cutoff = _parse_aware_cutoff(analysis_cutoff)

    if cutoff.date() != parsed_date:
        raise ValueError(
            "analysis_cutoff date in Asia/Ho_Chi_Minh must match analysis_date"
        )
    if mode == "close" and cutoff != expected_close:
        raise ValueError(
            "close analysis_cutoff must be 15:00:00 Asia/Ho_Chi_Minh"
        )
    return mode, cutoff.isoformat()


def _analysis_identity_fingerprint(
    analysis_date: str,
    analysis_mode: str,
    analysis_cutoff: datetime | str | None,
) -> str:
    """Fingerprint the frozen point-in-time identity stored in session JSON."""
    mode, cutoff = _normalize_analysis_identity(
        analysis_date, analysis_mode, analysis_cutoff
    )
    encoded = json.dumps(
        {
            "analysis_date": analysis_date,
            "analysis_mode": mode,
            "analysis_cutoff": cutoff,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _safe_component(value: str, name: str) -> str:
    if not value or value in {".", ".."} or any(ch in value for ch in "/\\\0"):
        raise ValueError(f"invalid {name}: {value!r}")
    return value


def default_runs_dir() -> Path:
    return Path(
        os.environ.get(
            "TRADINGAGENTS_STAGE_RUNS_DIR",
            Path.home() / ".tradingagents" / "runs",
        )
    ).expanduser()


def _legacy_social_profile() -> dict[str, Any]:
    """Identity assigned to schema-v1/upstream sessions during migration."""
    return {"provider": "legacy"}


def _legacy_media_profile() -> dict[str, Any]:
    """Identity assigned to sessions created before editorial RSS support."""
    return {"provider": "legacy"}


def _legacy_macro_profile() -> dict[str, Any]:
    """Identity assigned to sessions created before the Vietnam macro lane."""
    return {"provider": "legacy"}


def _validate_public_social_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Allow only the documented, non-secret immutable profile fields."""
    normalized = deepcopy(profile)
    if not isinstance(normalized, dict):
        raise ValueError("social_profile must be an object")
    provider = str(normalized.get("provider") or "").strip().lower()
    if provider == "legacy" and set(normalized) == {"provider"}:
        return {"provider": "legacy"}
    allowed = {
        "provider",
        "lookback_days",
        "min_posts",
        "min_unique_authors",
        "archive_id",
        "archive_schema_version",
        "prompt_version",
        "legacy_sources_enabled",
    }
    unexpected = sorted(set(normalized) - allowed)
    if unexpected:
        raise ValueError(
            "social_profile contains unsupported field(s): " + ", ".join(unexpected)
        )
    required = allowed - {"legacy_sources_enabled"}
    missing = sorted(required - set(normalized))
    if missing:
        raise ValueError(
            "social_profile is missing field(s): " + ", ".join(missing)
        )
    if not provider or len(provider) > 32:
        raise ValueError("social_profile provider is invalid")
    for name in ("lookback_days", "min_posts", "min_unique_authors", "archive_schema_version"):
        if isinstance(normalized[name], bool) or not isinstance(normalized[name], int):
            raise ValueError(f"social_profile {name} must be an integer")
    for name in ("archive_id", "prompt_version"):
        if not isinstance(normalized[name], str) or not normalized[name]:
            raise ValueError(f"social_profile {name} must be a non-empty string")
    normalized["provider"] = provider
    normalized["legacy_sources_enabled"] = bool(
        normalized.get("legacy_sources_enabled", False)
    )
    return normalized


def _validate_public_media_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Allow only non-secret, immutable editorial-media identity fields."""
    normalized = deepcopy(profile)
    if not isinstance(normalized, dict):
        raise ValueError("media_profile must be an object")
    if normalized == {"provider": "legacy"}:
        return {"provider": "legacy"}
    allowed = {
        "providers",
        "lookback_days",
        "min_articles",
        "archive_id",
        "archive_schema_version",
        "alias_policy_version",
        "prompt_version",
    }
    unexpected = sorted(set(normalized) - allowed)
    if unexpected:
        raise ValueError(
            "media_profile contains unsupported field(s): " + ", ".join(unexpected)
        )
    missing = sorted(allowed - set(normalized))
    if missing:
        raise ValueError("media_profile is missing field(s): " + ", ".join(missing))

    providers = normalized["providers"]
    if not isinstance(providers, (list, tuple)) or not providers:
        raise ValueError("media_profile providers must be a non-empty list")
    clean_providers: list[str] = []
    for item in providers:
        provider = str(item).strip().lower()
        if (
            not provider
            or len(provider) > 32
            or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in provider)
        ):
            raise ValueError("media_profile contains an invalid provider")
        if provider not in clean_providers:
            clean_providers.append(provider)
    normalized["providers"] = clean_providers

    for name in ("lookback_days", "min_articles", "archive_schema_version"):
        value = normalized[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"media_profile {name} must be a positive integer")
    for name in ("archive_id", "alias_policy_version", "prompt_version"):
        value = normalized[name]
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(f"media_profile {name} must be a non-empty string")
    return normalized


def _validate_public_macro_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Allow only immutable, non-secret Vietnam macro identity fields."""
    normalized = deepcopy(profile)
    if not isinstance(normalized, dict):
        raise ValueError("macro_profile must be an object")
    if normalized == {"provider": "legacy"}:
        return {"provider": "legacy"}

    allowed = {
        "provider",
        "providers",
        "lookback_months",
        "indicator_set_version",
        "archive_id",
        "archive_schema_version",
        "strict_point_in_time",
        "prompt_version",
    }
    unexpected = sorted(set(normalized) - allowed)
    if unexpected:
        raise ValueError(
            "macro_profile contains unsupported field(s): " + ", ".join(unexpected)
        )
    missing = sorted(allowed - set(normalized))
    if missing:
        raise ValueError("macro_profile is missing field(s): " + ", ".join(missing))

    provider = str(normalized["provider"] or "").strip().lower()
    if provider != "vn_macro":
        raise ValueError("macro_profile provider must be 'vn_macro'")
    normalized["provider"] = provider

    providers = normalized["providers"]
    if not isinstance(providers, (list, tuple)) or not providers:
        raise ValueError("macro_profile providers must be a non-empty list")
    clean_providers: list[str] = []
    supported_providers = {"nso_sdmx", "nso_release", "sbv_html"}
    for item in providers:
        source = str(item).strip().lower()
        if (
            not source
            or len(source) > 32
            or any(
                char not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                for char in source
            )
        ):
            raise ValueError("macro_profile contains an invalid provider")
        if source not in supported_providers:
            raise ValueError(f"macro_profile provider is unsupported: {source}")
        if source not in clean_providers:
            clean_providers.append(source)
    normalized["providers"] = clean_providers

    for name in ("lookback_months", "archive_schema_version"):
        value = normalized[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"macro_profile {name} must be a positive integer")
    if not isinstance(normalized["strict_point_in_time"], bool):
        raise ValueError("macro_profile strict_point_in_time must be a boolean")
    for name in (
        "indicator_set_version",
        "archive_id",
        "prompt_version",
    ):
        value = normalized[name]
        if not isinstance(value, str) or not value or len(value) > 128:
            raise ValueError(f"macro_profile {name} must be a non-empty string")
    return normalized


def _validate_public_endpoint(value: Any, field_name: str) -> str | None:
    """Reject credential-bearing endpoints instead of trying to redact identity."""
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError(f"{field_name} must be a valid public URL or null")
    try:
        parsed = urlsplit(value)
        # Accessing ``port`` validates malformed/non-numeric port strings.
        _ = parsed.port
    except ValueError:
        raise ValueError(f"{field_name} must be a valid public URL or null") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{field_name} must not contain credentials, query parameters or fragments"
        )
    return value


_LLM_SHARED_FIELDS = {
    "output_language",
    "temperature",
    "max_debate_rounds",
    "max_risk_discuss_rounds",
    "openai_reasoning_effort",
    "google_thinking_level",
    "anthropic_effort",
}
_LEGACY_LLM_FIELDS = {
    "provider",
    "backend_url",
    "quick_model",
    "deep_model",
    *_LLM_SHARED_FIELDS,
}


def _migrate_legacy_llm_identity(identity: dict[str, Any]) -> dict[str, Any]:
    """Convert the schema-v1..v3 flat identity into two public profiles."""
    if "quick" in identity or "deep" in identity:
        return identity
    unexpected = sorted(set(identity) - _LEGACY_LLM_FIELDS)
    if unexpected:
        raise ValueError(
            "llm contains unsupported/sensitive identity field(s): "
            + ", ".join(unexpected)
        )
    provider = identity.get("provider")
    base_url = identity.get("backend_url")
    migrated = {
        "quick": {
            "provider": provider,
            "model": identity.get("quick_model"),
            "base_url": base_url,
        },
        "deep": {
            "provider": provider,
            "model": identity.get("deep_model"),
            "base_url": base_url,
        },
    }
    migrated.update(
        {name: identity[name] for name in _LLM_SHARED_FIELDS if name in identity}
    )
    return migrated


def _validate_public_llm_identity(identity: dict[str, Any]) -> dict[str, Any]:
    """Validate the non-secret Quick/Deep execution identity in a session."""
    if not isinstance(identity, dict):
        raise ValueError("llm must be an object")
    normalized = deepcopy(_migrate_legacy_llm_identity(identity))
    allowed = {
        "quick",
        "deep",
        *_LLM_SHARED_FIELDS,
    }
    unexpected = sorted(set(normalized) - allowed)
    if unexpected:
        raise ValueError(
            "llm contains unsupported/sensitive identity field(s): "
            + ", ".join(unexpected)
        )
    for role in ("quick", "deep"):
        profile = normalized.get(role)
        if not isinstance(profile, dict):
            raise ValueError(f"llm {role} profile must be an object")
        profile_unexpected = sorted(set(profile) - {"provider", "model", "base_url"})
        if profile_unexpected:
            raise ValueError(
                f"llm {role} contains unsupported/sensitive identity field(s): "
                + ", ".join(profile_unexpected)
            )
        provider = profile.get("provider")
        if not isinstance(provider, str) or not provider.strip() or len(provider) > 64:
            raise ValueError(f"llm {role} provider must be a non-empty string")
        profile["provider"] = provider.strip().lower()
        model = profile.get("model")
        if model is not None and (
            not isinstance(model, str) or not model.strip() or len(model) > 256
        ):
            raise ValueError(f"llm {role} model must be a non-empty string or null")
        if isinstance(model, str):
            profile["model"] = model.strip()
        profile["base_url"] = _validate_public_endpoint(
            profile.get("base_url"), f"llm {role} base_url"
        )

    for name in (
        "output_language",
        "openai_reasoning_effort",
        "google_thinking_level",
        "anthropic_effort",
    ):
        if name not in normalized or normalized[name] is None:
            continue
        if not isinstance(normalized[name], str) or len(normalized[name]) > 256:
            raise ValueError(f"llm {name} must be a string or null")
    temperature = normalized.get("temperature")
    if temperature is not None and (
        isinstance(temperature, bool) or not isinstance(temperature, (int, float))
    ):
        raise ValueError("llm temperature must be numeric or null")
    for name in ("max_debate_rounds", "max_risk_discuss_rounds"):
        value = normalized.get(name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"llm {name} must be a non-negative integer or null")
    return normalized


def _validate_public_data_transport_identity(identity: dict[str, Any]) -> dict[str, Any]:
    """Validate GX transport identity while rejecting DSNs and credentials."""
    normalized = deepcopy(identity)
    if not isinstance(normalized, dict):
        raise ValueError("data_transport must be an object")
    transport = normalized.get("transport")
    if not isinstance(transport, str):
        raise ValueError("data_transport transport must be 'api' or 'postgres'")
    transport = transport.strip().lower()
    allowed_by_transport = {
        "api": {"transport", "base_url", "api_version"},
        "postgres": {"transport", "expected_database"},
    }
    if transport not in allowed_by_transport:
        raise ValueError("data_transport transport must be 'api' or 'postgres'")
    unexpected = sorted(set(normalized) - allowed_by_transport[transport])
    if unexpected:
        raise ValueError(
            "data_transport contains unsupported/sensitive identity field(s): "
            + ", ".join(unexpected)
        )
    normalized["transport"] = transport
    if transport == "api":
        if "base_url" in normalized:
            normalized["base_url"] = _validate_public_endpoint(
                normalized["base_url"], "data_transport base_url"
            )
        api_version = normalized.get("api_version")
        if api_version is not None and (
            not isinstance(api_version, str) or not api_version or len(api_version) > 64
        ):
            raise ValueError("data_transport api_version must be a non-empty string")
    else:
        database = normalized.get("expected_database")
        if database is not None and (
            not isinstance(database, str)
            or not database
            or len(database) > 128
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for char in database)
        ):
            raise ValueError(
                "data_transport expected_database must be a public database name"
            )
    return normalized


@dataclass
class StageSession:
    """JSON-serializable state shared by independently executed stages."""

    schema_version: int
    run_id: str
    ticker: str
    analysis_date: str
    analysis_mode: str
    analysis_cutoff: str
    asset_type: str
    selected_analysts: tuple[str, ...]
    llm: dict[str, Any]
    data_transport: dict[str, Any]
    social_profile: dict[str, Any]
    media_profile: dict[str, Any]
    macro_profile: dict[str, Any]
    created_at: str
    updated_at: str
    analysis_identity_fingerprint: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    completed_stages: list[str] = field(default_factory=list)
    stage_status: dict[str, str] = field(default_factory=dict)
    stage_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)

    CURRENT_SCHEMA_VERSION: ClassVar[int] = SCHEMA_VERSION

    _sealed_analysis_identity: tuple[str, str, str] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        mode, cutoff = _normalize_analysis_identity(
            self.analysis_date, self.analysis_mode, self.analysis_cutoff
        )
        expected_fingerprint = _analysis_identity_fingerprint(
            self.analysis_date, mode, cutoff
        )
        if (
            self.analysis_identity_fingerprint is not None
            and self.analysis_identity_fingerprint != expected_fingerprint
        ):
            raise ValueError(
                "session analysis identity fingerprint does not match; "
                "create a new run instead of editing the cutoff"
            )
        object.__setattr__(self, "analysis_mode", mode)
        object.__setattr__(self, "analysis_cutoff", cutoff)
        object.__setattr__(
            self, "analysis_identity_fingerprint", expected_fingerprint
        )
        object.__setattr__(
            self,
            "_sealed_analysis_identity",
            (self.analysis_date, mode, cutoff),
        )

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"analysis_date", "analysis_mode", "analysis_cutoff"} and hasattr(
            self, "_sealed_analysis_identity"
        ):
            current = getattr(self, name)
            if value != current:
                raise AttributeError(
                    f"{name} is immutable; create a new run to change the cutoff"
                )
        super().__setattr__(name, value)

    def _assert_sealed_analysis_identity(self) -> None:
        current = (
            self.analysis_date,
            self.analysis_mode,
            self.analysis_cutoff,
        )
        if current != self._sealed_analysis_identity:
            raise ValueError(
                "session analysis identity is immutable; create a new run"
            )
        expected_fingerprint = _analysis_identity_fingerprint(*current)
        if self.analysis_identity_fingerprint != expected_fingerprint:
            raise ValueError(
                "session analysis identity fingerprint does not match; "
                "create a new run"
            )

    @classmethod
    def create(
        cls,
        *,
        ticker: str,
        analysis_date: str,
        selected_analysts: tuple[str, ...] = ANALYST_STAGES,
        llm: dict[str, Any],
        data_transport: dict[str, Any],
        social_profile: dict[str, Any] | None = None,
        media_profile: dict[str, Any] | None = None,
        macro_profile: dict[str, Any] | None = None,
        asset_type: str = "stock",
        analysis_mode: str = "close",
        analysis_cutoff: datetime | str | None = None,
        run_id: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> StageSession:
        timestamp = _now()
        normalized_mode, normalized_cutoff = _normalize_analysis_identity(
            analysis_date,
            analysis_mode,
            analysis_cutoff,
        )
        if (
            normalized_mode == "close"
            and _parse_aware_cutoff(normalized_cutoff) > _now_vietnam()
        ):
            raise ValueError(
                "the requested 15:00 close is not completed yet; "
                "use an earlier date or live analysis"
            )
        normalized = tuple(selected_analysts)
        unknown = set(normalized) - set(ANALYST_STAGES)
        if unknown:
            raise ValueError(f"unknown analyst stages: {', '.join(sorted(unknown))}")
        return cls(
            schema_version=SCHEMA_VERSION,
            run_id=_safe_component(run_id or uuid.uuid4().hex, "run_id"),
            ticker=_safe_component(ticker, "ticker"),
            analysis_date=_safe_component(
                _parse_analysis_date(analysis_date).isoformat(), "analysis_date"
            ),
            analysis_mode=normalized_mode,
            analysis_cutoff=normalized_cutoff,
            asset_type=asset_type,
            selected_analysts=normalized,
            llm=_validate_public_llm_identity(llm),
            data_transport=_validate_public_data_transport_identity(data_transport),
            social_profile=_validate_public_social_profile(
                social_profile or _legacy_social_profile()
            ),
            media_profile=_validate_public_media_profile(
                media_profile or _legacy_media_profile()
            ),
            macro_profile=_validate_public_macro_profile(
                macro_profile or _legacy_macro_profile()
            ),
            created_at=timestamp,
            updated_at=timestamp,
            state={
                key: deepcopy(value)
                for key, value in (state or {}).items()
                if key not in {"messages", "analysis_mode", "analysis_cutoff"}
            },
            stage_status=dict.fromkeys(PIPELINE_STAGES, "not_run"),
        )

    @classmethod
    def load(cls, path: str | Path) -> StageSession:
        with Path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
        version = payload.get("schema_version")
        if version in {1, 2, 3, 4, 5}:
            # Sessions created before live analysis always represented the
            # deterministic 15:00 Vietnam close. Never infer a live cutoff from
            # the wall clock while resuming an older run.
            payload["analysis_mode"] = "close"
            payload["analysis_cutoff"] = None
            payload["analysis_identity_fingerprint"] = None
        if version == 1:
            # V1 predated provider-specific social evidence. It can still be
            # inspected/resumed with the legacy profile, but cannot silently be
            # converted into a FireAnt run by changing runtime configuration.
            payload["schema_version"] = SCHEMA_VERSION
            payload["social_profile"] = _legacy_social_profile()
            payload["media_profile"] = _legacy_media_profile()
            payload["macro_profile"] = _legacy_macro_profile()
        elif version == 2:
            # V2 introduced FireAnt social identity but predated the separate
            # CafeF/VnExpress editorial-media lane.
            payload["schema_version"] = SCHEMA_VERSION
            payload["media_profile"] = _legacy_media_profile()
            payload["macro_profile"] = _legacy_macro_profile()
        elif version == 3:
            # V3 introduced editorial media but still stored a single flat LLM
            # provider/backend identity. The validator below deterministically
            # expands it into matching Quick and Deep profiles.
            payload["schema_version"] = SCHEMA_VERSION
            payload["macro_profile"] = _legacy_macro_profile()
        elif version == 4:
            # V4 split Quick/Deep LLM identity but predates official NSO/SBV
            # macro evidence. It remains a legacy macro run on resume.
            payload["schema_version"] = SCHEMA_VERSION
            payload["macro_profile"] = _legacy_macro_profile()
        elif version == 5:
            # V5 introduced NSO/SBV macro identity but remained date-only.
            payload["schema_version"] = SCHEMA_VERSION
        elif version == SCHEMA_VERSION:
            if not payload.get("analysis_identity_fingerprint"):
                raise ValueError(
                    "schema-v6 session is missing its analysis identity fingerprint"
                )
        else:
            raise ValueError(
                f"unsupported session schema_version {version!r}; "
                f"expected 1, 2, 3, 4, 5 or {SCHEMA_VERSION}"
            )
        payload["analysis_mode"], payload["analysis_cutoff"] = (
            _normalize_analysis_identity(
                payload.get("analysis_date"),
                payload.get("analysis_mode"),
                payload.get("analysis_cutoff"),
            )
        )
        payload["social_profile"] = _validate_public_social_profile(
            payload.get("social_profile") or _legacy_social_profile()
        )
        payload["media_profile"] = _validate_public_media_profile(
            payload.get("media_profile") or _legacy_media_profile()
        )
        payload["macro_profile"] = _validate_public_macro_profile(
            payload.get("macro_profile") or _legacy_macro_profile()
        )
        payload["llm"] = _validate_public_llm_identity(payload.get("llm"))
        payload["data_transport"] = _validate_public_data_transport_identity(
            payload.get("data_transport")
        )
        payload["selected_analysts"] = tuple(payload.get("selected_analysts", ()))
        payload.setdefault("state", {})
        # LangChain messages are transient execution objects. Never carry or
        # rewrite them from a hand-edited/older session file.
        payload["state"].pop("messages", None)
        payload["state"].pop("analysis_mode", None)
        payload["state"].pop("analysis_cutoff", None)
        payload.setdefault(
            "stage_status",
            {
                stage: "completed" if stage in payload.get("completed_stages", []) else "not_run"
                for stage in PIPELINE_STAGES
            },
        )
        payload.setdefault("stage_metadata", {})
        return cls(**payload)

    def path(self, runs_dir: str | Path | None = None) -> Path:
        root = Path(runs_dir) if runs_dir is not None else default_runs_dir()
        return (
            root.expanduser()
            / _safe_component(self.ticker, "ticker")
            / _safe_component(self.analysis_date, "analysis_date")
            / _safe_component(self.run_id, "run_id")
            / "session.json"
        )

    def assert_identity(
        self,
        *,
        ticker: str,
        analysis_date: str,
        asset_type: str,
        selected_analysts: tuple[str, ...],
        llm: dict[str, Any],
        data_transport: dict[str, Any],
        analysis_mode: str | None = None,
        analysis_cutoff: datetime | str | None = None,
        social_profile: dict[str, Any] | None = None,
        media_profile: dict[str, Any] | None = None,
        macro_profile: dict[str, Any] | None = None,
    ) -> None:
        self._assert_sealed_analysis_identity()
        expected_mode, expected_cutoff = _normalize_analysis_identity(
            analysis_date,
            self.analysis_mode if analysis_mode is None else analysis_mode,
            self.analysis_cutoff if analysis_cutoff is None else analysis_cutoff,
        )
        expected = {
            "ticker": ticker,
            "analysis_date": analysis_date,
            "analysis_mode": expected_mode,
            "analysis_cutoff": expected_cutoff,
            "asset_type": asset_type,
            "selected_analysts": tuple(selected_analysts),
            "llm": _validate_public_llm_identity(llm),
            "data_transport": _validate_public_data_transport_identity(data_transport),
        }
        if social_profile is not None:
            expected["social_profile"] = _validate_public_social_profile(
                social_profile
            )
        if media_profile is not None:
            expected["media_profile"] = _validate_public_media_profile(media_profile)
        if macro_profile is not None:
            expected["macro_profile"] = _validate_public_macro_profile(macro_profile)
        actual = {key: getattr(self, key) for key in expected}
        actual["llm"] = _validate_public_llm_identity(actual["llm"])
        actual["data_transport"] = _validate_public_data_transport_identity(
            actual["data_transport"]
        )
        if actual != expected:
            differing = ", ".join(key for key in expected if actual[key] != expected[key])
            raise ValueError(
                "session run identity is immutable; create a new run to change: " + differing
            )

    def complete(
        self,
        stage: str,
        state_update: dict[str, Any],
        *,
        sources: list[dict[str, Any]] | None = None,
        warnings: list[str] | None = None,
        status: str = "completed",
    ) -> None:
        if stage not in PIPELINE_STAGES:
            raise ValueError(f"unknown stage: {stage}")
        if status not in {"completed", "unavailable"}:
            raise ValueError("completed stage status must be 'completed' or 'unavailable'")
        self.invalidate_downstream(stage)
        fingerprint = self.input_fingerprint(stage)
        self.state.update(
            {
                key: deepcopy(value)
                for key, value in state_update.items()
                if key not in {"messages", "analysis_mode", "analysis_cutoff"}
            }
        )
        if status == "completed" and stage not in self.completed_stages:
            self.completed_stages.append(stage)
        elif status == "unavailable" and stage in self.completed_stages:
            self.completed_stages.remove(stage)
        self.stage_status[stage] = status
        timestamp_key = "completed_at" if status == "completed" else "unavailable_at"
        self.stage_metadata[stage] = {
            timestamp_key: _now(),
            "input_fingerprint": fingerprint,
            "sources": deepcopy(sources or []),
            "warnings": list(warnings or []),
        }
        self.updated_at = _now()

    def record_failure(self, stage: str, error: str, *, unavailable: bool = False) -> None:
        if stage not in PIPELINE_STAGES:
            raise ValueError(f"unknown stage: {stage}")
        fingerprint = self.input_fingerprint(stage)
        self.invalidate_downstream(stage)
        if stage in self.completed_stages:
            self.completed_stages.remove(stage)
        for key in _STAGE_STATE_KEYS[stage]:
            self.state.pop(key, None)
        self.stage_status[stage] = "unavailable" if unavailable else "failed"
        self.stage_metadata[stage] = {
            "failed_at": _now(),
            "input_fingerprint": fingerprint,
            "sources": [],
            "warnings": [error],
        }
        self.updated_at = _now()

    def input_fingerprint(self, stage: str) -> str:
        self._assert_sealed_analysis_identity()
        payload = {
            "stage": stage,
            "ticker": self.ticker,
            "analysis_date": self.analysis_date,
            "analysis_mode": self.analysis_mode,
            "analysis_cutoff": self.analysis_cutoff,
            "asset_type": self.asset_type,
            "llm": self.llm,
            "data_transport": self.data_transport,
            "social_profile": self.social_profile,
            "upstream_status": {
                upstream: self.stage_status.get(upstream, "not_run")
                for upstream in UPSTREAM_STAGES[stage]
            },
            "upstream": {
                key: self.state.get(key)
                for upstream in UPSTREAM_STAGES[stage]
                for key in _STAGE_STATE_KEYS[upstream]
                if key in self.state
            },
        }
        # Editorial media affects only the two stages that consume it. The
        # sentiment fingerprint is also the durable daily-snapshot identity, so
        # a snapshot can never be reused across a changed archive/prompt/alias
        # profile. Other stage fingerprints remain stable across this schema
        # addition.
        if stage in {"sentiment", "news"}:
            payload["media_profile"] = self.media_profile
        if stage == "news":
            payload["macro_profile"] = self.macro_profile
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()

    def invalidate_downstream(self, stage: str) -> None:
        for downstream in DOWNSTREAM_STAGES[stage]:
            if downstream in self.completed_stages:
                self.completed_stages.remove(downstream)
            self.stage_status[downstream] = "not_run"
            self.stage_metadata.pop(downstream, None)
            for key in _STAGE_STATE_KEYS[downstream]:
                self.state.pop(key, None)

    def to_dict(self) -> dict[str, Any]:
        self._assert_sealed_analysis_identity()
        analysis_mode, analysis_cutoff = _normalize_analysis_identity(
            self.analysis_date,
            self.analysis_mode,
            self.analysis_cutoff,
        )
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "ticker": self.ticker,
            "analysis_date": self.analysis_date,
            "analysis_mode": analysis_mode,
            "analysis_cutoff": analysis_cutoff,
            "analysis_identity_fingerprint": self.analysis_identity_fingerprint,
            "asset_type": self.asset_type,
            "selected_analysts": list(self.selected_analysts),
            "llm": _validate_public_llm_identity(self.llm),
            "data_transport": _validate_public_data_transport_identity(
                self.data_transport
            ),
            "social_profile": _validate_public_social_profile(self.social_profile),
            "media_profile": _validate_public_media_profile(self.media_profile),
            "macro_profile": _validate_public_macro_profile(self.macro_profile),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state": {
                key: deepcopy(value)
                for key, value in self.state.items()
                if key not in {"messages", "analysis_mode", "analysis_cutoff"}
            },
            "completed_stages": list(self.completed_stages),
            "stage_status": deepcopy(self.stage_status),
            "stage_metadata": deepcopy(self.stage_metadata),
        }

    def save(self, path: str | Path | None = None) -> Path:
        destination = Path(path) if path is not None else self.path()
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)
            raise
        return destination


_STAGE_STATE_KEYS = {
    "market": ("market_report", "market_price_reference"),
    "sentiment": ("sentiment_report", "sentiment_source_metadata"),
    "news": ("news_report", "news_source_metadata"),
    "fundamentals": ("fundamentals_report",),
    "research": ("investment_debate_state", "investment_plan"),
    "trader": ("trader_investment_plan",),
    "risk": ("risk_debate_state", "final_trade_decision"),
}
