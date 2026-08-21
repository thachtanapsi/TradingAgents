from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.graph.stage_runner import (
    StagePrerequisiteError,
    TradingAgentsStageRunner,
    data_transport_identity,
    macro_profile_fingerprint,
    macro_profile_identity,
    media_profile_fingerprint,
    media_profile_identity,
    social_profile_identity,
)


class FakePropagator:
    def create_initial_state(
        self, ticker, analysis_date, asset_type="stock", past_context="", instrument_context=""
    ):
        return {
            "messages": [],
            "company_of_interest": ticker,
            "asset_type": asset_type,
            "instrument_context": instrument_context,
            "trade_date": analysis_date,
            "past_context": past_context,
            "market_report": "",
            "sentiment_report": "",
            "news_report": "",
            "fundamentals_report": "",
            "investment_debate_state": {},
            "risk_debate_state": {},
        }


class FakeGraph:
    def __init__(self, **kwargs):
        self.quick_thinking_llm = object()
        self.deep_thinking_llm = object()
        self.tool_nodes = {}
        self.propagator = FakePropagator()
        self.memory_log = None

    def resolve_instrument_context(self, ticker, asset_type):
        return f"{ticker}:{asset_type}"


class FakeRunner(TradingAgentsStageRunner):
    def _run_analyst(self, session, state, stage):
        return {f"{stage}_report" if stage != "sentiment" else "sentiment_report": stage}

    def _run_research(self, session, state):
        return {"investment_debate_state": {"history": "debate"}, "investment_plan": "plan"}

    def _run_trader(self, session, state):
        return {"trader_investment_plan": "trade"}

    def _run_risk(self, session, state):
        return {"risk_debate_state": {"history": "risk"}, "final_trade_decision": "hold"}


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_STAGE_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("GX_DATA_TRANSPORT", "api")
    config = {
        "llm_provider": "ollama",
        "backend_url": "http://127.0.0.1:11434/v1",
        "quick_think_llm": "qwen3:8b",
        "deep_think_llm": "qwen3:8b",
    }
    return FakeRunner(config=config, graph_factory=FakeGraph)


def test_stages_enforce_prerequisites_and_default_path(runner):
    session = runner.create_session(
        "HPG", "2026-08-12", selected_analysts=("market", "fundamentals"), run_id="abc"
    )
    with pytest.raises(StagePrerequisiteError, match="at least one"):
        runner.run_stage(session, "research")

    runner.run_stage(session, "market")
    runner.run_stage(session, "research")
    runner.run_stage(session, "trader")
    runner.run_stage(session, "risk")

    assert session.completed_stages == ["market", "research", "trader", "risk"]
    assert session.path().is_file()
    assert str(session.path()).endswith("HPG/2026-08-12/abc/session.json")


def test_runtime_profile_cannot_change_on_resume(runner):
    session = runner.create_session("HPG", "2026-08-12", selected_analysts=("market",))
    runner.config["quick_think_llm"] = "another-model"
    with pytest.raises(ValueError, match="immutable.*llm"):
        runner.run_stage(session, "market")


def test_default_run_executes_selected_analysts_then_downstream(runner):
    session = runner.create_session(
        "HPG", "2026-08-12", selected_analysts=("market", "fundamentals")
    )
    runner.run_default(session)
    assert session.state["final_trade_decision"] == "hold"


def test_live_identity_is_injected_for_execution_but_not_persisted(runner):
    observed = {}

    class IdentityRunner(FakeRunner):
        def _run_analyst(self, session, state, stage):
            observed["mode"] = state["analysis_mode"]
            observed["cutoff"] = state["analysis_cutoff"]
            return {
                "market_report": "market",
                # Even a node echo cannot duplicate immutable identity into state.
                "analysis_mode": "tampered",
                "analysis_cutoff": "2099-01-01T00:00:00+07:00",
            }

    stage_runner = IdentityRunner(config=runner.config, graph_factory=FakeGraph)
    session = stage_runner.create_session(
        "HPG",
        "2026-08-12",
        selected_analysts=("market",),
        analysis_mode="live",
        analysis_cutoff=datetime(
            2026, 8, 12, 16, 5, 31, 123456, ZoneInfo("Asia/Ho_Chi_Minh")
        ),
    )
    stage_runner.run_stage(session, "market")

    assert observed == {
        "mode": "live",
        "cutoff": "2026-08-12T16:05:31.123456+07:00",
    }
    assert "analysis_mode" not in session.state
    assert "analysis_cutoff" not in session.state
    reloaded = type(session).load(session.path())
    assert reloaded.analysis_cutoff == session.analysis_cutoff
    assert "analysis_cutoff" not in reloaded.state


def test_live_risk_decision_does_not_write_long_term_memory(runner):
    class RecordingMemory:
        def __init__(self):
            self.decisions = []

        def store_decision(self, **decision):
            self.decisions.append(decision)

    class MemoryGraph(FakeGraph):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.memory_log = RecordingMemory()

    stage_runner = FakeRunner(config=runner.config, graph_factory=MemoryGraph)
    live = stage_runner.create_session(
        "HPG",
        "2026-08-12",
        selected_analysts=("market",),
        analysis_mode="live",
        analysis_cutoff="2026-08-12T16:05:00+07:00",
    )
    stage_runner.run_default(live)
    memory = stage_runner._graph(("market",)).memory_log
    assert memory.decisions == []

    close = stage_runner.create_session(
        "HPG", "2026-08-13", selected_analysts=("market",)
    )
    stage_runner.run_default(close)
    assert len(memory.decisions) == 1
    assert memory.decisions[0]["trade_date"] == "2026-08-13"


def test_message_deltas_are_appended_across_multiple_tool_rounds():
    state = {"messages": []}
    first = AIMessage(
        content="",
        tool_calls=[{"name": "one", "args": {}, "id": "call-1", "type": "tool_call"}],
    )
    TradingAgentsStageRunner._merge_state(state, {"messages": [first]})
    TradingAgentsStageRunner._merge_state(
        state,
        {"messages": [ToolMessage(content="one result", tool_call_id="call-1")]},
    )
    second = AIMessage(
        content="",
        tool_calls=[{"name": "two", "args": {}, "id": "call-2", "type": "tool_call"}],
    )
    TradingAgentsStageRunner._merge_state(state, {"messages": [second]})

    assert [message.type for message in state["messages"]] == ["ai", "tool", "ai"]


def test_isolated_analyst_supplies_langgraph_runtime_to_tool_node(
    runner, monkeypatch
):
    stage_runner = TradingAgentsStageRunner(
        config=runner.config,
        graph_factory=FakeGraph,
    )

    class RecordingToolNode:
        def __init__(self):
            self.runtime = None

        def invoke(self, state, *, runtime=None):
            self.runtime = runtime
            return {
                "messages": [
                    ToolMessage(content="tool result", tool_call_id="call-1")
                ]
            }

    tool_node = RecordingToolNode()
    graph = stage_runner._graph(("market",))
    graph.tool_nodes["market"] = tool_node

    calls = 0

    def analyst_factory(_llm):
        def analyst_node(_state):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "market_tool",
                                    "args": {},
                                    "id": "call-1",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    ]
                }
            return {
                "messages": [AIMessage(content="done")],
                "market_report": "market report",
            }

        return analyst_node

    monkeypatch.setattr(
        "tradingagents.graph.stage_runner.create_market_analyst", analyst_factory
    )
    session = stage_runner.create_session(
        "HPG", "2026-08-12", selected_analysts=("market",)
    )
    stage_runner.run_stage(session, "market")

    assert isinstance(tool_node.runtime, Runtime)
    assert session.state["market_report"] == "market report"


def test_live_market_snapshot_is_prefetched_once_before_model_rounds(
    runner, monkeypatch
):
    stage_runner = TradingAgentsStageRunner(
        config=runner.config,
        graph_factory=FakeGraph,
    )
    snapshot_calls = []

    def snapshot(ticker, cutoff, *, include_live_quote=False):
        snapshot_calls.append((ticker, cutoff, include_live_quote))
        return "frozen OHLCV + separate quote"

    monkeypatch.setattr(
        "tradingagents.graph.stage_runner.build_verified_market_snapshot",
        snapshot,
    )
    monkeypatch.setattr(
        "tradingagents.graph.stage_runner.get_verified_price_reference",
        lambda ticker, cutoff: {
            "status": "available",
            "ticker": ticker,
            "close": "63300",
            "currency": "VND",
            "price_unit": "VND",
            "session_date": "2026-08-19",
            "analysis_cutoff": cutoff,
            "source": "gx_market_info",
            "point_in_time_quality": "exact",
        },
    )

    class RecordingToolNode:
        def invoke(self, _state, *, runtime=None):
            assert isinstance(runtime, Runtime)
            return {
                "messages": [
                    ToolMessage(content="stock data", tool_call_id="call-1")
                ]
            }

    calls = 0

    def analyst_factory(_llm):
        def analyst_node(state):
            nonlocal calls
            calls += 1
            assert state["verified_market_snapshot"] == (
                "frozen OHLCV + separate quote"
            )
            if calls == 1:
                return {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "get_stock_data",
                                    "args": {},
                                    "id": "call-1",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    ]
                }
            return {
                "messages": [AIMessage(content="done")],
                "market_report": "market report",
            }

        return analyst_node

    monkeypatch.setattr(
        "tradingagents.graph.stage_runner.create_market_analyst",
        analyst_factory,
    )
    stage_runner._graph(("market",)).tool_nodes["market"] = RecordingToolNode()
    session = stage_runner.create_session(
        "HPG",
        "2026-08-19",
        selected_analysts=("market",),
        analysis_mode="live",
        analysis_cutoff="2026-08-19T16:05:31.123456+07:00",
    )

    stage_runner.run_stage(session, "market")

    assert snapshot_calls == [
        ("HPG", "2026-08-19T16:05:31.123456+07:00", True)
    ]
    assert calls == 2
    assert "verified_market_snapshot" not in session.state
    assert session.state["market_price_reference"]["close"] == "63300"


def test_market_price_reference_is_persisted_and_risk_does_not_refetch(
    runner, monkeypatch
):
    reference_calls = []

    def reference(ticker, cutoff):
        reference_calls.append((ticker, cutoff))
        return {
            "status": "available",
            "ticker": ticker,
            "close": "63300",
            "currency": "VND",
            "price_unit": "VND",
            "session_date": "2026-08-12",
            "analysis_cutoff": cutoff,
            "source": "gx_market_info",
            "point_in_time_quality": "exact",
        }

    monkeypatch.setattr(
        "tradingagents.graph.stage_runner.get_verified_price_reference", reference
    )
    monkeypatch.setattr(
        "tradingagents.graph.stage_runner.create_market_analyst",
        lambda _llm: lambda _state: {
            "messages": [AIMessage(content="done")],
            "market_report": "market report",
        },
    )
    stage_runner = TradingAgentsStageRunner(
        config=runner.config,
        graph_factory=FakeGraph,
    )
    stage_runner._graph(("market",)).tool_nodes["market"] = object()
    session = stage_runner.create_session(
        "MWG", "2026-08-12", selected_analysts=("market",)
    )
    stage_runner.run_stage(session, "market")

    assert reference_calls == [("MWG", "2026-08-12T15:00:00+07:00")]
    assert session.state["market_price_reference"]["close"] == "63300"
    reloaded = type(session).load(session.path())
    assert reloaded.state["market_price_reference"] == session.state[
        "market_price_reference"
    ]

    # Build valid downstream prerequisites without invoking an LLM, then prove
    # Risk consumes the persisted reference without touching the market source.
    reloaded.complete(
        "research",
        {"investment_debate_state": {"history": "debate"}, "investment_plan": "plan"},
    )
    reloaded.complete("trader", {"trader_investment_plan": "trade"})
    captured = {}

    class CaptureRiskRunner(TradingAgentsStageRunner):
        def _run_risk(self, _session, state):
            captured["reference"] = state["market_price_reference"]
            return {
                "risk_debate_state": {"history": "risk"},
                "final_trade_decision": "hold",
            }

    risk_runner = CaptureRiskRunner(config=runner.config, graph_factory=FakeGraph)
    monkeypatch.setattr(
        "tradingagents.graph.stage_runner.get_verified_price_reference",
        lambda *_args, **_kwargs: pytest.fail("Risk must not refetch market data"),
    )
    risk_runner.run_stage(reloaded, "risk")

    assert captured["reference"]["analysis_cutoff"] == reloaded.analysis_cutoff


def test_session_does_not_persist_langchain_messages(runner):
    session = runner.create_session("HPG", "2026-08-12", selected_analysts=("market",))
    assert "messages" not in session.state


def test_no_data_failure_is_persisted_as_unavailable(runner):
    session = runner.create_session("HPG", "2026-08-12", selected_analysts=("market",))

    def no_data(*args, **kwargs):
        raise NoMarketDataError("HPG")

    runner._run_analyst = no_data
    with pytest.raises(NoMarketDataError):
        runner.run_stage(session, "market")

    reloaded = type(session).load(session.path())
    assert reloaded.stage_status["market"] == "unavailable"
    assert reloaded.completed_stages == []


def test_research_hydrates_missing_unavailable_analyst_report(runner):
    session = runner.create_session(
        "HPG", "2026-08-12", selected_analysts=("market", "news")
    )
    runner.run_stage(session, "market")

    def no_data(*args, **kwargs):
        raise NoMarketDataError("news unavailable")

    runner._run_analyst = no_data
    with pytest.raises(NoMarketDataError):
        runner.run_stage(session, "news")
    assert "news_report" not in session.state

    def research_with_existing_prompt_contract(session, state):
        assert state["market_report"] == "market"
        assert state["news_report"].startswith("[UNAVAILABLE:")
        assert state["sentiment_report"].startswith("[NOT_RUN:")
        assert state["fundamentals_report"].startswith("[NOT_RUN:")
        return {"investment_plan": "plan", "investment_debate_state": {}}

    runner._run_research = research_with_existing_prompt_contract
    runner.run_stage(session, "research")
    assert session.stage_status["research"] == "completed"


def test_failure_text_redacts_credentials(monkeypatch):
    monkeypatch.setenv("GX_ANALYSIS_DATA_API_KEY", "super-secret")
    monkeypatch.setenv("FRED_API_KEY", "fred-secret-canary")
    error = RuntimeError(
        "https://user:pass@example.test/x?token=super-secret "
        "postgresql://gdev:Apg@161VVT@db.internal/market fred-secret-canary"
    )
    safe = TradingAgentsStageRunner._safe_error(error)
    assert "user:pass" not in safe
    assert "super-secret" not in safe
    assert "Apg@161VVT" not in safe
    assert "fred-secret-canary" not in safe


def test_sentiment_and_news_metadata_warnings_are_redacted(monkeypatch):
    secret = "durable-warning-secret"
    monkeypatch.setenv("VN_MEDIA_ARCHIVE_ENCRYPTION_KEY", secret)
    unsafe = f"postgresql://user:password@db.internal/private?token={secret}"
    detached = (
        "Authorization: Bearer detached-sensitive-token; "
        "access_token=detached-query-token password=plain-password "
        "secret=plain-secret"
    )

    sentiment = TradingAgentsStageRunner._public_sentiment_metadata(
        {
            "status": "partial",
            "warnings": [unsafe, detached],
            "media_tone": {"status": "partial", "warnings": [unsafe]},
        }
    )
    news = TradingAgentsStageRunner._public_news_metadata(
        {
            "status": "partial",
            "warnings": [unsafe, detached],
            "editorial_media": {
                "status": "partial",
                "warnings": [unsafe],
                "sources": [
                    {
                        "provider": "cafef_rss",
                        "status": "partial",
                        "warnings": [unsafe],
                    }
                ],
            },
        }
    )

    serialized = str({"sentiment": sentiment, "news": news})
    assert secret not in serialized
    assert "user:password" not in serialized
    assert "detached-sensitive-token" not in serialized
    assert "detached-query-token" not in serialized
    assert "plain-password" not in serialized
    assert "plain-secret" not in serialized
    assert serialized.count("<redacted>") >= 4


def test_data_transport_identity_matches_programmatic_gx_config(monkeypatch):
    monkeypatch.setenv("GX_DATA_TRANSPORT", "api")
    identity = data_transport_identity(
        {
            "gx_market_info": {
                "transport": "postgres",
                "expected_database": "custom_market_db",
            }
        }
    )

    assert identity == {
        "transport": "postgres",
        "expected_database": "custom_market_db",
    }


def test_memory_context_excludes_same_date_and_future_entries(monkeypatch, tmp_path):
    monkeypatch.setenv("GX_DATA_TRANSPORT", "api")
    monkeypatch.setenv("TRADINGAGENTS_STAGE_RUNS_DIR", str(tmp_path))

    class FakeMemory:
        def load_entries(self):
            base = {
                "ticker": "HPG",
                "rating": "BUY",
                "raw": "+1%",
                "alpha": "+0.5%",
                "holding": "5d",
                "pending": False,
                "reflection": "lesson",
            }
            return [
                {**base, "date": "2026-08-10", "decision": "past-decision"},
                {**base, "date": "2026-08-12", "decision": "same-day-decision"},
                {**base, "date": "2026-08-13", "decision": "future-decision"},
            ]

    class FakeMemoryGraph(FakeGraph):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.memory_log = FakeMemory()

    stage_runner = FakeRunner(
        config={
            "llm_provider": "ollama",
            "quick_think_llm": "qwen3:8b",
            "deep_think_llm": "qwen3:8b",
        },
        graph_factory=FakeMemoryGraph,
    )
    session = stage_runner.create_session(
        "HPG", "2026-08-12", selected_analysts=("market",)
    )

    assert "past-decision" in session.state["past_context"]
    assert "same-day-decision" not in session.state["past_context"]
    assert "future-decision" not in session.state["past_context"]


def test_analyst_metadata_declares_stage_specific_vendor_chains(runner):
    runner.config.update(
        {
            "data_vendors": {
                "news_data": "yfinance",
                "macro_data": "fred",
                "prediction_markets": "polymarket",
            },
            "tool_vendors": {"get_news": "gx_market_info,yfinance"},
            "gx_market_info": {"strict_point_in_time": True},
        }
    )
    session = runner.create_session(
        "HPG", "2026-08-12", selected_analysts=("news",)
    )
    runner.run_stage(session, "news")

    sources = {
        item["tool"]: item for item in session.stage_metadata["news"]["sources"]
    }
    assert sources["get_news"]["vendor_chain"] == ["gx_market_info", "yfinance"]
    assert sources["get_news"]["gx_profile"]["strict_point_in_time"] is True
    assert sources["get_global_news"]["vendor_chain"] == ["yfinance"]
    assert sources["get_macro_indicators"]["vendor_chain"] == ["fred"]
    assert sources["get_prediction_markets"]["vendor_chain"] == ["polymarket"]
    assert all(not item["actual_vendor_observed"] for item in sources.values())


def test_social_profile_identity_is_public_and_stable(tmp_path):
    first = social_profile_identity(
        {
            "vn_social": {
                "provider": "fireant",
                "lookback_days": 7,
                "min_posts": 10,
                "min_unique_authors": 5,
                "archive_path": str(tmp_path / "social.sqlite3"),
                "prompt_version": "vn-social-v1",
                "authorized": True,
                "hosted_llm_authorized": False,
                "legacy_sources_enabled": False,
            }
        }
    )
    second = social_profile_identity(
        {
            "vn_social": {
                "provider": "fireant",
                "lookback_days": 7,
                "min_posts": 10,
                "min_unique_authors": 5,
                "archive_path": str(tmp_path / "social.sqlite3"),
                "prompt_version": "vn-social-v1",
                "authorized": True,
                "hosted_llm_authorized": False,
                "legacy_sources_enabled": False,
            }
        }
    )

    assert first == second
    assert len(first["archive_id"]) == 16
    assert "archive_path" not in first
    assert "token" not in first


def test_sentiment_actual_provenance_is_persisted_without_raw_identity(runner):
    metadata = {
        "status": "partial",
        "retail_social_signal": {
            "provider": "fireant",
            "status": "partial",
            "sample_size": 12,
            "unique_authors": 7,
            "point_in_time_quality": "partial",
            "warnings": ["archive gap"],
            "actual_vendor_observed": True,
            "raw_posts": ["must not persist"],
            "author": {"name": "must not persist"},
        },
        "media_tone": {
            "provider": "gx_market_info",
            "status": "available",
            "sample_size": 5,
            "unique_authors": 0,
            "warnings": [],
            "actual_vendor_observed": True,
        },
    }

    class MetadataRunner(FakeRunner):
        def _run_analyst(self, session, state, stage):
            return {
                "sentiment_report": "partial report",
                "sentiment_source_metadata": metadata,
            }

    social_runner = MetadataRunner(config=runner.config, graph_factory=FakeGraph)
    session = social_runner.create_session(
        "HPG", "2026-08-12", selected_analysts=("sentiment",)
    )
    social_runner.run_stage(session, "sentiment")

    stored = session.state["sentiment_source_metadata"]
    assert stored["retail_social_signal"]["provider"] == "fireant"
    assert "raw_posts" not in stored["retail_social_signal"]
    assert "author" not in stored["retail_social_signal"]
    assert session.stage_metadata["sentiment"]["warnings"] == ["archive gap"]
    assert any(
        item.get("actual_vendor_observed") is True
        for item in session.stage_metadata["sentiment"]["sources"]
    )


def test_gx_media_profile_uses_split_news_sources(runner, tmp_path):
    runner.config.update(
        {
            "data_vendors": {
                "news_data": "yfinance",
                "social_data": "fireant",
                "macro_data": "fred",
                "prediction_markets": "polymarket",
            },
            "tool_vendors": {
                "get_disclosures": "gx_market_info",
                "get_editorial_news": "vn_media",
                "get_global_news": "yfinance",
            },
            "gx_market_info": {"strict_point_in_time": True},
            "vn_media": {
                "providers": "cafef_rss,vnexpress_rss",
                "archive_path": str(tmp_path / "media.sqlite3"),
                "lookback_days": 7,
                "min_articles": 3,
                "archive_schema_version": 1,
                "alias_policy_version": "vn-media-alias-v1",
                "prompt_version": "vn-media-v1",
            },
        }
    )
    session = runner.create_session(
        "HPG", "2026-08-12", selected_analysts=("news",)
    )
    runner.run_stage(session, "news")

    sources = {
        item["tool"]: item
        for item in session.stage_metadata["news"]["sources"]
        if "tool" in item
    }
    assert "get_news" not in sources
    assert sources["get_disclosures"]["vendor_chain"] == ["gx_market_info"]
    assert sources["get_editorial_news"]["vendor_chain"] == ["vn_media"]


def test_media_profile_identity_and_runtime_state_exclude_secrets(
    runner, tmp_path, monkeypatch
):
    monkeypatch.setenv("TRADINGAGENTS_CAFEF_RSS_AUTHORIZED", "true")
    monkeypatch.setenv("VN_MEDIA_ARCHIVE_ENCRYPTION_KEY", "secret-key")
    media_config = {
        "providers": "cafef_rss",
        "archive_path": str(tmp_path / "media.sqlite3"),
        "lookback_days": 7,
        "min_articles": 3,
        "archive_schema_version": 1,
        "alias_policy_version": "vn-media-alias-v1",
        "prompt_version": "vn-media-v1",
    }
    identity = media_profile_identity({"vn_media": media_config})

    assert identity["providers"] == ["cafef_rss"]
    assert len(identity["archive_id"]) == 16
    assert "archive_path" not in identity
    assert "authorized" not in identity
    assert "encryption_key" not in identity

    captured = {}

    class CapturingRunner(FakeRunner):
        def _run_analyst(self, session, state, stage):
            captured.update(state)
            return {"sentiment_report": "media evidence"}

    config = {**runner.config, "vn_media": media_config}
    media_runner = CapturingRunner(config=config, graph_factory=FakeGraph)
    session = media_runner.create_session(
        "HPG", "2026-08-12", selected_analysts=("sentiment",)
    )
    media_runner.run_stage(session, "sentiment")

    assert captured["media_profile"] == session.media_profile
    assert captured["media_profile_fingerprint"] == media_profile_fingerprint(
        session.media_profile
    )
    serialized = session.to_dict()
    assert "media_profile" not in serialized["state"]
    assert "secret-key" not in str(serialized)


def test_media_profile_identity_reads_archive_uuid_without_opening_for_write(tmp_path):
    import sqlite3

    archive_path = tmp_path / "media.sqlite3"
    with sqlite3.connect(archive_path) as connection:
        connection.execute(
            "CREATE TABLE archive_meta ("
            "singleton INTEGER PRIMARY KEY, schema_version INTEGER, "
            "archive_id TEXT, key_verifier TEXT)"
        )
        connection.execute(
            "INSERT INTO archive_meta VALUES (1, 1, 'archive-from-db', 'verifier')"
        )
    before = archive_path.stat().st_mtime_ns

    identity = media_profile_identity(
        {
            "vn_media": {
                "providers": "cafef_rss",
                "archive_path": str(archive_path),
                "lookback_days": 7,
                "min_articles": 3,
                "archive_schema_version": 1,
                "alias_policy_version": "vn-media-alias-v1",
                "prompt_version": "vn-media-v1",
            }
        }
    )

    assert identity["archive_id"] == "archive-from-db"
    assert archive_path.stat().st_mtime_ns == before


def test_macro_profile_identity_is_public_and_news_runtime_state_is_transient(
    runner, tmp_path
):
    macro_config = {
        "enabled": True,
        "providers": "nso_sdmx,nso_release,sbv_html",
        "archive_path": str(tmp_path / "macro.sqlite3"),
        "lookback_months": 24,
        "strict_point_in_time": True,
        "indicator_set_version": "vn-macro-v1",
        "prompt_version": "vn-macro-v1",
        "archive_schema_version": 1,
    }
    identity = macro_profile_identity({"vn_macro": macro_config})
    assert identity["provider"] == "vn_macro"
    assert identity["providers"] == ["nso_sdmx", "nso_release", "sbv_html"]
    assert len(identity["archive_id"]) == 16
    assert "archive_path" not in identity

    captured = {}

    class CapturingRunner(FakeRunner):
        def _run_analyst(self, session, state, stage):
            captured.update(state)
            return {"news_report": "Vietnam macro evidence"}

    config = {
        **runner.config,
        "vn_macro": macro_config,
        "data_vendors": {
            "news_data": "yfinance",
            "macro_data": "fred",
            "vn_macro_data": "vn_macro",
            "prediction_markets": "polymarket",
        },
        "tool_vendors": {"get_vietnam_macro_context": "vn_macro"},
    }
    macro_runner = CapturingRunner(config=config, graph_factory=FakeGraph)
    session = macro_runner.create_session(
        "HPG", "2026-08-12", selected_analysts=("news",)
    )
    macro_runner.run_stage(session, "news")

    assert captured["macro_profile"] == session.macro_profile
    assert captured["macro_profile_fingerprint"] == macro_profile_fingerprint(
        session.macro_profile
    )
    assert "macro_profile" not in session.to_dict()["state"]
    tools = {
        item["tool"]: item
        for item in session.stage_metadata["news"]["sources"]
        if "tool" in item
    }
    assert "get_macro_indicators" not in tools
    assert "get_news" not in tools
    assert "get_disclosures" in tools
    assert tools["get_vietnam_macro_context"]["vendor_chain"] == ["vn_macro"]


def test_macro_profile_identity_reads_archive_uuid_without_writing(tmp_path):
    import sqlite3

    archive_path = tmp_path / "macro.sqlite3"
    with sqlite3.connect(archive_path) as connection:
        connection.execute(
            "CREATE TABLE archive_meta ("
            "singleton INTEGER PRIMARY KEY, schema_version INTEGER, archive_id TEXT)"
        )
        connection.execute(
            "INSERT INTO archive_meta VALUES (1, 1, 'macro-archive-from-db')"
        )
    before = archive_path.stat().st_mtime_ns

    identity = macro_profile_identity(
        {
            "vn_macro": {
                "enabled": True,
                "providers": "nso_sdmx",
                "archive_path": str(archive_path),
                "lookback_months": 24,
                "strict_point_in_time": True,
                "indicator_set_version": "vn-macro-v1",
                "prompt_version": "vn-macro-v1",
            }
        }
    )

    assert identity["archive_id"] == "macro-archive-from-db"
    assert archive_path.stat().st_mtime_ns == before


def test_news_macro_provenance_is_sanitized_and_counts_as_usable(runner):
    metadata = {
        "status": "partial",
        "official_disclosures": {"status": "unavailable", "sample_size": 0},
        "editorial_media": {"status": "unavailable", "sample_size": 0},
        "vn_macro": {
            "provider": "vn_macro",
            "status": "partial",
            "sample_size": 4,
            "observation_count": 4,
            "as_of": "2026-08-12T15:00:00+07:00",
            "fetch_ids": ["fetch-1"],
            "point_in_time_quality": "partial",
            "stale": True,
            "stale_indicators": ["vn_credit_growth"],
            "warnings": ["SBV source is stale"],
            "actual_vendor_observed": True,
            "source_results": [
                {
                    "provider": "nso_sdmx",
                    "status": "available",
                    "fetch_id": "fetch-1",
                    "count": 4,
                    "warnings": [],
                    "raw_response": "must-not-persist",
                }
            ],
            "observations": [{"value": "must-not-persist"}],
        },
    }

    class MetadataRunner(FakeRunner):
        def _run_analyst(self, session, state, stage):
            return {
                "news_report": "Macro-only context remains usable.",
                "news_source_metadata": metadata,
            }

    macro_runner = MetadataRunner(config=runner.config, graph_factory=FakeGraph)
    session = macro_runner.create_session(
        "HPG", "2026-08-12", selected_analysts=("news",)
    )
    macro_runner.run_stage(session, "news")

    assert session.stage_status["news"] == "completed"
    stored = session.state["news_source_metadata"]["vn_macro"]
    assert stored["observation_count"] == 4
    assert stored["source_results"][0]["count"] == 4
    assert "raw_response" not in stored["source_results"][0]
    assert "observations" not in stored
    assert session.stage_metadata["news"]["warnings"] == ["SBV source is stale"]


def test_news_unavailable_only_when_no_ticker_news_evidence(runner):
    unavailable = {
        "status": "unavailable",
        "official_disclosures": {"status": "unavailable", "sample_size": 0},
        "editorial_media": {"status": "unavailable", "sample_size": 0},
    }

    class NewsMetadataRunner(FakeRunner):
        def _run_analyst(self, session, state, stage):
            return {
                "news_report": "No ticker-news evidence is available.",
                "news_source_metadata": unavailable,
            }

    news_runner = NewsMetadataRunner(config=runner.config, graph_factory=FakeGraph)
    session = news_runner.create_session(
        "HPG", "2026-08-12", selected_analysts=("news",)
    )
    news_runner.run_stage(session, "news")

    assert session.stage_status["news"] == "unavailable"
    assert session.state["news_source_metadata"]["status"] == "unavailable"

    unavailable["editorial_media"] = {
        "status": "partial",
        "sample_size": 1,
        "sources": [
            {
                "provider": "cafef_rss",
                "status": "partial",
                "sample_size": 1,
                "raw_articles": ["must not persist"],
            }
        ],
    }
    second = news_runner.create_session(
        "HPG", "2026-08-12", selected_analysts=("news",)
    )
    news_runner.run_stage(second, "news")
    assert second.stage_status["news"] == "completed"
    stored_source = second.state["news_source_metadata"]["editorial_media"]["sources"][0]
    assert stored_source["provider"] == "cafef_rss"
    assert "raw_articles" not in stored_source
