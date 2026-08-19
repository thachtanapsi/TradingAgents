from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

import tradingagents.agents.analysts.news_analyst as news_module
import tradingagents.dataflows.interface as interface_module
from tradingagents.agents.utils.news_data_tools import EditorialEvidence
from tradingagents.agents.utils.vietnam_macro_tools import (
    MacroEvidence,
    load_vietnam_macro_evidence,
)
from tradingagents.dataflows.interface import (
    VENDOR_METHODS,
    get_category_for_method,
    route_to_vendor_result,
)


def _macro_evidence(status: str = "available") -> MacroEvidence:
    return MacroEvidence(
        status=status,
        block=json.dumps(
            {
                "status": status,
                "observations": [
                    {
                        "indicator_id": "vn_cpi_yoy",
                        "value": "4.45",
                        "unit": "percent",
                    }
                ],
            }
        ),
        observation_count=1 if status in {"available", "partial"} else 0,
        as_of="2026-08-13T15:00:00+07:00",
        fetch_ids=["macro-fetch"],
        point_in_time_quality="proxy",
        source_results=[
            {
                "provider": "nso_sdmx",
                "status": status,
                "fetch_id": "macro-fetch",
                "observation_count": 1,
                "point_in_time_quality": "proxy",
                "warnings": [],
            }
        ],
        actual_vendor_observed=True,
    )


@pytest.mark.unit
def test_vietnam_macro_router_is_separate_from_fred(monkeypatch):
    monkeypatch.setitem(
        VENDOR_METHODS["get_vietnam_macro_context"],
        "vn_macro",
        lambda *args: "VN_MACRO_OK",
    )
    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {
            "data_vendors": {
                "macro_data": "fred",
                "vn_macro_data": "vn_macro",
            }
        },
    )

    result = route_to_vendor_result(
        "get_vietnam_macro_context", "2026-08-13", 24
    )

    assert get_category_for_method("get_macro_indicators") == "macro_data"
    assert get_category_for_method("get_vietnam_macro_context") == "vn_macro_data"
    assert result.value == "VN_MACRO_OK"
    assert result.actual_vendor == "vn_macro"


@pytest.mark.unit
def test_macro_loader_builds_prompt_evidence_but_metadata_has_no_observations(
    monkeypatch,
):
    observation = SimpleNamespace(
        indicator_id="vn_usd_vnd_central",
        value="25580",
        unit="VND_per_USD",
        unit_multiplier=1,
        frequency="daily",
        period_start="2026-08-13T00:00:00+07:00",
        period_end="2026-08-13T23:59:59+07:00",
        published_at="2026-08-13T08:00:00+07:00",
        first_seen_at="2026-08-13T08:05:00+07:00",
        retrieved_at="2026-08-13T08:05:00+07:00",
        source_provider="sbv_html",
        source_series=None,
        source_url="https://sbv.gov.vn/vi/ty-gia?token=must-not-persist",
        provisional=False,
        point_in_time_quality="exact",
        derived_from=[],
        stale=False,
        warnings=[],
    )
    result = SimpleNamespace(
        status="available",
        as_of="2026-08-13T15:00:00+07:00",
        observations=[observation],
        source_results=[
            {
                "provider": "sbv_html",
                "status": "available",
                "fetch_ids": ["fetch-sbv"],
                "observation_count": 1,
                "point_in_time_quality": "exact",
                "warnings": [],
                "raw_html": "must not persist",
            }
        ],
        warnings=[],
    )
    service = SimpleNamespace(load_evidence=MagicMock(return_value=result))
    monkeypatch.setitem(
        sys.modules,
        "tradingagents.dataflows.vietnam_macro",
        SimpleNamespace(create_vietnam_macro_service_from_env=lambda: service),
    )

    evidence = load_vietnam_macro_evidence(
        "2026-08-13",
        state={"macro_profile": {"provider": "vn_macro", "lookback_months": 18}},
    )

    service.load_evidence.assert_called_once_with(
        "2026-08-13", lookback_months=18
    )
    assert evidence.status == "available"
    assert evidence.observation_count == 1
    assert "vn_usd_vnd_central" in evidence.block
    assert "token=must-not-persist" not in evidence.block
    metadata = evidence.metadata()
    assert "observations" not in metadata
    assert "raw_html" not in str(metadata)
    assert metadata["fetch_ids"] == ["fetch-sbv"]


@pytest.mark.unit
def test_macro_only_profile_uses_vietnam_news_path_and_never_binds_fred(monkeypatch):
    disclosure = news_module._DisclosureEvidence(
        status="available",
        provider="gx_market_info",
        block="### Disclosure",
        sample_size=1,
        actual_vendor_observed=True,
    )
    editorial = EditorialEvidence(
        status="disabled",
        provider="vn_media",
        block="<disabled>",
    )
    load_macro = MagicMock(return_value=_macro_evidence())
    monkeypatch.setattr(news_module, "_load_disclosures", lambda *args: disclosure)
    monkeypatch.setattr(
        news_module, "load_vietnam_editorial_evidence", lambda *args, **kwargs: editorial
    )
    monkeypatch.setattr(news_module, "load_vietnam_macro_evidence", load_macro)

    captured_tools: list[str] = []
    captured_prompt: list[object] = []

    def bind_tools(tools):
        captured_tools.extend(tool.name for tool in tools)
        return RunnableLambda(
            lambda prompt: captured_prompt.append(prompt)
            or AIMessage(content="Vietnam macro report")
        )

    state = {
        "company_of_interest": "VIC",
        "trade_date": "2026-08-13",
        "asset_type": "stock",
        "messages": [HumanMessage(content="VIC")],
        "media_profile": {"provider": "legacy"},
        "macro_profile": {"provider": "vn_macro", "lookback_months": 24},
        "macro_profile_fingerprint": "macro-fingerprint",
    }
    result = news_module.create_news_analyst(
        SimpleNamespace(bind_tools=bind_tools)
    )(state)

    load_macro.assert_called_once_with("2026-08-13", state=state)
    assert "get_macro_indicators" not in captured_tools
    assert "get_news" not in captured_tools
    assert captured_tools == ["get_global_news", "get_prediction_markets"]
    assert result["news_source_metadata"]["vn_macro"]["observation_count"] == 1
    assert result["news_source_metadata"]["macro_profile_fingerprint"] == (
        "macro-fingerprint"
    )
    system_text = "\n".join(
        str(message.content)
        for message in captured_prompt[0].messages
        if type(message).__name__ == "SystemMessage"
    )
    human_text = "\n".join(
        str(message.content)
        for message in captured_prompt[0].messages
        if type(message).__name__ == "HumanMessage"
    )
    assert "FRED is not a Vietnam macro source" in system_text
    assert "vn_cpi_yoy" not in system_text
    assert "vn_cpi_yoy" in human_text


@pytest.mark.unit
def test_vietnam_news_prefetches_macro_exactly_once_across_tool_loop(monkeypatch):
    disclosure = news_module._DisclosureEvidence(
        status="available", provider="gx_market_info", block="### disclosure"
    )
    editorial = EditorialEvidence(
        status="available", provider="cafef_rss", block="[]", sample_size=3
    )
    load_disclosure = MagicMock(return_value=disclosure)
    load_editorial = MagicMock(return_value=editorial)
    load_macro = MagicMock(return_value=_macro_evidence())
    monkeypatch.setattr(news_module, "_load_disclosures", load_disclosure)
    monkeypatch.setattr(
        news_module, "load_vietnam_editorial_evidence", load_editorial
    )
    monkeypatch.setattr(news_module, "load_vietnam_macro_evidence", load_macro)

    responses = iter(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_global_news",
                        "args": {"curr_date": "2026-08-13"},
                        "id": "global-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Completed report"),
        ]
    )
    tool_sets: list[list[str]] = []

    def bind_tools(tools):
        tool_sets.append([tool.name for tool in tools])
        return RunnableLambda(lambda _: next(responses))

    node = news_module.create_news_analyst(SimpleNamespace(bind_tools=bind_tools))
    first_message = HumanMessage(content="HPG")
    state = {
        "company_of_interest": "HPG",
        "trade_date": "2026-08-13",
        "asset_type": "stock",
        "messages": [first_message],
        "media_profile": {"providers": ["cafef_rss"]},
        "media_profile_fingerprint": "media-fp",
        "macro_profile": {"provider": "vn_macro"},
        "macro_profile_fingerprint": "macro-fp",
    }
    first = node(state)
    state["messages"] = [
        first_message,
        first["messages"][0],
        ToolMessage(content="global", tool_call_id="global-1"),
    ]
    second = node(state)

    assert load_disclosure.call_count == 1
    assert load_editorial.call_count == 1
    assert load_macro.call_count == 1
    assert all("get_macro_indicators" not in tools for tools in tool_sets)
    assert second["news_source_metadata"]["status"] == "available"


@pytest.mark.unit
def test_upstream_news_profile_keeps_fred_tool(monkeypatch):
    # Explicit legacy profiles override a configured GX default and exercise the
    # unchanged upstream branch deterministically.
    captured_tools: list[str] = []

    def bind_tools(tools):
        captured_tools.extend(tool.name for tool in tools)
        return RunnableLambda(lambda _: AIMessage(content="Upstream report"))

    state = {
        "company_of_interest": "NVDA",
        "trade_date": "2026-08-13",
        "asset_type": "stock",
        "messages": [HumanMessage(content="NVDA")],
        "media_profile": {"provider": "legacy"},
        "macro_profile": {"provider": "legacy"},
    }
    result = news_module.create_news_analyst(
        SimpleNamespace(bind_tools=bind_tools)
    )(state)

    assert "get_news" in captured_tools
    assert "get_macro_indicators" in captured_tools
    assert "news_source_metadata" not in result


@pytest.mark.unit
def test_unavailable_macro_does_not_hide_usable_news_lane():
    disclosure = news_module._DisclosureEvidence(
        status="available", provider="gx_market_info", block="### disclosure"
    )
    editorial = EditorialEvidence(
        status="unavailable", provider="vn_media", block="<unavailable>"
    )
    macro = MacroEvidence(status="unavailable")

    assert news_module._news_status(disclosure, editorial, macro) == "partial"
