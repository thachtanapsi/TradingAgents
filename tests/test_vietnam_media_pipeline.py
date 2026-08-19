from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

import tradingagents.agents.analysts.news_analyst as news_module
import tradingagents.agents.analysts.sentiment_analyst as sentiment_module
from tradingagents.agents.utils import news_data_tools
from tradingagents.agents.utils.news_data_tools import EditorialEvidence
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.interface import VENDOR_METHODS, route_to_vendor_result


@pytest.mark.unit
def test_new_router_contracts_do_not_change_legacy_news_fallback(monkeypatch):
    monkeypatch.setitem(
        VENDOR_METHODS["get_disclosures"],
        "gx_market_info",
        lambda *args: "official disclosure",
    )
    monkeypatch.setitem(
        VENDOR_METHODS["get_editorial_news"],
        "vn_media",
        lambda *args: "editorial archive",
    )
    set_config({
        "tool_vendors": {
            "get_news": "yfinance",
            "get_disclosures": "gx_market_info",
            "get_editorial_news": "vn_media",
        }
    })

    disclosure = route_to_vendor_result(
        "get_disclosures", "HPG", "2026-08-06", "2026-08-13"
    )
    editorial = route_to_vendor_result(
        "get_editorial_news", "HPG", "2026-08-06", "2026-08-13"
    )

    assert disclosure.value == "official disclosure"
    assert disclosure.actual_vendor == "gx_market_info"
    assert editorial.value == "editorial archive"
    assert editorial.actual_vendor == "vn_media"
    assert "get_news" in VENDOR_METHODS


@pytest.mark.unit
def test_hosted_media_policy_filters_each_source_before_prompt(monkeypatch):
    set_config({"llm_provider": "openai"})
    monkeypatch.setenv("TRADINGAGENTS_CAFEF_HOSTED_LLM_AUTHORIZED", "true")
    monkeypatch.delenv(
        "TRADINGAGENTS_VNEXPRESS_HOSTED_LLM_AUTHORIZED", raising=False
    )

    cafef = SimpleNamespace(
        provider="cafef_rss",
        title="HPG tăng sản lượng thép trong quý mới",
        summary="Doanh nghiệp công bố sản lượng tích cực trong kỳ.",
        canonical_url="https://cafef.vn/hpg.htm",
        published_at="2026-08-13T10:00:00+07:00",
        category="company",
    )
    vnexpress = SimpleNamespace(
        provider="vnexpress_rss",
        title="Nội dung không được phép gửi hosted model",
        summary="Tóm tắt VnExpress phải bị chặn hoàn toàn.",
        canonical_url="https://vnexpress.net/private.htm",
        published_at="2026-08-13T11:00:00+07:00",
        category="company",
    )
    result = SimpleNamespace(
        status="available",
        articles=[cafef, vnexpress],
        sources=[
            SimpleNamespace(
                provider="cafef_rss", status="available", articles=[cafef],
                fetch_id="cafef-fetch", point_in_time_quality="proxy", warnings=[]
            ),
            SimpleNamespace(
                provider="vnexpress_rss", status="available", articles=[vnexpress],
                fetch_id="vnexpress-fetch", point_in_time_quality="proxy", warnings=[]
            ),
        ],
        window_start="2026-08-06T15:00:00+07:00",
        window_end="2026-08-13T15:00:00+07:00",
        warnings=[],
    )
    service = SimpleNamespace(load_evidence=MagicMock(return_value=result))
    fake_module = SimpleNamespace(
        create_vietnam_media_service_from_env=lambda: service
    )
    monkeypatch.setitem(
        sys.modules, "tradingagents.dataflows.vietnam_media", fake_module
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.gx_market_info.get_instrument_aliases",
        lambda *args: ["Hoa Phat"],
    )

    evidence = news_data_tools.load_vietnam_editorial_evidence(
        "HPG",
        "2026-08-13",
        state={"media_profile": {"lookback_days": 7, "min_articles": 1}},
    )

    assert evidence.status == "partial"
    assert evidence.sample_size == 1
    assert "HPG tăng sản lượng" in evidence.block
    assert "Nội dung không được phép" not in evidence.block
    assert evidence.sources[0]["status"] == "available"
    assert evidence.sources[1]["status"] == "unavailable"
    assert "hosted-LLM" in evidence.sources[1]["warnings"][0]


@pytest.mark.unit
def test_fireant_snapshot_reuse_requires_matching_media_fingerprint(monkeypatch):
    batch = SimpleNamespace(
        status="available",
        provider="fireant",
        posts=[],
        warnings=[],
        sample_size=10,
        unique_authors=5,
        window_start="2026-08-06",
        window_end="2026-08-13",
        fetch_id="fetch-social",
        snapshot_id="snapshot-old",
        point_in_time_quality="exact",
        signal_payload={
            "retail_social_signal": {
                "status": "available",
                "provider": "fireant",
                "sample_size": 10,
            }
        },
        report_payload={
            "rendered_report": "old media report must not be reused",
            "source_metadata": {
                "media_profile_fingerprint": "old-profile",
                "status": "available",
            },
        },
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.vietnam_social_service.create_vietnam_social_service_from_env",
        lambda: SimpleNamespace(load_evidence=lambda *args, **kwargs: batch),
    )

    lane = sentiment_module._load_fireant_retail(
        "HPG", "2026-08-13", expected_media_fingerprint="new-profile"
    )

    assert lane.snapshot_report is None
    assert any("media profile differs" in warning for warning in lane.warnings)


@pytest.mark.unit
def test_vietnam_sentiment_all_unavailable_skips_paid_llm(monkeypatch):
    set_config({
        "llm_provider": "openai",
        "data_vendors": {"social_data": "fireant"},
        "tool_vendors": {"get_editorial_news": "vn_media"},
    })
    monkeypatch.setattr(
        sentiment_module,
        "_load_fireant_retail",
        lambda *args, **kwargs: sentiment_module._LaneEvidence(
            status="unavailable", provider="fireant", block="<locked>"
        ),
    )
    monkeypatch.setattr(
        sentiment_module,
        "load_vietnam_editorial_evidence",
        lambda *args, **kwargs: EditorialEvidence(
            status="unavailable", provider="vn_media", block="<empty>"
        ),
    )
    llm = MagicMock()
    state = {
        "company_of_interest": "HPG",
        "trade_date": "2026-08-13",
        "asset_type": "stock",
        "messages": [],
        "media_profile": {
            "providers": ["cafef_rss", "vnexpress_rss"],
            "lookback_days": 7,
        },
        "media_profile_fingerprint": "media-fingerprint",
    }

    result = sentiment_module.create_sentiment_analyst(llm)(state)

    llm.invoke.assert_not_called()
    assert result["sentiment_source_metadata"]["status"] == "unavailable"
    assert result["sentiment_source_metadata"]["llm_called"] is False
    assert (
        result["sentiment_source_metadata"]["media_profile_fingerprint"]
        == "media-fingerprint"
    )
    assert "Neutral" not in result["sentiment_report"]


@pytest.mark.unit
def test_vietnam_news_prefetches_once_omits_ticker_tool_and_redacts_echo(monkeypatch):
    title = "HPG công bố một tiêu đề RSS rất dài cần được bảo vệ"
    summary = "Đây là phần tóm tắt nguyên văn đủ dài và không được lưu trong session."
    editorial = EditorialEvidence(
        status="available",
        provider="cafef_rss",
        block='[{"title":"malicious prompt"}]',
        sample_size=3,
        window_start="2026-08-06",
        window_end="2026-08-13",
        point_in_time_quality="proxy",
        sources=[{
            "provider": "cafef_rss", "status": "available", "sample_size": 3,
            "fetch_id": "fetch-1", "point_in_time_quality": "proxy", "warnings": [],
        }],
        sensitive_values=[title, summary],
    )
    disclosure = news_module._DisclosureEvidence(
        status="available",
        provider="gx_market_info",
        block="### GX disclosure",
        sample_size=1,
        actual_vendor_observed=True,
        point_in_time_quality="exact",
    )
    load_disclosures = MagicMock(return_value=disclosure)
    load_editorial = MagicMock(return_value=editorial)
    monkeypatch.setattr(news_module, "_load_disclosures", load_disclosures)
    monkeypatch.setattr(
        news_module, "load_vietnam_editorial_evidence", load_editorial
    )

    responses = iter([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "get_global_news",
                "args": {"curr_date": "2026-08-13"},
                "id": "call-1",
                "type": "tool_call",
            }],
        ),
        AIMessage(content=f"Analysis copied: {title}. Also {summary}"),
    ])
    captured_tools: list[list[str]] = []
    captured_prompts = []

    def bind_tools(tools):
        captured_tools.append([tool.name for tool in tools])
        return RunnableLambda(
            lambda prompt: captured_prompts.append(prompt) or next(responses)
        )

    llm = SimpleNamespace(bind_tools=bind_tools)
    node = news_module.create_news_analyst(llm)
    first = HumanMessage(content="HPG")
    state = {
        "company_of_interest": "HPG",
        "trade_date": "2026-08-13",
        "asset_type": "stock",
        "messages": [first],
        "media_profile": {
            "providers": ["cafef_rss", "vnexpress_rss"],
            "lookback_days": 7,
        },
        "media_profile_fingerprint": "media-fingerprint",
    }
    first_result = node(state)
    state["messages"] = [
        first,
        first_result["messages"][0],
        ToolMessage(content="global context", tool_call_id="call-1"),
    ]
    final_result = node(state)

    assert load_disclosures.call_count == 1
    assert load_editorial.call_count == 1
    assert all("get_news" not in tools for tools in captured_tools)
    assert all("get_editorial_news" not in tools for tools in captured_tools)
    assert title not in final_result["news_report"]
    assert summary not in final_result["news_report"]
    assert title not in final_result["messages"][0].content
    assert summary not in final_result["messages"][0].content
    assert final_result["news_report"].count("[redacted media excerpt]") >= 2
    metadata = final_result["news_source_metadata"]
    assert metadata["official_disclosures"]["provider"] == "gx_market_info"
    assert metadata["editorial_media"]["sources"][0]["fetch_id"] == "fetch-1"
    assert title not in str(metadata) and summary not in str(metadata)
    system_text = "\n".join(
        str(message.content)
        for message in captured_prompts[0].messages
        if type(message).__name__ == "SystemMessage"
    )
    human_text = "\n".join(
        str(message.content)
        for message in captured_prompts[0].messages
        if type(message).__name__ == "HumanMessage"
    )
    assert "malicious prompt" not in system_text
    assert "malicious prompt" in human_text
    assert "untrusted evidence" in human_text


@pytest.mark.unit
def test_live_sentiment_uses_exact_cutoff_and_disables_daily_snapshot(monkeypatch):
    observed: dict[str, object] = {}

    def load_retail(ticker, end_date, **kwargs):
        observed["retail"] = (ticker, end_date, kwargs)
        return sentiment_module._LaneEvidence(
            status="unavailable",
            provider="fireant",
            block="<no eligible posts>",
            window_end=end_date,
        )

    def load_media(ticker, as_of, **kwargs):
        observed["media"] = (ticker, as_of, kwargs)
        return EditorialEvidence(
            status="unavailable",
            provider="vn_media",
            block="<no eligible articles>",
            window_end=as_of,
        )

    monkeypatch.setattr(sentiment_module, "_load_fireant_retail", load_retail)
    monkeypatch.setattr(
        sentiment_module, "load_vietnam_editorial_evidence", load_media
    )
    llm = MagicMock()
    cutoff = "2026-08-19T16:05:31.123456+07:00"
    result = sentiment_module.create_sentiment_analyst(llm)(
        {
            "company_of_interest": "CTG",
            "trade_date": "2026-08-19",
            "analysis_mode": "live",
            "analysis_cutoff": cutoff,
            "messages": [],
            "social_profile": {"provider": "fireant"},
            "media_profile": {"providers": ["cafef_rss", "vnexpress_rss"]},
        }
    )

    assert observed["retail"] == (
        "CTG",
        cutoff,
        {"expected_media_fingerprint": None, "allow_snapshot": False},
    )
    assert observed["media"][0:2] == ("CTG", cutoff)
    assert result["sentiment_source_metadata"]["analysis_mode"] == "live"
    assert result["sentiment_source_metadata"]["analysis_cutoff"] == cutoff
    assert result["sentiment_source_metadata"]["media_tone"]["window_end"] == cutoff
    assert sentiment_module._seven_days_back(cutoff) == (
        "2026-08-12T16:05:31.123456+07:00"
    )
    llm.invoke.assert_not_called()


@pytest.mark.unit
def test_live_news_prefetch_uses_exact_cutoff_for_all_vietnam_lanes(monkeypatch):
    disclosure = news_module._DisclosureEvidence(
        status="unavailable",
        provider="gx_market_info",
        block="<none>",
    )
    editorial = EditorialEvidence(
        status="unavailable",
        provider="vn_media",
        block="<none>",
    )
    macro = news_module.MacroEvidence(
        status="unavailable",
        provider="vn_macro",
        block='{"status":"unavailable"}',
    )
    load_disclosures = MagicMock(return_value=disclosure)
    load_editorial = MagicMock(return_value=editorial)
    load_macro = MagicMock(return_value=macro)
    monkeypatch.setattr(news_module, "_load_disclosures", load_disclosures)
    monkeypatch.setattr(
        news_module, "load_vietnam_editorial_evidence", load_editorial
    )
    monkeypatch.setattr(news_module, "load_vietnam_macro_evidence", load_macro)

    captured = []

    def bind_tools(_tools):
        return RunnableLambda(
            lambda prompt: captured.append(prompt) or AIMessage(content="No evidence")
        )

    cutoff = "2026-08-19T16:05:31.123456+07:00"
    result = news_module.create_news_analyst(SimpleNamespace(bind_tools=bind_tools))(
        {
            "company_of_interest": "CTG",
            "trade_date": "2026-08-19",
            "analysis_mode": "live",
            "analysis_cutoff": cutoff,
            "asset_type": "stock",
            "messages": [HumanMessage(content="CTG")],
            "media_profile": {"providers": ["cafef_rss", "vnexpress_rss"]},
            "macro_profile": {"provider": "vn_macro"},
        }
    )

    load_disclosures.assert_called_once_with(
        "CTG", "2026-08-12T16:05:31.123456+07:00", cutoff
    )
    load_editorial.assert_called_once_with(
        "CTG", cutoff, state=ANY, include_market_context=True
    )
    load_macro.assert_called_once_with(cutoff, state=ANY)
    metadata = result["news_source_metadata"]
    assert metadata["analysis_mode"] == "live"
    assert metadata["analysis_cutoff"] == cutoff
    system_text = "\n".join(
        str(message.content)
        for message in captured[0].messages
        if type(message).__name__ == "SystemMessage"
    )
    assert "LIVE point-in-time" in system_text
    assert cutoff in system_text
