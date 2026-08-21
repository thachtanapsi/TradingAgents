"""Background execution and safe presentation models for the local GX UI.

The browser never receives provider configuration, environment variables,
LangChain messages, raw social/media evidence, or complete session JSON.  It
only sees allowlisted analyst reports, aggregate progress, and chart points
loaded through the same GX transport as the run.
"""

from __future__ import annotations

import math
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_ANALYST_STAGES = ("market", "sentiment", "news", "fundamentals")
_PIPELINE_STAGES = (*_ANALYST_STAGES, "research", "trader", "risk")
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,15}$")
_MARKDOWN_FIELD_RE = re.compile(
    r"(?:\*\*)?{label}(?:\*\*)?\s*:\s*(?:\*\*)?([^\n*]+)",
    re.IGNORECASE,
)
_RATING_FIELD_RE = re.compile(
    r"(?:\*\*)?rating(?:\*\*)?\s*[:\-]\s*(?:\*\*)?([^\n*]+)",
    re.IGNORECASE,
)
_TARGET_NULLISH = {"", "n/a", "na", "none", "null", "unavailable"}
_TARGET_LEGACY_REASON = (
    "Target cũ được ẩn vì chưa có trạng thái, đơn vị tiền tệ "
    "và cơ sở định giá đã xác thực."
)
_TARGET_MISSING_REASON = (
    "Portfolio Manager chưa cung cấp giá mục tiêu theo contract đã xác thực."
)
_TARGET_INVALID_REASON = (
    "Giá mục tiêu bị từ chối vì các trường canonical không đầy đủ "
    "hoặc không hợp lệ."
)
_TARGET_VALIDATED_REASON = "Đã xác thực theo contract giá mục tiêu."
_SECRET_ENV_NAMES = tuple(dict.fromkeys((
    *(name for name in PROVIDER_API_KEY_ENV.values() if name),
    "OPENAI_API_KEY",
    "TRADINGAGENTS_QUICK_LLM_API_KEY",
    "TRADINGAGENTS_DEEP_LLM_API_KEY",
    "FIREANT_ACCESS_TOKEN",
    "FIREANT_ARCHIVE_ENCRYPTION_KEY",
    "VN_MEDIA_ARCHIVE_ENCRYPTION_KEY",
    "GX_ANALYSIS_DATA_API_KEY",
    "GX_MARKET_INFO_TV_TOKEN",
    "GX_MARKET_INFO_DATABASE_URL",
    "GX_MARKET_INFO_POSTGRES_DSN",
    "GX_TRADINGVIEW_API_KEY",
    "FRED_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
)))


@dataclass(frozen=True)
class RunRequest:
    """Validated browser request for a fresh full GX run."""

    ticker: str
    mode: str
    analysis_date: str | None = None
    collect_evidence: bool = False
    confirm_hosted_cost: bool = False

    @classmethod
    def from_payload(cls, payload: Any) -> RunRequest:
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        ticker = str(payload.get("ticker") or "").strip().upper()
        if not _TICKER_RE.fullmatch(ticker):
            raise ValueError("Ticker must contain 1-16 letters, digits, '.', '_' or '-'.")
        mode = str(payload.get("mode") or "").strip().lower()
        if mode not in {"close", "live"}:
            raise ValueError("Mode must be 'close' or 'live'.")
        collect_evidence = payload.get("collect_evidence", False)
        if not isinstance(collect_evidence, bool):
            raise ValueError("collect_evidence must be true or false.")
        confirm_hosted_cost = payload.get("confirm_hosted_cost", False)
        if not isinstance(confirm_hosted_cost, bool):
            raise ValueError("confirm_hosted_cost must be true or false.")
        raw_date = payload.get("analysis_date")
        if mode == "close":
            if not isinstance(raw_date, str):
                raise ValueError("A close run requires analysis_date in YYYY-MM-DD format.")
            try:
                parsed_date = date.fromisoformat(raw_date)
            except ValueError:
                raise ValueError("analysis_date must use YYYY-MM-DD.") from None
            if parsed_date.isoformat() != raw_date:
                raise ValueError("analysis_date must use YYYY-MM-DD.")
            if collect_evidence:
                raise ValueError("Evidence collection is available only in live mode.")
            analysis_date = raw_date
        else:
            if raw_date not in {None, ""}:
                raise ValueError("A live run freezes its own date; omit analysis_date.")
            analysis_date = None
        return cls(
            ticker=ticker,
            mode=mode,
            analysis_date=analysis_date,
            collect_evidence=collect_evidence,
            confirm_hosted_cost=confirm_hosted_cost,
        )


@dataclass
class DashboardJob:
    job_id: str
    request: RunRequest
    status: str = "queued"
    current_stage: str | None = None
    analysis_cutoff: str | None = None
    run_id: str | None = None
    stage_status: dict[str, str] = field(
        default_factory=lambda: dict.fromkeys(_PIPELINE_STAGES, "not_run")
    )
    result: dict[str, Any] | None = None
    evidence_collection: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.job_id,
                "ticker": self.request.ticker,
                "status": self.status,
                "current_stage": self.current_stage,
                "analysis_mode": self.request.mode,
                "analysis_cutoff": self.analysis_cutoff,
                "run_id": self.run_id,
                "stage_status": dict(self.stage_status),
                "progress": _progress_rows(self.stage_status, self.current_stage),
                "evidence_collection": deepcopy(self.evidence_collection),
                "warnings": list(self.warnings),
                "error": self.error,
                "result": deepcopy(self.result),
            }


def _progress_rows(statuses: dict[str, str], current: str | None) -> list[dict[str, str]]:
    rows = (
        ("market", "Market data & technical", "market"),
        ("sentiment", "Sentiment", "sentiment"),
        ("news", "News & macro", "news"),
        ("fundamentals", "Fundamentals", "fundamentals"),
        ("research", "Bull / Bear research", "research"),
        ("trader", "Trader", "trader"),
        ("risk", "Risk & portfolio", "risk"),
    )
    result = []
    for key, label, stage in rows:
        status = statuses.get(stage, "not_run")
        if current == stage and status not in {"completed", "unavailable", "failed"}:
            status = "running"
        result.append({"key": key, "label": label, "status": status})
    return result


def _redact_public_text(value: str) -> str:
    """Remove credentials while preserving ordinary public citation URLs."""
    message = value
    for name in _SECRET_ENV_NAMES:
        secret = os.environ.get(name)
        if secret:
            message = message.replace(secret, "[redacted]")
    # Include custom provider variables without making the browser sanitizer
    # depend on a complete static list of integrations.
    for name, secret in os.environ.items():
        if (
            secret
            and len(secret) >= 6
            and any(
                marker in name.upper()
                for marker in ("KEY", "TOKEN", "PASSWORD", "SECRET")
            )
        ):
            message = message.replace(secret, "[redacted]")
    # Sessions outlive environment rotation. Redact recognizable credential
    # formats even when the original secret is no longer present in os.environ.
    message = re.sub(
        r"\bsk-(?:proj-|ant-[A-Za-z0-9_-]*-)?[A-Za-z0-9_-]{16,}\b",
        "[redacted]",
        message,
        flags=re.IGNORECASE,
    )
    message = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[redacted]", message)
    message = re.sub(r"\bAIza[A-Za-z0-9_-]{30,}\b", "[redacted]", message)
    message = re.sub(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "[redacted]", message)
    message = re.sub(
        r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b",
        "[redacted]",
        message,
    )
    message = re.sub(
        r"(?i)postgres(?:ql)?://[^\s]+",
        "[redacted database endpoint]",
        message,
    )
    message = re.sub(
        r"(?i)(https?://)[^/\s]+@",
        r"\1[redacted]@",
        message,
    )
    message = re.sub(
        r"(?i)(\b(?:authorization\s*[:=]\s*)?bearer\s+)[^\s,;]+",
        r"\1[redacted]",
        message,
    )
    message = re.sub(
        r"(?i)([?&](?:access_?token|token|api_?key|key|password|secret)=)[^&\s]+",
        r"\1[redacted]",
        message,
    )
    message = re.sub(
        r"(?i)(\b(?:api[_-]?key|access_?token|token|password|authorization|secret|credential)\s*[=:]\s*)[^\s,;&]+",
        r"\1[redacted]",
        message,
    )
    return message


def _report_text(state: dict[str, Any], key: str) -> str | None:
    value = state.get(key)
    if not isinstance(value, str):
        return None
    value = _redact_public_text(value.strip())
    return value[:100_000] if value else None


def _nested_report(state: dict[str, Any], container: str, key: str) -> str | None:
    value = state.get(container)
    if not isinstance(value, dict):
        return None
    nested = value.get(key)
    if not isinstance(nested, str):
        return None
    nested = _redact_public_text(nested.strip())
    return nested[:100_000] if nested else None


def _markdown_field(report: str | None, label: str) -> str | None:
    if not report:
        return None
    pattern = re.compile(_MARKDOWN_FIELD_RE.pattern.format(label=re.escape(label)), re.I)
    match = pattern.search(report)
    if not match:
        return None
    value = match.group(1).strip().rstrip(". ")
    return value[:120] or None


def _recommendation(report: str | None) -> tuple[str | None, str | None]:
    match = _RATING_FIELD_RE.search(report) if report else None
    detailed = match.group(1).strip().rstrip(". ")[:120] if match else None
    if detailed is None:
        return None, None
    normalized = detailed.lower()
    if normalized in {"buy", "overweight"}:
        return "BUY", detailed
    if normalized == "hold":
        return "HOLD", detailed
    if normalized in {"sell", "underweight"}:
        return "SELL", detailed
    return None, detailed


def _numeric_field(report: str | None, label: str) -> float | None:
    value = _markdown_field(report, label)
    if value is None:
        return None
    # Structured agent output uses an invariant decimal point. Do not guess at
    # localized thousands separators or extract arbitrary numbers from prose.
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _price_target_summary(report: str | None) -> dict[str, Any]:
    """Return only a price target covered by the canonical PM contract.

    Historical reports may contain a bare ``Price Target`` line with no unit
    or validation status.  Such values are deliberately hidden: guessing that
    ``63.3`` means ``63,300 VND`` would silently rescale an investment output.
    """

    unavailable: dict[str, Any] = {
        "target_price": None,
        "target_price_status": "unavailable",
        "target_price_currency": None,
        "target_price_reason": _TARGET_MISSING_REASON,
    }
    if not report:
        return unavailable

    status = _markdown_field(report, "Price Target Status")
    currency = _markdown_field(report, "Price Target Currency")
    rationale = _markdown_field(report, "Price Target Rationale")
    unavailable_reason = _markdown_field(
        report, "Price Target Unavailable Reason"
    )
    has_bare_numeric_target = _numeric_field(report, "Price Target") is not None

    if status is None:
        if has_bare_numeric_target:
            unavailable["target_price_reason"] = _TARGET_LEGACY_REASON
        return unavailable

    normalized_status = status.strip().casefold()
    normalized_rationale = (rationale or "").strip()
    normalized_unavailable_reason = (unavailable_reason or "").strip()
    if normalized_status == "unavailable":
        if normalized_unavailable_reason.casefold() not in _TARGET_NULLISH:
            unavailable["target_price_reason"] = normalized_unavailable_reason[:120]
        # Compatibility with reports produced during the short transition to
        # the new contract, where the unavailable reason occupied Rationale.
        elif normalized_rationale.casefold() not in _TARGET_NULLISH:
            unavailable["target_price_reason"] = normalized_rationale[:120]
        return unavailable
    if normalized_status != "available":
        unavailable["target_price_reason"] = _TARGET_INVALID_REASON
        return unavailable

    target = _numeric_field(report, "Price Target")
    normalized_currency = (currency or "").strip().upper()
    if (
        target is None
        or target <= 0
        or not re.fullmatch(r"[A-Z]{2,12}", normalized_currency)
        or normalized_rationale.casefold() in _TARGET_NULLISH
        or unavailable_reason is None
        or normalized_unavailable_reason.casefold() not in _TARGET_NULLISH
    ):
        unavailable["target_price_reason"] = _TARGET_INVALID_REASON
        return unavailable

    return {
        "target_price": target,
        "target_price_status": "available",
        "target_price_currency": normalized_currency,
        # Overview is a compact status surface. The full valuation rationale
        # remains in Final Analysis / Quyết định cuối and must not be truncated
        # into an incoherent fragment inside the target card.
        "target_price_reason": _TARGET_VALIDATED_REASON,
    }


def build_dashboard_result(session: Any, chart: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Create the browser's allowlisted view model from a StageSession.

    Missing fields remain ``None``. In particular, the UI never turns a
    sentiment score into portfolio conviction and never infers risk from an
    action or target price.
    """
    state = session.state if isinstance(getattr(session, "state", None), dict) else {}
    final_decision = _report_text(state, "final_trade_decision")
    sentiment_report = _report_text(state, "sentiment_report")
    action, detailed_rating = _recommendation(final_decision)
    target_summary = _price_target_summary(final_decision)
    # PortfolioDecision has no confidence field. Sentiment confidence describes
    # evidence classification, not confidence in the final trade action, so it
    # must not be relabelled or converted to a percentage in the summary card.
    confidence = None
    # PortfolioDecision v1 has no typed final risk enum. Free-text fallback may
    # happen to contain a "Risk" heading, but treating that as a validated card
    # would fabricate a contract the pipeline does not currently provide.
    risk = None

    tabs = {
        "technical": [
            {"title": "Market & technical", "content": _report_text(state, "market_report")}
        ],
        "fundamental": [
            {"title": "Fundamental analysis", "content": _report_text(state, "fundamentals_report")}
        ],
        "news": [
            {"title": "News & macro", "content": _report_text(state, "news_report")},
            {"title": "Sentiment", "content": sentiment_report},
        ],
        "agents": [
            {
                "title": "Bull researcher",
                "content": _nested_report(
                    state, "investment_debate_state", "bull_history"
                ),
            },
            {
                "title": "Bear researcher",
                "content": _nested_report(
                    state, "investment_debate_state", "bear_history"
                ),
            },
            {"title": "Research manager", "content": _report_text(state, "investment_plan")},
            {"title": "Trader", "content": _report_text(state, "trader_investment_plan")},
        ],
        "risk": [
            {
                "title": "Aggressive risk analyst",
                "content": _nested_report(
                    state, "risk_debate_state", "aggressive_history"
                ),
            },
            {
                "title": "Neutral risk analyst",
                "content": _nested_report(
                    state, "risk_debate_state", "neutral_history"
                ),
            },
            {
                "title": "Conservative risk analyst",
                "content": _nested_report(
                    state, "risk_debate_state", "conservative_history"
                ),
            },
            {"title": "Portfolio manager", "content": final_decision},
        ],
    }
    return {
        "ticker": str(session.ticker),
        "analysis_date": str(session.analysis_date),
        "analysis_mode": str(session.analysis_mode),
        "analysis_cutoff": str(session.analysis_cutoff),
        "summary": {
            "recommendation": action,
            "detailed_rating": detailed_rating,
            "confidence": confidence,
            "confidence_source": "sentiment" if confidence else None,
            **target_summary,
            "time_horizon": _markdown_field(final_decision, "Time Horizon"),
            "risk": risk,
        },
        "chart": chart or [],
        "chart_source": "gx_market_info" if chart else None,
        "chart_currency": "VND" if chart else None,
        "tabs": tabs,
        "final_analysis": final_decision,
    }


def _safe_error(exc: BaseException) -> str:
    message = _redact_public_text(str(exc) or type(exc).__name__)
    # Operational errors do not need to expose endpoints at all. Reports use
    # the narrower sanitizer above so legitimate source citations survive.
    message = re.sub(
        r"(?i)(?:postgres(?:ql)?://|https?://)[^\s]+",
        "[redacted endpoint]",
        message,
    )
    return message[:500]


_COLLECTION_FIELDS = {
    "provider",
    "ticker",
    "status",
    "reason",
    "fetch_id",
    "articles_seen",
    "posts_seen",
    "versions_inserted",
    "retention_skips",
    "pages",
    "truncated",
    "warnings",
}


def _public_collection(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    elif hasattr(value, "__dataclass_fields__"):
        value = {name: getattr(value, name) for name in value.__dataclass_fields__}
    if isinstance(value, (list, tuple)):
        return [_public_collection(item) for item in value]
    if not isinstance(value, dict):
        return None
    if value and set(value).issubset({"media", "social"}):
        return {str(key): _public_collection(item) for key, item in value.items()}
    public: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _COLLECTION_FIELDS:
            continue
        if key == "warnings":
            items = item if isinstance(item, (list, tuple)) else [item]
            public[key] = [_safe_error(RuntimeError(str(entry))) for entry in items[:25]]
        elif isinstance(item, str):
            public[key] = _safe_error(RuntimeError(item))
        elif isinstance(item, (bool, int, float)) or item is None:
            public[key] = item
    return public


class DashboardService:
    """Own local UI jobs and execute at most one graph at a time."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        runner_factory: Callable[[dict[str, Any]], Any] | None = None,
        gx_client_factory: Callable[[dict[str, Any]], Any] | None = None,
        evidence_collector: Callable[[str], dict[str, Any]] | None = None,
        now: Callable[[], datetime] | None = None,
        history_repository: Any | None = None,
    ) -> None:
        self.config = deepcopy(config)
        self._runner_factory = runner_factory or self._default_runner
        self._gx_client_factory = gx_client_factory or self._default_gx_client
        self._evidence_collector = evidence_collector or self._default_collect_evidence
        self._now = now or (lambda: datetime.now(_VN_TZ))
        self._jobs: dict[str, DashboardJob] = {}
        self._jobs_lock = threading.Lock()
        # Dataflow configuration is process-global. Serial execution prevents
        # concurrent jobs from interleaving vendor/LLM profile state.
        self._run_lock = threading.Lock()
        if history_repository is None:
            from tradingagents.ui.history import SessionHistoryRepository

            history_repository = SessionHistoryRepository(
                active_run_ids=self._active_history_run_ids
            )
        self._history_repository = history_repository
        self._llm_profiles, self._hosted_cost_confirmation_required = (
            self._resolve_public_llm_profiles()
        )

    def _resolve_public_llm_profiles(self) -> tuple[dict[str, dict[str, Any]], bool]:
        from tradingagents.llm_clients.profiles import (
            is_local_llm_profile,
            resolve_llm_profile,
        )

        profiles: dict[str, dict[str, Any]] = {}
        hosted_required = False
        for role in ("quick", "deep"):
            profile = resolve_llm_profile(self.config, role)
            local = is_local_llm_profile(profile)
            profiles[role] = {
                "provider": profile.provider,
                "model": profile.model,
                "local": local,
            }
            hosted_required = hosted_required or not local
        return profiles, hosted_required

    def public_info(self) -> dict[str, Any]:
        """Return non-secret run-cost context shown before the explicit click."""
        now = self._vietnam_now()
        latest_close = (
            now.date() if now.time() >= time(15, 0) else now.date() - timedelta(days=1)
        )
        return {
            "llm": deepcopy(self._llm_profiles),
            "hosted_cost_confirmation_required": self._hosted_cost_confirmation_required,
            "latest_close_date": latest_close.isoformat(),
        }

    def _vietnam_now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("UI clock must return a timezone-aware timestamp.")
        return now.astimezone(_VN_TZ)

    def _validate_close_cutoff(self, request: RunRequest) -> None:
        if request.mode != "close":
            return
        requested = datetime.combine(
            date.fromisoformat(str(request.analysis_date)),
            time(15, 0),
            tzinfo=_VN_TZ,
        )
        if requested > self._vietnam_now():
            raise ValueError(
                "The requested close is not completed yet; choose an earlier "
                "date or use live mode."
            )

    @staticmethod
    def _default_runner(config: dict[str, Any]) -> Any:
        from tradingagents.graph.stage_runner import TradingAgentsStageRunner

        return TradingAgentsStageRunner(config=config)

    @staticmethod
    def _default_gx_client(config: dict[str, Any]) -> Any:
        from tradingagents.dataflows.gx_market_info import GxMarketInfoClient

        return GxMarketInfoClient.from_config(config)

    @staticmethod
    def _default_collect_evidence(ticker: str) -> dict[str, Any]:
        from tradingagents.dataflows.vietnam_media import (
            create_vietnam_media_service_from_env,
        )
        from tradingagents.dataflows.vietnam_social import (
            create_vietnam_social_service_from_env,
        )

        results: dict[str, Any] = {}
        for lane, factory in (
            ("media", create_vietnam_media_service_from_env),
            ("social", create_vietnam_social_service_from_env),
        ):
            try:
                results[lane] = _public_collection(factory().collect_once(ticker=ticker))
            except Exception as exc:  # noqa: BLE001 - evidence lanes fail independently
                results[lane] = {"status": "failed", "warnings": [_safe_error(exc)]}
        return results

    def start_run(self, payload: Any, *, background: bool = True) -> dict[str, Any]:
        request = RunRequest.from_payload(payload)
        if self._hosted_cost_confirmation_required and not request.confirm_hosted_cost:
            raise ValueError(
                "Confirm hosted LLM usage before starting this analysis."
            )
        self._validate_close_cutoff(request)
        job = DashboardJob(job_id=uuid.uuid4().hex, request=request)
        with self._jobs_lock:
            if any(item.status in {"queued", "running"} for item in self._jobs.values()):
                raise ValueError("Another analysis is already queued or running.")
            # Keep operational state bounded without deleting active jobs.
            finished = [
                job_id
                for job_id, item in self._jobs.items()
                if item.status in {"completed", "failed"}
            ]
            for old_id in finished[:-49]:
                self._jobs.pop(old_id, None)
            self._jobs[job.job_id] = job
        if background:
            threading.Thread(
                target=self._execute,
                args=(job,),
                name=f"tradingagents-ui-{job.job_id[:8]}",
                daemon=True,
            ).start()
        else:
            self._execute(job)
        return job.public()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            return None
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        return job.public() if job is not None else None

    def list_history(self, params: Mapping[str, str]) -> dict[str, Any]:
        """Return a filtered, read-only view of durable research runs."""
        if not isinstance(params, Mapping):
            raise ValueError("History filters must be a mapping.")
        return self._history_repository.list_history(params)

    def get_history(self, history_id: str) -> dict[str, Any] | None:
        """Return one allowlisted history detail without resuming the run."""
        return self._history_repository.get_history(history_id)

    def _active_history_run_ids(self) -> set[str]:
        with self._jobs_lock:
            return {
                item.run_id
                for item in self._jobs.values()
                if item.run_id and item.status in {"queued", "running"}
            }

    def _execute(self, job: DashboardJob) -> None:
        with self._run_lock:
            try:
                self._set_job(job, status="running")
                request = job.request
                if request.collect_evidence:
                    collected = self._evidence_collector(request.ticker)
                    self._set_job(job, evidence_collection=_public_collection(collected))

                if request.mode == "live":
                    cutoff = self._vietnam_now()
                    analysis_date = cutoff.date().isoformat()
                    analysis_cutoff: datetime | None = cutoff
                else:
                    analysis_date = str(request.analysis_date)
                    analysis_cutoff = None

                runner = self._runner_factory(deepcopy(self.config))
                session = runner.create_session(
                    request.ticker,
                    analysis_date,
                    selected_analysts=_ANALYST_STAGES,
                    asset_type="stock",
                    analysis_mode=request.mode,
                    analysis_cutoff=analysis_cutoff,
                )
                path = session.save()
                self._set_job(
                    job,
                    analysis_cutoff=session.analysis_cutoff,
                    run_id=session.run_id,
                )

                chart: list[dict[str, Any]] = []
                analyst_failures = False
                for stage in _PIPELINE_STAGES:
                    if (
                        stage in {"research", "trader", "risk"}
                        and analyst_failures
                        and not any(
                            session.stage_status.get(name) == "completed"
                            for name in _ANALYST_STAGES
                        )
                    ):
                        # Research only needs one completed analyst. If every
                        # selected analyst failed, StageRunner will reject it;
                        # still attempt it when at least one report survived.
                        break
                    self._set_stage(job, stage, "running")
                    try:
                        runner.run_stage_to(session, stage, session_path=str(path))
                    except Exception as exc:  # noqa: BLE001 - status is already persisted
                        status = session.stage_status.get(stage, "failed")
                        self._set_stage(job, stage, status)
                        self._add_warning(job, f"{stage}: {_safe_error(exc)}")
                        if stage in _ANALYST_STAGES:
                            analyst_failures = True
                            continue
                        break
                    status = session.stage_status.get(stage, "completed")
                    self._set_stage(job, stage, status)
                    if stage == "market" and status == "completed":
                        chart = self._load_chart(session)

                result = build_dashboard_result(session, chart)
                final_ready = session.stage_status.get("risk") == "completed"
                self._set_job(
                    job,
                    current_stage=None,
                    result=result,
                    status="completed" if final_ready else "failed",
                    error=None if final_ready else "The pipeline stopped before a final decision.",
                )
            except Exception as exc:  # noqa: BLE001 - never return tracebacks or credentials
                self._set_job(job, status="failed", current_stage=None, error=_safe_error(exc))

    def _load_chart(self, session: Any) -> list[dict[str, Any]]:
        try:
            cutoff = datetime.fromisoformat(str(session.analysis_cutoff))
            start = cutoff.date() - timedelta(days=270)
            client = self._gx_client_factory(deepcopy(self.config))
            frame = client.get_ohlcv(
                session.ticker,
                start.isoformat(),
                session.analysis_cutoff,
                "1D",
            )
            if frame is None or getattr(frame, "empty", True):
                return []
            date_column = "Date" if "Date" in frame.columns else frame.columns[0]
            close_column = "Close" if "Close" in frame.columns else None
            if close_column is None:
                return []
            points: list[dict[str, Any]] = []
            for _, row in frame.tail(180).iterrows():
                raw_date = row[date_column]
                raw_close = row[close_column]
                try:
                    close = float(raw_close)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(close):
                    continue
                day = raw_date.isoformat() if hasattr(raw_date, "isoformat") else str(raw_date)
                points.append({"date": day[:10], "close": close})
            return points
        except Exception:
            # A chart is supplemental. The market report remains authoritative
            # and a chart transport error must not expose infrastructure detail.
            return []

    @staticmethod
    def _set_job(job: DashboardJob, **changes: Any) -> None:
        with job._lock:
            for key, value in changes.items():
                setattr(job, key, value)

    @staticmethod
    def _set_stage(job: DashboardJob, stage: str, status: str) -> None:
        with job._lock:
            job.current_stage = stage if status == "running" else None
            job.stage_status[stage] = "not_run" if status == "running" else status

    @staticmethod
    def _add_warning(job: DashboardJob, warning: str) -> None:
        with job._lock:
            job.warnings.append(warning[:500])
