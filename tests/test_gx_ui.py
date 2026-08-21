from __future__ import annotations

import http.client
import json
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from cli.gx_ui import DashboardRequestHandler, create_server
from tradingagents.ui.dashboard import (
    DashboardService,
    RunRequest,
    _safe_error,
    build_dashboard_result,
)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def _local_config() -> dict:
    return {
        "llm_provider": "ollama",
        "quick_llm_provider": "ollama",
        "deep_llm_provider": "ollama",
        "quick_think_llm": "qwen3:8b",
        "deep_think_llm": "qwen3:8b",
        "quick_llm_base_url": "http://127.0.0.1:11434/v1",
        "deep_llm_base_url": "http://127.0.0.1:11434/v1",
    }


class _Session:
    ticker = "FPT"
    analysis_date = "2026-08-19"
    analysis_mode = "live"
    analysis_cutoff = "2026-08-19T16:05:31+07:00"
    run_id = "run-1"

    def __init__(self, path: Path):
        self._path = path
        self.state = {}
        self.stage_status = dict.fromkeys(
            ("market", "sentiment", "news", "fundamentals", "research", "trader", "risk"),
            "not_run",
        )

    def save(self):
        return self._path


class _Runner:
    def __init__(self, path: Path):
        self.session = _Session(path)
        self.stages = []

    def create_session(self, ticker, analysis_date, **kwargs):
        self.session.ticker = ticker
        self.session.analysis_date = analysis_date
        self.session.analysis_mode = kwargs["analysis_mode"]
        cutoff = kwargs.get("analysis_cutoff")
        self.session.analysis_cutoff = (
            cutoff.isoformat()
            if cutoff is not None
            else f"{analysis_date}T15:00:00+07:00"
        )
        return self.session

    def run_stage_to(self, session, stage, **_kwargs):
        self.stages.append(stage)
        session.stage_status[stage] = "completed"
        updates = {
            "market": ("market_report", "Technical trend from GX."),
            "sentiment": (
                "sentiment_report",
                "**Confidence:** High\nSentiment evidence narrative.",
            ),
            "news": ("news_report", "Vietnam news and macro."),
            "fundamentals": ("fundamentals_report", "GX fundamentals."),
            "research": ("investment_plan", "**Recommendation**: Buy"),
            "trader": ("trader_investment_plan", "**Action**: Buy"),
            "risk": (
                "final_trade_decision",
                "**Rating**: Buy\n\n"
                "**Price Target Status**: Available\n"
                "**Price Target**: 128500\n"
                "**Price Target Currency**: VND\n"
                "**Price Target Rationale**: DCF and peer valuation.\n"
                "**Price Target Unavailable Reason**: Unavailable\n\n"
                "Final thesis.",
            ),
        }
        key, value = updates[stage]
        session.state[key] = value


class _GxClient:
    def __init__(self):
        self.calls = []

    def get_ohlcv(self, ticker, start, end, resolution):
        self.calls.append((ticker, start, end, resolution))
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-08-17", "2026-08-18", "2026-08-19"]),
                "Close": [120_000, 121_500, 123_000],
            }
        )


def test_run_request_requires_explicit_valid_mode_and_date():
    request = RunRequest.from_payload(
        {"ticker": " fpt ", "mode": "close", "analysis_date": "2026-08-19"}
    )
    assert request.ticker == "FPT"
    with pytest.raises(ValueError, match="requires analysis_date"):
        RunRequest.from_payload({"ticker": "FPT", "mode": "close"})
    with pytest.raises(ValueError, match="only in live mode"):
        RunRequest.from_payload(
            {
                "ticker": "FPT",
                "mode": "close",
                "analysis_date": "2026-08-19",
                "collect_evidence": True,
            }
        )


def test_summary_uses_only_canonical_final_fields_and_does_not_conflate_sentiment(tmp_path):
    session = _Session(tmp_path / "session.json")
    session.state = {
        "sentiment_report": "**Confidence:** High\n**Overall Sentiment:** Bullish",
        "final_trade_decision": (
            "**Rating**: Overweight\n\n"
            "**Price Target Status**: Available\n"
            "**Price Target**: 128500\n"
            "**Price Target Currency**: VND\n"
            "**Price Target Rationale**: DCF and peer valuation.\n"
            "**Price Target Unavailable Reason**: Unavailable\n\n"
            "**Risk**: Medium\n\nRisk may be elevated."
        ),
    }
    result = build_dashboard_result(session)
    assert result["summary"] == {
        "recommendation": "BUY",
        "detailed_rating": "Overweight",
        "confidence": None,
        "confidence_source": None,
        "target_price": 128500.0,
        "target_price_status": "available",
        "target_price_currency": "VND",
        "target_price_reason": "Đã xác thực theo contract giá mục tiêu.",
        "time_horizon": None,
        "risk": None,
    }
    assert "sentiment_source_metadata" not in result
    assert "risk may be elevated" not in str(result["summary"]).lower()


def test_summary_hides_legacy_or_incomplete_target_instead_of_rescaling(tmp_path):
    session = _Session(tmp_path / "session.json")
    session.state = {
        "final_trade_decision": "**Rating**: Hold\n\n**Price Target**: 63.3"
    }
    legacy = build_dashboard_result(session)["summary"]
    assert legacy["target_price"] is None
    assert legacy["target_price_status"] == "unavailable"
    assert legacy["target_price_currency"] is None
    assert "Target cũ" in legacy["target_price_reason"]

    session.state["final_trade_decision"] = (
        "**Rating**: Hold\n\n"
        "**Price Target Status**: Available\n"
        "**Price Target**: 63300\n"
        "**Price Target Currency**: VND"
    )
    incomplete = build_dashboard_result(session)["summary"]
    assert incomplete["target_price"] is None
    assert incomplete["target_price_status"] == "unavailable"
    assert "không hợp lệ" in incomplete["target_price_reason"]


def test_summary_exposes_portfolio_manager_reason_when_target_is_unavailable(tmp_path):
    session = _Session(tmp_path / "session.json")
    session.state = {
        "final_trade_decision": (
            "**Rating**: Hold\n\n"
            "**Price Target Status**: Unavailable\n"
            "**Price Target**: Unavailable\n"
            "**Price Target Currency**: Unavailable\n"
            "**Price Target Rationale**: Unavailable\n"
            "**Price Target Unavailable Reason**: Thiếu valuation inputs đáng tin cậy."
        )
    }
    summary = build_dashboard_result(session)["summary"]
    assert summary["target_price"] is None
    assert summary["target_price_status"] == "unavailable"
    assert summary["target_price_currency"] is None
    assert summary["target_price_reason"] == "Thiếu valuation inputs đáng tin cậy"


def test_ui_redacts_every_provider_key_even_when_exception_has_no_label(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-canary-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-canary-secret")
    safe = _safe_error(
        RuntimeError("upstream anthropic-canary-secret aws-canary-secret")
    )
    assert "anthropic-canary-secret" not in safe
    assert "aws-canary-secret" not in safe


def test_ui_redacts_rotated_key_formats_not_present_in_environment():
    stale_openai = "sk-proj-abcdefghijklmnopqrstuvwx1234567890"
    stale_aws = "AKIAABCDEFGHIJKLMNOP"
    stale_google = "AIza" + "A" * 32
    stale_jwt = "eyJ" + "A" * 20 + "." + "B" * 20 + "." + "C" * 20

    safe = _safe_error(
        RuntimeError(f"old {stale_openai} {stale_aws} {stale_google} {stale_jwt}")
    )
    for secret in (stale_openai, stale_aws, stale_google, stale_jwt):
        assert secret not in safe


def test_browser_reports_redact_secrets_but_preserve_public_citations(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "report-secret-canary")
    session = _Session(tmp_path / "session.json")
    session.state = {
        "market_report": (
            "Evidence report-secret-canary from "
            "https://vnexpress.net/kinh-doanh"
        ),
        "final_trade_decision": (
            "**Rating**: Hold\n\n"
            "postgresql://user:p@ssword@db.internal/private"
        ),
        "investment_debate_state": {
            "bull_history": "Authorization: Bearer nested-secret-token"
        },
    }

    serialized = str(build_dashboard_result(session))
    assert "report-secret-canary" not in serialized
    assert "user:p@ssword" not in serialized
    assert "nested-secret-token" not in serialized
    assert "https://vnexpress.net/kinh-doanh" in serialized


def test_close_run_rejects_future_or_not_yet_completed_close(tmp_path):
    runner_calls = []
    service = DashboardService(
        _local_config(),
        runner_factory=lambda _config: runner_calls.append(True),
        now=lambda: datetime(2026, 8, 19, 10, 0, tzinfo=VN_TZ),
    )
    with pytest.raises(ValueError, match="close is not completed"):
        service.start_run(
            {"ticker": "FPT", "mode": "close", "analysis_date": "2026-08-19"},
            background=False,
        )
    with pytest.raises(ValueError, match="close is not completed"):
        service.start_run(
            {"ticker": "FPT", "mode": "close", "analysis_date": "2026-08-20"},
            background=False,
        )
    assert runner_calls == []


def test_synchronous_live_run_freezes_cutoff_runs_stages_and_loads_gx_chart(tmp_path):
    runner = _Runner(tmp_path / "session.json")
    gx = _GxClient()
    service = DashboardService(
        _local_config(),
        runner_factory=lambda _config: runner,
        gx_client_factory=lambda _config: gx,
        evidence_collector=lambda _ticker: {
            "media": {"status": "available", "raw_articles": ["must not escape"]},
            "social": {"status": "unavailable", "authors": ["private"]},
        },
        now=lambda: datetime(2026, 8, 19, 16, 5, 31, tzinfo=VN_TZ),
    )
    job = service.start_run(
        {
            "ticker": "FPT",
            "mode": "live",
            "collect_evidence": True,
            "confirm_hosted_cost": False,
        },
        background=False,
    )
    assert job["status"] == "completed"
    assert job["analysis_cutoff"] == "2026-08-19T16:05:31+07:00"
    assert runner.stages == [
        "market",
        "sentiment",
        "news",
        "fundamentals",
        "research",
        "trader",
        "risk",
    ]
    assert gx.calls[0][2] == "2026-08-19T16:05:31+07:00"
    assert job["result"]["chart"][-1] == {"date": "2026-08-19", "close": 123000.0}
    assert job["result"]["summary"]["confidence"] is None
    assert "must not escape" not in str(job)
    assert "private" not in str(job)


def test_hosted_profile_requires_confirmation_before_runner_construction(tmp_path):
    calls = []
    config = {
        "llm_provider": "openai",
        "quick_think_llm": "gpt-5.4-mini",
        "deep_think_llm": "gpt-5.5",
    }
    service = DashboardService(
        config,
        runner_factory=lambda _config: calls.append(True),
        gx_client_factory=lambda _config: _GxClient(),
    )
    assert service.public_info()["hosted_cost_confirmation_required"] is True
    with pytest.raises(ValueError, match="Confirm hosted LLM"):
        service.start_run(
            {"ticker": "FPT", "mode": "close", "analysis_date": "2026-08-19"},
            background=False,
        )
    assert calls == []


def test_server_is_loopback_only_and_static_js_avoids_dynamic_html(tmp_path):
    service = DashboardService(
        _local_config(), runner_factory=lambda _config: _Runner(tmp_path / "session.json")
    )
    with patch("cli.gx_ui.DashboardHTTPServer") as server_type:
        create_server(service, port=8765)
    server_type.assert_called_once_with(("127.0.0.1", 8765), service)
    javascript = (Path(__file__).parents[1] / "cli/static/gx_dashboard.js").read_text()
    assert "innerHTML" not in javascript
    assert "textContent" in javascript
    assert "https://" not in javascript
    assert "sessionStorage" in javascript
    assert "if (signature === lastJobRenderSignature) return;" in javascript


def test_history_static_assets_use_dom_only_markdown_and_local_dependencies():
    static_dir = Path(__file__).parents[1] / "cli/static"
    javascript = (static_dir / "gx_history.js").read_text()
    html = (static_dir / "gx_history.html").read_text()

    for forbidden in ("innerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in javascript
    assert 'src="http' not in html
    assert 'href="http' not in html
    assert "document.createTextNode(token)" in javascript
    assert 'url.protocol === "http:"' in javascript
    assert 'url.protocol === "https:"' in javascript
    assert "!url.username && !url.password" in javascript
    assert 'anchor.referrerPolicy = "no-referrer"' in javascript
    assert 'const historyIdPattern = /^[0-9a-f]{64}$/' in javascript
    assert '"vendor_chain"' in javascript
    assert '"tool"' in javascript
    assert 'id="run-form"' not in html
    assert 'id="history-panel"' in html
    assert 'href="/">Trang Research</a>' in html
    assert '{ id: "sentiment", label: "Tâm lý thị trường"' in javascript
    assert '{ id: "news", label: "Tin tức"' in javascript
    assert "sessionStorage" not in javascript
    assert 'fetch(`/api/runs/' not in javascript


def test_history_query_parser_requires_single_values_and_bounds_field_count():
    assert DashboardRequestHandler._single_query_parameters(
        "query=CTG&mode=live&page=2"
    ) == {"query": "CTG", "mode": "live", "page": "2"}
    with pytest.raises(ValueError, match="only once"):
        DashboardRequestHandler._single_query_parameters("status=failed&status=partial")
    with pytest.raises(ValueError, match="Max number of fields"):
        DashboardRequestHandler._single_query_parameters(
            "&".join(f"field{i}=x" for i in range(17))
        )


def test_history_viewer_uses_safe_local_dom_rendering():
    static_root = Path(__file__).parents[1] / "cli/static"
    javascript = (static_root / "gx_history.js").read_text()
    html = (static_root / "gx_history.html").read_text()

    for forbidden in ("innerHTML", "insertAdjacentHTML", "eval(", "dangerouslySetInnerHTML"):
        assert forbidden not in javascript
    assert "document.createElement" in javascript
    assert "!url.username && !url.password" in javascript
    assert 'anchor.referrerPolicy = "no-referrer"' in javascript
    assert "const targetDetail = targetAvailable ? null : targetReason" in javascript
    assert "https://" not in html


def test_research_overview_uses_compact_target_status_not_full_rationale():
    static_root = Path(__file__).parents[1] / "cli/static"
    javascript = (static_root / "gx_dashboard.js").read_text()
    html = (static_root / "gx_dashboard.html").read_text()

    assert "targetSource.hidden = targetAvailable" in javascript
    assert "/static/gx_dashboard.js?v=7" in html


def test_http_server_rejects_untrusted_host_origin_and_missing_token(tmp_path):
    service = DashboardService(
        _local_config(), runner_factory=lambda _config: _Runner(tmp_path / "session.json")
    )
    history_id = "a" * 64
    service.list_history = lambda parameters: {
        "items": [],
        "total": 0,
        "page": int(parameters.get("page", "1")),
        "page_size": 20,
        "total_pages": 0,
        "skipped_invalid": 0,
    }
    service.get_history = lambda requested_id: (
        {"history_id": history_id, "ticker": "FPT"}
        if requested_id == history_id
        else None
    )
    try:
        server = create_server(service, port=0)
    except PermissionError:
        pytest.skip("the execution sandbox blocks loopback sockets")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/", headers={"Host": "attacker.invalid"})
        response = connection.getresponse()
        response.read()
        assert response.status == 403

        body = json.dumps(
            {"ticker": "FPT", "mode": "live", "confirm_hosted_cost": False}
        )
        connection.request(
            "POST",
            "/api/runs",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": "https://attacker.invalid",
            },
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 403

        connection.request("GET", "/api/info")
        response = connection.getresponse()
        response.read()
        assert response.status == 403

        connection.request(
            "GET",
            "/api/info",
            headers={"X-TradingAgents-UI-Token": server.ui_token},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert payload["llm"]["quick"]["provider"] == "ollama"
        assert "Content-Security-Policy" in response.headers

        connection.request("GET", "/")
        response = connection.getresponse()
        research_page = response.read().decode()
        assert response.status == 200
        assert "Kết quả tổng hợp" in research_page
        assert 'id="run-form"' in research_page
        assert 'id="history-panel"' not in research_page
        assert 'href="/history">Lịch sử Research</a>' in research_page

        connection.request("GET", "/history")
        response = connection.getresponse()
        history_page = response.read().decode()
        assert response.status == 200
        assert "Lịch sử Research" in history_page
        assert 'id="history-panel"' in history_page
        assert 'id="run-form"' not in history_page
        assert server.ui_token in history_page

        connection.request(
            "GET",
            "/api/history?query=FPT&page=2",
            headers={"X-TradingAgents-UI-Token": server.ui_token},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert payload["page"] == 2

        connection.request(
            "GET",
            f"/api/history/{history_id}",
            headers={"X-TradingAgents-UI-Token": server.ui_token},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert payload == {"history_id": history_id, "ticker": "FPT"}

        connection.request(
            "GET",
            "/api/history/not-a-valid-id",
            headers={"X-TradingAgents-UI-Token": server.ui_token},
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 404
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
