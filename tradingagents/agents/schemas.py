"""Pydantic schemas used by agents that produce structured output.

The framework's primary artifact is still prose: each agent's natural-language
reasoning is what users read in the saved markdown reports and what the
downstream agents read as context.  Structured output is layered onto the
three decision-making agents (Research Manager, Trader, Portfolio Manager)
so that:

- Their outputs follow consistent section headers across runs and providers
- Each provider's native structured-output mode is used (json_schema for
  OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic)
- Schema field descriptions become the model's output instructions, freeing
  the prompt body to focus on context and the rating-scale guidance
- A render helper turns the parsed Pydantic instance back into the same
  markdown shape the rest of the system already consumes, so display,
  memory log, and saved reports keep working unchanged
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# LLMs sometimes write a placeholder string ("None", "N/A", ...) into an optional
# numeric field instead of omitting it. Coerce those to None so the structured
# call validates instead of erroring (#1058). Pydantic still parses real numeric
# strings ("189.5") to float.
_NULLISH_FLOAT = {
    "",
    "none",
    "n/a",
    "na",
    "null",
    "nil",
    "-",
    "tbd",
    "unknown",
    "unavailable",
}


def _coerce_optional_float(value):
    if isinstance(value, str) and value.strip().lower() in _NULLISH_FLOAT:
        return None
    return value


# ---------------------------------------------------------------------------
# Shared rating types
# ---------------------------------------------------------------------------


class PortfolioRating(str, Enum):
    """5-tier rating used by the Research Manager and Portfolio Manager."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


class TraderAction(str, Enum):
    """3-tier transaction direction used by the Trader.

    The Trader's job is to translate the Research Manager's investment plan
    into a concrete transaction proposal: should the desk execute a Buy, a
    Sell, or sit on Hold this round.  Position sizing and the nuanced
    Overweight / Underweight calls happen later at the Portfolio Manager.
    """

    BUY = "Buy"
    HOLD = "Hold"
    SELL = "Sell"


# ---------------------------------------------------------------------------
# Research Manager
# ---------------------------------------------------------------------------


class ResearchPlan(BaseModel):
    """Structured investment plan produced by the Research Manager.

    Hand-off to the Trader: the recommendation pins the directional view,
    the rationale captures which side of the bull/bear debate carried the
    argument, and the strategic actions translate that into concrete
    instructions the trader can execute against.
    """

    recommendation: PortfolioRating = Field(
        description=(
            "The investment recommendation. Exactly one of Buy / Overweight / "
            "Hold / Underweight / Sell. Reserve Hold for situations where the "
            "evidence on both sides is genuinely balanced; otherwise commit to "
            "the side with the stronger arguments."
        ),
    )
    rationale: str = Field(
        description=(
            "Conversational summary of the key points from both sides of the "
            "debate, ending with which arguments led to the recommendation. "
            "Speak naturally, as if to a teammate."
        ),
    )
    strategic_actions: str = Field(
        description=(
            "Concrete steps for the trader to implement the recommendation, "
            "including position sizing guidance consistent with the rating."
        ),
    )


def render_research_plan(plan: ResearchPlan) -> str:
    """Render a ResearchPlan to markdown for storage and the trader's prompt context."""
    return "\n".join([
        f"**Recommendation**: {plan.recommendation.value}",
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
    ])


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------


class TraderProposal(BaseModel):
    """Structured transaction proposal produced by the Trader.

    The trader reads the Research Manager's investment plan and the analyst
    reports, then turns them into a concrete transaction: what action to
    take, the reasoning that justifies it, and the practical levels for
    entry, stop-loss, and sizing.
    """

    action: TraderAction = Field(
        description="The transaction direction. Exactly one of Buy / Hold / Sell.",
    )
    reasoning: str = Field(
        description=(
            "The case for this action, anchored in the analysts' reports and "
            "the research plan. Two to four sentences."
        ),
    )
    entry_price: float | None = Field(
        default=None,
        description="Optional entry price target in the instrument's quote currency.",
    )
    stop_loss: float | None = Field(
        default=None,
        description="Optional stop-loss price in the instrument's quote currency.",
    )
    position_sizing: str | None = Field(
        default=None,
        description="Optional sizing guidance, e.g. '5% of portfolio'.",
    )

    @field_validator("entry_price", "stop_loss", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)


def render_trader_proposal(proposal: TraderProposal) -> str:
    """Render a TraderProposal to markdown.

    The trailing ``FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**`` line is
    preserved for backward compatibility with the analyst stop-signal text
    and any external code that greps for it.
    """
    parts = [
        f"**Action**: {proposal.action.value}",
        "",
        f"**Reasoning**: {proposal.reasoning}",
    ]
    if proposal.entry_price is not None:
        parts.extend(["", f"**Entry Price**: {proposal.entry_price}"])
    if proposal.stop_loss is not None:
        parts.extend(["", f"**Stop Loss**: {proposal.stop_loss}"])
    if proposal.position_sizing:
        parts.extend(["", f"**Position Sizing**: {proposal.position_sizing}"])
    parts.extend([
        "",
        f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value.upper()}**",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------


class PortfolioDecision(BaseModel):
    """Structured output produced by the Portfolio Manager.

    The model fills every field as part of its primary LLM call; no separate
    extraction pass is required. Field descriptions double as the model's
    output instructions, so the prompt body only needs to convey context and
    the rating-scale guidance.
    """

    rating: PortfolioRating = Field(
        description=(
            "The final position rating. Exactly one of Buy / Overweight / Hold / "
            "Underweight / Sell, picked based on the analysts' debate."
        ),
    )
    executive_summary: str = Field(
        description=(
            "A concise action plan covering entry strategy, position sizing, "
            "key risk levels, and time horizon. Two to four sentences."
        ),
    )
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in specific evidence from the analysts' "
            "debate. If prior lessons are referenced in the prompt context, "
            "incorporate them; otherwise rely solely on the current analysis."
        ),
    )
    price_target: float | None = Field(
        ...,
        description=(
            "Required nullable target price in the instrument's quote currency. "
            "Return null when the supplied evidence cannot support a defensible target."
        ),
    )
    price_target_currency: str | None = Field(
        ...,
        description=(
            "Required nullable ISO-style quote currency such as VND or USD. It must "
            "be present whenever price_target is numeric."
        ),
    )
    price_target_rationale: str | None = Field(
        ...,
        description=(
            "Required nullable concise valuation basis for a numeric target. It must "
            "be null when price_target is null."
        ),
    )
    price_target_unavailable_reason: str | None = Field(
        ...,
        description=(
            "Required nullable reason a defensible target is unavailable. It must be "
            "present exactly when price_target is null."
        ),
    )
    time_horizon: str | None = Field(
        default=None,
        description="Optional recommended holding period, e.g. '3-6 months'.",
    )

    @field_validator("price_target", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)

    @field_validator("price_target")
    @classmethod
    def _positive_finite_price_target(cls, value):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("price_target must be a positive finite number or null")
        return value

    @field_validator("price_target_currency", mode="before")
    @classmethod
    def _normalize_price_target_currency(cls, value):
        if value is None:
            return None
        text = str(value).strip()
        if text.lower() in _NULLISH_FLOAT:
            return None
        if not text.isalpha() or not 2 <= len(text) <= 12:
            raise ValueError("price_target_currency must be an alphabetic currency code")
        return text.upper()

    @field_validator("price_target_rationale", "price_target_unavailable_reason")
    @classmethod
    def _normalize_target_explanation(cls, value):
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("price-target explanation must not be empty")
        return text

    @model_validator(mode="after")
    def _target_contract_is_complete(self):
        if self.price_target is not None:
            if self.price_target_currency is None:
                raise ValueError("numeric price_target requires price_target_currency")
            if self.price_target_rationale is None:
                raise ValueError("numeric price_target requires price_target_rationale")
            if self.price_target_unavailable_reason is not None:
                raise ValueError(
                    "numeric price_target cannot have price_target_unavailable_reason"
                )
        else:
            if self.price_target_rationale is not None:
                raise ValueError("null price_target cannot have price_target_rationale")
            if self.price_target_unavailable_reason is None:
                raise ValueError(
                    "null price_target requires price_target_unavailable_reason"
                )
        return self


def render_pm_decision(decision: PortfolioDecision) -> str:
    """Render a PortfolioDecision back to the markdown shape the rest of the system expects.

    Memory log, CLI display, and saved report files all read this markdown,
    so the rendered output preserves the exact section headers (``**Rating**``,
    ``**Executive Summary**``, ``**Investment Thesis**``) that downstream
    parsers and the report writers already handle.
    """
    parts = [
        f"**Rating**: {decision.rating.value}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
    ]
    target_status = "Available" if decision.price_target is not None else "Unavailable"
    parts.extend(
        [
            "",
            f"**Price Target Status**: {target_status}",
            "",
            f"**Price Target**: {decision.price_target if decision.price_target is not None else 'Unavailable'}",
            "",
            f"**Price Target Currency**: {decision.price_target_currency or 'Unavailable'}",
            "",
            f"**Price Target Rationale**: {decision.price_target_rationale or 'Unavailable'}",
            "",
            "**Price Target Unavailable Reason**: "
            f"{decision.price_target_unavailable_reason or 'Unavailable'}",
        ]
    )
    if decision.time_horizon:
        parts.extend(["", f"**Time Horizon**: {decision.time_horizon}"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Sentiment Analyst
# ---------------------------------------------------------------------------


class SentimentBand(str, Enum):
    """Discrete sentiment direction produced by the Sentiment Analyst.

    Six tiers keep the signal granular enough to be actionable while remaining
    small enough for every provider to map reliably from its JSON output.
    """

    BULLISH = "Bullish"
    MILDLY_BULLISH = "Mildly Bullish"
    NEUTRAL = "Neutral"
    MIXED = "Mixed"
    MILDLY_BEARISH = "Mildly Bearish"
    BEARISH = "Bearish"


SocialStatus = Literal["available", "partial", "unavailable", "disabled"]
SentimentConfidence = Literal["low", "medium", "high"]


class EvidenceSignal(BaseModel):
    """A typed sentiment signal derived from one independent evidence lane.

    ``unavailable`` and ``disabled`` deliberately carry no direction.  This is
    the important distinction between an absent source and genuinely balanced
    evidence: only the latter may be labelled ``Neutral`` with a score of 5.
    """

    status: SocialStatus
    provider: str = Field(min_length=1)
    band: SentimentBand | None = None
    score: float | None = Field(default=None, ge=0.0, le=10.0)
    confidence: SentimentConfidence | None = None
    sample_size: int = Field(default=0, ge=0)
    unique_authors: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_absence_has_no_direction(self):
        if self.status in {"unavailable", "disabled"} and any(
            value is not None for value in (self.band, self.score, self.confidence)
        ):
            raise ValueError(
                f"{self.status} evidence must not have band, score, or confidence"
            )
        if self.status == "available" and any(
            value is None for value in (self.band, self.score, self.confidence)
        ):
            raise ValueError(
                "available evidence requires band, score, and confidence"
            )
        if self.status == "partial" and not self.warnings:
            raise ValueError("partial evidence must describe its coverage gap in warnings")
        return self


def _legacy_evidence(values: dict) -> dict:
    """Build lane metadata for callers using the pre-v2 report shape.

    ``SentimentReport`` was public before evidence lanes existed.  Synthesising
    two explicitly-labelled legacy lanes lets old integrations keep creating
    reports while every newly-produced report uses the full contract.
    """

    has_direction = all(
        values.get(field) is not None
        for field in ("overall_band", "overall_score", "confidence")
    )
    signal = {
        "status": "available" if has_direction else "unavailable",
        "provider": "legacy_combined",
        "band": values.get("overall_band") if has_direction else None,
        "score": values.get("overall_score") if has_direction else None,
        "confidence": values.get("confidence") if has_direction else None,
        "sample_size": 0,
        "unique_authors": 0,
        "warnings": ["Evidence-lane provenance was not supplied by this legacy caller."],
    }
    return {"retail_social_signal": dict(signal), "media_tone": dict(signal)}


class SentimentReport(BaseModel):
    """Structured sentiment report produced by the Sentiment Analyst.

    Replaces the previous free-form prose output so downstream consumers
    (dashboards, audit logs, PDF renderers, other agents) can read
    ``overall_band`` and ``overall_score`` without maintaining fragile regex
    fallbacks that drift with every model release. ``narrative`` preserves the
    rich source-by-source analysis; ``render_sentiment_report`` prepends a
    deterministic header so the saved report stays human-readable.
    """

    status: Literal["available", "partial", "unavailable"] = Field(
        default="available",
        description=(
            "Overall evidence availability. Use available only when both evidence "
            "lanes are available, partial when at least one lane is usable, and "
            "unavailable when neither lane is usable."
        ),
    )
    overall_band: SentimentBand | None = Field(
        default=None,
        description=(
            "Overall sentiment direction. Exactly one of: "
            "Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. "
            "Use Mixed when sources point in clearly different directions. "
            "Use Neutral only when substantive evidence is genuinely balanced. "
            "It must be null when status is unavailable."
        ),
    )
    overall_score: float | None = Field(
        default=None,
        ge=0.0,
        le=10.0,
        description=(
            "Numeric sentiment intensity on a 0–10 scale. "
            "0 = maximally bearish, 5 = neutral, 10 = maximally bullish. "
            "Guideline for consistency with overall_band: "
            "Bullish ~6.5–10, Mildly Bullish ~5.5–6.4, Neutral/Mixed ~4.5–5.5, "
            "Mildly Bearish ~3.5–4.4, Bearish ~0–3.4. "
            "Only the 0–10 bounds are enforced."
        ),
    )
    confidence: SentimentConfidence | None = Field(
        default=None,
        description=(
            "Confidence in the assessment based on data quality and sample size. "
            "It must be null when status is unavailable."
        ),
    )
    retail_social_signal: EvidenceSignal
    media_tone: EvidenceSignal
    narrative: str = Field(
        description=(
            "Full sentiment report covering, in order: "
            "(1) source-by-source breakdown with specific evidence (cite message "
            "counts, ratios, notable posts); "
            "(2) cross-source divergences and alignments; "
            "(3) dominant narrative themes; "
            "(4) catalysts and risks surfaced by the data; "
            "(5) a markdown table summarising key sentiment signals, their "
            "direction, source, and supporting evidence. "
            "Keep it informative and substantive: develop each section thoroughly "
            "with concrete evidence so every point adds new signal for the trader."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_shape(cls, value):
        if not isinstance(value, dict):
            return value
        values = dict(value)
        if "retail_social_signal" not in values and "media_tone" not in values:
            values.update(_legacy_evidence(values))
        return values

    @model_validator(mode="after")
    def _validate_overall_semantics(self):
        directional = (self.overall_band, self.overall_score, self.confidence)
        if self.status == "unavailable":
            if any(value is not None for value in directional):
                raise ValueError(
                    "unavailable sentiment must not have band, score, or confidence"
                )
        elif self.status == "available" and any(value is None for value in directional):
            raise ValueError(
                "available sentiment requires band, score, and confidence"
            )
        elif self.status == "partial":
            present = [value is not None for value in directional]
            if any(present) and not all(present):
                raise ValueError(
                    "partial sentiment direction requires band, score, and confidence together"
                )

        lanes = (self.retail_social_signal, self.media_tone)
        enabled = [lane for lane in lanes if lane.status != "disabled"]
        usable = [lane.status in {"available", "partial"} for lane in enabled]
        expected_status = (
            "available"
            if enabled and all(lane.status == "available" for lane in enabled)
            else "partial"
            if any(usable)
            else "unavailable"
        )
        if self.status != expected_status:
            raise ValueError(
                f"sentiment status must be {expected_status!r} for the supplied evidence lanes"
            )
        if self.status == "partial" and not any(lane.warnings for lane in lanes):
            raise ValueError(
                "partial sentiment must identify a missing source or coverage gap"
            )
        return self


def render_sentiment_report(report: SentimentReport) -> str:
    """Render a SentimentReport to the markdown shape the rest of the system expects.

    The structured header (band + score + confidence) is prepended to the
    narrative so the saved report is both human-readable and machine-parseable
    without regex.
    """
    if report.status == "unavailable":
        overall = "**Overall Sentiment:** **Unavailable**"
        confidence = "**Confidence:** Unavailable"
    elif report.overall_band is None:
        overall = "**Overall Sentiment:** **Not scored**"
        confidence = "**Confidence:** Unavailable"
    else:
        overall = (
            f"**Overall Sentiment:** **{report.overall_band.value}** "
            f"(Score: {report.overall_score:.1f}/10)"
        )
        confidence = f"**Confidence:** {report.confidence.capitalize()}"

    def lane_row(name: str, lane: EvidenceSignal) -> str:
        direction = lane.band.value if lane.band is not None else "—"
        score = f"{lane.score:.1f}" if lane.score is not None else "—"
        return (
            f"| {name} | {lane.status} | {lane.provider} | {direction} | "
            f"{score} | {lane.sample_size} | {lane.unique_authors} |"
        )

    warnings = []
    for lane_name, lane in (
        ("Retail social", report.retail_social_signal),
        ("Media tone", report.media_tone),
    ):
        warnings.extend(f"- {lane_name}: {warning}" for warning in lane.warnings)

    parts = [
        f"**Sentiment Status:** {report.status.capitalize()}",
        overall,
        confidence,
        "",
        "### Evidence coverage",
        "| Lane | Status | Provider | Direction | Score | Sample | Authors |",
        "|---|---|---|---|---:|---:|---:|",
        lane_row("Retail social", report.retail_social_signal),
        lane_row("Media tone", report.media_tone),
    ]
    if warnings:
        parts.extend(["", "### Coverage warnings", *warnings])
    parts.extend(["", report.narrative])
    return "\n".join(parts)
