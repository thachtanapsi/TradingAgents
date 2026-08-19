"""Live GX tools must use the immutable injected cutoff, not LLM arguments."""

from __future__ import annotations

import pytest

from tradingagents.agents.utils import core_stock_tools, fundamental_data_tools


@pytest.mark.unit
def test_live_cutoff_is_hidden_from_model_schema_and_overrides_end_date(monkeypatch):
    calls = []
    monkeypatch.setattr(
        core_stock_tools,
        "route_to_vendor",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "ok",
    )
    cutoff = "2026-08-19T16:05:31.123456+07:00"

    schema = core_stock_tools.get_stock_data.tool_call_schema.model_json_schema()
    assert "state" not in schema["properties"]
    result = core_stock_tools.get_stock_data.invoke(
        {
            "symbol": "CTG",
            "start_date": "2026-08-01",
            "end_date": "2099-12-31",
            "state": {
                "analysis_mode": "live",
                "analysis_cutoff": cutoff,
            },
        }
    )

    assert result == "ok"
    assert calls == [
        (("get_stock_data", "CTG", "2026-08-01", cutoff), {})
    ]


@pytest.mark.unit
def test_live_fundamentals_ignores_model_supplied_date(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fundamental_data_tools,
        "route_to_vendor",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "ok",
    )
    cutoff = "2026-08-19T16:05:31+07:00"

    output = fundamental_data_tools.get_fundamentals.invoke(
        {
            "ticker": "CTG",
            "curr_date": "2099-12-31",
            "state": {
                "analysis_mode": "live",
                "analysis_cutoff": cutoff,
            },
        }
    )

    assert output == "ok"
    assert calls == [(('get_fundamentals', 'CTG', cutoff), {})]
