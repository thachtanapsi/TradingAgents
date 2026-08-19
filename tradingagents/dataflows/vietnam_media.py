"""Authorization-locked CafeF/VnExpress RSS collection and archive facade.

Analyst calls are archive-only.  The only live HTTP paths are the explicit
collector and ``status(live=True)`` diagnostics used by the GX CLI.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import random
import re
import time as time_module
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests

from .config import get_config
from .vietnam_media_archive import VietnamMediaArchive

try:
    from defusedxml import ElementTree as SafeElementTree
except ImportError:  # pragma: no cover - guarded before live parsing.
    SafeElementTree = None  # type: ignore[assignment]


UTC = timezone.utc
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ALIAS_POLICY_VERSION = "vn-media-alias-v2"
PROMPT_VERSION = "vn-media-v1"
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_MARKET_CONTEXT_CATEGORIES = {"market", "macro", "banking", "business"}
_TICKER_CONTEXT = re.compile(
    r"\b(?:hose|hnx|upcom|cổ\s+phiếu|chứng\s+khoán|mã\s+cp|tăng\s+trần|giảm\s+sàn)\b",
    re.IGNORECASE,
)
_FINANCIAL_HEADLINE_CONTEXT = re.compile(
    r"\b(?:doanh\s+thu|lợi\s+nhuận|cổ\s+tức|kết\s+quả\s+kinh\s+doanh|"
    r"báo\s+cáo\s+tài\s+chính|lãi\s+(?:gấp|ròng|sau\s+thuế|trước\s+thuế|"
    r"tăng|giảm))\b",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


class MediaStatus(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


@dataclass(frozen=True)
class MediaArticle:
    provider: str
    provider_article_id: str
    title: str
    summary: str
    canonical_url: str
    published_at: datetime
    first_seen_at: datetime
    retrieved_at: datetime
    category: str
    matched_tickers: list[str]
    relevance_reasons: list[str]
    content_hash: str


@dataclass
class MediaSourceResult:
    provider: str
    status: MediaStatus
    articles: list[MediaArticle] = field(default_factory=list)
    fetch_id: str | None = None
    point_in_time_quality: Literal["proxy", "partial"] = "partial"
    warnings: list[str] = field(default_factory=list)
    ticker: str = ""
    feed_url: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None
    http_status: int | None = None
    request_succeeded: bool = False
    etag: str | None = None
    last_modified: str | None = None
    alias_policy_version: str = ALIAS_POLICY_VERSION


@dataclass
class VietnamMediaResult:
    status: MediaStatus
    ticker: str
    articles: list[MediaArticle]
    sources: list[MediaSourceResult]
    window_start: datetime
    window_end: datetime
    selected_count: int
    deduplicated_count: int
    warnings: list[str]


@dataclass(frozen=True)
class _Feed:
    provider: str
    url: str
    category: str


FEEDS = (
    _Feed("cafef_rss", "https://cafef.vn/thi-truong-chung-khoan.rss", "market"),
    _Feed("cafef_rss", "https://cafef.vn/doanh-nghiep.rss", "company"),
    _Feed("cafef_rss", "https://cafef.vn/tai-chinh-ngan-hang.rss", "banking"),
    _Feed("cafef_rss", "https://cafef.vn/vi-mo-dau-tu.rss", "macro"),
    _Feed("vnexpress_rss", "https://vnexpress.net/rss/kinh-doanh.rss", "business"),
)
_PROVIDER_HOSTS = {"cafef_rss": "cafef.vn", "vnexpress_rss": "vnexpress.net"}
_AUTH_ENV = {
    "cafef_rss": "TRADINGAGENTS_CAFEF_RSS_AUTHORIZED",
    "vnexpress_rss": "TRADINGAGENTS_VNEXPRESS_RSS_AUTHORIZED",
}


def _env_true(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() in {"1", "true", "yes", "on"}


def _split_values(value: Any, *, upper: bool = False) -> tuple[str, ...]:
    parts = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    clean = [str(item).strip() for item in parts if str(item).strip()]
    clean = [item.upper() if upper else item.lower() for item in clean]
    return tuple(dict.fromkeys(clean))


@dataclass(frozen=True)
class VietnamMediaConfig:
    providers: tuple[str, ...]
    watchlist: tuple[str, ...]
    lookback_days: int
    min_articles: int
    poll_seconds: int
    retention_days: int
    archive_path: Path
    encryption_key: str = field(repr=False, compare=False)
    timeout_seconds: float = 10.0
    max_retries: int = 3
    alias_policy_version: str = ALIAS_POLICY_VERSION
    prompt_version: str = PROMPT_VERSION

    @classmethod
    def from_env(cls, config: dict[str, Any] | None = None) -> VietnamMediaConfig:
        active = config or get_config()
        settings = dict(active.get("vn_media") or {})
        providers = _split_values(
            os.environ.get("TRADINGAGENTS_VN_MEDIA_PROVIDERS")
            or settings.get("providers")
        )
        unknown = sorted(set(providers) - set(_AUTH_ENV))
        if unknown:
            raise ValueError("unsupported Vietnam media provider(s): " + ", ".join(unknown))
        instance = cls(
            providers=providers,
            watchlist=_split_values(
                os.environ.get("TRADINGAGENTS_VN_MEDIA_TICKERS")
                or settings.get("tickers"),
                upper=True,
            ),
            lookback_days=int(settings.get("lookback_days", 7)),
            min_articles=int(settings.get("min_articles", 3)),
            poll_seconds=int(settings.get("poll_seconds", 300)),
            retention_days=int(settings.get("raw_retention_days", 30)),
            archive_path=Path(
                str(
                    os.environ.get("TRADINGAGENTS_VN_MEDIA_ARCHIVE_PATH")
                    or settings.get("archive_path")
                    or "~/.tradingagents/cache/media/vn_media.sqlite3"
                )
            ).expanduser(),
            encryption_key=os.environ.get("VN_MEDIA_ARCHIVE_ENCRYPTION_KEY", ""),
            timeout_seconds=float(settings.get("timeout_seconds", 10.0)),
            max_retries=int(settings.get("max_retries", 3)),
            alias_policy_version=str(
                settings.get("alias_policy_version") or ALIAS_POLICY_VERSION
            ),
            prompt_version=str(settings.get("prompt_version") or PROMPT_VERSION),
        )
        if not 1 <= instance.lookback_days <= 90:
            raise ValueError("media lookback_days must be between 1 and 90")
        if not 1 <= instance.min_articles <= 100:
            raise ValueError("media min_articles must be between 1 and 100")
        if not 60 <= instance.poll_seconds <= 86_400:
            raise ValueError("media poll_seconds must be between 60 and 86400")
        if not 1 <= instance.retention_days <= 365:
            raise ValueError("media retention_days must be between 1 and 365")
        if not 1 <= instance.max_retries <= 3:
            raise ValueError("media max_retries must be between 1 and 3")
        return instance

    def authorized(self, provider: str) -> bool:
        return provider in self.providers and _env_true(_AUTH_ENV[provider])


def _cutoff(value: str | date | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return datetime.combine(value, time(15, 0), VN_TZ)
    else:
        raw = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return datetime.combine(date.fromisoformat(raw), time(15, 0), VN_TZ)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=VN_TZ)
    return parsed.astimezone(VN_TZ)


def _clean_text(value: Any, limit: int = 2000) -> str:
    text = html.unescape(_TAG_RE.sub(" ", str(value or "")))
    return " ".join(text.split())[:limit]


def _canonical_url(value: str, provider: str) -> str:
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    expected = _PROVIDER_HOSTS[provider]
    if (
        parsed.scheme.lower() != "https"
        or not (host == expected or host.endswith("." + expected))
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("RSS item URL is outside the provider HTTPS allowlist")
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
    ]
    return urlunsplit(("https", host + port, parsed.path or "/", urlencode(query), ""))


def _validated_feed_url(value: str, provider: str) -> str:
    """Validate a feed/redirect URL without normalizing its query semantics."""
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    expected = _PROVIDER_HOSTS[provider]
    if (
        parsed.scheme.lower() != "https"
        or not (host == expected or host.endswith("." + expected))
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("RSS URL is outside the provider HTTPS allowlist")
    return value


def _published(value: str) -> datetime:
    parsed = parsedate_to_datetime(value)
    if parsed is None:
        raise ValueError("RSS item publication time is invalid")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=VN_TZ)
    return parsed.astimezone(UTC)


def _matches(ticker: str, aliases: list[str], title: str, summary: str) -> tuple[bool, list[str]]:
    body = f"{title} {summary}"
    folded = body.casefold()
    reasons: list[str] = []
    for alias in aliases:
        normalized = " ".join(str(alias).split())
        if len(normalized) >= 4 and normalized.casefold() in folded:
            reasons.append(f"company_alias:{normalized[:80]}")
    ticker_pattern = rf"(?<![A-Z0-9]){re.escape(ticker)}(?![A-Z0-9])"
    ticker_match = re.search(ticker_pattern, body, re.I)
    financial_headline_match = re.search(ticker_pattern, title, re.I) and (
        _FINANCIAL_HEADLINE_CONTEXT.search(title)
    )
    if ticker_match and (
        _TICKER_CONTEXT.search(body) or financial_headline_match or reasons or len(ticker) > 4
    ):
        reasons.append("contextual_ticker")
    return bool(reasons), list(dict.fromkeys(reasons))


def _ticker_mapping(
    ticker: str, aliases: list[str], title: str, summary: str
) -> tuple[list[str], list[str]]:
    """Map only direct company evidence; market context is selected separately."""
    matched, reasons = _matches(ticker, aliases, title, summary)
    return ([ticker] if matched else []), reasons


def _node_text(node: Any, local_name: str) -> str:
    for child in list(node):
        if str(child.tag).split("}")[-1].lower() == local_name.lower():
            return "".join(child.itertext()).strip()
    return ""


def _parse_feed(
    content: bytes,
    *,
    feed: _Feed,
    ticker: str,
    aliases: list[str],
    observed_at: datetime,
) -> list[MediaArticle]:
    if SafeElementTree is None:
        raise RuntimeError("install the 'vn-media' optional dependency for safe RSS XML parsing")
    root = SafeElementTree.fromstring(content)
    output: list[MediaArticle] = []
    for node in root.iter():
        if str(node.tag).split("}")[-1].lower() != "item":
            continue
        title = _clean_text(_node_text(node, "title"), 500)
        summary = _clean_text(
            _node_text(node, "description") or _node_text(node, "summary"), 2000
        )
        link = _node_text(node, "link")
        published_raw = _node_text(node, "pubDate") or _node_text(node, "published")
        guid = _node_text(node, "guid")
        if not title or not link or not published_raw:
            continue
        try:
            canonical = _canonical_url(link, feed.provider)
            published_at = _published(published_raw)
        except (TypeError, ValueError, OverflowError):
            continue
        matched_tickers, reasons = _ticker_mapping(ticker, aliases, title, summary)
        article_id = guid.strip() or canonical
        digest = hashlib.sha256(
            json.dumps(
                [feed.provider, title, summary, canonical, published_at.isoformat()],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        output.append(
            MediaArticle(
                provider=feed.provider,
                provider_article_id=article_id,
                title=title,
                summary=summary,
                canonical_url=canonical,
                published_at=published_at,
                first_seen_at=observed_at,
                retrieved_at=observed_at,
                category=feed.category,
                matched_tickers=matched_tickers,
                relevance_reasons=reasons,
                content_hash=digest,
            )
        )
    return output


def _retry_after(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(float(value), 60.0))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(0.0, min((parsed - now.astimezone(UTC)).total_seconds(), 60.0))
        except (TypeError, ValueError, OverflowError):
            return None


class RssMediaClient:
    def __init__(
        self,
        config: VietnamMediaConfig,
        *,
        session: Any = None,
        sleep: Any = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.sleep = sleep or time_module.sleep

    def fetch(
        self,
        feed: _Feed,
        ticker: str,
        aliases: list[str],
        *,
        cache_headers: dict[str, str] | None = None,
    ) -> MediaSourceResult:
        started = datetime.now(UTC)
        fetch_id = uuid.uuid4().hex
        if not self.config.authorized(feed.provider):
            return MediaSourceResult(
                provider=feed.provider,
                status=MediaStatus.DISABLED,
                ticker=ticker,
                feed_url=feed.url,
                fetch_id=fetch_id,
                started_at=started,
                completed_at=started,
                warnings=[f"{feed.provider} collection is authorization locked."],
            )
        headers = {
            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9",
            "User-Agent": "TradingAgents-GX-RSS/1.0 (+APG authorized collector)",
            **(cache_headers or {}),
        }
        warnings: list[str] = []
        for attempt in range(self.config.max_retries):
            response = None
            try:
                response = self.session.get(
                    feed.url,
                    headers=headers,
                    timeout=self.config.timeout_seconds,
                    allow_redirects=False,
                    stream=True,
                )
                if response.status_code != 304 and 300 <= response.status_code < 400:
                    location = urljoin(feed.url, response.headers.get("Location", ""))
                    _validated_feed_url(location, feed.provider)
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
                    response = None
                    response = self.session.get(
                        location,
                        headers=headers,
                        timeout=self.config.timeout_seconds,
                        allow_redirects=False,
                        stream=True,
                    )
                    if response.status_code != 304 and 300 <= response.status_code < 400:
                        warnings.append(
                            f"{feed.provider} returned an unsupported redirect chain."
                        )
                        break
                completed = datetime.now(UTC)
                if response.status_code == 304:
                    return MediaSourceResult(
                        provider=feed.provider,
                        status=MediaStatus.AVAILABLE,
                        ticker=ticker,
                        feed_url=feed.url,
                        fetch_id=fetch_id,
                        started_at=started,
                        completed_at=completed,
                        http_status=304,
                        request_succeeded=True,
                        point_in_time_quality="proxy",
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt + 1 < self.config.max_retries:
                        delay = _retry_after(response.headers.get("Retry-After"), completed)
                        self.sleep(delay if delay is not None else min(2**attempt + random.random(), 5))
                        continue
                    warnings.append(f"{feed.provider} returned HTTP {response.status_code}.")
                    break
                if response.status_code != 200:
                    warnings.append(f"{feed.provider} returned HTTP {response.status_code}.")
                    break
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        warnings.append(
                            f"{feed.provider} RSS response exceeded the 2 MiB safety limit."
                        )
                        break
                    chunks.append(chunk)
                if total > MAX_RESPONSE_BYTES:
                    break
                content = b"".join(chunks)
                # first_seen_at is stamped only after the entire HTTP body has
                # arrived. A response crossing an as-of cutoff therefore cannot
                # leak into an earlier historical query.
                completed = datetime.now(UTC)
                try:
                    articles = _parse_feed(
                        content,
                        feed=feed,
                        ticker=ticker,
                        aliases=aliases,
                        observed_at=completed,
                    )
                except Exception as exc:  # noqa: BLE001 - XML parser types are optional.
                    warnings.append(
                        f"{feed.provider} RSS payload was malformed ({type(exc).__name__})."
                    )
                    break
                return MediaSourceResult(
                    provider=feed.provider,
                    status=MediaStatus.AVAILABLE,
                    articles=articles,
                    ticker=ticker,
                    feed_url=feed.url,
                    fetch_id=fetch_id,
                    started_at=started,
                    completed_at=completed,
                    http_status=200,
                    request_succeeded=True,
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                    point_in_time_quality="proxy",
                    warnings=warnings,
                    alias_policy_version=self.config.alias_policy_version,
                )
            except requests.RequestException as exc:
                if attempt + 1 < self.config.max_retries:
                    self.sleep(min(2**attempt + random.random(), 5))
                    continue
                warnings.append(f"{feed.provider} RSS retrieval failed ({type(exc).__name__}).")
            except Exception as exc:  # noqa: BLE001 - injected transports fail closed.
                warnings.append(f"{feed.provider} RSS retrieval failed ({type(exc).__name__}).")
                break
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        return MediaSourceResult(
            provider=feed.provider,
            status=MediaStatus.UNAVAILABLE,
            ticker=ticker,
            feed_url=feed.url,
            fetch_id=fetch_id,
            started_at=started,
            completed_at=datetime.now(UTC),
            warnings=warnings or [f"{feed.provider} RSS retrieval failed."],
        )


def _gx_aliases(ticker: str) -> tuple[list[str], list[str]]:
    try:
        from .gx_market_info import get_instrument_aliases

        aliases = get_instrument_aliases(ticker)
        return aliases, ["GX aliases are current-state metadata; historical matching is proxy."]
    except Exception as exc:  # noqa: BLE001 - collection can retain ticker-context evidence.
        return [], [f"GX company aliases were unavailable ({type(exc).__name__})."]


class VietnamMediaService:
    def __init__(
        self,
        config: VietnamMediaConfig,
        *,
        client: RssMediaClient | None = None,
        archive: VietnamMediaArchive | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.archive = archive

    @property
    def archive_id(self) -> str | None:
        return self.archive.archive_id if self.archive else None

    def profile_fingerprint(self) -> str:
        payload = {
            "providers": self.config.providers,
            "lookback_days": self.config.lookback_days,
            "min_articles": self.config.min_articles,
            "archive_id": self.archive_id,
            "alias_policy_version": self.config.alias_policy_version,
            "prompt_version": self.config.prompt_version,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def status(self, *, live: bool = False) -> dict[str, Any]:
        sources = []
        issues: list[str] = []
        for provider in self.config.providers:
            authorized = self.config.authorized(provider)
            item: dict[str, Any] = {
                "provider": provider,
                "status": "available" if authorized else "disabled",
            }
            if live and authorized:
                provider_feeds = [feed for feed in FEEDS if feed.provider == provider]
                probes = [
                    self.client.fetch(feed, "VNINDEX", [], cache_headers={})
                    for feed in provider_feeds
                ] if self.client else []
                succeeded = bool(probes) and all(
                    probe.request_succeeded for probe in probes
                )
                item["request_succeeded"] = succeeded
                item["feeds_checked"] = len(probes)
                item["status"] = "available" if succeeded else "unavailable"
                for probe in probes:
                    if not probe.request_succeeded:
                        issues.extend(probe.warnings)
            sources.append(item)
        active = [item for item in sources if item["status"] != "disabled"]
        return {
            "status": "disabled" if not active else "unavailable" if issues else "available",
            "enabled": bool(self.config.providers),
            "archive_ready": self.archive is not None,
            "archive_id": self.archive_id,
            "watchlist": list(self.config.watchlist),
            "sources": sources,
            "issues": issues,
        }

    def collect_once(self, ticker: str | None = None) -> list[dict[str, Any]]:
        if self.archive is None or self.client is None:
            raise RuntimeError("encrypted Vietnam media archive is unavailable")
        symbols = [ticker.upper()] if ticker else list(self.config.watchlist)
        if not symbols:
            raise RuntimeError("TRADINGAGENTS_VN_MEDIA_TICKERS is empty")
        results: list[dict[str, Any]] = []
        for symbol in symbols:
            with self.archive.collection_lock(symbol) as acquired:
                if not acquired:
                    results.append({"ticker": symbol, "status": "skipped", "reason": "collector_locked"})
                    continue
                aliases, alias_warnings = _gx_aliases(symbol)
                for feed in FEEDS:
                    if feed.provider not in self.config.providers:
                        continue
                    fetched = self.client.fetch(
                        feed,
                        symbol,
                        aliases,
                        cache_headers=self.archive.feed_cache_headers(feed.url),
                    )
                    fetched.warnings.extend(alias_warnings)
                    results.append(self.archive.record_fetch(fetched))
        return results

    def load_evidence(
        self,
        ticker: str,
        as_of: str | date | datetime,
        lookback_days: int | None = None,
        aliases: list[str] | None = None,
        include_market_context: bool = False,
    ) -> VietnamMediaResult:
        del aliases  # Matching happens at first observation during collection.
        cutoff = _cutoff(as_of)
        days = self.config.lookback_days if lookback_days is None else int(lookback_days)
        if not 1 <= days <= 90:
            raise ValueError("lookback_days must be between 1 and 90")
        start = cutoff - timedelta(days=days)
        if self.archive is None:
            return VietnamMediaResult(
                status=MediaStatus.UNAVAILABLE,
                ticker=ticker.upper(),
                articles=[],
                sources=[],
                window_start=start,
                window_end=cutoff,
                selected_count=0,
                deduplicated_count=0,
                warnings=["Encrypted Vietnam media archive is unavailable."],
            )
        articles = self.archive.articles_for_window(
            start=start,
            as_of=cutoff,
            article_factory=MediaArticle,
            ticker=ticker,
            alias_policy_version=self.config.alias_policy_version,
            include_categories=_MARKET_CONTEXT_CATEGORIES if include_market_context else None,
        )
        unique: dict[str, MediaArticle] = {}
        seen_fallbacks: set[str] = set()
        for article in articles:
            key = article.canonical_url or f"{article.provider}:{article.provider_article_id}"
            fallback = hashlib.sha256(
                json.dumps(
                    [" ".join(article.title.casefold().split()), article.published_at.date().isoformat()],
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            if key in unique or fallback in seen_fallbacks:
                continue
            unique.setdefault(key, article)
            seen_fallbacks.add(fallback)
        ordered = sorted(unique.values(), key=lambda item: item.published_at, reverse=True)
        company = [item for item in ordered if ticker.upper() in item.matched_tickers][:20]
        market = [item for item in ordered if ticker.upper() not in item.matched_tickers][:10]
        selected = company + market if include_market_context else company
        source_results: list[MediaSourceResult] = []
        warnings: list[str] = [
            "RSS ordering/history cannot prove complete upstream coverage; PIT quality is proxy."
        ]
        active_providers = [
            provider for provider in self.config.providers if self.config.authorized(provider)
        ]
        complete_all = True
        for provider in self.config.providers:
            if provider not in active_providers:
                source_results.append(
                    MediaSourceResult(provider, MediaStatus.DISABLED, warnings=["Authorization locked."])
                )
                continue
            provider_feeds = [feed.url for feed in FEEDS if feed.provider == provider]
            complete, coverage_warnings, fetch_id = self.archive.coverage_for_window(
                ticker,
                provider,
                start=start,
                as_of=cutoff,
                poll_seconds=self.config.poll_seconds,
                expected_feed_urls=provider_feeds,
            )
            complete_all = complete_all and complete
            subset = [item for item in selected if item.provider == provider]
            source_status = MediaStatus.AVAILABLE if complete else MediaStatus.PARTIAL
            source_results.append(
                MediaSourceResult(
                    provider=provider,
                    status=source_status,
                    articles=subset,
                    fetch_id=fetch_id,
                    point_in_time_quality="proxy" if complete else "partial",
                    warnings=coverage_warnings,
                )
            )
            warnings.extend(coverage_warnings)
        if not active_providers or not selected:
            status = MediaStatus.UNAVAILABLE
            warnings.append("No relevant archived editorial article was available.")
        elif len(selected) < self.config.min_articles or not complete_all:
            status = MediaStatus.PARTIAL
            if len(selected) < self.config.min_articles:
                warnings.append(
                    f"Only {len(selected)} article(s); minimum is {self.config.min_articles}."
                )
        else:
            status = MediaStatus.AVAILABLE
        return VietnamMediaResult(
            status=status,
            ticker=ticker.upper(),
            articles=selected,
            sources=source_results,
            window_start=start,
            window_end=cutoff,
            selected_count=len(selected),
            deduplicated_count=max(0, len(articles) - len(unique)),
            warnings=list(dict.fromkeys(warnings)),
        )

    def purge(self) -> dict[str, int]:
        if self.archive is None:
            raise RuntimeError("encrypted Vietnam media archive is unavailable")
        return self.archive.purge_expired(retention_days=self.config.retention_days)


def create_vietnam_media_service_from_env(
    session: Any = None, sleep: Any = None
) -> VietnamMediaService:
    config = VietnamMediaConfig.from_env()
    archive = None
    if config.encryption_key and (any(config.authorized(p) for p in config.providers) or config.archive_path.exists()):
        archive = VietnamMediaArchive(config.archive_path, config.encryption_key)
    client = RssMediaClient(config, session=session, sleep=sleep)
    return VietnamMediaService(config, client=client, archive=archive)


def render_vietnam_media_result(result: VietnamMediaResult) -> str:
    if result.status in {MediaStatus.UNAVAILABLE, MediaStatus.DISABLED} or not result.articles:
        return "DATA_UNAVAILABLE: No archived Vietnamese editorial media is available."
    lines = [
        f"## Vietnam editorial media for {result.ticker}",
        f"Status: {result.status.value}; PIT quality: proxy; articles: {len(result.articles)}",
        "RSS title/summary evidence only; full article pages were not fetched.",
    ]
    for article in result.articles:
        attribution = "Theo cafef" if article.provider == "cafef_rss" else "VnExpress"
        lines.extend(
            [
                f"### {article.title}",
                f"- Source: {attribution}",
                f"- Published: {article.published_at.isoformat()}",
                f"- Link: {article.canonical_url}",
                f"- RSS summary: {article.summary}",
            ]
        )
    return "\n".join(lines)


def get_editorial_news(
    ticker: str, start_date: str, end_date: str, aliases: list[str] | None = None
) -> str:
    service = create_vietnam_media_service_from_env()
    start_cutoff = _cutoff(start_date)
    end_cutoff = _cutoff(end_date)
    result = service.load_evidence(
        ticker,
        end_date,
        lookback_days=max(1, (end_cutoff.date() - start_cutoff.date()).days),
        aliases=aliases,
    )
    return render_vietnam_media_result(result)


__all__ = [
    "MediaArticle",
    "MediaSourceResult",
    "MediaStatus",
    "VietnamMediaConfig",
    "VietnamMediaResult",
    "VietnamMediaService",
    "create_vietnam_media_service_from_env",
    "get_editorial_news",
    "render_vietnam_media_result",
]
