from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import tradingagents.ui.history as history_module
from tradingagents.graph.stage_session import PIPELINE_STAGES, StageSession
from tradingagents.ui.dashboard import DashboardService
from tradingagents.ui.history import MAX_SESSION_BYTES, SessionHistoryRepository


def _session(root, ticker="CTG", analysis_date="2026-08-12", run_id="run-1"):
    session = StageSession.create(
        ticker=ticker,
        analysis_date=analysis_date,
        selected_analysts=("market", "sentiment", "news", "fundamentals"),
        llm={"provider": "ollama", "quick_model": "qwen3:8b"},
        data_transport={"transport": "api", "base_url": "http://gx"},
        run_id=run_id,
    )
    return session, session.path(root)


def _complete(session):
    session.complete("market", {"market_report": "Technical report"})
    session.complete(
        "sentiment",
        {
            "sentiment_report": "Sentiment report",
            "sentiment_source_metadata": {
                "status": "available",
                "retail_social_signal": {
                    "provider": "fireant",
                    "status": "available",
                    "sample_size": 12,
                },
            },
        },
        sources=[{"provider": "fireant", "status": "available"}],
    )
    session.complete(
        "news",
        {
            "news_report": "News report",
            "news_source_metadata": {
                "status": "available",
                "editorial_media": {
                    "provider": "vn_media",
                    "status": "available",
                    "sample_size": 4,
                    "source_url": "https://vnexpress.net/kinh-doanh",
                },
            },
        },
    )
    session.complete("fundamentals", {"fundamentals_report": "Fundamental report"})
    session.complete(
        "research",
        {
            "investment_plan": "Investment plan",
            "investment_debate_state": {
                "bull_history": "Bull thesis",
                "bear_history": "Bear thesis",
            },
        },
    )
    session.complete("trader", {"trader_investment_plan": "Trader plan"})
    session.complete(
        "risk",
        {
            "final_trade_decision": (
                "**Rating**: Buy\n\n"
                "**Price Target Status**: Available\n"
                "**Price Target**: 42000\n"
                "**Price Target Currency**: VND\n"
                "**Price Target Rationale**: DCF and peer valuation.\n"
                "**Price Target Unavailable Reason**: Unavailable\n\n"
                "**Time Horizon**: 3–6 tháng"
            ),
            "risk_debate_state": {
                "aggressive_history": "Aggressive view",
                "neutral_history": "Neutral view",
                "conservative_history": "Conservative view",
            },
        },
    )
    return session


def test_history_list_filter_sort_pagination_and_detail(tmp_path):
    first, first_path = _session(tmp_path, run_id="run-a")
    first.state["instrument_context"] = (
        "The instrument to analyze is `CTG`. Resolved identity: "
        "Company: Ngân hàng TMCP Công Thương Việt Nam; Exchange: HOSE."
    )
    _complete(first).save(first_path)
    second, second_path = _session(
        tmp_path, ticker="HPG", analysis_date="2026-08-13", run_id="run-b"
    )
    second.complete("market", {"market_report": "HPG market"})
    second.updated_at = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    second.save(second_path)

    repository = SessionHistoryRepository(tmp_path)
    page = repository.list_history({"page": "1", "page_size": "1"})
    assert page["total"] == 2
    assert page["total_pages"] == 2
    assert page["items"][0]["ticker"] == "HPG"
    assert page["items"][0]["status"] == "partial"
    assert "relative_path" not in str(page)

    filtered = repository.list_history(
        {
            "query": "ctg",
            "mode": "close",
            "status": "completed",
            "from": "2026-08-12",
            "to": "2026-08-12",
        }
    )
    assert filtered["total"] == 1
    item = filtered["items"][0]
    assert item["company_name"] == "Ngân hàng TMCP Công Thương Việt Nam"
    assert item["completed_stages"] == len(PIPELINE_STAGES)
    assert item["summary"]["recommendation"] == "BUY"
    assert item["summary"]["target_price"] == 42000.0
    assert item["summary"]["target_price_status"] == "available"
    assert item["summary"]["target_price_currency"] == "VND"
    assert item["summary"]["target_price_reason"] == (
        "Đã xác thực theo contract giá mục tiêu."
    )
    assert item["summary"]["time_horizon"] == "3–6 tháng"

    detail = repository.get_history(item["history_id"])
    assert detail is not None
    assert detail["sections"] == {
        "technical": "Technical report",
        "fundamentals": "Fundamental report",
        "sentiment": "Sentiment report",
        "news": "News report",
    }
    assert detail["plans"] == {"investment": "Investment plan", "trader": "Trader plan"}
    assert detail["debates"]["bull"] == "Bull thesis"
    assert detail["debates"]["conservative"] == "Conservative view"
    assert detail["sources"]["sentiment"]["retail_social_signal"]["sample_size"] == 12
    assert (
        detail["sources"]["news"]["editorial_media"]["source_url"]
        == "https://vnexpress.net/kinh-doanh"
    )


def test_history_does_not_publish_unvalidated_legacy_target(tmp_path):
    session, path = _session(tmp_path, run_id="legacy-target")
    session.complete(
        "risk",
        {
            "final_trade_decision": (
                "**Rating**: Hold\n\n**Price Target**: 63.3\n\n"
                "**Time Horizon**: 6–12 tháng"
            )
        },
    )
    session.save(path)

    repository = SessionHistoryRepository(tmp_path)
    item = repository.list_history({})["items"][0]
    assert item["summary"]["target_price"] is None
    assert item["summary"]["target_price_status"] == "unavailable"
    assert item["summary"]["target_price_currency"] is None
    assert "Target cũ" in item["summary"]["target_price_reason"]

    detail = repository.get_history(item["history_id"])
    assert detail is not None
    assert detail["summary"] == item["summary"]


def test_history_active_overlay_and_dashboard_service_delegation(tmp_path):
    session, path = _session(tmp_path, run_id="active-run")
    session.complete("market", {"market_report": "running"})
    session.save(path)
    active = {"active-run"}
    repository = SessionHistoryRepository(tmp_path, active_run_ids=lambda: active)
    service = DashboardService(
        {
            "llm_provider": "ollama",
            "quick_think_llm": "qwen3:8b",
            "deep_think_llm": "qwen3:8b",
        },
        history_repository=repository,
    )

    listed = service.list_history({})
    assert listed["items"][0]["status"] == "running"
    detail = service.get_history(listed["items"][0]["history_id"])
    assert detail is not None and detail["status"] == "running"
    assert service.get_history("not-an-id") is None


def test_company_name_extraction_stops_before_instrument_instruction(tmp_path):
    session, path = _session(tmp_path, run_id="company-only")
    session.state["instrument_context"] = (
        "Resolved identity: Company: Công ty CP Foo.Bar. "
        "Do not substitute a different company or ticker."
    )
    session.save(path)
    item = SessionHistoryRepository(tmp_path).list_history({})["items"][0]
    assert item["company_name"] == "Công ty CP Foo.Bar"


def test_history_isolates_malformed_oversized_symlink_and_path_mismatch(tmp_path):
    valid, valid_path = _session(tmp_path, run_id="valid")
    valid.save(valid_path)

    malformed = tmp_path / "CTG" / "2026-08-12" / "malformed" / "session.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("{not json", encoding="utf-8")

    oversized = tmp_path / "CTG" / "2026-08-12" / "large" / "session.json"
    oversized.parent.mkdir(parents=True)
    oversized.write_bytes(b"x" * (MAX_SESSION_BYTES + 1))

    mismatched, _ = _session(tmp_path, run_id="actual")
    mismatch_path = tmp_path / "CTG" / "2026-08-12" / "wrong" / "session.json"
    mismatched.save(mismatch_path)

    symlink_path = tmp_path / "CTG" / "2026-08-12" / "linked" / "session.json"
    symlink_path.parent.mkdir(parents=True)
    os.symlink(valid_path, symlink_path)

    result = SessionHistoryRepository(tmp_path).list_history({})
    assert result["total"] == 1
    assert result["items"][0]["run_id"] == "valid"
    assert result["skipped_invalid"] == 4


def test_history_migrates_legacy_in_memory_without_rewriting_file(tmp_path):
    session, path = _session(tmp_path, run_id="legacy")
    payload = session.to_dict()
    payload["schema_version"] = 5
    payload.pop("analysis_identity_fingerprint", None)
    path.parent.mkdir(parents=True)
    original = json.dumps(payload, ensure_ascii=False)
    path.write_text(original, encoding="utf-8")

    repository = SessionHistoryRepository(tmp_path)
    result = repository.list_history({})
    assert result["total"] == 1
    assert path.read_text(encoding="utf-8") == original
    detail = repository.get_history(result["items"][0]["history_id"])
    assert detail is not None
    assert detail["analysis_cutoff"] == "2026-08-12T15:00:00+07:00"


def test_history_isolates_non_object_deep_json_and_invalid_metadata(tmp_path):
    non_object = tmp_path / "CTG" / "2026-08-12" / "string" / "session.json"
    non_object.parent.mkdir(parents=True)
    non_object.write_text('"not-an-object"', encoding="utf-8")

    deep = tmp_path / "CTG" / "2026-08-12" / "deep" / "session.json"
    deep.parent.mkdir(parents=True)
    deep.write_text("[" * 1_100 + "0" + "]" * 1_100, encoding="utf-8")

    invalid, invalid_path = _session(tmp_path, run_id="bad-metadata")
    payload = invalid.to_dict()
    payload["stage_metadata"] = "not-an-object"
    invalid_path.parent.mkdir(parents=True)
    invalid_path.write_text(json.dumps(payload), encoding="utf-8")

    result = SessionHistoryRepository(tmp_path).list_history({})
    assert result["items"] == []
    assert result["skipped_invalid"] == 3


def test_history_bounded_read_rejects_file_that_grows_after_fstat(tmp_path, monkeypatch):
    session, path = _session(tmp_path, run_id="growing")
    session.save(path)
    original_fstat = history_module.os.fstat
    expanded = False

    def grow_after_stat(descriptor):
        nonlocal expanded
        info = original_fstat(descriptor)
        if not expanded and info.st_size == path.stat().st_size:
            expanded = True
            with path.open("ab") as handle:
                handle.write(b" " * (MAX_SESSION_BYTES + 1 - info.st_size))
        return info

    monkeypatch.setattr(history_module.os, "fstat", grow_after_stat)
    result = SessionHistoryRepository(tmp_path).list_history({})
    assert result["total"] == 0
    assert result["skipped_invalid"] == 1


def test_history_redacts_secrets_and_uses_positive_source_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "history-secret-canary")
    session, path = _session(tmp_path, run_id="secret")
    session.complete(
        "news",
        {
            "news_report": (
                "Report history-secret-canary "
                "https://example.test/article?auth=report-secret#fragment"
            ),
            "news_source_metadata": {
                "status": "partial",
                "provider": "vn_media",
                "source_url": "https://vnexpress.net/kinh-doanh?auth=old-secret#part",
                "canonical_url": ("https://example.test/sk-proj-abcdefghijklmnop1234/article"),
                "archive_id": "must-not-escape",
                "raw_articles": ["private article"],
                "warnings": [
                    "Bearer token-secret",
                    "postgresql://user:password@db/private",
                    f"session file {tmp_path}/CTG/2026-08-12/secret/session.json",
                ],
            },
        },
        sources=[
            {
                "provider": "vn_media",
                "status": "partial",
                "input_fingerprint": "private-fingerprint",
            }
        ],
    )
    session.save(path)

    listed = SessionHistoryRepository(tmp_path).list_history({})
    detail = SessionHistoryRepository(tmp_path).get_history(listed["items"][0]["history_id"])
    serialized = json.dumps(detail, ensure_ascii=False)
    assert "history-secret-canary" not in serialized
    assert "token-secret" not in serialized
    assert "user:password" not in serialized
    assert "must-not-escape" not in serialized
    assert "private article" not in serialized
    assert "private-fingerprint" not in serialized
    assert "old-secret" not in serialized
    assert "report-secret" not in serialized
    assert "sk-proj-abcdefghijklmnop1234" not in serialized
    assert str(tmp_path) not in serialized
    assert "https://vnexpress.net/kinh-doanh" in serialized


@pytest.mark.parametrize("schema_version", [1, 2, 3, 4, 5, 6])
def test_history_reads_every_supported_schema_without_rewriting(tmp_path, schema_version):
    session, path = _session(tmp_path, run_id=f"schema-{schema_version}")
    session.complete("market", {"market_report": f"schema {schema_version}"})
    payload = session.to_dict()
    payload["schema_version"] = schema_version
    if schema_version < 6:
        payload.pop("analysis_identity_fingerprint", None)
    if schema_version == 1:
        payload.pop("social_profile", None)
        payload.pop("media_profile", None)
        payload.pop("macro_profile", None)
    elif schema_version == 2:
        payload.pop("media_profile", None)
        payload.pop("macro_profile", None)
    elif schema_version in {3, 4}:
        payload.pop("macro_profile", None)
    path.parent.mkdir(parents=True)
    original = json.dumps(payload, ensure_ascii=False)
    path.write_text(original, encoding="utf-8")

    repository = SessionHistoryRepository(tmp_path)
    listed = repository.list_history({})
    assert listed["total"] == 1
    assert repository.get_history(listed["items"][0]["history_id"])["ticker"] == "CTG"
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "params",
    [
        {"query": "x" * 65},
        {"mode": "today"},
        {"status": "unknown"},
        {"from": "2026-08-13", "to": "2026-08-12"},
        {"page": "0"},
        {"page_size": "101"},
        {"unexpected": "value"},
    ],
)
def test_history_rejects_invalid_filters(tmp_path, params):
    with pytest.raises(ValueError, match="History|Unsupported"):
        SessionHistoryRepository(tmp_path).list_history(params)
