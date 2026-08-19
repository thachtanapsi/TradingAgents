"""Non-interactive GX-oriented CLI for modular TradingAgents runs."""

from __future__ import annotations

import importlib
import json
import os
import re
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import typer
from dotenv import load_dotenv

ANALYST_STAGES = ("market", "sentiment", "news", "fundamentals")
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

app = typer.Typer(
    name="tradingagents-gx",
    help="Run TradingAgents with GX data as durable, modular stages.",
    no_args_is_help=True,
)
social_app = typer.Typer(
    name="social",
    help="Manage the fail-closed Vietnam FireAnt archive and snapshots.",
    no_args_is_help=True,
)
media_app = typer.Typer(
    name="media",
    help="Manage the fail-closed CafeF/VnExpress RSS archive.",
    no_args_is_help=True,
)
macro_app = typer.Typer(
    name="macro",
    help="Manage the point-in-time NSO/SBV Vietnam macro archive.",
    no_args_is_help=True,
)
app.add_typer(social_app, name="social")
app.add_typer(media_app, name="media")
app.add_typer(macro_app, name="macro")

# Typer's public declaration style intentionally constructs these descriptors at
# import time; module-level singletons keep Ruff B008 focused on real defaults.
_ENV_FILE_OPTION = typer.Option(
    None,
    "--env-file",
    exists=True,
    dir_okay=False,
    readable=True,
    help="Load this profile and override stale variables exported by the shell.",
)
_SESSION_OPTION = typer.Option(
    None, "--session", exists=True, dir_okay=False, help="Resume this session.json."
)
_SESSION_ARGUMENT = typer.Argument(..., exists=True, dir_okay=False, metavar="SESSION")


@app.callback()
def main(
    ctx: typer.Context,
    env_file: Path | None = _ENV_FILE_OPTION,
) -> None:
    if env_file is not None:
        load_dotenv(env_file, override=True)
    # Import only after --env-file is loaded: DEFAULT_CONFIG applies environment
    # overrides at import time.
    from tradingagents.default_config import DEFAULT_CONFIG, _apply_env_overrides

    config = _apply_env_overrides(deepcopy(DEFAULT_CONFIG))
    try:
        from tradingagents.default_config import apply_gx_market_info_defaults
    except ImportError:
        pass
    else:
        config = apply_gx_market_info_defaults(config)
    # Social subcommands do not construct a StageRunner, so publish the same
    # resolved profile that dataflow factories read.  Without this, the CLI
    # displayed the upstream ``legacy`` social profile even though GX had
    # selected FireAnt.
    from tradingagents.dataflows.config import set_config

    set_config(config)
    ctx.obj = {"config": config}


def _analysts(value: str) -> tuple[str, ...]:
    items = tuple(part.strip().lower() for part in value.split(",") if part.strip())
    items = tuple("sentiment" if item == "social" else item for item in items)
    unknown = set(items) - set(ANALYST_STAGES)
    if unknown:
        raise typer.BadParameter("unknown analyst(s): " + ", ".join(sorted(unknown)))
    if not items:
        raise typer.BadParameter("at least one analyst is required")
    return items


def _load_session(path: Path):
    from tradingagents.graph.stage_session import StageSession

    try:
        return StageSession.load(path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(str(exc), param_hint="SESSION") from exc


def _runner(ctx: typer.Context):
    from tradingagents.graph.stage_runner import TradingAgentsStageRunner

    return TradingAgentsStageRunner(config=ctx.obj["config"])


def _social_service():
    """Load the optional FireAnt implementation only for explicit social commands."""
    last_error: ImportError | None = None
    # The service is separate from the domain/archive module in the full
    # implementation. Retain the compatibility path for early extension builds.
    for module_name in (
        "tradingagents.dataflows.vietnam_social_service",
        "tradingagents.dataflows.vietnam_social",
    ):
        try:
            module = importlib.import_module(module_name)
            factory = module.create_vietnam_social_service_from_env
            return factory()
        except (ImportError, AttributeError) as exc:
            if isinstance(exc, ImportError):
                last_error = exc
    raise RuntimeError(
        "Vietnam social support is not installed; install the optional "
        "FireAnt/encryption dependencies"
    ) from last_error


def _media_service():
    """Load optional Vietnam media support only for explicit media operations."""
    try:
        module = importlib.import_module("tradingagents.dataflows.vietnam_media")
        factory = module.create_vietnam_media_service_from_env
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "Vietnam media support is not installed; install the optional "
            "RSS/encryption dependencies"
        ) from exc
    return factory()


def _macro_service():
    """Load optional Vietnam macro support only for explicit operations."""
    try:
        module = importlib.import_module("tradingagents.dataflows.vietnam_macro")
        factory = module.create_vietnam_macro_service_from_env
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "Vietnam macro support is not installed; install the optional "
            "NSO/SBV parser dependencies with .[vn-macro]"
        ) from exc
    return factory()


def _jsonable(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _vietnam_now() -> datetime:
    """Single injectable clock used to freeze a live run's immutable cutoff."""
    return datetime.now(_VN_TZ)


_COLLECTION_PUBLIC_FIELDS = {
    "provider",
    "ticker",
    "status",
    "reason",
    "fetch_id",
    "feed_url",
    "articles_seen",
    "posts_seen",
    "versions_inserted",
    "retention_skips",
    "pages",
    "truncated",
    "watermark_stopped",
    "ordering_violated",
    "request_succeeded",
    "http_status",
    "warnings",
}


def _public_collection_result(value):
    """Keep operational counters/provenance, never raw evidence or identity."""
    value = _jsonable(value)
    if isinstance(value, list):
        return [_public_collection_result(item) for item in value]
    if not isinstance(value, dict):
        return None
    sanitized = {}
    for key, item in value.items():
        if key not in _COLLECTION_PUBLIC_FIELDS:
            continue
        if key == "warnings":
            warnings = item if isinstance(item, list) else [item]
            sanitized[key] = [
                _safe_runtime_error(RuntimeError(str(warning)))
                for warning in warnings[:100]
            ]
        elif isinstance(item, str):
            # Provider results are untrusted operational payloads.  Apply the
            # same credential/query-string scrubber used for exceptions before
            # printing even allow-listed string fields such as ``feed_url``.
            sanitized[key] = _safe_runtime_error(RuntimeError(item))
        elif isinstance(item, (int, float, bool)) or item is None:
            sanitized[key] = item
    return sanitized


def _collect_current_evidence(ticker: str) -> dict[str, object]:
    """Collect media then FireAnt; each lane fails independently and safely."""
    symbol = ticker.strip().upper()
    results: dict[str, object] = {}
    for lane, factory in (("media", _media_service), ("social", _social_service)):
        try:
            service = factory()
            result = service.collect_once(ticker=symbol)
            results[lane] = _public_collection_result(result)
        except Exception as exc:  # noqa: BLE001 - a lane must not block the other
            warning = _safe_runtime_error(exc)
            results[lane] = {"status": "failed", "warnings": [warning]}
            typer.echo(f"WARN collect {lane}: {warning}", err=True)
    return results


def _validate_new_run_time(
    analysis_date: str | None,
    *,
    as_of_now: bool,
    collect_evidence: bool,
) -> None:
    if (analysis_date is None) == (not as_of_now):
        raise typer.BadParameter("provide exactly one of --date or --as-of-now")
    if collect_evidence and not as_of_now:
        raise typer.BadParameter("--collect-evidence requires --as-of-now")
    if analysis_date is not None:
        try:
            date.fromisoformat(analysis_date)
        except ValueError as exc:
            raise typer.BadParameter("--date must use YYYY-MM-DD") from exc


def _prepare_new_run_time(
    analysis_date: str | None,
    *,
    as_of_now: bool,
    collect_evidence: bool,
) -> tuple[str, str, datetime | None]:
    """Freeze one VN-aware cutoff after any caller-managed evidence collection."""
    _validate_new_run_time(
        analysis_date,
        as_of_now=as_of_now,
        collect_evidence=collect_evidence,
    )
    if not as_of_now:
        return str(analysis_date), "close", None

    cutoff = _vietnam_now()
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise RuntimeError("live analysis clock must return a timezone-aware datetime")
    cutoff = cutoff.astimezone(_VN_TZ)
    typer.echo(f"Analysis cutoff: {cutoff.isoformat()}")
    return cutoff.date().isoformat(), "live", cutoff


def _social_status_payload(*, live: bool = False) -> dict:
    payload = _jsonable(_social_service().status(live=live))
    if not isinstance(payload, dict):
        raise RuntimeError("Vietnam social status returned an invalid payload")
    return payload


def _social_status_outcome(payload: dict) -> tuple[str, str]:
    provider = str(payload.get("provider") or "fireant")
    authorized = bool(payload.get("authorized", False))
    enabled = bool(payload.get("enabled", True))
    issues = [str(item) for item in payload.get("issues", [])]
    if not enabled or not authorized:
        detail = "authorization locked" if not authorized else "disabled"
        return "SKIP", f"{provider}: {detail}"
    if issues or not bool(payload.get("archive_ready", False)):
        detail = "; ".join(issues) or "archive is not ready"
        return "FAIL", f"{provider}: {detail}"
    watchlist = payload.get("watchlist") or []
    return "OK", f"{provider}; archive ready; watchlist={len(watchlist)}"


def _media_status_payload(*, live: bool = False) -> dict:
    payload = _jsonable(_media_service().status(live=live))
    if not isinstance(payload, dict):
        raise RuntimeError("Vietnam media status returned an invalid payload")
    return payload


def _media_status_outcome(payload: dict) -> tuple[str, str]:
    """Map provider status into doctor semantics without exposing raw content."""
    status = str(payload.get("status") or "").strip().lower()
    enabled = bool(payload.get("enabled", True))
    issues = [str(item) for item in payload.get("issues", [])]
    sources = payload.get("sources") or []
    active_sources = [
        item
        for item in sources
        if isinstance(item, dict)
        and str(item.get("status") or "").lower() != "disabled"
    ]
    if not enabled or status == "disabled" or (sources and not active_sources):
        return "SKIP", "authorization locked"
    if status in {"failed", "unavailable"} or issues:
        return "FAIL", "; ".join(issues) or status
    if not bool(payload.get("archive_ready", False)):
        return "FAIL", "archive is not ready"
    watchlist = payload.get("watchlist") or []
    provider_names = [
        str(item.get("provider"))
        for item in active_sources
        if item.get("provider")
    ]
    detail = ",".join(provider_names) or "vn_media"
    return "OK", f"{detail}; archive ready; watchlist={len(watchlist)}"


def _macro_status_payload(*, live: bool = False) -> dict:
    payload = _jsonable(_macro_service().status(live=live))
    if not isinstance(payload, dict):
        raise RuntimeError("Vietnam macro status returned an invalid payload")
    return payload


def _macro_status_outcome(payload: dict) -> tuple[str, str]:
    """Map archive/source health into offline/live doctor semantics."""
    enabled = bool(payload.get("enabled", True))
    status = str(payload.get("status") or "").strip().lower()
    issues = [str(item) for item in payload.get("issues", [])]
    warnings = [str(item) for item in payload.get("warnings", [])]
    archive_ready = bool(payload.get("archive_ready", False))
    source_rows = [
        item for item in (payload.get("sources") or []) if isinstance(item, dict)
    ]
    raw_count = (
        payload.get("observation_count")
        or payload.get("observations")
        or payload.get("fetch_run_count")
        or sum(int(item.get("observation_count") or 0) for item in source_rows)
        or 0
    )
    count = raw_count if isinstance(raw_count, int) else len(raw_count or [])
    if not enabled or status == "disabled":
        return "SKIP", "disabled"
    if payload.get("usable") is False:
        return "FAIL", "; ".join(issues or warnings) or "no usable macro observations"
    if not archive_ready or status == "failed":
        detail = "; ".join(issues) or status or "archive is not ready"
        return "FAIL", detail
    providers = payload.get("providers") or source_rows
    provider_names = [
        str(item.get("provider") or item.get("name"))
        if isinstance(item, dict)
        else str(item)
        for item in providers
    ]
    provider_names = [item for item in provider_names if item and item != "None"]
    detail = ",".join(provider_names) or "vn_macro"
    degraded = status == "partial" or bool(issues or warnings)
    if status == "unavailable":
        return "FAIL", "; ".join(issues or warnings) or "no usable macro observations"
    if degraded:
        suffix = "; ".join(issues or warnings)
        return "OK", (
            f"{detail}; partial; observations={count}"
            + (f"; {suffix}" if suffix else "")
        )
    return "OK", f"{detail}; archive ready; observations={count}"


def _is_local_llm_profile(config: dict) -> bool:
    from tradingagents.llm_clients.profiles import (
        is_local_llm_profile,
        resolve_llm_profile,
    )

    try:
        profile = resolve_llm_profile(config, "quick")
    except ValueError:
        return False
    return is_local_llm_profile(profile)


@app.command()
def full(
    ctx: typer.Context,
    ticker: str = typer.Option(..., "--ticker"),
    analysis_date: str | None = typer.Option(
        None,
        "--date",
        "--analysis-date",
        help="Completed-session date (YYYY-MM-DD); mutually exclusive with --as-of-now.",
    ),
    as_of_now: bool = typer.Option(
        False,
        "--as-of-now",
        help="Freeze one current Asia/Ho_Chi_Minh cutoff for a live run.",
    ),
    collect_evidence: bool = typer.Option(
        False,
        "--collect-evidence",
        help="Before a live run, collect media then FireAnt evidence for this ticker.",
    ),
    analysts: str = typer.Option(
        ",".join(ANALYST_STAGES), help="Comma-separated analyst stages."
    ),
    asset_type: str = typer.Option("stock", help="Asset type stored in run identity."),
) -> None:
    """Create a run and execute all selected analysts through final risk decision."""
    selected = _analysts(analysts)
    _validate_new_run_time(
        analysis_date,
        as_of_now=as_of_now,
        collect_evidence=collect_evidence,
    )
    try:
        if collect_evidence:
            collected = _collect_current_evidence(ticker)
            typer.echo(
                "Evidence collection: "
                + json.dumps(collected, ensure_ascii=False, sort_keys=True)
            )
        resolved_date, analysis_mode, analysis_cutoff = _prepare_new_run_time(
            analysis_date,
            as_of_now=as_of_now,
            collect_evidence=collect_evidence,
        )
        runner = _runner(ctx)
        session = runner.create_session(
            ticker,
            resolved_date,
            selected_analysts=selected,
            asset_type=asset_type,
            analysis_mode=analysis_mode,
            analysis_cutoff=analysis_cutoff,
        )
        path = session.save()
        typer.echo(f"Created {path}")
        runner.run_default_to(session, session_path=str(path))
        typer.echo(session.state.get("final_trade_decision", ""))
        typer.echo(f"Session: {session.path()}")
    except typer.BadParameter:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI must not emit credential-bearing tracebacks
        _exit_runtime("full run", exc)


@app.command()
def stage(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="market|sentiment|news|fundamentals|research|trader|risk"),
    session_file: Path | None = _SESSION_OPTION,
    ticker: str | None = typer.Option(None, help="Required only when creating a new run."),
    analysis_date: str | None = typer.Option(
        None,
        "--date",
        "--analysis-date",
        help="Completed-session date; mutually exclusive with --as-of-now for a new run.",
    ),
    as_of_now: bool = typer.Option(
        False,
        "--as-of-now",
        help="Freeze one current Asia/Ho_Chi_Minh cutoff for a new live run.",
    ),
    collect_evidence: bool = typer.Option(
        False,
        "--collect-evidence",
        help="Before a new live run, collect media then FireAnt for this ticker.",
    ),
    analysts: str = typer.Option(
        ",".join(ANALYST_STAGES), help="Selected analysts for a newly created run."
    ),
    asset_type: str = typer.Option("stock"),
) -> None:
    """Execute one stage, persisting output and invalidating downstream results."""
    if session_file is not None and (
        analysis_date is not None or as_of_now or collect_evidence
    ):
        raise typer.BadParameter(
            "--session cannot be combined with --date, --as-of-now or --collect-evidence"
        )
    if session_file is None and not ticker:
        raise typer.BadParameter(
            "provide --session, or --ticker with exactly one of --date or --as-of-now"
        )
    if session_file is None:
        _validate_new_run_time(
            analysis_date,
            as_of_now=as_of_now,
            collect_evidence=collect_evidence,
        )
    selected = _analysts(analysts) if session_file is None else None
    session = None
    try:
        if session_file is not None:
            runner = _runner(ctx)
            session = _load_session(session_file)
        else:
            if collect_evidence:
                collected = _collect_current_evidence(str(ticker))
                typer.echo(
                    "Evidence collection: "
                    + json.dumps(collected, ensure_ascii=False, sort_keys=True)
                )
            resolved_date, analysis_mode, analysis_cutoff = _prepare_new_run_time(
                analysis_date,
                as_of_now=as_of_now,
                collect_evidence=collect_evidence,
            )
            runner = _runner(ctx)
            session = runner.create_session(
                ticker,
                resolved_date,
                selected_analysts=selected,
                asset_type=asset_type,
                analysis_mode=analysis_mode,
                analysis_cutoff=analysis_cutoff,
            )
            session.save()
        persistence_path = str(session_file) if session_file is not None else None
        runner.run_stage_to(session, name, session_path=persistence_path)
        typer.echo(f"Completed {name}; session: {session_file or session.path()}")
    except typer.BadParameter:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI must not emit credential-bearing tracebacks
        if session is not None:
            typer.echo(f"Session: {session_file or session.path()}", err=True)
        _exit_runtime(f"stage {name}", exc)


@app.command()
def show(
    session_file: Path = _SESSION_ARGUMENT,
    raw: bool = typer.Option(False, "--json", help="Print the full session JSON."),
) -> None:
    """Show immutable identity, progress, and available outputs without loading LLMs."""
    session = _load_session(session_file)
    if raw:
        typer.echo(json.dumps(session.to_dict(), ensure_ascii=False, indent=2))
        return
    typer.echo(f"Run: {session.run_id}")
    typer.echo(f"Instrument: {session.ticker} @ {session.analysis_date} ({session.asset_type})")
    typer.echo(f"Analysis: {session.analysis_mode} @ {session.analysis_cutoff}")
    quick = session.llm.get("quick") or {}
    deep = session.llm.get("deep") or {}
    typer.echo(
        "LLM quick: "
        f"{quick.get('provider')} / {quick.get('model')} @ {quick.get('base_url') or 'default'}"
    )
    typer.echo(
        "LLM deep: "
        f"{deep.get('provider')} / {deep.get('model')} @ {deep.get('base_url') or 'default'}"
    )
    typer.echo(f"GX transport: {session.data_transport.get('transport')}")
    typer.echo("Completed: " + (", ".join(session.completed_stages) or "none"))
    typer.echo(
        "Status: "
        + ", ".join(f"{stage}={status}" for stage, status in session.stage_status.items())
    )
    for key in (
        "market_report",
        "sentiment_report",
        "news_report",
        "fundamentals_report",
        "investment_plan",
        "trader_investment_plan",
        "final_trade_decision",
    ):
        if session.state.get(key):
            typer.echo(f"\n## {key}\n{session.state[key]}")


@social_app.command("status")
def social_status() -> None:
    """Inspect local authorization/archive configuration without network calls."""
    try:
        payload = _social_status_payload(live=False)
        outcome, detail = _social_status_outcome(payload)
        typer.echo(f"{outcome} social: {detail}")
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        if outcome == "FAIL":
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - redact any provider detail
        _exit_runtime("social status", exc)


@social_app.command("collect")
def social_collect(
    once: bool = typer.Option(
        False,
        "--once",
        help="Run one watchlist/ticker collection pass and exit.",
    ),
    ticker: str | None = typer.Option(None, "--ticker", help="Collect one ticker."),
) -> None:
    """Collect one encrypted FireAnt archive pass.

    The long-running scheduler remains an operations concern; this command is
    intentionally explicit and bounded so reruns cannot accidentally create
    duplicate collectors.
    """
    if not once:
        raise typer.BadParameter("--once is required; schedule this command every 5 minutes")
    try:
        result = _social_service().collect_once(ticker=ticker.upper() if ticker else None)
        typer.echo(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001 - redact auth/archive failures
        _exit_runtime("social collect", exc)


@social_app.command("purge")
def social_purge() -> None:
    """Purge expired encrypted raw/author data while retaining snapshots."""
    try:
        result = _social_service().purge()
        typer.echo(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        _exit_runtime("social purge", exc)


@social_app.command("snapshot")
def social_snapshot(
    ctx: typer.Context,
    analysis_date: str = typer.Option(..., "--date", "--analysis-date"),
    live_llm: bool = typer.Option(
        False,
        "--live-llm",
        help="Allow the configured quick LLM to derive and persist snapshots.",
    ),
) -> None:
    """Create idempotent daily sentiment snapshots for the configured watchlist."""
    if not live_llm:
        raise typer.BadParameter("--live-llm is required; no LLM is called by default")
    config = ctx.obj["config"]
    social_config = config.get("vn_social") or {}
    hosted_authorized = os.environ.get(
        "TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not _is_local_llm_profile(config) and not hosted_authorized:
        raise typer.BadParameter(
            "hosted social content is locked; set "
            "TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED=true only after approval"
        )
    watchlist = _configured_social_watchlist(social_config)
    if not watchlist:
        raise typer.BadParameter("TRADINGAGENTS_VN_SOCIAL_TICKERS is empty")
    try:
        target = date.fromisoformat(analysis_date)
    except ValueError as exc:
        raise typer.BadParameter("--date must use YYYY-MM-DD") from exc

    try:
        _assert_snapshot_time(target)
        service = _social_service()
        # Snapshot generation is archive-only. Only doctor --live-social and
        # collect are allowed to call FireAnt over the network.
        status_payload = _jsonable(service.status(live=False))
        outcome, detail = _social_status_outcome(status_payload)
        if outcome != "OK":
            raise RuntimeError(f"social preflight {outcome.lower()}: {detail}")
        _assert_completed_gx_session(config, target)
        runner = _runner(ctx)
        results = []
        for ticker in watchlist:
            session = runner.create_session(
                ticker,
                target.isoformat(),
                selected_analysts=("sentiment",),
            )
            prompt_version = str(
                session.social_profile.get("prompt_version") or "vn-social-v1"
            )
            model_profile = (
                f"{session.llm.get('provider')}:{session.llm.get('quick_model')}"
            )
            expected_fingerprint = session.input_fingerprint("sentiment")
            existing = service.get_snapshot(
                ticker,
                target.isoformat(),
            )
            if existing is not None:
                if (
                    existing.model_profile != model_profile
                    or existing.fingerprint != expected_fingerprint
                ):
                    raise RuntimeError(
                        f"snapshot identity mismatch for {ticker}; bump the social "
                        "prompt version or use a new archive"
                    )
                existing_payload = _jsonable(existing)
                if not isinstance(existing_payload, dict):
                    existing_payload = {
                        "snapshot_id": getattr(existing, "snapshot_id", None)
                    }
                results.append(
                    {**existing_payload, "created": False, "skipped": True}
                )
                continue
            claim = service.claim_snapshot(ticker, target.isoformat())
            if not bool(getattr(claim, "acquired", False)):
                results.append(
                    {
                        "ticker": ticker,
                        "analysis_date": target.isoformat(),
                        "created": False,
                        "skipped": True,
                        "reason": "snapshot_claim_held_or_completed",
                    }
                )
                continue
            try:
                runner.run_stage(session, "sentiment")
                from tradingagents.graph.stage_runner import media_profile_fingerprint

                media_fingerprint = media_profile_fingerprint(
                    getattr(session, "media_profile", {"provider": "legacy"})
                )
                metadata = session.state.get("sentiment_source_metadata") or {}
                retail = metadata.get("retail_social_signal") or {}
                stage_meta = session.stage_metadata.get("sentiment") or {}
                report = session.state.get("sentiment_report") or ""
                report_status = str(metadata.get("status") or "unavailable")
                retail_status = str(retail.get("status") or "unavailable")
                snapshot = service.save_snapshot(
                    ticker,
                    target.isoformat(),
                    signal_payload=retail,
                    model_profile=model_profile,
                    prompt_version=prompt_version,
                    fingerprint=str(
                        stage_meta.get("input_fingerprint") or expected_fingerprint
                    ),
                    status=retail_status,
                    report_status=report_status,
                    statistics={
                        "sample_size": retail.get("sample_size", 0),
                        "unique_authors": retail.get("unique_authors", 0),
                        "point_in_time_quality": retail.get("point_in_time_quality"),
                        "window_start": retail.get("window_start"),
                        "window_end": retail.get("window_end"),
                        "warnings": retail.get("warnings", []),
                        # Bind FireAnt daily snapshots to the editorial-media
                        # identity consumed by the same Sentiment stage. This
                        # prevents a later run from reusing a snapshot produced
                        # with a different RSS archive/prompt/alias policy.
                        "media_profile_fingerprint": media_fingerprint,
                    },
                    report_payload={
                        "rendered_report": report,
                        "source_metadata": metadata,
                        "media_profile_fingerprint": media_fingerprint,
                    },
                )
                results.append(_jsonable(snapshot))
            finally:
                service.release_snapshot_claim(claim)
        typer.echo(json.dumps(results, ensure_ascii=False, indent=2))
    except typer.BadParameter:
        raise
    except Exception as exc:  # noqa: BLE001
        _exit_runtime("social snapshot", exc)


@media_app.command("status")
def media_status() -> None:
    """Inspect authorization/archive configuration without network calls."""
    try:
        payload = _media_status_payload(live=False)
        outcome, detail = _media_status_outcome(payload)
        typer.echo(f"{outcome} media: {detail}")
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        if outcome == "FAIL":
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _exit_runtime("media status", exc)


@media_app.command("collect")
def media_collect(
    once: bool = typer.Option(
        False,
        "--once",
        help="Run one configured-feed collection pass and exit.",
    ),
    ticker: str | None = typer.Option(
        None, "--ticker", help="Collect and match one ticker."
    ),
) -> None:
    """Collect one encrypted CafeF/VnExpress RSS archive pass."""
    if not once:
        raise typer.BadParameter(
            "--once is required; schedule this command every 5 minutes"
        )
    try:
        result = _media_service().collect_once(
            ticker=ticker.upper() if ticker else None
        )
        typer.echo(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        _exit_runtime("media collect", exc)


@media_app.command("purge")
def media_purge() -> None:
    """Purge expired encrypted RSS content while retaining aggregate audit."""
    try:
        result = _media_service().purge()
        typer.echo(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        _exit_runtime("media purge", exc)


@macro_app.command("status")
def macro_status() -> None:
    """Inspect the local NSO/SBV archive without making network requests."""
    try:
        payload = _macro_status_payload(live=False)
        outcome, detail = _macro_status_outcome(payload)
        typer.echo(f"{outcome} macro: {detail}")
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        if outcome == "FAIL":
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001
        _exit_runtime("macro status", exc)


@macro_app.command("collect")
def macro_collect(
    once: bool = typer.Option(
        False,
        "--once",
        help="Run one bounded NSO/SBV collection pass and exit.",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Optional source group/provider: nso, sbv, nso_sdmx, nso_release or sbv_html.",
    ),
) -> None:
    """Collect one normalized public Vietnam macro archive pass."""
    if not once:
        raise typer.BadParameter(
            "--once is required; schedule this command at 08:15 and 16:30 Vietnam time"
        )
    normalized = source.strip().lower() if source else None
    allowed = {None, "nso", "sbv", "nso_sdmx", "nso_release", "sbv_html"}
    if normalized not in allowed:
        raise typer.BadParameter(
            "--source must be nso, sbv, nso_sdmx, nso_release or sbv_html"
        )
    try:
        result = _macro_service().collect_once(source=normalized)
        typer.echo(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        _exit_runtime("macro collect", exc)


@macro_app.command("show")
def macro_show(
    as_of: str = typer.Option(..., "--as-of", help="YYYY-MM-DD or ISO-8601 cutoff."),
    raw: bool = typer.Option(False, "--json", help="Print structured evidence JSON."),
    lookback_months: int | None = typer.Option(
        None,
        "--lookback-months",
        min=1,
        help="Override the configured historical window for this query.",
    ),
) -> None:
    """Show archive-only evidence available at a point-in-time cutoff."""
    try:
        # Preserve date-only precision: the service applies the documented
        # Vietnamese 15:00 analysis cutoff.
        if "T" in as_of or " " in as_of:
            datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        else:
            date.fromisoformat(as_of)
    except ValueError as exc:
        raise typer.BadParameter("--as-of must use YYYY-MM-DD or ISO-8601") from exc

    try:
        result = _macro_service().load_evidence(
            as_of,
            lookback_months=lookback_months,
        )
        payload = _jsonable(result.to_dict() if hasattr(result, "to_dict") else result)
        if not isinstance(payload, dict):
            raise RuntimeError("Vietnam macro evidence returned an invalid payload")
        if raw:
            typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        typer.echo(
            f"Macro: {payload.get('status', 'unknown')} @ {payload.get('as_of', as_of)}"
        )
        for observation in payload.get("observations", []):
            if not isinstance(observation, dict):
                continue
            unit = str(observation.get("unit") or "").strip()
            multiplier = int(observation.get("unit_multiplier") or 1)
            unit_display = (
                f"{unit} ×{multiplier:,}" if multiplier != 1 else unit
            ).strip()
            period = observation.get("period_end") or observation.get("period_start")
            provider = observation.get("source_provider") or "unknown"
            typer.echo(
                f"- {observation.get('indicator_id')}: {observation.get('value')} "
                f"{unit_display} ({period}; {provider})".rstrip()
            )
        for warning in payload.get("warnings", []):
            typer.echo(f"WARN: {warning}")
    except typer.BadParameter:
        raise
    except Exception as exc:  # noqa: BLE001
        _exit_runtime("macro show", exc)


def _configured_social_watchlist(settings: dict) -> list[str]:
    raw = settings.get("tickers") or ""
    values = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return list(
        dict.fromkeys(
            str(item).strip().upper() for item in values if str(item).strip()
        )
    )


def _assert_completed_gx_session(config: dict, target: date) -> None:
    from tradingagents.dataflows.gx_market_info import get_gx_market_info_client

    last = get_gx_market_info_client(config).get_last_trading_session(
        as_of=target.isoformat()
    )
    last_date = str(last)[:10]
    if last_date != target.isoformat():
        raise RuntimeError(
            f"{target.isoformat()} is not a completed GX trading session "
            f"(last completed: {last_date or 'unavailable'})"
        )


def _assert_snapshot_time(target: date, *, now: datetime | None = None) -> None:
    """Do not create today's close snapshot before the scheduled 15:15 VN time."""
    local_now = (now or datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))).astimezone(
        ZoneInfo("Asia/Ho_Chi_Minh")
    )
    if target > local_now.date():
        raise RuntimeError("cannot create a sentiment snapshot for a future date")
    if target == local_now.date() and local_now.time() < time(15, 15):
        raise RuntimeError("today's sentiment snapshot is available after 15:15 Asia/Ho_Chi_Minh")


def _check_llm_profile(
    config: dict,
    role: str,
    *,
    model_cache: dict[str, tuple[bool, object]] | None = None,
) -> tuple[bool, str]:
    """Validate one role without invoking a paid hosted LLM."""
    from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
    from tradingagents.llm_clients.profiles import (
        resolve_llm_api_key,
        resolve_llm_profile,
    )

    try:
        profile = resolve_llm_profile(config, role)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        return False, _safe_runtime_error(exc)
    try:
        api_key, _ = resolve_llm_api_key(profile)
    except ValueError as exc:
        return False, _safe_runtime_error(exc)

    endpoint = profile.effective_base_url
    identity = f"{profile.provider}/{profile.model}"
    if endpoint:
        identity += " @ " + _safe_runtime_error(RuntimeError(endpoint))

    if profile.provider == "ollama":
        if not endpoint:
            return False, f"{identity}; base URL is missing"
        cache = model_cache if model_cache is not None else {}
        cached = cache.get(endpoint)
        if cached is None:
            try:
                response = requests.get(endpoint.rstrip("/") + "/models", timeout=2)
                response.raise_for_status()
                available = {
                    item.get("id")
                    for item in response.json().get("data", [])
                    if isinstance(item, dict)
                }
                cached = (True, available)
            except (requests.RequestException, TypeError, ValueError) as exc:
                cached = (False, _safe_runtime_error(exc))
            cache[endpoint] = cached
        cache_ok, cache_value = cached
        if not cache_ok:
            return False, f"{identity}; {cache_value}"
        available = cache_value if isinstance(cache_value, set) else set()
        if profile.model not in available:
            return False, f"{identity}; model tag is not pulled"
        return True, f"{identity}; model present"

    if profile.provider == "openai_compatible":
        if not endpoint:
            role_name = role.upper()
            return (
                False,
                "TRADINGAGENTS_LLM_BACKEND_URL is missing; alternatively set "
                f"TRADINGAGENTS_{role_name}_LLM_BASE_URL",
            )
        return True, identity

    role_key_env = f"TRADINGAGENTS_{role.upper()}_LLM_API_KEY"
    if profile.provider == "bedrock":
        if os.environ.get(role_key_env):
            return False, f"{role_key_env} is not supported by Bedrock"
        return (
            True,
            identity + "; AWS credential-chain validation occurs on first request",
        )

    if profile.provider == "azure":
        missing = []
        if not api_key:
            missing.append(f"{role_key_env} or AZURE_OPENAI_API_KEY")
        if not endpoint:
            missing.append(f"TRADINGAGENTS_{role.upper()}_LLM_BASE_URL or AZURE_OPENAI_ENDPOINT")
        if not os.environ.get("OPENAI_API_VERSION"):
            missing.append("OPENAI_API_VERSION")
        return (
            not missing,
            identity if not missing else "missing: " + ", ".join(missing),
        )

    if profile.provider not in PROVIDER_API_KEY_ENV:
        return False, f"unsupported LLM provider: {profile.provider}"
    provider_key_env = PROVIDER_API_KEY_ENV[profile.provider]
    if provider_key_env and not api_key:
        return False, f"missing: {role_key_env} or {provider_key_env}"
    return True, identity


def _check_llms(config: dict) -> dict[str, tuple[bool, str]]:
    model_cache: dict[str, tuple[bool, object]] = {}
    return {
        role: _check_llm_profile(config, role, model_cache=model_cache)
        for role in ("quick", "deep")
    }


def _check_llm(config: dict) -> tuple[bool, str]:
    """Backward-compatible aggregate used by external diagnostic callers."""
    results = _check_llms(config)
    return (
        all(ok for ok, _ in results.values()),
        "; ".join(f"{role}: {detail}" for role, (_, detail) in results.items()),
    )


def _check_gx(config: dict | None = None) -> tuple[bool, str]:
    """Use the adapter when present; import stays lazy for keyless CLI commands."""
    settings = (config or {}).get("gx_market_info") or {}
    transport = str(
        settings.get("transport") or os.environ.get("GX_DATA_TRANSPORT", "api")
    ).lower()
    try:
        from tradingagents.dataflows.gx_market_info import get_gx_market_info_client
    except ImportError:
        return False, "GX adapter is not installed in this checkout"

    try:
        client = get_gx_market_info_client(config)
        as_of = date.today().isoformat()
        last_session = client.get_last_trading_session(as_of=as_of)
        start = (date.today() - timedelta(days=14)).isoformat()
        frame = client.get_ohlcv(
            "HPG", start_date=start, end_date=as_of, resolution="1D"
        )
        if not last_session:
            return False, f"{transport}; trading calendar returned no completed session"
        if getattr(frame, "empty", not bool(len(frame))):
            return False, f"{transport}; HPG OHLCV probe returned no rows"
        return True, f"{transport}; last session {last_session}"
    except Exception as exc:  # noqa: BLE001 - doctor reports adapter/runtime errors
        return False, f"{transport}: {_safe_runtime_error(exc)}"


def _safe_runtime_error(exc: Exception) -> str:
    """Redact credentials before a doctor failure is printed to a terminal/log."""
    from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

    message = re.sub(
        r"(https?://|postgres(?:ql)?://)[^/\s]+@",
        r"\1<redacted>@",
        str(exc),
    )
    message = re.sub(
        r"(?i)([?&](?:access_?token|token|api_key|key|password|secret)=)[^&\s]+",
        r"\1<redacted>",
        message,
    )
    message = re.sub(
        r"(?i)(\b(?:authorization\s*[:=]\s*)?bearer\s+)[^\s,;]+",
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


def _exit_runtime(operation: str, exc: Exception) -> None:
    typer.echo(f"FAIL {operation}: {_safe_runtime_error(exc)}", err=True)
    raise typer.Exit(code=1) from None


def _check_social(config: dict, *, live: bool = False) -> tuple[str, str]:
    settings = config.get("vn_social") or {}
    if not bool(settings.get("authorized", False)):
        return "SKIP", "authorization locked"
    try:
        payload = _social_status_payload(live=live)
        return _social_status_outcome(payload)
    except Exception as exc:  # noqa: BLE001 - doctor must be traceback/secret safe
        return "FAIL", _safe_runtime_error(exc)


def _env_true(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _check_media(config: dict, *, live: bool = False) -> tuple[str, str]:
    settings = config.get("vn_media") or {}
    raw = settings.get("providers") or ""
    providers = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    enabled = {str(item).strip().lower() for item in providers if str(item).strip()}
    authorization_env = {
        "cafef_rss": "TRADINGAGENTS_CAFEF_RSS_AUTHORIZED",
        "vnexpress_rss": "TRADINGAGENTS_VNEXPRESS_RSS_AUTHORIZED",
    }
    unknown = sorted(enabled - authorization_env.keys())
    if unknown:
        return "FAIL", "unsupported media provider(s): " + ", ".join(unknown)
    if not any(_env_true(authorization_env[name]) for name in enabled & authorization_env.keys()):
        return "SKIP", "authorization locked"
    try:
        payload = _media_status_payload(live=live)
        return _media_status_outcome(payload)
    except Exception as exc:  # noqa: BLE001
        return "FAIL", _safe_runtime_error(exc)


def _check_macro(config: dict, *, live: bool = False) -> tuple[str, str]:
    settings = config.get("vn_macro") or {}
    if not bool(settings.get("enabled", False)):
        return "SKIP", "disabled"
    try:
        payload = _macro_status_payload(live=live)
        return _macro_status_outcome(payload)
    except Exception as exc:  # noqa: BLE001
        return "FAIL", _safe_runtime_error(exc)


@app.command()
def doctor(
    ctx: typer.Context,
    live_social: bool = typer.Option(
        False,
        "--live-social",
        help="Opt in to a live FireAnt connectivity/authentication check.",
    ),
    live_media: bool = typer.Option(
        False,
        "--live-media",
        help="Opt in to live CafeF/VnExpress RSS connectivity checks.",
    ),
    live_macro: bool = typer.Option(
        False,
        "--live-macro",
        help="Opt in to live NSO/SBV connectivity and parser checks.",
    ),
) -> None:
    """Check GX/configuration without an LLM; external sources stay offline by default."""
    from tradingagents.graph.stage_session import default_runs_dir

    checks = {
        # Absence is normal on a fresh install; the first atomic session save
        # creates the hierarchy. Fail only when an existing target is not a dir.
        "runs_dir": (
            not default_runs_dir().exists() or default_runs_dir().is_dir(),
            str(default_runs_dir()),
        ),
        "gx": _check_gx(ctx.obj["config"]),
    }
    failed = False
    for role, (ok, detail) in _check_llms(ctx.obj["config"]).items():
        typer.echo(f"{'OK' if ok else 'FAIL'} llm {role}: {detail}")
        failed = failed or not ok
    for name, (ok, detail) in checks.items():
        typer.echo(f"{'OK' if ok else 'FAIL'} {name}: {detail}")
        failed = failed or not ok
    social_outcome, social_detail = _check_social(
        ctx.obj["config"], live=live_social
    )
    typer.echo(f"{social_outcome} social: {social_detail}")
    failed = failed or social_outcome == "FAIL"
    media_outcome, media_detail = _check_media(
        ctx.obj["config"], live=live_media
    )
    typer.echo(f"{media_outcome} media: {media_detail}")
    failed = failed or media_outcome == "FAIL"
    macro_outcome, macro_detail = _check_macro(
        ctx.obj["config"], live=live_macro
    )
    typer.echo(f"{macro_outcome} macro: {macro_detail}")
    failed = failed or macro_outcome == "FAIL"
    if failed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
