from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tradingagents.graph.stage_session import StageSession


def _session():
    return StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        selected_analysts=("market", "fundamentals"),
        llm={"provider": "ollama", "quick_model": "qwen3:8b"},
        data_transport={"transport": "api", "base_url": "http://gx"},
        run_id="run-1",
    )


def test_session_round_trip_and_version(tmp_path):
    session = _session()
    destination = tmp_path / "session.json"
    session.complete("market", {"market_report": "market"})
    session.save(destination)

    loaded = StageSession.load(destination)
    assert loaded.to_dict() == session.to_dict()
    assert json.loads(destination.read_text())["schema_version"] == 6
    assert loaded.analysis_mode == "close"
    assert loaded.analysis_cutoff == "2026-08-12T15:00:00+07:00"
    assert loaded.stage_status["market"] == "completed"
    assert len(loaded.stage_metadata["market"]["input_fingerprint"]) == 64


def test_replacing_upstream_stage_invalidates_downstream_outputs():
    session = _session()
    session.complete("market", {"market_report": "v1"})
    session.complete("fundamentals", {"fundamentals_report": "fundamentals"})
    session.complete(
        "research",
        {"investment_plan": "plan", "investment_debate_state": {"history": "debate"}},
    )
    session.complete("trader", {"trader_investment_plan": "trade"})
    session.complete(
        "risk", {"final_trade_decision": "hold", "risk_debate_state": {"history": "risk"}}
    )

    session.complete("market", {"market_report": "v2"})

    assert session.completed_stages == ["market", "fundamentals"]
    assert session.state["market_report"] == "v2"
    assert "investment_plan" not in session.state
    assert "trader_investment_plan" not in session.state
    assert "final_trade_decision" not in session.state


def test_identity_cannot_change_during_resume():
    session = _session()
    with pytest.raises(ValueError, match="immutable.*llm"):
        session.assert_identity(
            ticker="HPG",
            analysis_date="2026-08-12",
            asset_type="stock",
            selected_analysts=("market", "fundamentals"),
            llm={"provider": "openai", "quick_model": "gpt"},
            data_transport={"transport": "api", "base_url": "http://gx"},
        )


def test_unknown_schema_version_fails_loudly(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"schema_version": 999}))
    with pytest.raises(ValueError, match="unsupported session schema_version"):
        StageSession.load(path)


@pytest.mark.parametrize(
    ("llm", "transport", "field"),
    [
        (
            {"provider": "openai", "api_key": "session-secret"},
            {"transport": "api"},
            "api_key",
        ),
        (
            {"provider": "openai"},
            {
                "transport": "postgres",
                "database_url": "postgresql://user:password@db/private",
            },
            "database_url",
        ),
    ],
)
def test_session_create_rejects_sensitive_identity_fields(llm, transport, field):
    with pytest.raises(ValueError, match=field):
        StageSession.create(
            ticker="HPG",
            analysis_date="2026-08-12",
            llm=llm,
            data_transport=transport,
        )


def test_session_load_rejects_sensitive_legacy_identity_fields(tmp_path):
    payload = _session().to_dict()
    payload["schema_version"] = 2
    payload["llm"]["api_key"] = "must-not-load"
    payload["data_transport"]["database_url"] = (
        "postgresql://user:password@db/private"
    )
    path = tmp_path / "sensitive-v2.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="unsupported/sensitive.*api_key"):
        StageSession.load(path)


def test_session_save_revalidates_mutated_identity(tmp_path):
    session = _session()
    session.llm["api_key"] = "mutated-secret"

    with pytest.raises(ValueError, match="unsupported/sensitive.*api_key"):
        session.save(tmp_path / "session.json")
    assert not (tmp_path / "session.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backend_url", "https://user:password@llm.example/v1"),
        ("backend_url", "https://llm.example/v1?api_key=secret"),
    ],
)
def test_session_rejects_credential_bearing_public_endpoints(field, value):
    with pytest.raises(ValueError, match="must not contain credentials"):
        StageSession.create(
            ticker="HPG",
            analysis_date="2026-08-12",
            llm={"provider": "openai_compatible", field: value},
            data_transport={"transport": "api"},
        )


def test_failed_rerun_removes_stale_stage_and_downstream_outputs():
    session = _session()
    session.complete("market", {"market_report": "old"})
    session.complete("research", {"investment_plan": "old plan"})
    session.complete("trader", {"trader_investment_plan": "old trade"})

    session.record_failure("market", "safe failure")

    assert session.stage_status["market"] == "failed"
    assert session.stage_status["research"] == "not_run"
    assert session.stage_status["trader"] == "not_run"
    assert "market_report" not in session.state
    assert "investment_plan" not in session.state
    assert "trader_investment_plan" not in session.state


def test_input_fingerprint_ignores_stale_downstream_outputs():
    session = _session()
    session.complete("market", {"market_report": "market"})
    session.complete("research", {"investment_plan": "plan"})
    before = session.input_fingerprint("market")
    session.state["final_trade_decision"] = "stale downstream"
    assert session.input_fingerprint("market") == before


def test_transient_messages_are_never_serialized_or_reloaded(tmp_path):
    session = _session()
    session.state["messages"] = [{"role": "user", "content": "transient"}]
    path = session.save(tmp_path / "session.json")

    assert "messages" not in json.loads(path.read_text())["state"]

    payload = session.to_dict()
    payload["state"]["messages"] = [{"role": "user", "content": "legacy"}]
    path.write_text(json.dumps(payload))
    assert "messages" not in StageSession.load(path).state


def test_v1_session_migrates_to_legacy_social_profile(tmp_path):
    payload = _session().to_dict()
    payload["schema_version"] = 1
    payload.pop("social_profile")
    path = tmp_path / "v1.json"
    path.write_text(json.dumps(payload))

    loaded = StageSession.load(path)

    assert loaded.schema_version == 6
    assert loaded.social_profile == {"provider": "legacy"}
    assert loaded.media_profile == {"provider": "legacy"}
    assert loaded.macro_profile == {"provider": "legacy"}


def test_v2_session_migrates_to_legacy_media_profile(tmp_path):
    payload = _session().to_dict()
    payload["schema_version"] = 2
    payload.pop("media_profile")
    path = tmp_path / "v2.json"
    path.write_text(json.dumps(payload))

    loaded = StageSession.load(path)

    assert loaded.schema_version == 6
    assert loaded.social_profile == {"provider": "legacy"}
    assert loaded.media_profile == {"provider": "legacy"}
    assert loaded.macro_profile == {"provider": "legacy"}


def test_v3_session_migrates_flat_llm_identity_to_quick_and_deep(tmp_path):
    payload = _session().to_dict()
    payload["schema_version"] = 3
    payload["llm"] = {
        "provider": "ollama",
        "backend_url": "http://127.0.0.1:11434/v1",
        "quick_model": "quick:8b",
        "deep_model": "deep:14b",
        "output_language": "Vietnamese",
    }
    path = tmp_path / "v3.json"
    path.write_text(json.dumps(payload))

    loaded = StageSession.load(path)

    assert loaded.schema_version == 6
    assert loaded.llm["quick"] == {
        "provider": "ollama",
        "model": "quick:8b",
        "base_url": "http://127.0.0.1:11434/v1",
    }
    assert loaded.llm["deep"]["model"] == "deep:14b"
    assert loaded.llm["output_language"] == "Vietnamese"
    assert loaded.macro_profile == {"provider": "legacy"}


def test_v4_session_migrates_to_legacy_macro_profile(tmp_path):
    payload = _session().to_dict()
    payload["schema_version"] = 4
    payload.pop("macro_profile")
    path = tmp_path / "v4.json"
    path.write_text(json.dumps(payload))

    loaded = StageSession.load(path)

    assert loaded.schema_version == 6
    assert loaded.macro_profile == {"provider": "legacy"}


def test_v5_session_migrates_to_deterministic_close_cutoff(tmp_path):
    payload = _session().to_dict()
    payload["schema_version"] = 5
    payload.pop("analysis_mode")
    payload.pop("analysis_cutoff")
    path = tmp_path / "v5.json"
    path.write_text(json.dumps(payload))

    loaded = StageSession.load(path)

    assert loaded.schema_version == 6
    assert loaded.analysis_mode == "close"
    assert loaded.analysis_cutoff == "2026-08-12T15:00:00+07:00"


def test_live_session_requires_aware_cutoff_on_same_vietnam_date():
    with pytest.raises(ValueError, match="required for live"):
        StageSession.create(
            ticker="HPG",
            analysis_date="2026-08-12",
            analysis_mode="live",
            llm={"provider": "fake"},
            data_transport={"transport": "api"},
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        StageSession.create(
            ticker="HPG",
            analysis_date="2026-08-12",
            analysis_mode="live",
            analysis_cutoff=datetime(2026, 8, 12, 16, 5),
            llm={"provider": "fake"},
            data_transport={"transport": "api"},
        )
    with pytest.raises(ValueError, match="must match analysis_date"):
        StageSession.create(
            ticker="HPG",
            analysis_date="2026-08-12",
            analysis_mode="live",
            analysis_cutoff="2026-08-13T00:01:00+07:00",
            llm={"provider": "fake"},
            data_transport={"transport": "api"},
        )


def test_live_cutoff_is_canonical_vietnam_time_and_round_trips(tmp_path):
    session = StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        analysis_mode="live",
        analysis_cutoff=datetime(2026, 8, 12, 9, 5, 31, 123456, timezone.utc),
        llm={"provider": "fake"},
        data_transport={"transport": "api"},
    )
    assert session.analysis_cutoff == "2026-08-12T16:05:31.123456+07:00"
    assert StageSession.load(session.save(tmp_path / "live.json")).analysis_cutoff == (
        session.analysis_cutoff
    )
    assert "analysis_mode" not in session.to_dict()["state"]
    assert "analysis_cutoff" not in session.to_dict()["state"]


def test_live_cutoff_cannot_be_mutated_or_hand_edited(tmp_path):
    session = StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        analysis_mode="live",
        analysis_cutoff="2026-08-12T16:05:00+07:00",
        llm={"provider": "fake"},
        data_transport={"transport": "api"},
    )
    with pytest.raises(AttributeError, match="immutable"):
        session.analysis_cutoff = "2026-08-12T16:10:00+07:00"

    payload = session.to_dict()
    payload["analysis_cutoff"] = "2026-08-12T16:10:00+07:00"
    path = tmp_path / "edited.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="fingerprint does not match"):
        StageSession.load(path)

    payload.pop("analysis_identity_fingerprint")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="missing.*identity fingerprint"):
        StageSession.load(path)


def test_close_mode_rejects_non_close_cutoff():
    with pytest.raises(ValueError, match="must be 15:00:00"):
        StageSession.create(
            ticker="HPG",
            analysis_date="2026-08-12",
            analysis_mode="close",
            analysis_cutoff="2026-08-12T16:00:00+07:00",
            llm={"provider": "fake"},
            data_transport={"transport": "api"},
        )


def test_analysis_cutoff_changes_every_stage_fingerprint_and_identity():
    first = StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        analysis_mode="live",
        analysis_cutoff="2026-08-12T16:00:00+07:00",
        llm={"provider": "fake"},
        data_transport={"transport": "api"},
    )
    second = StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        analysis_mode="live",
        analysis_cutoff="2026-08-12T16:05:00+07:00",
        llm={"provider": "fake"},
        data_transport={"transport": "api"},
    )

    for stage in (
        "market",
        "sentiment",
        "news",
        "fundamentals",
        "research",
        "trader",
        "risk",
    ):
        assert first.input_fingerprint(stage) != second.input_fingerprint(stage)
    with pytest.raises(ValueError, match="immutable.*analysis_cutoff"):
        first.assert_identity(
            ticker=first.ticker,
            analysis_date=first.analysis_date,
            analysis_mode="live",
            analysis_cutoff=second.analysis_cutoff,
            asset_type=first.asset_type,
            selected_analysts=first.selected_analysts,
            llm=first.llm,
            data_transport=first.data_transport,
        )


def test_nested_llm_profiles_reject_role_credentials():
    with pytest.raises(ValueError, match="unsupported/sensitive.*api_key"):
        StageSession.create(
            ticker="HPG",
            analysis_date="2026-08-12",
            llm={
                "quick": {
                    "provider": "openai",
                    "model": "gpt-5.4-mini",
                    "base_url": None,
                    "api_key": "must-not-persist",
                },
                "deep": {
                    "provider": "openai",
                    "model": "gpt-5.5",
                    "base_url": None,
                },
            },
            data_transport={"transport": "api"},
        )


def test_social_profile_is_immutable_and_rejects_secrets():
    session = _session()
    with pytest.raises(ValueError, match="immutable.*social_profile"):
        session.assert_identity(
            ticker=session.ticker,
            analysis_date=session.analysis_date,
            asset_type=session.asset_type,
            selected_analysts=session.selected_analysts,
            llm=session.llm,
            data_transport=session.data_transport,
            social_profile={
                "provider": "fireant",
                "lookback_days": 7,
                "min_posts": 10,
                "min_unique_authors": 5,
                "archive_id": "different-archive",
                "archive_schema_version": 2,
                "prompt_version": "vn-social-v1",
                "legacy_sources_enabled": False,
            },
        )

    with pytest.raises(ValueError, match="unsupported field"):
        StageSession.create(
            ticker="HPG",
            analysis_date="2026-08-12",
            llm={"provider": "fake"},
            data_transport={"transport": "api"},
            social_profile={"provider": "fireant", "access_token": "secret"},
        )


def test_media_profile_is_immutable_and_rejects_runtime_authorization():
    profile = {
        "providers": ["cafef_rss", "vnexpress_rss"],
        "lookback_days": 7,
        "min_articles": 3,
        "archive_id": "archive-a",
        "archive_schema_version": 1,
        "alias_policy_version": "vn-media-alias-v1",
        "prompt_version": "vn-media-v1",
    }
    session = StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        llm={"provider": "fake"},
        data_transport={"transport": "api"},
        media_profile=profile,
    )
    changed = {**profile, "archive_id": "archive-b"}

    with pytest.raises(ValueError, match="immutable.*media_profile"):
        session.assert_identity(
            ticker=session.ticker,
            analysis_date=session.analysis_date,
            asset_type=session.asset_type,
            selected_analysts=session.selected_analysts,
            llm=session.llm,
            data_transport=session.data_transport,
            social_profile=session.social_profile,
            media_profile=changed,
        )

    with pytest.raises(ValueError, match="unsupported field"):
        StageSession.create(
            ticker="HPG",
            analysis_date="2026-08-12",
            llm={"provider": "fake"},
            data_transport={"transport": "api"},
            media_profile={**profile, "cafef_authorized": True},
        )


def test_media_profile_changes_only_media_consuming_stage_fingerprints():
    profile = {
        "providers": ["cafef_rss"],
        "lookback_days": 7,
        "min_articles": 3,
        "archive_id": "archive-a",
        "archive_schema_version": 1,
        "alias_policy_version": "vn-media-alias-v1",
        "prompt_version": "vn-media-v1",
    }
    first = StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        llm={"provider": "fake"},
        data_transport={"transport": "api"},
        media_profile=profile,
    )
    second = StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        llm={"provider": "fake"},
        data_transport={"transport": "api"},
        media_profile={**profile, "prompt_version": "vn-media-v2"},
    )

    assert first.input_fingerprint("sentiment") != second.input_fingerprint("sentiment")
    assert first.input_fingerprint("news") != second.input_fingerprint("news")
    assert first.input_fingerprint("market") == second.input_fingerprint("market")


def test_macro_profile_is_immutable_secret_free_and_news_scoped():
    profile = {
        "provider": "vn_macro",
        "providers": ["nso_sdmx", "nso_release", "sbv_html"],
        "lookback_months": 24,
        "indicator_set_version": "vn-macro-v1",
        "archive_id": "macro-archive-a",
        "archive_schema_version": 1,
        "strict_point_in_time": True,
        "prompt_version": "vn-macro-v1",
    }
    first = StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        llm={"provider": "fake"},
        data_transport={"transport": "api"},
        macro_profile=profile,
    )
    changed = {**profile, "prompt_version": "vn-macro-v2"}
    second = StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        llm={"provider": "fake"},
        data_transport={"transport": "api"},
        macro_profile=changed,
    )

    with pytest.raises(ValueError, match="immutable.*macro_profile"):
        first.assert_identity(
            ticker=first.ticker,
            analysis_date=first.analysis_date,
            asset_type=first.asset_type,
            selected_analysts=first.selected_analysts,
            llm=first.llm,
            data_transport=first.data_transport,
            macro_profile=changed,
        )
    with pytest.raises(ValueError, match="unsupported field"):
        StageSession.create(
            ticker="HPG",
            analysis_date="2026-08-12",
            llm={"provider": "fake"},
            data_transport={"transport": "api"},
            macro_profile={**profile, "archive_path": "/secret/path"},
        )
    with pytest.raises(ValueError, match="provider is unsupported"):
        StageSession.create(
            ticker="HPG",
            analysis_date="2026-08-12",
            llm={"provider": "fake"},
            data_transport={"transport": "api"},
            macro_profile={**profile, "providers": ["fred"]},
        )

    assert first.input_fingerprint("news") != second.input_fingerprint("news")
    assert first.input_fingerprint("sentiment") == second.input_fingerprint("sentiment")
    assert first.input_fingerprint("market") == second.input_fingerprint("market")


def test_sentiment_unavailable_report_and_metadata_are_retained():
    session = _session()
    session.complete(
        "sentiment",
        {
            "sentiment_report": "**Overall Sentiment:** **Unavailable**",
            "sentiment_source_metadata": {
                "status": "unavailable",
                "retail_social_signal": {
                    "provider": "fireant",
                    "status": "unavailable",
                    "sample_size": 0,
                },
            },
        },
        status="unavailable",
    )

    assert session.stage_status["sentiment"] == "unavailable"
    assert session.state["sentiment_source_metadata"]["status"] == "unavailable"
    assert session.state["sentiment_report"].endswith("**Unavailable**")
