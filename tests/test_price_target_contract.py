"""Fail-safe Portfolio Manager price-target contract."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from tradingagents.agents.managers.portfolio_manager import (
    _normalize_price_target,
    create_portfolio_manager,
)
from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating


def _reference_state(*, close: str = "63300", currency: str = "VND") -> dict:
    cutoff = "2026-08-19T15:00:00+07:00"
    return {
        "company_of_interest": "MWG",
        "analysis_cutoff": cutoff,
        "market_price_reference": {
            "status": "available",
            "ticker": "MWG",
            "close": close,
            "currency": currency,
            "price_unit": currency,
            "session_date": "2026-08-19",
            "analysis_cutoff": cutoff,
            "source": "gx_market_info",
            "point_in_time_quality": "exact",
        },
    }


def _report(target: str, *, currency: str = "VND", rationale: str = "DCF") -> str:
    return (
        "**Rating**: Overweight\n\n"
        f"**Price Target**: {target}\n\n"
        f"**Price Target Currency**: {currency}\n\n"
        f"**Price Target Rationale**: {rationale}"
    )


@pytest.mark.unit
class TestPortfolioDecisionSchema:
    def test_numeric_target_requires_currency_and_rationale(self):
        with pytest.raises(ValidationError):
            PortfolioDecision(
                rating=PortfolioRating.BUY,
                executive_summary="summary",
                investment_thesis="thesis",
                price_target=83500,
                price_target_currency=None,
                price_target_rationale=None,
                price_target_unavailable_reason=None,
            )

    def test_unavailable_target_requires_reason(self):
        with pytest.raises(ValidationError):
            PortfolioDecision(
                rating=PortfolioRating.HOLD,
                executive_summary="summary",
                investment_thesis="thesis",
                price_target=None,
                price_target_currency="VND",
                price_target_rationale=None,
                price_target_unavailable_reason=None,
            )

    def test_target_and_unavailable_reason_are_mutually_exclusive(self):
        with pytest.raises(ValidationError):
            PortfolioDecision(
                rating=PortfolioRating.BUY,
                executive_summary="summary",
                investment_thesis="thesis",
                price_target=83500,
                price_target_currency="VND",
                price_target_rationale="DCF",
                price_target_unavailable_reason="also unavailable",
            )


@pytest.mark.unit
class TestPriceTargetNormalization:
    def test_accepts_full_vnd_target_against_frozen_close(self):
        result = _normalize_price_target(_report("83500"), _reference_state())

        assert "**Price Target Status**: Available" in result
        assert "**Price Target**: 83500" in result
        assert "**Price Target Currency**: VND" in result

    @pytest.mark.parametrize("target", ["83.5", "0", "NaN", "6.33e4", "63.3k"])
    def test_rejects_bad_scale_or_noncanonical_target_without_rescaling(self, target):
        result = _normalize_price_target(_report(target), _reference_state())

        assert "**Price Target Status**: Unavailable" in result
        assert "**Price Target**: Unavailable" in result
        assert "63300" not in result

    def test_rejects_wrong_currency(self):
        result = _normalize_price_target(
            _report("83500", currency="USD"), _reference_state()
        )

        assert "**Price Target Status**: Unavailable" in result
        assert "does not match" in result

    def test_rejects_target_when_reference_is_missing(self):
        state = _reference_state()
        state.pop("market_price_reference")
        result = _normalize_price_target(_report("83500"), state)

        assert "**Price Target Status**: Unavailable" in result
        assert "No verified completed-session price reference" in result

    def test_rejects_unprovenanced_partial_reference_from_fallback_vendor(self):
        state = _reference_state()
        state["market_price_reference"].update(
            {
                "close": "63.3",
                "currency": None,
                "price_unit": None,
                "source": "unprovenanced_ohlcv",
                "point_in_time_quality": "partial",
            }
        )

        result = _normalize_price_target(_report("83500"), state)

        assert "**Price Target Status**: Unavailable" in result
        assert "completed-session close or quote currency is unavailable" in result

    def test_free_text_fallback_is_always_canonicalized(self):
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError
        llm.invoke.return_value = MagicMock(
            content="**Rating**: Hold\n\n**Price Target**: 63.3"
        )
        risk = {
            "history": "risk",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "latest_speaker": "Neutral",
            "count": 1,
        }
        state = {
            **_reference_state(),
            "risk_debate_state": risk,
            "investment_plan": "plan",
            "trader_investment_plan": "trade",
        }

        result = create_portfolio_manager(llm)(state)["final_trade_decision"]

        assert "**Price Target Status**: Unavailable" in result
        assert "**Price Target**: 63.3" not in result
        llm.invoke.assert_called_once()
