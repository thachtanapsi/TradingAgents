"""The market analyst is bound (and prompt-instructed) to call
get_verified_market_snapshot; if the executor ToolNode doesn't register it, the
call fails and the model reports the tool "unavailable" and skips verification.

Regression guard for that wiring gap (snapshot bound to the LLM but missing from
the market ToolNode).
"""
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from tradingagents.agents.analysts.market_analyst import (
    _market_tools,
    create_market_analyst,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph


@pytest.mark.unit
def test_market_toolnode_can_execute_verified_snapshot():
    # _create_tool_nodes does not use self -> call unbound (avoids building LLMs).
    nodes = TradingAgentsGraph._create_tool_nodes(None)
    market_tools = set(nodes["market"].tools_by_name)
    assert "get_verified_market_snapshot" in market_tools, (
        "get_verified_market_snapshot is bound to the market analyst but not "
        "registered in the market ToolNode, so the model's call fails."
    )
    # the other core market tools must remain too
    assert {"get_stock_data", "get_indicators"} <= market_tools


@pytest.mark.unit
def test_live_market_with_prefetched_snapshot_cannot_request_duplicate_snapshot():
    live_tools = {
        item.name
        for item in _market_tools(
            {
                "analysis_mode": "live",
                "verified_market_snapshot": "immutable evidence",
            }
        )
    }
    close_tools = {
        item.name for item in _market_tools({"analysis_mode": "close"})
    }

    assert live_tools == {"get_stock_data", "get_indicators"}
    assert "get_verified_market_snapshot" in close_tools


@pytest.mark.unit
def test_prefetched_live_snapshot_is_injected_into_rendered_market_prompt():
    captured = {}

    class CapturingLlm:
        def bind_tools(self, tools):
            captured["tools"] = [item.name for item in tools]

            def invoke(prompt):
                captured["prompt"] = "\n".join(
                    str(message.content) for message in prompt.to_messages()
                )
                return AIMessage(content="market report")

            return RunnableLambda(invoke)

    node = create_market_analyst(CapturingLlm())
    result = node(
        {
            "company_of_interest": "CTG",
            "asset_type": "stock",
            "instrument_context": "CTG on HOSE",
            "trade_date": "2026-08-19",
            "analysis_mode": "live",
            "analysis_cutoff": "2026-08-19T16:05:00+07:00",
            "verified_market_snapshot": "UNIQUE_FROZEN_SNAPSHOT",
            "messages": [],
        }
    )

    assert "UNIQUE_FROZEN_SNAPSHOT" in captured["prompt"]
    assert "{verified_market_snapshot}" not in captured["prompt"]
    assert captured["tools"] == ["get_stock_data", "get_indicators"]
    assert result["market_report"] == "market report"


@pytest.mark.unit
def test_gx_vietnam_macro_news_toolnode_never_exposes_fred():
    graph = type(
        "GraphConfig",
        (),
        {
            "config": {
                "vn_macro": {
                    "enabled": True,
                    "providers": "nso_sdmx,nso_release,sbv_html",
                }
            }
        },
    )()

    news_tools = set(TradingAgentsGraph._create_tool_nodes(graph)["news"].tools_by_name)

    assert "get_macro_indicators" not in news_tools
    assert "get_news" not in news_tools
    assert {"get_global_news", "get_prediction_markets"} <= news_tools


@pytest.mark.unit
def test_upstream_news_toolnode_keeps_fred_when_vietnam_macro_disabled():
    graph = type(
        "GraphConfig",
        (),
        {"config": {"vn_macro": {"enabled": False, "providers": "nso_sdmx"}}},
    )()

    news_tools = set(TradingAgentsGraph._create_tool_nodes(graph)["news"].tools_by_name)

    assert "get_macro_indicators" in news_tools
