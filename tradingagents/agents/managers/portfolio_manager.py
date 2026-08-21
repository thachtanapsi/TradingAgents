"""Portfolio Manager: synthesises the risk-analyst debate into the final decision.

Uses LangChain's ``with_structured_output`` so the LLM produces a typed
``PortfolioDecision`` directly, in a single call.  The result is rendered
back to markdown for storage in ``final_trade_decision`` so memory log,
CLI display, and saved reports continue to consume the same shape they do
today.  When a provider does not expose structured output, the agent falls
back gracefully to free-text generation.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
)

logger = logging.getLogger(__name__)

_TARGET_FIELD_NAMES = (
    "Price Target Status",
    "Price Target",
    "Price Target Currency",
    "Price Target Rationale",
    "Price Target Unavailable Reason",
)
_MIN_REFERENCE_RATIO = Decimal("0.05")
_MAX_REFERENCE_RATIO = Decimal("20")


def _canonical_field(report: str, label: str) -> str | None:
    match = re.search(
        rf"(?im)^\s*(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*[:\-]\s*(.*?)\s*$",
        report,
    )
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _decimal_value(value: object) -> Decimal | None:
    text = str(value or "").strip()
    if not text or text.lower() in {
        "none",
        "null",
        "n/a",
        "na",
        "unavailable",
        "unknown",
        "-",
    }:
        return None
    if not re.fullmatch(r"\+?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text):
        return None
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    if not number.is_finite() or number <= 0:
        return None
    return number


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _trusted_price_reference(state: dict) -> tuple[dict | None, str | None]:
    reference = state.get("market_price_reference")
    if not isinstance(reference, dict) or reference.get("status") != "available":
        return None, "No verified completed-session price reference is available."
    expected_ticker = str(state.get("company_of_interest", "")).strip().upper()
    if not expected_ticker or str(reference.get("ticker", "")).strip().upper() != expected_ticker:
        return None, "The price reference does not match the analyzed ticker."
    expected_cutoff = str(state.get("analysis_cutoff", "")).strip()
    if not expected_cutoff or str(reference.get("analysis_cutoff", "")) != expected_cutoff:
        return None, "The price reference does not match the analysis cutoff."
    close = _decimal_value(reference.get("close"))
    currency = str(reference.get("currency") or "").strip().upper()
    quality = str(reference.get("point_in_time_quality") or "").strip().lower()
    if close is None or not currency:
        return None, "The completed-session close or quote currency is unavailable."
    if quality != "exact":
        return None, "The completed-session price reference is not exact point-in-time data."
    if currency == "VND" and str(reference.get("price_unit") or "").upper() != "VND":
        return None, "The GX price reference is not expressed in full VND units."
    return {**reference, "close": close, "currency": currency}, None


def _without_target_fields(report: str) -> str:
    labels = "|".join(re.escape(label) for label in _TARGET_FIELD_NAMES)
    cleaned = re.sub(
        rf"(?im)^\s*(?:\*\*)?(?:{labels})(?:\*\*)?\s*[:\-].*?(?:\n|$)",
        "",
        report,
    )
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _unavailable_target_block(currency: str | None, reason: str) -> str:
    return "\n".join(
        (
            "**Price Target Status**: Unavailable",
            "",
            "**Price Target**: Unavailable",
            "",
            f"**Price Target Currency**: {currency or 'Unavailable'}",
            "",
            "**Price Target Rationale**: Unavailable",
            "",
            f"**Price Target Unavailable Reason**: {reason}",
        )
    )


def _normalize_price_target(report: str, state: dict) -> str:
    """Validate the PM target against the frozen PIT close and canonicalize it.

    This runs for both structured and free-text provider paths.  It never guesses
    a missing unit and never rescales values (for example 63.3 to 63,300).
    """
    body = _without_target_fields(str(report or ""))
    reference, reference_error = _trusted_price_reference(state)
    reference_currency = reference["currency"] if reference else None
    raw_target = _canonical_field(report, "Price Target")
    target = _decimal_value(raw_target)
    raw_currency = _canonical_field(report, "Price Target Currency")
    currency = str(raw_currency or "").strip().upper() or None
    rationale = _canonical_field(report, "Price Target Rationale")
    stated_unavailable = _canonical_field(report, "Price Target Unavailable Reason")

    rejection: str | None = None
    if target is None:
        rejection = stated_unavailable
        if not rejection or rejection.lower() in {"unavailable", "n/a", "none"}:
            rejection = (
                reference_error
                or "Portfolio Manager did not provide a numeric target with a valuation basis."
            )
    elif reference_error:
        rejection = reference_error
    elif not currency:
        rejection = "The target did not specify a quote currency."
    elif currency != reference_currency:
        rejection = (
            f"The target currency {currency} does not match the verified "
            f"reference currency {reference_currency}."
        )
    elif reference_currency == "VND" and target != target.to_integral_value():
        rejection = "GX price targets must be expressed as integral full-VND amounts."
    elif not rationale or rationale.lower() in {"unavailable", "n/a", "none"}:
        rejection = "The target did not include a valuation rationale."
    else:
        ratio = target / reference["close"]
        if ratio < _MIN_REFERENCE_RATIO or ratio > _MAX_REFERENCE_RATIO:
            rejection = (
                "The target/reference-price ratio is outside the scale-safety range; "
                "the value was rejected rather than rescaled."
            )

    if rejection:
        block = _unavailable_target_block(reference_currency or currency, rejection)
    else:
        block = "\n".join(
            (
                "**Price Target Status**: Available",
                "",
                f"**Price Target**: {_decimal_text(target)}",
                "",
                f"**Price Target Currency**: {currency}",
                "",
                f"**Price Target Rationale**: {rationale}",
                "",
                "**Price Target Unavailable Reason**: Unavailable",
            )
        )
    return f"{body}\n\n{block}" if body else block


def _price_reference_prompt(state: dict) -> str:
    reference, error = _trusted_price_reference(state)
    if error:
        return f"Unavailable: {error}"
    return (
        f"ticker={reference['ticker']}; completed close={_decimal_text(reference['close'])}; "
        f"currency={reference['currency']}; price_unit={reference.get('price_unit')}; "
        f"session_date={reference.get('session_date')}; "
        f"analysis_cutoff={reference.get('analysis_cutoff')}; PIT=exact"
    )


def _invoke_portfolio_manager(structured_llm, llm, prompt, state: dict) -> str:
    if structured_llm is not None:
        try:
            result = structured_llm.invoke(prompt)
            if result is None:
                raise ValueError("structured output returned no parsed result")
            return _normalize_price_target(render_pm_decision(result), state)
        except Exception as exc:
            logger.warning(
                "Portfolio Manager structured output failed (%s); retrying once as free text",
                type(exc).__name__,
            )
    response = llm.invoke(prompt)
    return _normalize_price_target(str(response.content), state)


def create_portfolio_manager(llm):
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state)

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]
        price_reference = _price_reference_prompt(state)

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )

        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
- Frozen completed-daily-close reference: **{price_reference}**
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

Be decisive and ground every conclusion in specific evidence from the analysts.

**Mandatory price-target contract:**
- Always return every price-target field in the schema.
- If the frozen reference is usable, return a target in the exact same currency and
  full price unit, plus a concise valuation rationale. For VND the target must be an
  integral full-VND amount (for example 83500, never 83.5 or "83.5k").
- If the evidence cannot support a defensible target or the reference is unavailable,
  return price_target=null, price_target_rationale=null, and a specific
  price_target_unavailable_reason. Never invent or rescale a target.
- A numeric target must have price_target_unavailable_reason=null.

{NO_EXTERNAL_TOOLS}{get_language_instruction()}"""

        final_trade_decision = _invoke_portfolio_manager(
            structured_llm,
            llm,
            prompt,
            state,
        )

        new_risk_debate_state = {
            "judge_decision": final_trade_decision,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
        }

    return portfolio_manager_node
