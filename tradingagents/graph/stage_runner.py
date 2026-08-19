"""Run TradingAgents as durable, independently resumable stages."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from tradingagents.agents import (
    create_aggressive_debator,
    create_bear_researcher,
    create_bull_researcher,
    create_conservative_debator,
    create_fundamentals_analyst,
    create_market_analyst,
    create_neutral_debator,
    create_news_analyst,
    create_portfolio_manager,
    create_research_manager,
    create_sentiment_analyst,
    create_trader,
)
from tradingagents.agents.utils.agent_states import InvestDebateState, RiskDebateState
from tradingagents.agents.utils.agent_utils import build_instrument_context
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorNotConfiguredError
from tradingagents.dataflows.market_data_validator import (
    build_verified_market_snapshot,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
from tradingagents.llm_clients.profiles import resolve_llm_profile

from .stage_session import ANALYST_STAGES, PIPELINE_STAGES, StageSession

_ANALYST_WIRE_KEYS = {
    "market": "market",
    "sentiment": "social",
    "news": "news",
    "fundamentals": "fundamentals",
}
_REPORT_KEYS = {
    "market": "market_report",
    "sentiment": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}
_STAGE_DATA_TOOLS = {
    "market": ("get_stock_data", "get_indicators"),
    "sentiment": ("get_news", "get_social_data"),
    "news": (
        "get_news",
        "get_global_news",
        "get_insider_transactions",
        "get_macro_indicators",
        "get_prediction_markets",
    ),
    "fundamentals": (
        "get_fundamentals",
        "get_balance_sheet",
        "get_cashflow",
        "get_income_statement",
    ),
}
_TOOL_CATEGORY = {
    "get_stock_data": "core_stock_apis",
    "get_indicators": "technical_indicators",
    "get_fundamentals": "fundamental_data",
    "get_balance_sheet": "fundamental_data",
    "get_cashflow": "fundamental_data",
    "get_income_statement": "fundamental_data",
    "get_news": "news_data",
    "get_disclosures": "news_data",
    "get_editorial_news": "news_data",
    "get_social_data": "social_data",
    "get_global_news": "news_data",
    "get_insider_transactions": "news_data",
    "get_macro_indicators": "macro_data",
    "get_vietnam_macro_context": "vn_macro_data",
    "get_prediction_markets": "prediction_markets",
}


class StagePrerequisiteError(ValueError):
    """Raised when a stage is requested before its required inputs exist."""


def llm_identity(config: dict[str, Any]) -> dict[str, Any]:
    """Non-secret LLM identity persisted with a run."""
    quick = resolve_llm_profile(config, "quick")
    deep = resolve_llm_profile(config, "deep")
    quick_identity = quick.public_identity()
    deep_identity = deep.public_identity()
    return {
        "quick": {
            **quick_identity,
            "base_url": _public_endpoint(quick_identity["base_url"]),
        },
        "deep": {
            **deep_identity,
            "base_url": _public_endpoint(deep_identity["base_url"]),
        },
        # These settings alter stage output and therefore belong to the durable
        # execution profile.  Locking them prevents a resumed debate from
        # silently mixing languages, reasoning modes, or round counts.
        "output_language": config.get("output_language"),
        "temperature": config.get("temperature"),
        "max_debate_rounds": config.get("max_debate_rounds"),
        "max_risk_discuss_rounds": config.get("max_risk_discuss_rounds"),
        "openai_reasoning_effort": config.get("openai_reasoning_effort"),
        "google_thinking_level": config.get("google_thinking_level"),
        "anthropic_effort": config.get("anthropic_effort"),
    }


def data_transport_identity(config: dict[str, Any]) -> dict[str, Any]:
    """Non-secret GX transport identity persisted with a run."""
    # Match ``GxMarketInfoClient.from_config``: an explicit/programmatically
    # supplied config is authoritative, while environment variables remain a
    # fallback for minimal configs.  The GX CLI reapplies environment overrides
    # before constructing the runner, so both entry paths produce the same run
    # identity as the adapter actually uses.
    settings = config.get("gx_market_info") or {}
    transport = str(
        settings.get("transport") or os.environ.get("GX_DATA_TRANSPORT", "api")
    ).lower()
    identity = {"transport": transport}
    if transport == "api":
        identity.update(
            {
                "base_url": _public_endpoint(
                    settings.get("base_url")
                    or os.environ.get(
                        "GX_MARKET_INFO_BASE_URL", "http://127.0.0.1:5005"
                    )
                ),
                "api_version": settings.get("api_version")
                or os.environ.get("GX_MARKET_INFO_API_VERSION", "v1.0.7"),
            }
        )
    elif transport == "postgres":
        identity["expected_database"] = settings.get(
            "expected_database"
        ) or os.environ.get("GX_MARKET_INFO_EXPECTED_DB", "g_market_info_1229")
    else:
        raise ValueError("GX_DATA_TRANSPORT must be 'api' or 'postgres'")
    return identity


def social_profile_identity(config: dict[str, Any]) -> dict[str, Any]:
    """Return the non-secret immutable social policy for a durable run."""
    settings = config.get("vn_social") or {}
    provider = str(settings.get("provider") or "legacy").strip().lower()
    if provider == "legacy":
        return {"provider": "legacy"}
    from pathlib import Path

    from tradingagents.dataflows.vietnam_social_archive import ARCHIVE_SCHEMA_VERSION

    archive_path = str(
        settings.get("archive_path")
        or os.environ.get("TRADINGAGENTS_SOCIAL_ARCHIVE_PATH")
        or "~/.tradingagents/cache/social/vn_social.sqlite3"
    )
    archive_id = hashlib.sha256(
        os.path.abspath(os.path.expanduser(archive_path)).encode()
    ).hexdigest()[:16]
    expanded = Path(archive_path).expanduser()
    if expanded.exists():
        try:
            import sqlite3

            uri = f"file:{expanded.resolve()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
                row = connection.execute(
                    "SELECT value FROM archive_meta WHERE key='archive_id'"
                ).fetchone()
            if row and row[0]:
                archive_id = str(row[0])
        except (OSError, sqlite3.Error):
            # An unavailable archive is diagnosed by doctor/provider. Identity
            # remains deterministic without opening it read-write here.
            pass
    return {
        "provider": provider,
        "lookback_days": int(settings.get("lookback_days", 7)),
        "min_posts": int(settings.get("min_posts", 10)),
        "min_unique_authors": int(settings.get("min_unique_authors", 5)),
        "archive_id": archive_id,
        "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
        "prompt_version": str(settings.get("prompt_version") or "vn-social-v1"),
        "legacy_sources_enabled": bool(
            settings.get("legacy_sources_enabled", False)
        ),
    }


def _runtime_social_profile_identity(config: dict[str, Any]) -> dict[str, Any]:
    """Prefer the archive UUID exposed by the live service when it is readable."""
    identity = social_profile_identity(config)
    if identity.get("provider") != "fireant":
        return identity
    try:
        from tradingagents.dataflows.vietnam_social import (
            create_vietnam_social_service_from_env,
        )

        service = create_vietnam_social_service_from_env()
        if service.archive is not None and service.archive.archive_id:
            identity["archive_id"] = str(service.archive.archive_id)
    except Exception:  # noqa: BLE001 - provider/doctor surfaces archive diagnostics
        pass
    return identity


def _configured_media_providers(settings: dict[str, Any]) -> list[str]:
    raw = settings.get("providers") or ""
    values = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return list(
        dict.fromkeys(
            str(item).strip().lower() for item in values if str(item).strip()
        )
    )


def media_profile_identity(config: dict[str, Any]) -> dict[str, Any]:
    """Return the allowlisted, non-secret immutable editorial-media profile."""
    settings = config.get("vn_media") or {}
    providers = _configured_media_providers(settings)
    if not providers:
        return {"provider": "legacy"}

    archive_path = str(
        settings.get("archive_path")
        or os.environ.get("TRADINGAGENTS_VN_MEDIA_ARCHIVE_PATH")
        or "~/.tradingagents/cache/media/vn_media.sqlite3"
    )
    archive_id = hashlib.sha256(
        os.path.abspath(os.path.expanduser(archive_path)).encode()
    ).hexdigest()[:16]
    expanded = os.path.abspath(os.path.expanduser(archive_path))
    if os.path.exists(expanded):
        try:
            import sqlite3

            uri = f"file:{expanded}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
                row = connection.execute(
                    "SELECT archive_id FROM archive_meta WHERE singleton=1"
                ).fetchone()
            if row and row[0]:
                archive_id = str(row[0])
        except (OSError, sqlite3.Error):
            # Provider/doctor reports unreadable/corrupt archives. Session
            # identity remains deterministic without opening it read-write.
            pass

    archive_schema_version = int(settings.get("archive_schema_version", 1))
    try:
        from tradingagents.dataflows.vietnam_media_archive import (
            ARCHIVE_SCHEMA_VERSION,
        )
    except (ImportError, AttributeError):
        pass
    else:
        archive_schema_version = int(ARCHIVE_SCHEMA_VERSION)

    return {
        "providers": providers,
        "lookback_days": int(settings.get("lookback_days", 7)),
        "min_articles": int(settings.get("min_articles", 3)),
        "archive_id": archive_id,
        "archive_schema_version": archive_schema_version,
        "alias_policy_version": str(
            settings.get("alias_policy_version") or "vn-media-alias-v2"
        ),
        "prompt_version": str(settings.get("prompt_version") or "vn-media-v1"),
    }


def media_profile_fingerprint(profile: dict[str, Any]) -> str:
    """Stable public identity used to bind reports and social snapshots."""
    encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _runtime_media_profile_identity(config: dict[str, Any]) -> dict[str, Any]:
    """Prefer the archive UUID exposed by the live media service when readable."""
    identity = media_profile_identity(config)
    if identity.get("provider") == "legacy":
        return identity
    try:
        import importlib

        module = importlib.import_module("tradingagents.dataflows.vietnam_media")
        service = module.create_vietnam_media_service_from_env()
        archive = getattr(service, "archive", None)
        if archive is not None and getattr(archive, "archive_id", None):
            identity["archive_id"] = str(archive.archive_id)
    except Exception:  # noqa: BLE001 - provider/doctor surfaces diagnostics
        pass
    return identity


def _configured_macro_providers(settings: dict[str, Any]) -> list[str]:
    raw = settings.get("providers") or ""
    values = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return list(
        dict.fromkeys(
            str(item).strip().lower() for item in values if str(item).strip()
        )
    )


def _vn_macro_enabled(config: dict[str, Any]) -> bool:
    settings = config.get("vn_macro") or {}
    return bool(settings.get("enabled", False)) and bool(
        _configured_macro_providers(settings)
    )


def macro_profile_identity(config: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable, non-secret identity of Vietnam macro evidence."""
    settings = config.get("vn_macro") or {}
    if not _vn_macro_enabled(config):
        return {"provider": "legacy"}
    providers = _configured_macro_providers(settings)
    supported = {"nso_sdmx", "nso_release", "sbv_html"}
    unknown = sorted(set(providers) - supported)
    if unknown:
        raise ValueError(
            "unsupported Vietnam macro provider(s): " + ", ".join(unknown)
        )
    lookback_months = int(settings.get("lookback_months", 24))
    if not 1 <= lookback_months <= 120:
        raise ValueError("vn_macro lookback_months must be between 1 and 120")
    if not bool(settings.get("strict_point_in_time", True)):
        raise ValueError("vn_macro requires strict_point_in_time=true")

    archive_path = str(
        settings.get("archive_path")
        or os.environ.get("TRADINGAGENTS_VN_MACRO_ARCHIVE_PATH")
        or "~/.tradingagents/cache/macro/vn_macro.sqlite3"
    )
    expanded = os.path.abspath(os.path.expanduser(archive_path))
    archive_id = hashlib.sha256(expanded.encode()).hexdigest()[:16]
    if os.path.exists(expanded):
        try:
            import sqlite3

            uri = f"file:{expanded}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
                row = None
                # The macro archive uses key/value metadata. Retain the second
                # query for early fixtures and inspection-only compatibility.
                try:
                    row = connection.execute(
                        "SELECT value FROM archive_meta WHERE key='archive_id'"
                    ).fetchone()
                except sqlite3.Error:
                    row = connection.execute(
                        "SELECT archive_id FROM archive_meta WHERE singleton=1"
                    ).fetchone()
            if row and row[0]:
                archive_id = str(row[0])
        except (OSError, sqlite3.Error):
            # The service/doctor owns archive diagnostics. Identity derivation
            # is deliberately read-only and remains deterministic on failure.
            pass

    archive_schema_version = int(settings.get("archive_schema_version", 1))
    try:
        from tradingagents.dataflows.vietnam_macro_archive import (
            ARCHIVE_SCHEMA_VERSION,
        )
    except (ImportError, AttributeError):
        pass
    else:
        archive_schema_version = int(ARCHIVE_SCHEMA_VERSION)

    return {
        "provider": "vn_macro",
        "providers": providers,
        "lookback_months": lookback_months,
        "indicator_set_version": str(
            settings.get("indicator_set_version") or "vn-macro-v1"
        ),
        "archive_id": archive_id,
        "archive_schema_version": archive_schema_version,
        "strict_point_in_time": bool(
            settings.get("strict_point_in_time", True)
        ),
        "prompt_version": str(settings.get("prompt_version") or "vn-macro-v1"),
    }


def macro_profile_fingerprint(profile: dict[str, Any]) -> str:
    """Stable public fingerprint for News evidence and graph checkpoints."""
    encoded = json.dumps(profile, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _runtime_macro_profile_identity(config: dict[str, Any]) -> dict[str, Any]:
    """Prefer the UUID exposed by the live archive without persisting its path."""
    identity = macro_profile_identity(config)
    if identity.get("provider") != "vn_macro":
        return identity
    try:
        from tradingagents.dataflows.vietnam_macro import (
            create_vietnam_macro_service_from_env,
        )
    except ImportError:
        return identity
    # Archive permission/schema errors must fail before a session locks an
    # identity that cannot actually be reproduced on resume.
    service = create_vietnam_macro_service_from_env()
    if getattr(service, "archive_id", None):
        identity["archive_id"] = str(service.archive_id)
    return identity


def _public_endpoint(value: Any) -> str | None:
    """Remove URL credentials/query fragments before writing session metadata."""
    if not value:
        return None
    parsed = urlsplit(str(value))
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


class TradingAgentsStageRunner:
    """Execute analysts, research, trader and risk stages against a session.

    Analyst stages reuse the existing agent factory plus its existing ToolNode in
    a small tool-call loop.  Downstream stages reuse the same node factories and
    debate ordering as the full graph.  A graph factory may be injected for tests.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        graph_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = deepcopy(config or DEFAULT_CONFIG)
        set_config(self.config)
        if graph_factory is None:
            from .trading_graph import TradingAgentsGraph

            graph_factory = TradingAgentsGraph
        self._graph_factory = graph_factory
        self._graphs: dict[tuple[str, ...], Any] = {}

    def create_session(
        self,
        ticker: str,
        analysis_date: str,
        *,
        selected_analysts: tuple[str, ...] = ANALYST_STAGES,
        asset_type: str = "stock",
        analysis_mode: str = "close",
        analysis_cutoff: datetime | str | None = None,
        run_id: str | None = None,
    ) -> StageSession:
        session = StageSession.create(
            ticker=ticker,
            analysis_date=analysis_date,
            selected_analysts=selected_analysts,
            asset_type=asset_type,
            analysis_mode=analysis_mode,
            analysis_cutoff=analysis_cutoff,
            run_id=run_id,
            llm=llm_identity(self.config),
            data_transport=data_transport_identity(self.config),
            social_profile=_runtime_social_profile_identity(self.config),
            media_profile=_runtime_media_profile_identity(self.config),
            macro_profile=_runtime_macro_profile_identity(self.config),
        )
        graph = self._graph(tuple(_ANALYST_WIRE_KEYS[a] for a in selected_analysts))
        memory = getattr(graph, "memory_log", None)
        past_context = self._past_context_as_of(memory, ticker, analysis_date)
        state = graph.propagator.create_initial_state(
            ticker,
            analysis_date,
            asset_type=asset_type,
            past_context=past_context,
            instrument_context=self._instrument_context(graph, ticker, asset_type),
        )
        session.state = self._json_state(state)
        session.state.pop("analysis_mode", None)
        session.state.pop("analysis_cutoff", None)
        return session

    def run_stage(self, session: StageSession, stage: str) -> StageSession:
        return self.run_stage_to(session, stage)

    def run_stage_to(
        self,
        session: StageSession,
        stage: str,
        *,
        session_path: str | None = None,
    ) -> StageSession:
        normalized = stage.lower()
        if normalized == "social":
            normalized = "sentiment"
        if normalized not in PIPELINE_STAGES:
            raise ValueError(f"unknown stage: {stage}")
        self._assert_runtime_identity(session)
        self._require(session, normalized)
        state = self._hydrate_state(session.state, session.stage_status)
        # Keep the immutable profile authoritative at execution time without
        # duplicating it inside the persisted mutable state payload.
        state["social_profile"] = deepcopy(session.social_profile)
        state["media_profile"] = deepcopy(session.media_profile)
        state["media_profile_fingerprint"] = media_profile_fingerprint(
            session.media_profile
        )
        state["macro_profile"] = deepcopy(session.macro_profile)
        state["macro_profile_fingerprint"] = macro_profile_fingerprint(
            session.macro_profile
        )
        # These values come exclusively from immutable session identity. They
        # are execution-only state, so agents/tools cannot move the PIT cutoff
        # and the mutable JSON payload never stores a second copy.
        state["analysis_mode"] = session.analysis_mode
        state["analysis_cutoff"] = session.analysis_cutoff

        try:
            if normalized in ANALYST_STAGES:
                update = self._run_analyst(session, state, normalized)
            elif normalized == "research":
                update = self._run_research(session, state)
            elif normalized == "trader":
                update = self._run_trader(session, state)
            else:
                update = self._run_risk(session, state)
        except Exception as exc:
            session.record_failure(
                normalized,
                self._safe_error(exc),
                unavailable=self._is_unavailable(exc),
            )
            session.save(session_path)
            raise

        serialized_update = self._json_state(update)
        serialized_update.pop("analysis_mode", None)
        serialized_update.pop("analysis_cutoff", None)
        if normalized == "sentiment" and "sentiment_source_metadata" in serialized_update:
            serialized_update["sentiment_source_metadata"] = (
                self._public_sentiment_metadata(
                    serialized_update["sentiment_source_metadata"]
                )
            )
        if normalized == "news" and "news_source_metadata" in serialized_update:
            serialized_update["news_source_metadata"] = self._public_news_metadata(
                serialized_update["news_source_metadata"]
            )
        result_status = self._stage_result_status(normalized, serialized_update)
        session.complete(
            normalized,
            serialized_update,
            sources=self._stage_sources(
                session, normalized, state_update=serialized_update
            ),
            warnings=self._stage_warnings(
                session, normalized, state_update=serialized_update
            ),
            status=result_status,
        )
        session.save(session_path)
        if normalized == "risk":
            self._store_final_decision(session)
        return session

    def run_default(self, session: StageSession) -> StageSession:
        """Run selected analysts followed by research, trader and risk."""
        for analyst in session.selected_analysts:
            self.run_stage(session, analyst)
        for stage in ("research", "trader", "risk"):
            self.run_stage(session, stage)
        return session

    def run_default_to(
        self, session: StageSession, *, session_path: str | None = None
    ) -> StageSession:
        """Run the default path while preserving an explicitly supplied file."""
        for analyst in session.selected_analysts:
            self.run_stage_to(session, analyst, session_path=session_path)
        for stage in ("research", "trader", "risk"):
            self.run_stage_to(session, stage, session_path=session_path)
        return session

    def _assert_runtime_identity(self, session: StageSession) -> None:
        session.assert_identity(
            ticker=session.ticker,
            analysis_date=session.analysis_date,
            asset_type=session.asset_type,
            selected_analysts=session.selected_analysts,
            llm=llm_identity(self.config),
            data_transport=data_transport_identity(self.config),
            analysis_mode=session.analysis_mode,
            analysis_cutoff=session.analysis_cutoff,
            social_profile=_runtime_social_profile_identity(self.config),
            media_profile=_runtime_media_profile_identity(self.config),
            macro_profile=_runtime_macro_profile_identity(self.config),
        )

    def _require(self, session: StageSession, stage: str) -> None:
        if stage in ANALYST_STAGES:
            if stage not in session.selected_analysts:
                raise StagePrerequisiteError(
                    f"stage '{stage}' was not selected for run {session.run_id}"
                )
            return
        requirements = {
            "research": (),
            "trader": ("research",),
            "risk": ("trader",),
        }[stage]
        if stage == "research":
            available = [
                analyst
                for analyst in session.selected_analysts
                if analyst in session.completed_stages
                and bool(session.state.get(_REPORT_KEYS[analyst]))
            ]
            if not available:
                raise StagePrerequisiteError(
                    "stage 'research' requires at least one completed, non-empty analyst report"
                )
            return
        missing = [item for item in requirements if item not in session.completed_stages]
        if missing:
            raise StagePrerequisiteError(
                f"stage '{stage}' requires completed stage(s): {', '.join(missing)}"
            )

    def _graph(self, analysts: tuple[str, ...]) -> Any:
        if analysts not in self._graphs:
            self._graphs[analysts] = self._graph_factory(
                selected_analysts=analysts,
                config=deepcopy(self.config),
                debug=False,
            )
        return self._graphs[analysts]

    @staticmethod
    def _instrument_context(graph: Any, ticker: str, asset_type: str) -> str:
        resolver = getattr(graph, "resolve_instrument_context", None)
        if resolver is not None:
            return resolver(ticker, asset_type)
        return build_instrument_context(ticker, asset_type)

    @staticmethod
    def _past_context_as_of(memory: Any, ticker: str, analysis_date: str) -> str:
        """Build memory context using only resolved entries before analysis_date.

        ``TradingMemoryLog.get_past_context`` currently has no point-in-time
        argument.  Filtering its parsed entries here keeps a historical GX run
        from consuming decisions/reflections that happened in its future.
        Unknown memory implementations fail closed instead of loading an
        unbounded context.
        """
        if memory is None or not hasattr(memory, "load_entries"):
            return ""
        try:
            cutoff = date.fromisoformat(analysis_date)
            entries = [
                entry
                for entry in memory.load_entries()
                if not entry.get("pending")
                and date.fromisoformat(str(entry.get("date", ""))) < cutoff
            ]
        except (OSError, TypeError, ValueError):
            return ""

        same: list[dict[str, Any]] = []
        cross: list[dict[str, Any]] = []
        for entry in reversed(entries):
            if len(same) >= 5 and len(cross) >= 3:
                break
            if entry.get("ticker") == ticker and len(same) < 5:
                same.append(entry)
            elif entry.get("ticker") != ticker and len(cross) < 3:
                cross.append(entry)

        parts: list[str] = []
        if same:
            parts.append(f"Past analyses of {ticker} (most recent first):")
            parts.extend(TradingAgentsStageRunner._format_memory_entry(item) for item in same)
        if cross:
            parts.append("Recent cross-ticker lessons:")
            parts.extend(
                TradingAgentsStageRunner._format_memory_entry(item, reflection_only=True)
                for item in cross
            )
        return "\n\n".join(parts)

    @staticmethod
    def _format_memory_entry(entry: dict[str, Any], *, reflection_only: bool = False) -> str:
        tag = (
            f"[{entry.get('date', '')} | {entry.get('ticker', '')} | "
            f"{entry.get('rating', '')} | {entry.get('raw') or 'n/a'}"
        )
        if reflection_only:
            reflection = str(entry.get("reflection") or "")
            if reflection:
                return f"{tag}]\n{reflection}"
            decision = str(entry.get("decision") or "")
            suffix = "..." if len(decision) > 300 else ""
            return f"{tag}]\n{decision[:300]}{suffix}"
        tag += f" | {entry.get('alpha') or 'n/a'} | {entry.get('holding') or 'n/a'}]"
        sections = [tag, f"DECISION:\n{entry.get('decision') or ''}"]
        if entry.get("reflection"):
            sections.append(f"REFLECTION:\n{entry['reflection']}")
        return "\n\n".join(sections)

    def _stage_sources(
        self,
        session: StageSession,
        stage: str,
        *,
        state_update: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if stage in ANALYST_STAGES:
            tool_vendors = self.config.get("tool_vendors") or {}
            data_vendors = self.config.get("data_vendors") or {}
            sources: list[dict[str, Any]] = []
            for tool in self._stage_data_tools(stage):
                category = _TOOL_CATEGORY[tool]
                vendors = str(
                    tool_vendors.get(tool)
                    or data_vendors.get(category)
                    or "default"
                )
                source: dict[str, Any] = {
                    "kind": "configured_tool_source",
                    "tool": tool,
                    "category": category,
                    "vendor_chain": [
                        item.strip() for item in vendors.split(",") if item.strip()
                    ],
                    "ticker": session.ticker,
                    "analysis_date": session.analysis_date,
                    # The current tool loop does not expose per-call result
                    # metadata. Be explicit that this is configured intent,
                    # not an assertion that a fallback vendor was actually used.
                    "actual_vendor_observed": False,
                }
                if "gx_market_info" in source["vendor_chain"]:
                    source["gx_profile"] = {
                        "transport": session.data_transport.get("transport"),
                        "strict_point_in_time": bool(
                            (self.config.get("gx_market_info") or {}).get(
                                "strict_point_in_time", False
                            )
                        ),
                    }
                sources.append(source)
            if stage == "sentiment":
                observed = self._public_sentiment_metadata(
                    (state_update or {}).get("sentiment_source_metadata")
                )
                if observed:
                    lane_observed = any(
                        bool((observed.get(name) or {}).get("actual_vendor_observed"))
                        for name in ("retail_social_signal", "media_tone")
                    )
                    sources.append(
                        {
                            "kind": "observed_sentiment_sources",
                            "actual_vendor_observed": lane_observed,
                            **observed,
                        }
                    )
            if stage == "news":
                observed_news = self._public_news_metadata(
                    (state_update or {}).get("news_source_metadata")
                )
                if observed_news:
                    sources.append(
                        {
                            "kind": "observed_news_sources",
                            "actual_vendor_observed": any(
                                bool((observed_news.get(name) or {}).get(
                                    "actual_vendor_observed"
                                ))
                                for name in (
                                    "official_disclosures",
                                    "editorial_media",
                                    "vn_macro",
                                )
                            ),
                            **observed_news,
                        }
                    )
            return sources
        upstream = {
            "research": list(session.selected_analysts),
            "trader": ["research"],
            "risk": ["research", "trader"],
        }[stage]
        return [{"kind": "session_reports", "input_stages": upstream}]

    def _stage_data_tools(self, stage: str) -> tuple[str, ...]:
        """Use Vietnam evidence lanes independently of one another."""
        if stage not in {"sentiment", "news"}:
            return _STAGE_DATA_TOOLS[stage]
        media_enabled = bool(
            _configured_media_providers(self.config.get("vn_media") or {})
        )
        if stage == "sentiment":
            return (
                ("get_editorial_news", "get_social_data")
                if media_enabled
                else _STAGE_DATA_TOOLS[stage]
            )
        tools = list(_STAGE_DATA_TOOLS[stage])
        vietnam_macro = _vn_macro_enabled(self.config)
        if media_enabled or vietnam_macro:
            tools.remove("get_news")
            tools.insert(0, "get_disclosures")
        if media_enabled:
            tools.insert(1, "get_editorial_news")
        if vietnam_macro:
            tools[tools.index("get_macro_indicators")] = (
                "get_vietnam_macro_context"
            )
        return tuple(tools)

    @staticmethod
    def _stage_warnings(
        session: StageSession,
        stage: str,
        *,
        state_update: dict[str, Any] | None = None,
    ) -> list[str]:
        if stage == "sentiment":
            metadata = TradingAgentsStageRunner._public_sentiment_metadata(
                (state_update or {}).get("sentiment_source_metadata")
            )
            warnings = metadata.get("warnings", []) if metadata else []
            for lane_name in ("retail_social_signal", "media_tone"):
                lane = metadata.get(lane_name, {}) if metadata else {}
                warnings.extend(lane.get("warnings", []))
            return list(dict.fromkeys(str(item) for item in warnings))
        if stage == "news":
            metadata = TradingAgentsStageRunner._public_news_metadata(
                (state_update or {}).get("news_source_metadata")
            )
            warnings = metadata.get("warnings", []) if metadata else []
            for lane_name in ("official_disclosures", "editorial_media", "vn_macro"):
                lane = metadata.get(lane_name, {}) if metadata else {}
                warnings.extend(lane.get("warnings", []))
                for source in [
                    *lane.get("sources", []),
                    *lane.get("source_results", []),
                ]:
                    warnings.extend(source.get("warnings", []))
            return list(dict.fromkeys(str(item) for item in warnings))
        if stage != "research":
            return []
        return [
            f"{analyst} analyst status is {session.stage_status.get(analyst, 'not_run')}"
            for analyst in session.selected_analysts
            if session.stage_status.get(analyst) != "completed"
        ]

    def _run_analyst(
        self, session: StageSession, state: dict[str, Any], stage: str
    ) -> dict[str, Any]:
        if stage == "market" and session.analysis_mode == "live":
            # Fetch once, before any model invocation.  The Market Analyst
            # injects this immutable evidence into every reasoning round and
            # removes the snapshot tool from its live tool list, preventing a
            # model-dependent or duplicate quote/OHLCV request.
            state["verified_market_snapshot"] = build_verified_market_snapshot(
                session.ticker,
                session.analysis_cutoff,
                include_live_quote=True,
            )
        wire_key = _ANALYST_WIRE_KEYS[stage]
        graph = self._graph((wire_key,))
        factories = {
            "market": create_market_analyst,
            "sentiment": create_sentiment_analyst,
            "news": create_news_analyst,
            "fundamentals": create_fundamentals_analyst,
        }
        node = factories[stage](graph.quick_thinking_llm)
        tool_node = graph.tool_nodes[wire_key]
        max_iterations = int(self.config.get("max_recur_limit", 100))

        # A prior analyst/session message must not bias this isolated run.
        state["messages"] = [HumanMessage(content=session.ticker)]
        # Downstream hydration uses explicit missing-stage markers. An analyst
        # must nevertheless prove it generated its own report during this run.
        state[_REPORT_KEYS[stage]] = ""
        for _ in range(max_iterations):
            update = node(state)
            self._merge_state(state, update)
            messages = state.get("messages") or []
            last = messages[-1] if messages else None
            if not getattr(last, "tool_calls", None):
                report_key = _REPORT_KEYS[stage]
                if not state.get(report_key):
                    raise RuntimeError(f"{stage} analyst returned no {report_key}")
                result = {report_key: state[report_key]}
                if stage == "sentiment" and state.get("sentiment_source_metadata"):
                    result["sentiment_source_metadata"] = self._public_sentiment_metadata(
                        state["sentiment_source_metadata"]
                    )
                if stage == "news" and state.get("news_source_metadata"):
                    result["news_source_metadata"] = self._public_news_metadata(
                        state["news_source_metadata"]
                    )
                return result
            # ``ToolNode`` is normally invoked by a compiled LangGraph, which
            # injects a run-scoped Runtime automatically.  The stage runner
            # deliberately executes the analyst/tool loop in isolation, so it
            # must provide the equivalent empty runtime itself.  Without it,
            # LangGraph >= 1.0 fails before the tool executes with
            # ``Missing required config key 'N/A' for 'tools'``.
            tool_update = tool_node.invoke(state, runtime=Runtime())
            self._merge_state(state, tool_update)
        raise RuntimeError(f"{stage} analyst exceeded {max_iterations} tool iterations")

    def _run_research(
        self, session: StageSession, state: dict[str, Any]
    ) -> dict[str, Any]:
        graph = self._graph(tuple(_ANALYST_WIRE_KEYS[a] for a in session.selected_analysts))
        state["investment_debate_state"] = InvestDebateState(
            bull_history="",
            bear_history="",
            history="",
            current_response="",
            judge_decision="",
            count=0,
        )
        bull = create_bull_researcher(graph.quick_thinking_llm)
        bear = create_bear_researcher(graph.quick_thinking_llm)
        rounds = int(self.config.get("max_debate_rounds", 1))
        for _ in range(rounds):
            state.update(bull(state))
            state.update(bear(state))
        state.update(create_research_manager(graph.deep_thinking_llm)(state))
        return {
            "investment_debate_state": state["investment_debate_state"],
            "investment_plan": state["investment_plan"],
        }

    def _run_trader(
        self, session: StageSession, state: dict[str, Any]
    ) -> dict[str, Any]:
        graph = self._graph(tuple(_ANALYST_WIRE_KEYS[a] for a in session.selected_analysts))
        update = create_trader(graph.quick_thinking_llm)(state)
        return {"trader_investment_plan": update["trader_investment_plan"]}

    def _run_risk(
        self, session: StageSession, state: dict[str, Any]
    ) -> dict[str, Any]:
        graph = self._graph(tuple(_ANALYST_WIRE_KEYS[a] for a in session.selected_analysts))
        state["risk_debate_state"] = RiskDebateState(
            aggressive_history="",
            conservative_history="",
            neutral_history="",
            history="",
            latest_speaker="",
            current_aggressive_response="",
            current_conservative_response="",
            current_neutral_response="",
            judge_decision="",
            count=0,
        )
        aggressive = create_aggressive_debator(graph.quick_thinking_llm)
        conservative = create_conservative_debator(graph.quick_thinking_llm)
        neutral = create_neutral_debator(graph.quick_thinking_llm)
        rounds = int(self.config.get("max_risk_discuss_rounds", 1))
        for _ in range(rounds):
            state.update(aggressive(state))
            state.update(conservative(state))
            state.update(neutral(state))
        state.update(create_portfolio_manager(graph.deep_thinking_llm)(state))
        return {
            "risk_debate_state": state["risk_debate_state"],
            "final_trade_decision": state["final_trade_decision"],
        }

    @staticmethod
    def _json_state(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: TradingAgentsStageRunner._json_state(item)
                for key, item in value.items()
                if key != "messages"
            }
        if isinstance(value, (list, tuple)):
            return [TradingAgentsStageRunner._json_state(item) for item in value]
        if hasattr(value, "type") and hasattr(value, "content"):
            return {
                "type": getattr(value, "type", "human"),
                "content": value.content,
            }
        if hasattr(value, "model_dump"):
            return TradingAgentsStageRunner._json_state(value.model_dump())
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @staticmethod
    def _hydrate_state(
        state: dict[str, Any], stage_status: dict[str, str] | None = None
    ) -> dict[str, Any]:
        hydrated = deepcopy(state)
        # Failed/unavailable analysts deliberately have stale output removed
        # from persisted JSON.  Existing researcher prompts index every report
        # key directly, so recreate empty placeholders for isolated downstream
        # execution once at the in-memory boundary.
        statuses = stage_status or {}
        for analyst, report_key in _REPORT_KEYS.items():
            status = statuses.get(analyst, "not_run")
            if status == "completed" and hydrated.get(report_key):
                continue
            marker = {
                "unavailable": "UNAVAILABLE: the selected data source returned no usable report",
                "failed": "FAILED: the analyst stage failed; no report is available",
                "completed": "MISSING: the completed analyst report is absent from session state",
                "not_run": "NOT_RUN: this analyst stage has not been executed",
            }.get(status, f"{status.upper()}: no analyst report is available")
            hydrated[report_key] = f"[{marker}]"
        hydrated["messages"] = [HumanMessage(content=hydrated["company_of_interest"])]
        return hydrated

    @staticmethod
    def _merge_state(state: dict[str, Any], update: dict[str, Any]) -> None:
        """Apply a node delta with LangGraph-like additive message semantics."""
        for key, value in update.items():
            if key == "messages":
                state.setdefault("messages", []).extend(value or [])
            else:
                state[key] = value

    def _store_final_decision(self, session: StageSession) -> None:
        if session.analysis_mode == "live":
            # Multiple intraday decisions can legitimately share one date.
            # Keep them in their durable sessions without overwriting the
            # date-keyed long-term decision memory.
            return
        graph = self._graph(tuple(_ANALYST_WIRE_KEYS[a] for a in session.selected_analysts))
        memory = getattr(graph, "memory_log", None)
        decision = session.state.get("final_trade_decision")
        if memory is not None and decision:
            memory.store_decision(
                ticker=session.ticker,
                trade_date=session.analysis_date,
                final_trade_decision=decision,
            )

    @staticmethod
    def _is_unavailable(exc: Exception) -> bool:
        if isinstance(exc, (NoMarketDataError, VendorNotConfiguredError)):
            return True
        return type(exc).__name__ in {
            "GxNoDataError",
            "GxUnsupportedDataError",
            "UnsupportedGxOperationError",
        }

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        message = str(exc)
        message = re.sub(
            r"(https?://|postgres(?:ql)?://)[^/\s]+@", r"\1<redacted>@", message
        )
        message = re.sub(
            r"(?i)([?&](?:access_?token|token|api_?key|key|password|secret)=)[^&\s]+",
            r"\1<redacted>",
            message,
        )
        message = re.sub(
            r"(?i)(\b(?:authorization\s*[:=]\s*)?bearer\s+)[^\s,;]+",
            r"\1<redacted>",
            message,
        )
        message = re.sub(
            r"(?i)(\b(?:access_?token|api_?key|password|secret)\s*[:=]\s*)[^\s,;&]+",
            r"\1<redacted>",
            message,
        )
        secret_names = {
            *(name for name in PROVIDER_API_KEY_ENV.values() if name),
            "TRADINGAGENTS_QUICK_LLM_API_KEY",
            "TRADINGAGENTS_DEEP_LLM_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "GX_MARKET_INFO_TV_TOKEN",
            "GX_ANALYSIS_DATA_API_KEY",
            "GX_MARKET_INFO_DATABASE_URL",
            "FIREANT_ACCESS_TOKEN",
            "FIREANT_ARCHIVE_ENCRYPTION_KEY",
            "VN_MEDIA_ARCHIVE_ENCRYPTION_KEY",
        }
        for name in secret_names:
            secret = os.environ.get(name)
            if secret:
                message = message.replace(secret, "<redacted>")
        return message[:1000]

    @staticmethod
    def _public_warnings(value: Any) -> list[str]:
        """Redact and bound provider warnings before durable persistence."""
        if value is None:
            return []
        items = value if isinstance(value, (list, tuple)) else [value]
        return [
            TradingAgentsStageRunner._safe_error(RuntimeError(str(item)))
            for item in items[:100]
        ]

    @staticmethod
    def _public_sentiment_metadata(value: Any) -> dict[str, Any]:
        """Keep provenance/coverage only; never raw posts or author identity."""
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        elif hasattr(value, "__dataclass_fields__"):
            value = {
                key: getattr(value, key)
                for key in value.__dataclass_fields__
            }
        if not isinstance(value, dict):
            return {}

        scalar_fields = {
            "provider",
            "status",
            "input_status",
            "band",
            "score",
            "confidence",
            "sample_size",
            "unique_authors",
            "window_start",
            "window_end",
            "fetch_id",
            "snapshot_id",
            "point_in_time_quality",
            "provider_sentiment_counts",
            "warnings",
            "attempted_vendors",
            "actual_vendor_observed",
            "llm_called",
            "snapshot_reused",
            "media_profile_fingerprint",
            "analysis_mode",
            "analysis_cutoff",
        }
        sanitized: dict[str, Any] = {}
        for key in scalar_fields:
            if key in value:
                sanitized[key] = (
                    TradingAgentsStageRunner._public_warnings(value[key])
                    if key == "warnings"
                    else TradingAgentsStageRunner._json_state(value[key])
                )
        for key in ("retail_social_signal", "media_tone"):
            lane = value.get(key)
            if isinstance(lane, dict):
                sanitized[key] = TradingAgentsStageRunner._public_sentiment_metadata(
                    lane
                )
        sources = value.get("sources")
        if isinstance(sources, list):
            sanitized["sources"] = [
                TradingAgentsStageRunner._public_sentiment_metadata(item)
                for item in sources
                if isinstance(item, dict)
            ]
        return sanitized

    @staticmethod
    def _public_news_metadata(value: Any) -> dict[str, Any]:
        """Keep news provenance/coverage; never persist RSS title or summary."""
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        elif hasattr(value, "__dataclass_fields__"):
            value = {
                key: getattr(value, key)
                for key in value.__dataclass_fields__
            }
        if not isinstance(value, dict):
            return {}

        scalar_fields = {
            "provider",
            "status",
            "sample_size",
            "window_start",
            "window_end",
            "fetch_id",
            "point_in_time_quality",
            "warnings",
            "attempted_vendors",
            "actual_vendor_observed",
            "media_profile_fingerprint",
            "observation_count",
            "as_of",
            "fetch_ids",
            "stale",
            "stale_indicators",
            "macro_profile_fingerprint",
            "count",
            "analysis_mode",
            "analysis_cutoff",
        }
        sanitized = {
            key: (
                TradingAgentsStageRunner._public_warnings(value[key])
                if key == "warnings"
                else TradingAgentsStageRunner._json_state(value[key])
            )
            for key in scalar_fields
            if key in value
        }
        for key in ("official_disclosures", "editorial_media", "vn_macro"):
            lane = value.get(key)
            if isinstance(lane, dict):
                sanitized[key] = TradingAgentsStageRunner._public_news_metadata(lane)
        for collection_key in ("sources", "source_results"):
            sources = value.get(collection_key)
            if isinstance(sources, list):
                sanitized[collection_key] = [
                    TradingAgentsStageRunner._public_news_metadata(item)
                    for item in sources
                    if isinstance(item, dict)
                ]
        return sanitized

    @staticmethod
    def _stage_result_status(stage: str, state_update: dict[str, Any]) -> str:
        if stage == "sentiment":
            metadata = TradingAgentsStageRunner._public_sentiment_metadata(
                state_update.get("sentiment_source_metadata")
            )
            return (
                "unavailable"
                if metadata.get("status") == "unavailable"
                else "completed"
            )
        if stage == "news":
            metadata = TradingAgentsStageRunner._public_news_metadata(
                state_update.get("news_source_metadata")
            )
            lanes = [
                metadata.get("official_disclosures") or {},
                metadata.get("editorial_media") or {},
                metadata.get("vn_macro") or {},
            ]
            usable = any(
                lane.get("status") in {"available", "partial"}
                and int(lane.get("sample_size") or 0) > 0
                for lane in lanes
            )
            if metadata.get("status") == "unavailable" and not usable:
                return "unavailable"
        return "completed"
