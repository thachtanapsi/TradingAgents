"""FireAnt-backed Vietnam retail-social collection and archive service.

The feature is deliberately fail-closed.  Merely configuring FireAnt does not
authorize collection: callers must set the explicit authorization flag and
provide both a Bearer token and an archive encryption key.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import random
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time as datetime_time, timedelta, timezone
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from tradingagents.dataflows.config import get_config

from .vietnam_social_archive import (
    ARCHIVE_SCHEMA_VERSION,
    ArchiveConfigurationError,
    SnapshotRecord,
    VietnamSocialArchive,
)

FIREANT_BASE_URL = "https://api.fireant.vn"
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_TICKER_RE = re.compile(r"^[A-Z0-9]{1,16}$")
UTC = timezone.utc


class SocialStatus(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


@dataclass(frozen=True)
class SocialPost:
    provider: str
    provider_post_id: str
    ticker: str
    text: str
    published_at: datetime
    first_seen_at: datetime
    provider_sentiment: int | None
    engagement: dict[str, Any]
    author: dict[str, Any]
    tagged_symbols: list[str]
    content_hash: str
    is_ai_generated: bool = False
    author_key: str | None = None


@dataclass(frozen=True)
class SocialFetchResult:
    fetch_id: str
    provider: str
    ticker: str
    status: SocialStatus
    posts: list[SocialPost]
    pages: int
    started_at: datetime
    completed_at: datetime
    truncated: bool = False
    ordering_violated: bool = False
    watermark_stopped: bool = False
    request_succeeded: bool = True
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SocialEvidenceBatch:
    status: SocialStatus
    provider: str
    ticker: str
    posts: list[SocialPost] = field(default_factory=list)
    snapshot_id: str | None = None
    signal_payload: dict[str, Any] | None = None
    report_payload: dict[str, Any] | None = None
    sample_size: int = 0
    unique_authors: int = 0
    window_start: str | None = None
    window_end: str | None = None
    fetch_id: str | None = None
    point_in_time_quality: str = "partial"
    warnings: list[str] = field(default_factory=list)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self._ignored_depth += 1
        elif tag.lower() in {"br", "p", "div", "li"} and self.parts:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag.lower() in {"p", "div", "li"} and self.parts:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def sanitize_post_text(value: Any) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except Exception:
        return ""
    return re.sub(r"\s+", " ", html.unescape("".join(parser.parts))).strip()


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid value for {name}: expected a boolean")


def _int_setting(settings: dict[str, Any], key: str, env: str, default: int) -> int:
    raw: Any = os.environ.get(env)
    if raw in (None, ""):
        raw = settings.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid value for {env}: expected an integer") from None


def _validate_ticker(value: str) -> str:
    ticker = str(value or "").strip().upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise ValueError("ticker must contain only 1-16 uppercase letters or digits")
    return ticker


@dataclass(frozen=True)
class VietnamSocialConfig:
    provider: str
    authorized: bool
    hosted_llm_authorized: bool
    access_token: str | None = field(repr=False, compare=False)
    encryption_key: str | None = field(repr=False, compare=False)
    watchlist: tuple[str, ...]
    archive_path: Path
    lookback_days: int = 7
    min_posts: int = 10
    min_unique_authors: int = 5
    poll_seconds: int = 300
    retention_days: int = 90
    timeout_seconds: float = 10.0
    page_size: int = 100
    max_pages: int = 20
    max_attempts: int = 3
    prompt_version: str = "vn-social-v1"
    enabled: bool = True

    @classmethod
    def from_env(cls) -> VietnamSocialConfig:
        settings = get_config().get("vn_social") or {}
        provider = (
            str(
                os.environ.get("TRADINGAGENTS_VN_SOCIAL_PROVIDER")
                or settings.get("provider")
                or "legacy"
            )
            .strip()
            .lower()
        )
        authorized = _bool_env(
            "TRADINGAGENTS_FIREANT_AUTHORIZED",
            bool(settings.get("authorized", False)),
        )
        hosted = _bool_env(
            "TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED",
            bool(settings.get("hosted_llm_authorized", False)),
        )
        raw_tickers = os.environ.get("TRADINGAGENTS_VN_SOCIAL_TICKERS")
        if raw_tickers is None:
            raw_tickers = settings.get("tickers", "")
        values = (
            raw_tickers if isinstance(raw_tickers, (list, tuple)) else str(raw_tickers).split(",")
        )
        watchlist = tuple(
            dict.fromkeys(_validate_ticker(item) for item in values if str(item).strip())
        )
        archive_raw = (
            os.environ.get("TRADINGAGENTS_SOCIAL_ARCHIVE_PATH")
            or settings.get("archive_path")
            or "~/.tradingagents/cache/social/vn_social.sqlite3"
        )
        config = cls(
            provider=provider,
            authorized=authorized,
            hosted_llm_authorized=hosted,
            access_token=os.environ.get("FIREANT_ACCESS_TOKEN") or None,
            encryption_key=os.environ.get("FIREANT_ARCHIVE_ENCRYPTION_KEY") or None,
            watchlist=watchlist,
            archive_path=Path(str(archive_raw)).expanduser(),
            lookback_days=_int_setting(
                settings, "lookback_days", "TRADINGAGENTS_VN_SOCIAL_LOOKBACK_DAYS", 7
            ),
            min_posts=_int_setting(settings, "min_posts", "TRADINGAGENTS_VN_SOCIAL_MIN_POSTS", 10),
            min_unique_authors=_int_setting(
                settings,
                "min_unique_authors",
                "TRADINGAGENTS_VN_SOCIAL_MIN_UNIQUE_AUTHORS",
                5,
            ),
            poll_seconds=_int_setting(
                settings, "poll_seconds", "TRADINGAGENTS_VN_SOCIAL_POLL_SECONDS", 300
            ),
            retention_days=_int_setting(
                settings,
                "raw_retention_days",
                "TRADINGAGENTS_SOCIAL_RAW_RETENTION_DAYS",
                90,
            ),
            timeout_seconds=float(settings.get("timeout_seconds", 10.0)),
            page_size=int(settings.get("page_size", 100)),
            max_pages=int(settings.get("max_pages", 20)),
            max_attempts=int(settings.get("max_attempts", 3)),
            prompt_version=str(settings.get("prompt_version") or "vn-social-v1"),
            enabled=provider == "fireant",
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.provider not in {"fireant", "legacy", "disabled", "none", "off"}:
            raise ValueError("unsupported Vietnam social provider")
        bounds = {
            "lookback_days": (self.lookback_days, 1, 90),
            "min_posts": (self.min_posts, 1, 1000),
            "min_unique_authors": (self.min_unique_authors, 1, 1000),
            "poll_seconds": (self.poll_seconds, 30, 86400),
            "retention_days": (self.retention_days, 1, 3650),
            "page_size": (self.page_size, 1, 100),
            "max_pages": (self.max_pages, 1, 20),
            "max_attempts": (self.max_attempts, 1, 10),
        }
        for name, (value, lower, upper) in bounds.items():
            if not lower <= value <= upper:
                raise ValueError(f"{name} must be between {lower} and {upper}")
        if not 0 < self.timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0 and 60")

    def profile_fingerprint(self, archive_id: str | None = None) -> str:
        """Fingerprint settings that define archive/snapshot compatibility.

        The normalized archive path is hashed instead of persisted.  Credentials
        are intentionally excluded: rotating a token/key must not make anonymous
        aggregate snapshots unusable, and secrets must never enter session data.
        """
        archive_identity = archive_id or hashlib.sha256(
            os.path.abspath(str(self.archive_path.expanduser())).encode("utf-8")
        ).hexdigest()[:16]
        profile = {
            "archive_id": archive_identity,
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "lookback_days": self.lookback_days,
            "min_posts": self.min_posts,
            "min_unique_authors": self.min_unique_authors,
            "poll_seconds": self.poll_seconds,
            "prompt_version": self.prompt_version,
            "provider": self.provider,
        }
        canonical = json.dumps(profile, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_VN_TZ)
    return parsed.astimezone(UTC)


def _tag_symbols(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        return []
    symbols: list[str] = []
    for item in payload:
        if isinstance(item, str):
            raw = item
        elif isinstance(item, dict):
            raw = item.get("symbol") or item.get("ticker") or item.get("code") or ""
        else:
            continue
        symbol = str(raw).strip().upper()
        if _TICKER_RE.fullmatch(symbol):
            symbols.append(symbol)
    return list(dict.fromkeys(symbols))


def _engagement(payload: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "likes": ("totalLikes", "likeCount", "likes"),
        "comments": ("totalComments", "commentCount", "comments"),
        "replies": ("totalReplies", "replyCount", "replies"),
        "shares": ("totalShares", "shareCount", "shares"),
        "views": ("totalViews", "viewCount", "views"),
    }
    result: dict[str, Any] = {}
    for name, keys in aliases.items():
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[name] = value
                break
    return result


def _author(payload: dict[str, Any]) -> dict[str, Any]:
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    return {
        key: value
        for key, value in {
            "id": user.get("id") or payload.get("userID"),
            "username": payload.get("userName") or user.get("userName"),
            "name": user.get("name"),
            "bio": user.get("bio"),
            "isAuthentic": user.get("isAuthentic"),
        }.items()
        if value not in (None, "")
    }


def _post_from_payload(
    payload: Any,
    *,
    ticker: str,
    first_seen_at: datetime,
) -> SocialPost | None:
    if not isinstance(payload, dict):
        return None
    post_id = payload.get("postID") or payload.get("id")
    published = _parse_datetime(payload.get("date") or payload.get("createdAt"))
    tags = _tag_symbols(payload.get("taggedSymbols"))
    if post_id in (None, "") or published is None or ticker not in tags:
        return None
    text_fields = [
        payload.get("content"),
        payload.get("title"),
        payload.get("summary"),
        payload.get("description"),
    ]
    text = ""
    for item in text_fields:
        candidate = sanitize_post_text(item)
        if candidate:
            text = candidate
            break
    if not text:
        return None
    sentiment = payload.get("sentiment")
    if isinstance(sentiment, bool) or sentiment not in {-1, 0, 1}:
        sentiment = None
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return SocialPost(
        provider="fireant",
        provider_post_id=str(post_id),
        ticker=ticker,
        text=text,
        published_at=published,
        first_seen_at=first_seen_at.astimezone(UTC),
        provider_sentiment=sentiment,
        engagement=_engagement(payload),
        author=_author(payload),
        tagged_symbols=tags,
        content_hash=content_hash,
        is_ai_generated=bool(payload.get("isAIGenerated", False)),
    )


def _retry_after_seconds(response: Any, attempt: int, jitter: Callable[[], float]) -> float:
    header = getattr(response, "headers", {}).get("Retry-After")
    if header:
        try:
            return min(max(float(header), 0.0), 60.0)
        except (TypeError, ValueError):
            pass
    return min((2**attempt) + jitter(), 10.0)


class FireAntClient:
    def __init__(
        self,
        config: VietnamSocialConfig,
        *,
        session: Any = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.sleep = sleep or time.sleep
        self.jitter = jitter or random.random
        self.now = now or (lambda: datetime.now(UTC))

    def _now_utc(self) -> datetime:
        value = self.now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _page(self, ticker: str, offset: int) -> tuple[list[Any] | None, str | None]:
        url = f"{FIREANT_BASE_URL}/symbols/{ticker}/posts"
        headers = {
            "Authorization": f"Bearer {self.config.access_token}",
            "Accept": "application/json",
            "User-Agent": "TradingAgents-GX/1",
        }
        parameters = {"type": 0, "offset": offset, "limit": self.config.page_size}
        for attempt in range(self.config.max_attempts):
            try:
                response = self.session.get(
                    url,
                    params=parameters,
                    headers=headers,
                    timeout=self.config.timeout_seconds,
                )
            except requests.RequestException:
                if attempt + 1 < self.config.max_attempts:
                    self.sleep(min((2**attempt) + self.jitter(), 10.0))
                    continue
                return None, "FireAnt request timed out or failed."

            status = int(getattr(response, "status_code", 0))
            if status in {401, 403}:
                return None, f"FireAnt authorization failed with HTTP {status}."
            if status == 429 or 500 <= status <= 599:
                if attempt + 1 < self.config.max_attempts:
                    self.sleep(_retry_after_seconds(response, attempt, self.jitter))
                    continue
                return None, f"FireAnt request failed with HTTP {status}."
            if not 200 <= status <= 299:
                return None, f"FireAnt request failed with HTTP {status}."
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError):
                return None, "FireAnt returned malformed JSON."
            if not isinstance(payload, list):
                return None, "FireAnt returned an unexpected response shape."
            return payload, None
        return None, "FireAnt request failed."

    def fetch_posts(
        self,
        ticker: str,
        *,
        known_keys: set[tuple[str, str]] | None = None,
        page_limit: int | None = None,
    ) -> SocialFetchResult:
        symbol = _validate_ticker(ticker)
        started = self._now_utc()
        fetch_id = uuid.uuid4().hex
        if not self.config.authorized:
            return SocialFetchResult(
                fetch_id=fetch_id,
                provider="fireant",
                ticker=symbol,
                status=SocialStatus.DISABLED,
                posts=[],
                pages=0,
                started_at=started,
                completed_at=self._now_utc(),
                request_succeeded=False,
                warnings=["FireAnt collection is authorization locked."],
            )
        if not self.config.access_token:
            return SocialFetchResult(
                fetch_id=fetch_id,
                provider="fireant",
                ticker=symbol,
                status=SocialStatus.UNAVAILABLE,
                posts=[],
                pages=0,
                started_at=started,
                completed_at=self._now_utc(),
                request_succeeded=False,
                warnings=["FIREANT_ACCESS_TOKEN is missing."],
            )

        posts: list[SocialPost] = []
        seen: set[tuple[str, str]] = set()
        warnings: list[str] = []
        pages = 0
        truncated = False
        ordering_violated = False
        watermark_stopped = False
        previous_date: datetime | None = None
        maximum_pages = self.config.max_pages if page_limit is None else int(page_limit)
        if not 1 <= maximum_pages <= self.config.max_pages:
            raise ValueError("page_limit must be between 1 and configured max_pages")
        archived_keys = known_keys or set()
        for page_number in range(maximum_pages):
            payload, error = self._page(symbol, page_number * self.config.page_size)
            if error:
                warnings.append(error)
                status = SocialStatus.PARTIAL if posts else SocialStatus.UNAVAILABLE
                return SocialFetchResult(
                    fetch_id=fetch_id,
                    provider="fireant",
                    ticker=symbol,
                    status=status,
                    posts=posts,
                    pages=pages,
                    started_at=started,
                    completed_at=self._now_utc(),
                    truncated=True,
                    ordering_violated=ordering_violated,
                    watermark_stopped=watermark_stopped,
                    request_succeeded=pages > 0,
                    warnings=warnings,
                )
            pages += 1
            assert payload is not None
            # Observation time is stamped only after this page has arrived.
            # A request start timestamp would incorrectly claim the posts were
            # known while the provider response was still in flight. Tests
            # control the injected clock instead of forging a first_seen value.
            page_observed = self._now_utc()
            page_identities: list[tuple[str, str]] = []
            for item in payload:
                post = _post_from_payload(item, ticker=symbol, first_seen_at=page_observed)
                if post is None:
                    continue
                if previous_date is not None and post.published_at > previous_date:
                    ordering_violated = True
                previous_date = post.published_at
                identity = (post.provider_post_id, post.content_hash)
                page_identities.append(identity)
                if identity in seen:
                    continue
                seen.add(identity)
                posts.append(post)
            if len(payload) < self.config.page_size:
                break
            if archived_keys and page_identities and all(
                identity in archived_keys for identity in page_identities
            ):
                # Offset pagination has no cursor/since contract. Once a whole
                # page is already archived, stop rather than downloading the
                # same deep history every five minutes. A preceding page with
                # new posts is still collected before this watermark page.
                watermark_stopped = True
                warnings.append(
                    "FireAnt incremental pagination stopped at an archived page; "
                    "provider ordering/completeness is not contractual."
                )
                break
        else:
            if maximum_pages >= self.config.max_pages:
                truncated = True
                warnings.append("FireAnt pagination reached the configured max_pages limit.")
            else:
                truncated = True
                warnings.append("FireAnt pagination stopped at an explicit page limit.")

        if ordering_violated:
            warnings.append("FireAnt pagination order was not monotonically descending.")
        if not posts:
            status = SocialStatus.UNAVAILABLE
            warnings.append("FireAnt returned no eligible exact-ticker posts.")
        elif truncated or ordering_violated or watermark_stopped:
            status = SocialStatus.PARTIAL
        else:
            status = SocialStatus.AVAILABLE
        return SocialFetchResult(
            fetch_id=fetch_id,
            provider="fireant",
            ticker=symbol,
            status=status,
            posts=posts,
            pages=pages,
            started_at=started,
            completed_at=self._now_utc(),
            truncated=truncated,
            ordering_violated=ordering_violated,
            watermark_stopped=watermark_stopped,
            request_succeeded=pages > 0,
            warnings=warnings,
        )


def _author_selection_key(post: SocialPost) -> str:
    if post.author_key:
        return post.author_key
    for key in ("id", "user_id", "username", "name"):
        if post.author.get(key):
            return f"{key}:{post.author[key]}"
    return f"post:{post.provider_post_id}"


def _engagement_score(post: SocialPost) -> float:
    return float(
        sum(
            value
            for value in post.engagement.values()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        )
    )


def select_prompt_posts(posts: list[SocialPost], limit: int = 40) -> list[SocialPost]:
    """Select recent + engaged evidence, capped at three posts per author."""
    if limit <= 0:
        return []
    eligible = [post for post in posts if post.text and not post.is_ai_generated]
    recent = sorted(
        eligible, key=lambda post: (post.published_at, post.provider_post_id), reverse=True
    )
    engaged = sorted(
        eligible,
        key=lambda post: (_engagement_score(post), post.published_at),
        reverse=True,
    )
    recent_target = min(20, limit)
    engaged_target = min(20, max(limit - recent_target, 0))
    selected: list[SocialPost] = []
    selected_ids: set[tuple[str, str]] = set()
    author_counts: dict[str, int] = {}

    def take(candidates: list[SocialPost], target: int) -> None:
        added = 0
        for post in candidates:
            if added >= target or len(selected) >= limit:
                break
            identity = (post.provider_post_id, post.content_hash)
            author = _author_selection_key(post)
            if identity in selected_ids or author_counts.get(author, 0) >= 3:
                continue
            selected.append(post)
            selected_ids.add(identity)
            author_counts[author] = author_counts.get(author, 0) + 1
            added += 1

    take(recent, recent_target)
    take(engaged, engaged_target)
    if len(selected) < limit:
        take(recent + engaged, limit - len(selected))
    return selected


def _analysis_cutoff(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_VN_TZ)
        return parsed.astimezone(UTC)
    raw = str(value)
    try:
        if len(raw) == 10:
            local = datetime.combine(
                datetime.fromisoformat(raw).date(), datetime_time(15, 0), tzinfo=_VN_TZ
            )
            return local.astimezone(UTC)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("as_of must be YYYY-MM-DD or an ISO datetime") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_VN_TZ)
    return parsed.astimezone(UTC)


class VietnamSocialService:
    def __init__(
        self,
        config: VietnamSocialConfig,
        *,
        client: FireAntClient | None = None,
        archive: VietnamSocialArchive | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.archive = archive

    def _profile_fingerprint(self) -> str:
        archive_id = self.archive.archive_id if self.archive is not None else None
        return self.config.profile_fingerprint(archive_id)

    def _issues(self, *, require_watchlist: bool) -> list[str]:
        issues: list[str] = []
        if self.config.provider != "fireant":
            issues.append("FireAnt provider is disabled.")
        if self.config.authorized:
            if not self.config.access_token:
                issues.append("FIREANT_ACCESS_TOKEN is missing.")
            if not self.config.encryption_key:
                issues.append("FIREANT_ARCHIVE_ENCRYPTION_KEY is missing.")
            if require_watchlist and not self.config.watchlist:
                issues.append("TRADINGAGENTS_VN_SOCIAL_TICKERS is empty.")
        return issues

    def status(self, live: bool = False) -> dict[str, Any]:
        issues = self._issues(require_watchlist=True)
        payload: dict[str, Any] = {
            "provider": "fireant",
            "enabled": self.config.enabled,
            "authorized": self.config.authorized,
            "hosted_llm_authorized": self.config.hosted_llm_authorized,
            "archive_ready": self.archive is not None,
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "archive_id": self.archive.archive_id if self.archive is not None else None,
            "profile_fingerprint": self._profile_fingerprint(),
            "watchlist": list(self.config.watchlist),
            "issues": issues,
            "live_checked": False,
        }
        if live and not issues and self.config.watchlist and self.client is not None:
            # A doctor probe needs auth/schema connectivity, not a deep
            # backfill. Limiting it to one page prevents an accidental 2,000
            # item request from an otherwise read-only health check.
            result = self.client.fetch_posts(self.config.watchlist[0], page_limit=1)
            payload["live_checked"] = True
            payload["live_status"] = result.status.value
            payload["live_sample_size"] = len(result.posts)
            # Doctor checks authentication/connectivity, not evidence coverage.
            # An authenticated HTTP 200 with [] is a healthy provider call even
            # though the retail signal itself remains unavailable.
            if not result.request_succeeded:
                payload["issues"] = [*issues, *result.warnings]
        return payload

    def _require_operational(self, *, require_watchlist: bool = True) -> None:
        if not self.config.authorized:
            raise RuntimeError("FireAnt collection is authorization locked")
        issues = self._issues(require_watchlist=require_watchlist)
        if issues:
            raise RuntimeError("; ".join(issues))
        if self.client is None or self.archive is None:
            raise RuntimeError("FireAnt service is not initialized")

    def collect_once(self, ticker: str | None = None) -> list[dict[str, Any]]:
        # An explicit one-shot ticker is intentionally independent from the
        # scheduled collector watchlist.  The watchlist remains mandatory only
        # for ``collect_once()`` without a ticker, where it defines the scope of
        # the batch job.
        explicit_ticker = ticker is not None
        self._require_operational(require_watchlist=not explicit_ticker)
        targets = (
            (_validate_ticker(ticker),)
            if explicit_ticker
            else self.config.watchlist
        )
        results = []
        for symbol in targets:
            with self.archive.collection_lock(symbol) as acquired:
                if not acquired:
                    results.append(
                        {
                            "ticker": symbol,
                            "status": "skipped",
                            "reason": "collector_lock_held",
                        }
                    )
                    continue
                fetched = self.client.fetch_posts(
                    symbol,
                    known_keys=self.archive.known_content_keys(symbol),
                )
                results.append(self.archive.record_fetch(fetched))
        return results

    def load_evidence(
        self,
        ticker: str,
        as_of: str | datetime,
        lookback_days: int | None = None,
        *,
        allow_snapshot: bool = True,
    ) -> SocialEvidenceBatch:
        symbol = _validate_ticker(ticker)
        cutoff = _analysis_cutoff(as_of)
        days = self.config.lookback_days if lookback_days is None else int(lookback_days)
        if not 1 <= days <= 90:
            raise ValueError("lookback_days must be between 1 and 90")
        window_start = cutoff - timedelta(days=days)
        base = {
            "provider": "fireant",
            "ticker": symbol,
            "window_start": window_start.isoformat(),
            "window_end": cutoff.isoformat(),
        }
        if not self.config.authorized:
            return SocialEvidenceBatch(
                status=SocialStatus.DISABLED,
                warnings=["FireAnt collection is authorization locked."],
                **base,
            )
        if self.archive is None:
            return SocialEvidenceBatch(
                status=SocialStatus.UNAVAILABLE,
                warnings=["Encrypted FireAnt archive is unavailable."],
                **base,
            )

        analysis_date = cutoff.astimezone(_VN_TZ).date().isoformat()
        # Daily snapshots are produced after the 15:00 Vietnam close. Never
        # expose one to an intraday caller from the same calendar date.
        snapshot = (
            self.get_snapshot(symbol, analysis_date)
            if allow_snapshot
            and cutoff.astimezone(_VN_TZ).time() >= datetime_time(15, 0)
            else None
        )
        if snapshot is not None:
            statistics = snapshot.statistics or {}
            snapshot_window_end = _parse_datetime(statistics.get("window_end"))
            if snapshot_window_end is not None and snapshot_window_end > cutoff:
                snapshot = None
        if snapshot is not None:
            statistics = snapshot.statistics or {}
            return SocialEvidenceBatch(
                status=SocialStatus(snapshot.status),
                snapshot_id=snapshot.snapshot_id,
                signal_payload=snapshot.signal_payload,
                report_payload=snapshot.report_payload,
                sample_size=int(statistics.get("sample_size", 0) or 0),
                unique_authors=int(statistics.get("unique_authors", 0) or 0),
                point_in_time_quality=str(statistics.get("point_in_time_quality") or "proxy"),
                warnings=list(statistics.get("warnings", []) or []),
                **base,
            )

        posts = self.archive.posts_for_window(
            symbol, start=window_start, as_of=cutoff, post_factory=SocialPost
        )
        primary = [post for post in posts if not post.is_ai_generated]
        author_keys = {_author_selection_key(post) for post in primary}
        complete, coverage_warnings, fetch_id = self.archive.coverage_for_window(
            symbol,
            start=window_start,
            as_of=cutoff,
            poll_seconds=self.config.poll_seconds,
        )
        warnings = list(coverage_warnings)
        warnings.append(
            "FireAnt does not provide a revision history for same-content changes to "
            "AI flags, tags, author metadata, or publication timestamps."
        )
        if len(primary) < self.config.min_posts:
            warnings.append(
                f"Only {len(primary)} eligible posts; minimum is {self.config.min_posts}."
            )
        if len(author_keys) < self.config.min_unique_authors:
            warnings.append(
                f"Only {len(author_keys)} unique authors; minimum is "
                f"{self.config.min_unique_authors}."
            )
        enough = (
            len(primary) >= self.config.min_posts
            and len(author_keys) >= self.config.min_unique_authors
        )
        status = (
            SocialStatus.AVAILABLE
            if enough and complete
            else SocialStatus.PARTIAL
            if enough
            else SocialStatus.UNAVAILABLE
        )
        return SocialEvidenceBatch(
            status=status,
            posts=primary,
            sample_size=len(primary),
            unique_authors=len(author_keys),
            fetch_id=fetch_id,
            # FireAnt documents only offset/limit pagination. Even uninterrupted
            # polling cannot prove upstream ordering or full historical coverage.
            point_in_time_quality="proxy" if complete else "partial",
            warnings=warnings,
            **base,
        )

    def save_snapshot(
        self,
        ticker: str,
        analysis_date: str,
        **kwargs: Any,
    ) -> SnapshotRecord:
        self._require_operational(require_watchlist=False)
        symbol = _validate_ticker(ticker)
        supplied_profile = kwargs.pop("profile_fingerprint", None)
        expected_profile = self._profile_fingerprint()
        if supplied_profile not in (None, expected_profile):
            raise ValueError("snapshot profile_fingerprint does not match service configuration")
        return self.archive.save_snapshot(
            symbol,
            analysis_date,
            profile_fingerprint=expected_profile,
            **kwargs,
        )

    def claim_snapshot(
        self,
        ticker: str,
        analysis_date: str,
        *,
        lease_seconds: int = 900,
    ) -> Any:
        """Claim one LLM snapshot job before the expensive stage invocation."""
        self._require_operational(require_watchlist=False)
        return self.archive.claim_snapshot(
            _validate_ticker(ticker),
            analysis_date,
            prompt_version=self.config.prompt_version,
            profile_fingerprint=self._profile_fingerprint(),
            lease_seconds=lease_seconds,
        )

    def release_snapshot_claim(self, claim: Any) -> bool:
        if self.archive is None:
            return False
        return self.archive.release_snapshot_claim(claim)

    def get_snapshot(
        self,
        ticker: str,
        analysis_date: str,
        *,
        model_profile: str | None = None,
        fingerprint: str | None = None,
    ) -> SnapshotRecord | None:
        """Load only a snapshot compatible with this immutable social profile."""
        if self.archive is None:
            return None
        return self.archive.get_snapshot(
            _validate_ticker(ticker),
            analysis_date,
            prompt_version=self.config.prompt_version,
            profile_fingerprint=self._profile_fingerprint(),
            model_profile=model_profile,
            fingerprint=fingerprint,
            strict=True,
        )

    def purge(self) -> dict[str, int]:
        if self.archive is None:
            raise RuntimeError("Encrypted FireAnt archive is unavailable")
        return self.archive.purge_expired(retention_days=self.config.retention_days)


def create_vietnam_social_service_from_env(
    session: Any = None,
    sleep: Callable[[float], None] | None = None,
) -> VietnamSocialService:
    config = VietnamSocialConfig.from_env()
    archive = None
    archive_error = None
    if config.encryption_key and (config.authorized or config.archive_path.exists()):
        try:
            archive = VietnamSocialArchive(config.archive_path, config.encryption_key)
        except ArchiveConfigurationError as exc:
            archive_error = str(exc)
    client = FireAntClient(config, session=session, sleep=sleep) if config.access_token else None
    service = VietnamSocialService(config, client=client, archive=archive)
    if archive_error and config.authorized:
        # Preserve a safe diagnostic for status without retaining a raw exception.
        original_issues = service._issues

        def issues_with_archive(*, require_watchlist: bool) -> list[str]:
            return [*original_issues(require_watchlist=require_watchlist), archive_error]

        service._issues = issues_with_archive  # type: ignore[method-assign]
    return service


def get_social_data(
    ticker: str,
    as_of: str,
    lookback_days: int | None = None,
) -> SocialEvidenceBatch:
    """Vendor-router facade; analysis reads the archive and never calls FireAnt live."""
    return create_vietnam_social_service_from_env().load_evidence(
        ticker, as_of, lookback_days=lookback_days
    )


__all__ = [
    "FireAntClient",
    "SocialEvidenceBatch",
    "SocialFetchResult",
    "SocialPost",
    "SocialStatus",
    "VietnamSocialConfig",
    "VietnamSocialService",
    "create_vietnam_social_service_from_env",
    "get_social_data",
    "sanitize_post_text",
    "select_prompt_posts",
]
