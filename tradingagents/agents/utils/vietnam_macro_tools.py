from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any
from urllib.parse import urlsplit, urlunsplit

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor

_MACRO_STATUSES = {"available", "partial", "unavailable", "disabled"}
_PIT_QUALITIES = {"exact", "proxy", "partial"}
_OFFICIAL_HOSTS = {"nsdp.nso.gov.vn", "nso.gov.vn", "www.nso.gov.vn", "sbv.gov.vn", "www.sbv.gov.vn"}


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _scalar(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime, Decimal)):
        return str(value)
    return value


def _status(value: Any, default: str = "unavailable") -> str:
    normalized = str(_scalar(value) or default).strip().lower()
    return normalized if normalized in _MACRO_STATUSES else default


def _safe_warning(value: Any) -> str:
    warning = str(value)
    warning = re.sub(r"(?i)(bearer\s+)\S+", r"\1<redacted>", warning)
    warning = re.sub(r"(?i)sk-[A-Za-z0-9_-]+", "<redacted>", warning)
    warning = re.sub(
        r"(?i)(password|api[_-]?key|token|secret)\s*[=:]\s*\S+",
        r"\1=<redacted>",
        warning,
    )
    return warning[:500]


def _official_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in _OFFICIAL_HOSTS or parsed.username or parsed.password:
        return None
    # Query strings and fragments can carry credentials and are not needed for
    # attribution inside an LLM prompt/session.
    return urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


def _observation_payload(observation: Any) -> dict[str, Any]:
    row = {
        "indicator_id": str(_value(observation, "indicator_id", "")),
        "value": str(_value(observation, "value", "")),
        "unit": str(_value(observation, "unit", "")),
        "unit_multiplier": int(_value(observation, "unit_multiplier", 1) or 1),
        "frequency": str(_value(observation, "frequency", "")),
        "period_start": _scalar(_value(observation, "period_start")),
        "period_end": _scalar(_value(observation, "period_end")),
        "published_at": _scalar(_value(observation, "published_at")),
        "first_seen_at": _scalar(_value(observation, "first_seen_at")),
        "retrieved_at": _scalar(_value(observation, "retrieved_at")),
        "source_provider": str(_value(observation, "source_provider", "unknown")),
        "source_series": _scalar(_value(observation, "source_series")),
        "provisional": _value(observation, "provisional"),
        "point_in_time_quality": str(
            _value(observation, "point_in_time_quality", "partial") or "partial"
        ),
        "derived_from": [
            str(item) for item in (_value(observation, "derived_from", []) or [])
        ],
        "stale": bool(_value(observation, "stale", False)),
        "warnings": [
            _safe_warning(item) for item in (_value(observation, "warnings", []) or [])
        ],
    }
    source_url = _official_url(_value(observation, "source_url"))
    if source_url:
        row["source_url"] = source_url
    return row


def _source_payload(source: Any) -> dict[str, Any]:
    provider = _value(source, "provider", _value(source, "source_provider", "unknown"))
    count = _value(
        source,
        "observation_count",
        _value(source, "sample_size", _value(source, "observations_seen", 0)),
    )
    raw_fetch_ids = _value(source, "fetch_ids", []) or []
    if isinstance(raw_fetch_ids, str):
        raw_fetch_ids = [raw_fetch_ids]
    fetch_ids = [str(item) for item in raw_fetch_ids if str(item)]
    fetch_id = str(_value(source, "fetch_id", "") or "") or None
    if fetch_id and fetch_id not in fetch_ids:
        fetch_ids.insert(0, fetch_id)
    result = {
        "provider": str(provider),
        "status": _status(_value(source, "status")),
        "fetch_id": fetch_id,
        "fetch_ids": fetch_ids,
        "observation_count": int(count or 0),
        "point_in_time_quality": str(
            _value(source, "point_in_time_quality", "partial") or "partial"
        ),
        "warnings": [
            _safe_warning(item) for item in (_value(source, "warnings", []) or [])
        ],
    }
    return result


@dataclass
class MacroEvidence:
    """Archive-only Vietnam macro evidence safe for prompts and sessions."""

    status: str
    provider: str = "vn_macro"
    block: str = "{}"
    observation_count: int = 0
    warnings: list[str] = field(default_factory=list)
    as_of: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    fetch_ids: list[str] = field(default_factory=list)
    point_in_time_quality: str = "partial"
    stale: bool = False
    stale_indicators: list[str] = field(default_factory=list)
    source_results: list[dict[str, Any]] = field(default_factory=list)
    actual_vendor_observed: bool = False

    @property
    def usable(self) -> bool:
        return self.status in {"available", "partial"} and self.observation_count > 0

    def metadata(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": self.provider,
            "actual_vendor_observed": self.actual_vendor_observed,
            "sample_size": self.observation_count,
            "observation_count": self.observation_count,
            "as_of": self.as_of,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "fetch_ids": list(self.fetch_ids),
            "point_in_time_quality": self.point_in_time_quality,
            "stale": self.stale,
            "stale_indicators": list(self.stale_indicators),
            "warnings": [_safe_warning(item) for item in self.warnings],
            "source_results": [dict(item) for item in self.source_results],
        }


def is_vietnam_macro_profile(state: dict[str, Any] | None = None) -> bool:
    """Return whether the run explicitly uses the Vietnam macro profile."""
    profile = (state or {}).get("macro_profile")
    if isinstance(profile, dict):
        if profile.get("enabled") is False:
            return False
        provider = str(profile.get("provider") or "").strip().lower()
        if provider:
            return provider == "vn_macro"
        providers = profile.get("providers")
        if isinstance(providers, str):
            providers = [item.strip() for item in providers.split(",")]
        if isinstance(providers, list):
            return any(str(item).strip().lower() not in {"", "legacy"} for item in providers)

    from tradingagents.dataflows.config import get_config

    config = get_config()
    category_vendor = str(
        (config.get("data_vendors") or {}).get("vn_macro_data", "")
    ).strip().lower()
    tool_vendor = str(
        (config.get("tool_vendors") or {}).get("get_vietnam_macro_context", "")
    ).strip().lower()
    macro_config = config.get("vn_macro") or {}
    return (
        macro_config.get("enabled", True) is not False
        and (category_vendor == "vn_macro" or tool_vendor == "vn_macro")
    )


def load_vietnam_macro_evidence(
    as_of: str,
    *,
    state: dict[str, Any] | None = None,
) -> MacroEvidence:
    """Read canonical macro evidence from SQLite; never collect network data."""
    profile = (state or {}).get("macro_profile") or {}
    if isinstance(profile, dict):
        provider = str(profile.get("provider") or "").strip().lower()
        if provider == "legacy" or profile.get("enabled") is False:
            return MacroEvidence(
                status="disabled",
                block='{"status":"disabled","observations":[]}',
                as_of=as_of,
                warnings=["Vietnam macro evidence is disabled for this run profile."],
            )
    if not is_vietnam_macro_profile(state):
        return MacroEvidence(
            status="disabled",
            block='{"status":"disabled","observations":[]}',
            as_of=as_of,
            warnings=["Vietnam macro evidence is not configured for this run."],
        )

    lookback = profile.get("lookback_months") if isinstance(profile, dict) else None
    try:
        from tradingagents.dataflows.vietnam_macro import (
            create_vietnam_macro_service_from_env,
        )

        result = create_vietnam_macro_service_from_env().load_evidence(
            as_of,
            lookback_months=lookback,
        )
    except Exception as exc:  # optional archive must not abort a news analysis
        return MacroEvidence(
            status="unavailable",
            block='{"status":"unavailable","observations":[]}',
            as_of=as_of,
            warnings=[f"Vietnam macro archive could not be loaded ({type(exc).__name__})."],
        )

    status = _status(_value(result, "status"))
    observations = [
        _observation_payload(item) for item in (_value(result, "observations", []) or [])
    ]
    if status not in {"available", "partial"}:
        # Fail closed if a malformed provider attaches stale rows to a disabled
        # or unavailable result.
        observations = []
    sources = [
        _source_payload(item) for item in (_value(result, "source_results", []) or [])
    ]
    warnings = [
        _safe_warning(item) for item in (_value(result, "warnings", []) or [])
    ]
    warnings.extend(
        warning for source in sources for warning in source.get("warnings", [])
    )
    qualities = {
        str(item.get("point_in_time_quality") or "partial") for item in observations
    }
    if not qualities:
        qualities = {
            str(item.get("point_in_time_quality") or "partial") for item in sources
        }
    if not qualities or "partial" in qualities or not qualities.issubset(_PIT_QUALITIES):
        pit_quality = "partial"
    elif "proxy" in qualities:
        pit_quality = "proxy"
    else:
        pit_quality = "exact"
    stale_indicators = sorted(
        {
            str(item["indicator_id"])
            for item in observations
            if item.get("stale") and item.get("indicator_id")
        }
    )
    if status in {"available", "partial"} and not observations:
        status = "unavailable"
        warnings.append(
            "Vietnam macro provider returned no point-in-time observations."
        )
    if stale_indicators and status == "available":
        status = "partial"
        warnings.append("At least one Vietnam macro observation is stale.")
    fetch_ids = list(
        dict.fromkeys(
            str(fetch_id)
            for source in sources
            for fetch_id in (
                source.get("fetch_ids") or [source.get("fetch_id")]
            )
            if fetch_id
        )
    )
    effective_as_of = str(_scalar(_value(result, "as_of", as_of)))
    period_starts = [
        str(item["period_start"])
        for item in observations
        if item.get("period_start")
    ]
    period_ends = [
        str(item["period_end"])
        for item in observations
        if item.get("period_end")
    ]
    prompt_payload = {
        "status": status,
        "as_of": effective_as_of,
        "observations": observations,
        "sources": sources,
        "warnings": list(dict.fromkeys(warnings)),
    }
    return MacroEvidence(
        status=status,
        block=json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True),
        observation_count=len(observations),
        warnings=list(dict.fromkeys(warnings)),
        as_of=effective_as_of,
        window_start=min(period_starts) if period_starts else None,
        window_end=max(period_ends) if period_ends else None,
        fetch_ids=fetch_ids,
        point_in_time_quality=pit_quality,
        stale=bool(stale_indicators),
        stale_indicators=stale_indicators,
        source_results=sources,
        actual_vendor_observed=bool(observations),
    )


@tool
def get_vietnam_macro_context(
    curr_date: Annotated[str, "Analysis date in yyyy-mm-dd format"],
    look_back_months: Annotated[
        int | None, "Trailing month window; omit to use 24 months"
    ] = None,
) -> str:
    """Retrieve point-in-time Vietnam macro context from the local archive."""
    return route_to_vendor(
        "get_vietnam_macro_context",
        curr_date,
        look_back_months,
    )
