from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from tradingagents.agents.utils.analysis_context import effective_tool_cutoff
from tradingagents.dataflows.interface import route_to_vendor


@dataclass
class EditorialEvidence:
    """Archive-only editorial evidence safe to pass between analyst layers."""

    status: str
    provider: str
    block: str
    sample_size: int = 0
    warnings: list[str] = field(default_factory=list)
    window_start: str | None = None
    window_end: str | None = None
    point_in_time_quality: str = "partial"
    fetch_id: str | None = None
    actual_vendor_observed: bool = True
    sources: list[dict[str, Any]] = field(default_factory=list)
    sensitive_values: list[str] = field(default_factory=list, repr=False)

    @property
    def usable(self) -> bool:
        return self.status in {"available", "partial"}

    def metadata(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": self.provider,
            "actual_vendor_observed": self.actual_vendor_observed,
            "sample_size": self.sample_size,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "fetch_id": self.fetch_id,
            "point_in_time_quality": self.point_in_time_quality,
            "warnings": [_safe_warning(item) for item in self.warnings],
            "sources": [dict(item) for item in self.sources],
        }


def _safe_warning(value: Any) -> str:
    warning = str(value)
    warning = re.sub(r"(?i)(bearer\s+)\S+", r"\1<redacted>", warning)
    warning = re.sub(r"(?i)sk-[A-Za-z0-9_-]+", "<redacted>", warning)
    return warning[:500]


def _status_value(value: Any, default: str = "unavailable") -> str:
    raw = getattr(value, "value", value)
    normalized = str(raw or default).strip().lower()
    return normalized if normalized in {"available", "partial", "unavailable", "disabled"} else default


def _is_local_llm() -> bool:
    from tradingagents.dataflows.config import get_config
    from tradingagents.llm_clients.profiles import (
        is_local_llm_profile,
        resolve_llm_profile,
    )

    try:
        profile = resolve_llm_profile(get_config(), "quick")
    except ValueError:
        return False
    return is_local_llm_profile(profile)


def _hosted_media_authorized(provider: str) -> bool:
    if _is_local_llm():
        return True
    env_name = {
        "cafef_rss": "TRADINGAGENTS_CAFEF_HOSTED_LLM_AUTHORIZED",
        "vnexpress_rss": "TRADINGAGENTS_VNEXPRESS_HOSTED_LLM_AUTHORIZED",
    }.get(provider.lower())
    return bool(env_name) and os.environ.get(env_name, "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def is_vietnam_media_profile(state: dict[str, Any] | None = None) -> bool:
    profile = (state or {}).get("media_profile")
    if isinstance(profile, dict):
        if str(profile.get("provider") or "").strip().lower() == "legacy":
            return False
        providers = profile.get("providers")
        if isinstance(providers, list):
            return bool(providers) and providers != ["legacy"]
        if isinstance(providers, str):
            return bool(providers.strip()) and providers.strip().lower() != "legacy"
    from tradingagents.dataflows.config import get_config

    config = get_config()
    return str((config.get("tool_vendors") or {}).get("get_editorial_news", "")).strip().lower() == "vn_media"


def _article_value(article: Any, name: str, default: Any = None) -> Any:
    if isinstance(article, dict):
        return article.get(name, default)
    return getattr(article, name, default)


def _source_metadata(source: Any, *, status: str, articles: list[Any], warnings: list[str]) -> dict[str, Any]:
    return {
        "provider": str(_article_value(source, "provider", "unknown")),
        "status": status,
        "sample_size": len(articles),
        "fetch_id": str(_article_value(source, "fetch_id", "") or "") or None,
        "point_in_time_quality": str(_article_value(source, "point_in_time_quality", "partial") or "partial"),
        "warnings": [_safe_warning(item) for item in warnings],
    }


def load_vietnam_editorial_evidence(
    ticker: str,
    as_of: str,
    *,
    state: dict[str, Any] | None = None,
    include_market_context: bool = False,
) -> EditorialEvidence:
    """Load RSS evidence from SQLite only; never collect network data here."""
    profile = (state or {}).get("media_profile") or {}
    lookback = profile.get("lookback_days") if isinstance(profile, dict) else None

    try:
        from tradingagents.dataflows.vietnam_media import (
            create_vietnam_media_service_from_env,
        )

        result = create_vietnam_media_service_from_env().load_evidence(
            ticker,
            as_of,
            lookback_days=lookback,
            include_market_context=include_market_context,
        )
    except Exception as exc:  # optional/locked archive is typed evidence state
        return EditorialEvidence(
            status="unavailable",
            provider="vn_media",
            block="<Vietnam editorial media archive unavailable>",
            warnings=[f"Vietnam media archive could not be loaded ({type(exc).__name__})."],
            window_end=as_of,
            actual_vendor_observed=False,
        )

    allowed_articles: list[Any] = []
    sources_meta: list[dict[str, Any]] = []
    enabled_statuses: list[str] = []
    aggregate_warnings = [_safe_warning(item) for item in (_article_value(result, "warnings", []) or [])]
    for source in _article_value(result, "sources", []) or []:
        provider = str(_article_value(source, "provider", "unknown"))
        source_status = _status_value(_article_value(source, "status"))
        source_articles = list(_article_value(source, "articles", []) or [])
        source_warnings = [_safe_warning(item) for item in (_article_value(source, "warnings", []) or [])]
        if source_status == "disabled":
            # Fail closed even if a malformed provider result attaches stale
            # articles to an authorization-locked source.
            source_articles = []
        elif not _hosted_media_authorized(provider):
            source_status = "unavailable"
            source_articles = []
            source_warnings.append(
                f"{provider} RSS content was withheld because hosted-LLM use is not authorized."
            )
        if source_status != "disabled":
            enabled_statuses.append(source_status)
        allowed_articles.extend(source_articles)
        sources_meta.append(
            _source_metadata(source, status=source_status, articles=source_articles, warnings=source_warnings)
        )

    # A provider may return articles only on the aggregate result. Apply the
    # same per-source policy rather than accidentally bypassing hosted gates.
    if not (_article_value(result, "sources", []) or []):
        for article in _article_value(result, "articles", []) or []:
            if _hosted_media_authorized(str(_article_value(article, "provider", ""))):
                allowed_articles.append(article)

    min_articles = int(profile.get("min_articles", 3) or 3) if isinstance(profile, dict) else 3
    if not allowed_articles:
        status = "unavailable"
    elif len(allowed_articles) < min_articles:
        status = "partial"
        aggregate_warnings.append(
            f"Editorial media has {len(allowed_articles)} eligible article(s), below minimum {min_articles}."
        )
    elif enabled_statuses and all(value == "available" for value in enabled_statuses):
        status = "available"
    else:
        status = "partial"
    if status == "partial" and not aggregate_warnings and not any(
        source.get("warnings") for source in sources_meta
    ):
        aggregate_warnings.append(
            "Editorial media coverage is incomplete for at least one enabled source."
        )

    rows: list[dict[str, Any]] = []
    sensitive_values: list[str] = []
    for article in allowed_articles:
        title = re.sub(r"\s+", " ", str(_article_value(article, "title", ""))).strip()
        summary = re.sub(r"\s+", " ", str(_article_value(article, "summary", ""))).strip()
        url = str(_article_value(article, "canonical_url", "") or "")
        if title:
            sensitive_values.append(title)
        if summary:
            sensitive_values.append(summary)
        rows.append({
            "provider": str(_article_value(article, "provider", "unknown")),
            "published_at": str(_article_value(article, "published_at", "")),
            "category": str(_article_value(article, "category", "")),
            "title": title,
            "summary": summary,
            "source_url": url,
        })

    providers = sorted({str(row["provider"]) for row in rows})
    window_start = str(_article_value(result, "window_start", "") or "") or None
    window_end = str(_article_value(result, "window_end", "") or as_of)
    qualities = [str(item.get("point_in_time_quality") or "partial") for item in sources_meta]
    pit = "partial" if not sources_meta or "partial" in qualities else "proxy"
    return EditorialEvidence(
        status=status,
        provider=",".join(providers) if providers else "vn_media",
        block=json.dumps(rows, ensure_ascii=False, sort_keys=True),
        sample_size=len(rows),
        warnings=list(dict.fromkeys(aggregate_warnings + [w for s in sources_meta for w in s["warnings"]])),
        window_start=window_start,
        window_end=window_end,
        point_in_time_quality=pit,
        fetch_id=next((str(s["fetch_id"]) for s in sources_meta if s.get("fetch_id")), None),
        sources=sources_meta,
        sensitive_values=sensitive_values,
        actual_vendor_observed=bool(sources_meta),
    )


def redact_media_excerpts(text: str, sensitive_values: list[str]) -> str:
    """Prevent RSS title/summary text from being persisted verbatim."""
    redacted = str(text or "")
    for value in sorted(set(sensitive_values), key=len, reverse=True):
        normalized = re.sub(r"\s+", " ", value).strip()
        if len(normalized) < 12:
            continue
        redacted = re.sub(re.escape(normalized), "[redacted media excerpt]", redacted, flags=re.I)
        words = normalized.split()
        for start in range(max(0, len(words) - 5)):
            fragment = r"\s+".join(re.escape(word) for word in words[start : start + 6])
            redacted = re.sub(fragment, "[redacted media excerpt]", redacted, flags=re.I)
    return redacted


@tool
def get_news(
    ticker: Annotated[str, "Ticker symbol"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    state: Annotated[dict[str, Any] | None, InjectedState] = None,
) -> str:
    """
    Retrieve news data for a given ticker symbol.
    Uses the configured news_data vendor.
    Args:
        ticker (str): Ticker symbol
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted string containing news data
    """
    cutoff = effective_tool_cutoff(state, end_date) or end_date
    return route_to_vendor("get_news", ticker, start_date, cutoff)


@tool
def get_editorial_news(
    ticker: Annotated[str, "Ticker symbol"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    state: Annotated[dict[str, Any] | None, InjectedState] = None,
) -> str:
    """Retrieve Vietnam editorial RSS evidence from its local archive."""
    cutoff = effective_tool_cutoff(state, end_date) or end_date
    return route_to_vendor("get_editorial_news", ticker, start_date, cutoff)


@tool
def get_disclosures(
    ticker: Annotated[str, "Ticker symbol"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    state: Annotated[dict[str, Any] | None, InjectedState] = None,
) -> str:
    """Retrieve official exchange/company disclosures for a ticker."""
    cutoff = effective_tool_cutoff(state, end_date) or end_date
    return route_to_vendor("get_disclosures", ticker, start_date, cutoff)

@tool
def get_global_news(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int | None, "Days to look back; omit to use the configured default"] = None,
    limit: Annotated[int | None, "Max articles to return; omit to use the configured default"] = None,
    state: Annotated[dict[str, Any] | None, InjectedState] = None,
) -> str:
    """
    Retrieve global news data.
    Uses the configured news_data vendor. Defaults for look_back_days and
    limit come from DEFAULT_CONFIG (global_news_lookback_days,
    global_news_article_limit); pass explicit values to override.

    Args:
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Number of days to look back; omit to inherit config
        limit (int): Maximum number of articles to return; omit to inherit config

    Returns:
        str: A formatted string containing global news data
    """
    cutoff = effective_tool_cutoff(state, curr_date) or curr_date
    return route_to_vendor("get_global_news", cutoff, look_back_days, limit)

@tool
def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol"],
) -> str:
    """
    Retrieve insider transaction information about a company.
    Uses the configured news_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
    Returns:
        str: A report of insider transaction data
    """
    return route_to_vendor("get_insider_transactions", ticker)
