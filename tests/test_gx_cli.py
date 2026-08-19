from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from cli.gx_main import app
from tradingagents.graph.stage_session import StageSession


def _ok_llms(_config):
    return {"quick": (True, "fake"), "deep": (True, "fake")}


@pytest.fixture(autouse=True)
def _isolate_macro_doctor(monkeypatch):
    """Doctor unit tests must never initialize the real NSO/SBV archive."""
    import cli.gx_main as gx_main

    monkeypatch.setattr(gx_main, "_check_macro", lambda config, live=False: ("OK", "fake"))


def test_show_does_not_initialize_llm_or_gx(tmp_path):
    session = StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        selected_analysts=("market",),
        llm={"provider": "ollama", "quick_model": "qwen3:8b", "deep_model": "qwen3:8b"},
        data_transport={"transport": "api"},
        run_id="show-test",
    )
    path = session.save(tmp_path / "session.json")

    result = CliRunner().invoke(app, ["show", str(path)])

    assert result.exit_code == 0
    assert "Run: show-test" in result.output
    assert "Analysis: close @ 2026-08-12T15:00:00+07:00" in result.output
    assert "Completed: none" in result.output


def test_env_file_is_loaded_before_doctor(monkeypatch, tmp_path):
    profile = tmp_path / "local.env"
    profile.write_text("TRADINGAGENTS_LLM_PROVIDER=openai_compatible\n")
    monkeypatch.delenv("TRADINGAGENTS_LLM_PROVIDER", raising=False)

    result = CliRunner().invoke(app, ["--env-file", str(profile), "doctor"])

    assert result.exit_code == 1
    assert "FAIL llm" in result.output
    assert "TRADINGAGENTS_LLM_BACKEND_URL is missing" in result.output


def test_env_file_reapplies_model_overrides(monkeypatch, tmp_path):
    profile = tmp_path / "models.env"
    profile.write_text(
        "TRADINGAGENTS_LLM_PROVIDER=ollama\n"
        "TRADINGAGENTS_QUICK_THINK_LLM=unit-quick\n"
        "TRADINGAGENTS_DEEP_THINK_LLM=unit-deep\n"
    )
    monkeypatch.delenv("TRADINGAGENTS_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_QUICK_THINK_LLM", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_DEEP_THINK_LLM", raising=False)

    import cli.gx_main as gx_main

    captured = {}

    def fake_check(config):
        captured.update(config)
        return {"quick": (False, "forced"), "deep": (False, "forced")}

    monkeypatch.setattr(gx_main, "_check_llms", fake_check)
    result = CliRunner().invoke(app, ["--env-file", str(profile), "doctor"])

    assert result.exit_code == 1
    assert captured["quick_think_llm"] == "unit-quick"
    assert captured["deep_think_llm"] == "unit-deep"


def test_explicit_env_file_overrides_stale_export(monkeypatch, tmp_path):
    profile = tmp_path / "selected.env"
    profile.write_text("TRADINGAGENTS_LLM_PROVIDER=openai_compatible\n")
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "openai")

    import cli.gx_main as gx_main

    captured = {}

    def fake_check(config):
        captured.update(config)
        return {"quick": (False, "forced"), "deep": (False, "forced")}

    monkeypatch.setattr(gx_main, "_check_llms", fake_check)
    CliRunner().invoke(app, ["--env-file", str(profile), "doctor"])

    assert captured["llm_provider"] == "openai_compatible"


def test_stage_resume_persists_exact_supplied_path(monkeypatch, tmp_path):
    custom = tmp_path / "custom-location" / "state.json"
    session = StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        selected_analysts=("market",),
        llm={"provider": "fake"},
        data_transport={"transport": "api"},
        run_id="custom-path",
    )
    session.save(custom)

    class FakeStageRunner:
        def run_stage_to(self, loaded, name, *, session_path=None):
            assert session_path == str(custom)
            loaded.complete("market", {"market_report": "done"})
            loaded.save(session_path)

    import cli.gx_main as gx_main

    monkeypatch.setattr(gx_main, "_runner", lambda ctx: FakeStageRunner())
    result = CliRunner().invoke(app, ["stage", "market", "--session", str(custom)])

    assert result.exit_code == 0
    assert StageSession.load(custom).state["market_report"] == "done"


@pytest.mark.parametrize(
    "command, expected",
    [
        (["full", "--ticker", "HPG"], "exactly one of --date or --as-of-now"),
        (
            [
                "full",
                "--ticker",
                "HPG",
                "--date",
                "2026-08-19",
                "--as-of-now",
            ],
            "exactly one of --date or --as-of-now",
        ),
        (
            [
                "full",
                "--ticker",
                "HPG",
                "--date",
                "2026-08-19",
                "--collect-evidence",
            ],
            "--collect-evidence requires --as-of-now",
        ),
    ],
)
def test_full_requires_one_time_mode(command, expected):
    result = CliRunner().invoke(app, command)

    assert result.exit_code != 0
    assert expected in result.output


def test_full_as_of_now_freezes_one_vietnam_cutoff(monkeypatch, tmp_path):
    import cli.gx_main as gx_main

    cutoff = datetime(
        2026,
        8,
        19,
        10,
        11,
        12,
        345678,
        tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"),
    )
    calls = {"clock": 0, "create": None}

    def fake_now():
        calls["clock"] += 1
        return cutoff

    class Session:
        state = {"final_trade_decision": "HOLD"}

        @staticmethod
        def save():
            return tmp_path / "session.json"

        @staticmethod
        def path():
            return tmp_path / "session.json"

    class Runner:
        @staticmethod
        def create_session(ticker, analysis_date, **kwargs):
            calls["create"] = (ticker, analysis_date, kwargs)
            return Session()

        @staticmethod
        def run_default_to(session, *, session_path):
            assert session_path == str(tmp_path / "session.json")

    monkeypatch.setattr(gx_main, "_vietnam_now", fake_now)
    monkeypatch.setattr(gx_main, "_runner", lambda ctx: Runner())

    result = CliRunner().invoke(
        app,
        [
            "full",
            "--ticker",
            "VIC",
            "--as-of-now",
            "--analysts",
            "news",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["clock"] == 1
    ticker, analysis_date, kwargs = calls["create"]
    assert (ticker, analysis_date) == ("VIC", "2026-08-19")
    assert kwargs["analysis_mode"] == "live"
    assert kwargs["analysis_cutoff"] is cutoff
    assert "Analysis cutoff: 2026-08-19T10:11:12.345678+07:00" in result.output


def test_live_collection_is_ordered_nonfatal_and_redacted(monkeypatch, tmp_path):
    import cli.gx_main as gx_main

    secret = "live-collector-secret"
    monkeypatch.setenv("FIREANT_ACCESS_TOKEN", secret)
    events = []

    class MediaService:
        @staticmethod
        def collect_once(*, ticker):
            events.append(("media", ticker))
            raise RuntimeError(
                f"https://user:password@feed.example/rss?access_token={secret}"
            )

    class SocialService:
        @staticmethod
        def collect_once(*, ticker):
            events.append(("social", ticker))
            return {
                "provider": "fireant",
                "ticker": ticker,
                "status": "available",
                "posts_seen": 12,
                "warnings": [
                    f"retry key={secret}",
                    "Authorization: Bearer detached-sensitive-token",
                ],
                "content": "raw post must never be printed",
                "author": {"name": "private author"},
            }

    cutoff = datetime(
        2026, 8, 19, 14, 30, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")
    )

    def fake_now():
        events.append(("clock", None))
        return cutoff

    class Session:
        state = {"final_trade_decision": "HOLD"}

        @staticmethod
        def save():
            return tmp_path / "session.json"

        @staticmethod
        def path():
            return tmp_path / "session.json"

    class Runner:
        @staticmethod
        def create_session(ticker, analysis_date, **kwargs):
            events.append(("create", ticker))
            assert kwargs["analysis_mode"] == "live"
            assert kwargs["analysis_cutoff"] is cutoff
            return Session()

        @staticmethod
        def run_default_to(session, *, session_path):
            return None

    monkeypatch.setattr(gx_main, "_media_service", lambda: MediaService())
    monkeypatch.setattr(gx_main, "_social_service", lambda: SocialService())
    monkeypatch.setattr(gx_main, "_vietnam_now", fake_now)

    def fake_runner(ctx):
        events.append(("runner", None))
        return Runner()

    monkeypatch.setattr(gx_main, "_runner", fake_runner)
    monkeypatch.setattr(
        gx_main,
        "_macro_service",
        lambda: pytest.fail("--collect-evidence must not collect macro data"),
    )

    result = CliRunner().invoke(
        app,
        [
            "full",
            "--ticker",
            "vic",
            "--as-of-now",
            "--collect-evidence",
            "--analysts",
            "news",
        ],
    )

    assert result.exit_code == 0, result.output
    assert events == [
        ("media", "VIC"),
        ("social", "VIC"),
        ("clock", None),
        ("runner", None),
        ("create", "vic"),
    ]
    assert '"posts_seen": 12' in result.output
    assert '"status": "failed"' in result.output
    assert secret not in result.output
    assert "detached-sensitive-token" not in result.output
    assert "user:password" not in result.output
    assert "raw post must never be printed" not in result.output
    assert "private author" not in result.output


@pytest.mark.parametrize(
    "extra",
    [
        ["--date", "2026-08-19"],
        ["--as-of-now"],
        ["--collect-evidence"],
    ],
)
def test_stage_resume_rejects_new_run_time_flags(monkeypatch, tmp_path, extra):
    import cli.gx_main as gx_main

    session = StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        selected_analysts=("market",),
        llm={"provider": "fake"},
        data_transport={"transport": "api"},
        run_id="resume-flags",
    )
    path = session.save(tmp_path / "session.json")
    monkeypatch.setattr(
        gx_main,
        "_runner",
        lambda ctx: pytest.fail("invalid resume flags must fail before runner creation"),
    )
    monkeypatch.setattr(
        gx_main,
        "_vietnam_now",
        lambda: pytest.fail("resume must not read the live clock"),
    )

    result = CliRunner().invoke(
        app,
        ["stage", "market", "--session", str(path), *extra],
    )

    assert result.exit_code != 0
    assert "--session cannot be combined" in result.output


def test_stage_new_close_passes_close_identity(monkeypatch, tmp_path):
    import cli.gx_main as gx_main

    captured = {}

    class Session:
        @staticmethod
        def save():
            return tmp_path / "session.json"

        @staticmethod
        def path():
            return tmp_path / "session.json"

    class Runner:
        @staticmethod
        def create_session(ticker, analysis_date, **kwargs):
            captured.update(ticker=ticker, analysis_date=analysis_date, **kwargs)
            return Session()

        @staticmethod
        def run_stage_to(session, name, *, session_path=None):
            assert name == "market"

    monkeypatch.setattr(gx_main, "_runner", lambda ctx: Runner())
    monkeypatch.setattr(
        gx_main,
        "_vietnam_now",
        lambda: pytest.fail("close mode must not read the live clock"),
    )

    result = CliRunner().invoke(
        app,
        [
            "stage",
            "market",
            "--ticker",
            "HPG",
            "--date",
            "2026-08-19",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["analysis_date"] == "2026-08-19"
    assert captured["analysis_mode"] == "close"
    assert captured["analysis_cutoff"] is None


def test_doctor_accepts_fresh_missing_runs_directory(monkeypatch, tmp_path):
    missing = tmp_path / "not-created-yet"
    monkeypatch.setenv("TRADINGAGENTS_STAGE_RUNS_DIR", str(missing))

    import cli.gx_main as gx_main

    monkeypatch.setattr(gx_main, "_check_llms", _ok_llms)
    monkeypatch.setattr(gx_main, "_check_gx", lambda config=None: (True, "fake"))

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert f"OK runs_dir: {missing}" in result.output


def test_doctor_applies_gx_vendor_profile(monkeypatch):
    import cli.gx_main as gx_main

    captured = {}
    monkeypatch.setattr(gx_main, "_check_llms", _ok_llms)

    def fake_gx(config=None):
        captured.update(config)
        return True, "fake"

    monkeypatch.setattr(gx_main, "_check_gx", fake_gx)
    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert captured["data_vendors"]["core_stock_apis"] == "gx_market_info"
    assert captured["data_vendors"]["technical_indicators"] == "gx_market_info"


def test_gx_doctor_redacts_adapter_error(monkeypatch):
    import cli.gx_main as gx_main
    import tradingagents.dataflows.gx_market_info as gx_adapter

    secret = "doctor-secret"
    monkeypatch.setenv("GX_ANALYSIS_DATA_API_KEY", secret)

    def fail_factory(config):
        raise RuntimeError(
            f"postgresql://user:password@db.internal/g_market_info_1229?token={secret}"
        )

    monkeypatch.setattr(gx_adapter, "get_gx_market_info_client", fail_factory)
    ok, detail = gx_main._check_gx(
        {"gx_market_info": {"transport": "postgres"}}
    )

    assert not ok
    assert "user:password" not in detail
    assert secret not in detail


def test_ollama_doctor_verifies_both_configured_model_tags(monkeypatch):
    import cli.gx_main as gx_main

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "quick:8b"}, {"id": "deep:14b"}]}

    def fake_get(url, timeout):
        captured.update(url=url, timeout=timeout)
        return Response()

    monkeypatch.setattr(gx_main.requests, "get", fake_get)
    results = gx_main._check_llms(
        {
            "llm_provider": "ollama",
            "backend_url": "http://ollama.internal:11434/v1",
            "quick_think_llm": "quick:8b",
            "deep_think_llm": "deep:14b",
        }
    )

    assert all(ok for ok, _ in results.values())
    assert captured["url"] == "http://ollama.internal:11434/v1/models"
    assert "quick:8b" in results["quick"][1]
    assert "deep:14b" in results["deep"][1]


def test_doctor_checks_mixed_quick_and_deep_profiles(monkeypatch):
    import cli.gx_main as gx_main

    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "qwen3:8b"}]}

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return Response()

    monkeypatch.setattr(gx_main.requests, "get", fake_get)
    monkeypatch.setenv("TRADINGAGENTS_DEEP_LLM_API_KEY", "deep-key")
    results = gx_main._check_llms(
        {
            "quick_llm_provider": "ollama",
            "quick_llm_base_url": "http://127.0.0.1:11434/v1",
            "quick_think_llm": "qwen3:8b",
            "deep_llm_provider": "openai",
            "deep_llm_base_url": "https://api.openai.com/v1",
            "deep_think_llm": "gpt-5.5",
        }
    )

    assert results["quick"][0]
    assert results["deep"][0]
    assert calls == [("http://127.0.0.1:11434/v1/models", 2)]


def test_azure_doctor_uses_role_endpoint_model_and_key(monkeypatch):
    import cli.gx_main as gx_main

    monkeypatch.setenv("TRADINGAGENTS_QUICK_LLM_API_KEY", "azure-role-key")
    monkeypatch.setenv("OPENAI_API_VERSION", "2025-03-01-preview")
    ok, detail = gx_main._check_llm_profile(
        {
            "quick_llm_provider": "azure",
            "quick_llm_base_url": "https://quick.openai.azure.com",
            "quick_think_llm": "quick-deployment",
        },
        "quick",
    )

    assert ok
    assert "azure/quick-deployment" in detail


def test_hosted_doctor_uses_canonical_provider_key_mapping(monkeypatch):
    import cli.gx_main as gx_main

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    ok, detail = gx_main._check_llm_profile({"llm_provider": "xai"}, "quick")
    assert not ok and "XAI_API_KEY" in detail
    monkeypatch.setenv("XAI_API_KEY", "configured-but-never-called")
    ok, detail = gx_main._check_llm_profile({"llm_provider": "xai"}, "quick")
    assert ok and "xai/gpt-5.4-mini" in detail


def test_stage_cli_redacts_runtime_exception(monkeypatch, tmp_path):
    custom = tmp_path / "session.json"
    session = StageSession.create(
        ticker="HPG",
        analysis_date="2026-08-12",
        selected_analysts=("market",),
        llm={"provider": "fake"},
        data_transport={"transport": "api"},
        run_id="redaction-test",
    )
    session.save(custom)
    secret = "terminal-secret"
    monkeypatch.setenv("GX_ANALYSIS_DATA_API_KEY", secret)

    class FailingRunner:
        def run_stage_to(self, loaded, name, *, session_path=None):
            raise RuntimeError(
                f"https://user:password@example.test/data?token={secret}"
            )

    import cli.gx_main as gx_main

    monkeypatch.setattr(gx_main, "_runner", lambda ctx: FailingRunner())
    result = CliRunner().invoke(app, ["stage", "market", "--session", str(custom)])

    assert result.exit_code == 1
    assert "FAIL stage market" in result.output
    assert "user:password" not in result.output
    assert secret not in result.output


def test_doctor_skips_locked_social_without_import_or_network(monkeypatch):
    import cli.gx_main as gx_main

    monkeypatch.setattr(gx_main, "_check_llms", _ok_llms)
    monkeypatch.setattr(gx_main, "_check_gx", lambda config=None: (True, "fake"))

    def forbidden():
        raise AssertionError("locked/offline doctor must not initialize social service")

    monkeypatch.setattr(gx_main, "_social_service", forbidden)
    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "SKIP social: authorization locked" in result.output


def test_doctor_skips_locked_media_without_import_or_network(monkeypatch):
    import cli.gx_main as gx_main

    monkeypatch.setattr(gx_main, "_check_llms", _ok_llms)
    monkeypatch.setattr(gx_main, "_check_gx", lambda config=None: (True, "fake"))
    monkeypatch.delenv("TRADINGAGENTS_CAFEF_RSS_AUTHORIZED", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_VNEXPRESS_RSS_AUTHORIZED", raising=False)

    def forbidden():
        raise AssertionError("locked doctor must not initialize media service")

    monkeypatch.setattr(gx_main, "_media_service", forbidden)
    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "SKIP media: authorization locked" in result.output


def test_doctor_rejects_unknown_media_provider_before_import(monkeypatch):
    import cli.gx_main as gx_main

    monkeypatch.setattr(gx_main, "_check_llms", _ok_llms)
    monkeypatch.setattr(gx_main, "_check_gx", lambda config=None: (True, "fake"))
    monkeypatch.setenv("TRADINGAGENTS_VN_MEDIA_PROVIDERS", "typo_provider")

    def forbidden():
        raise AssertionError("invalid config must fail before provider import")

    monkeypatch.setattr(gx_main, "_media_service", forbidden)
    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "FAIL media: unsupported media provider(s): typo_provider" in result.output


def test_doctor_live_media_is_explicit_and_redacts_failure(monkeypatch):
    import cli.gx_main as gx_main

    monkeypatch.setattr(gx_main, "_check_llms", _ok_llms)
    monkeypatch.setattr(gx_main, "_check_gx", lambda config=None: (True, "fake"))
    monkeypatch.setenv("TRADINGAGENTS_CAFEF_RSS_AUTHORIZED", "true")
    secret = "media-archive-secret"
    monkeypatch.setenv("VN_MEDIA_ARCHIVE_ENCRYPTION_KEY", secret)
    called = []

    class Service:
        def status(self, *, live=False):
            called.append(live)
            raise RuntimeError(
                f"https://user:{secret}@cafef.vn/feed?token={secret}"
            )

    monkeypatch.setattr(gx_main, "_media_service", lambda: Service())
    result = CliRunner().invoke(app, ["doctor", "--live-media"])

    assert result.exit_code == 1
    assert called == [True]
    assert "FAIL media" in result.output
    assert secret not in result.output
    assert "user:" not in result.output


def test_doctor_live_social_is_explicit_and_redacts_failure(monkeypatch):
    import cli.gx_main as gx_main

    monkeypatch.setattr(gx_main, "_check_llms", _ok_llms)
    monkeypatch.setattr(gx_main, "_check_gx", lambda config=None: (True, "fake"))
    monkeypatch.setenv("TRADINGAGENTS_FIREANT_AUTHORIZED", "true")
    secret = "fireant-doctor-secret"
    monkeypatch.setenv("FIREANT_ACCESS_TOKEN", secret)

    called = []

    class Service:
        def status(self, *, live=False):
            called.append(live)
            raise RuntimeError(f"https://user:{secret}@fireant.test/posts?token={secret}")

    monkeypatch.setattr(gx_main, "_social_service", lambda: Service())
    result = CliRunner().invoke(app, ["doctor", "--live-social"])

    assert result.exit_code == 1
    assert called == [True]
    assert "FAIL social" in result.output
    assert secret not in result.output
    assert "user:" not in result.output


def test_social_collect_once_forwards_ticker(monkeypatch):
    import cli.gx_main as gx_main

    captured = {}

    class Service:
        def collect_once(self, ticker=None):
            captured["ticker"] = ticker
            return {"status": "completed", "ticker": ticker}

    monkeypatch.setattr(gx_main, "_social_service", lambda: Service())
    result = CliRunner().invoke(
        app, ["social", "collect", "--once", "--ticker", "hpg"]
    )

    assert result.exit_code == 0
    assert captured == {"ticker": "HPG"}
    assert '"status": "completed"' in result.output


def test_social_collect_requires_bounded_once_flag():
    result = CliRunner().invoke(app, ["social", "collect"])

    assert result.exit_code != 0
    assert "--once is required" in result.output


def test_media_collect_once_forwards_uppercase_ticker(monkeypatch):
    import cli.gx_main as gx_main

    captured = {}

    class Service:
        def collect_once(self, ticker=None):
            captured["ticker"] = ticker
            return [{"status": "completed", "ticker": ticker}]

    monkeypatch.setattr(gx_main, "_media_service", lambda: Service())
    result = CliRunner().invoke(
        app, ["media", "collect", "--once", "--ticker", "hpg"]
    )

    assert result.exit_code == 0
    assert captured == {"ticker": "HPG"}
    assert '"status": "completed"' in result.output


def test_media_collect_requires_bounded_once_flag():
    result = CliRunner().invoke(app, ["media", "collect"])

    assert result.exit_code != 0
    assert "--once is required" in result.output


def test_media_status_and_purge_are_offline_service_operations(monkeypatch):
    import cli.gx_main as gx_main

    calls = []

    class Service:
        def status(self, *, live=False):
            calls.append(("status", live))
            return {
                "status": "available",
                "enabled": True,
                "archive_ready": True,
                "watchlist": ["HPG"],
                "sources": [
                    {"provider": "cafef_rss", "status": "available"}
                ],
                "issues": [],
            }

        def purge(self):
            calls.append(("purge", False))
            return {"article_versions": 4, "feed_runs": 1}

    monkeypatch.setattr(gx_main, "_media_service", lambda: Service())
    status = CliRunner().invoke(app, ["media", "status"])
    purge = CliRunner().invoke(app, ["media", "purge"])

    assert status.exit_code == 0
    assert "OK media: cafef_rss; archive ready; watchlist=1" in status.output
    assert purge.exit_code == 0
    assert '"article_versions": 4' in purge.output
    assert calls == [("status", False), ("purge", False)]


def test_macro_status_collect_and_show_are_bounded_archive_operations(monkeypatch):
    import cli.gx_main as gx_main

    calls = []

    class Result:
        @staticmethod
        def to_dict():
            return {
                "status": "partial",
                "as_of": "2026-08-18T15:00:00+07:00",
                "observations": [
                    {
                        "indicator_id": "vn_cpi_yoy",
                        "value": "4.45",
                        "unit": "percent",
                        "unit_multiplier": 1000000,
                        "period_end": "2026-07-31T23:59:59+07:00",
                        "source_provider": "nso_sdmx",
                    }
                ],
                "source_results": [],
                "warnings": ["SBV data unavailable"],
            }

    class Service:
        def status(self, *, live=False):
            calls.append(("status", live))
            return {
                "enabled": True,
                "status": "available",
                "archive_ready": True,
                "providers": ["nso_sdmx", "nso_release", "sbv_html"],
                "observation_count": 12,
                "issues": [],
                "warnings": [],
            }

        def collect_once(self, source=None):
            calls.append(("collect", source))
            return [{"provider": source or "all", "status": "available"}]

        def load_evidence(self, as_of, lookback_months=None):
            calls.append(("show", as_of, lookback_months))
            return Result()

    monkeypatch.setattr(gx_main, "_macro_service", lambda: Service())

    status = CliRunner().invoke(app, ["macro", "status"])
    collect = CliRunner().invoke(
        app, ["macro", "collect", "--once", "--source", "nso"]
    )
    show = CliRunner().invoke(
        app,
        [
            "macro",
            "show",
            "--as-of",
            "2026-08-18",
            "--lookback-months",
            "12",
            "--json",
        ],
    )

    assert status.exit_code == 0
    assert "OK macro: nso_sdmx,nso_release,sbv_html" in status.output
    assert collect.exit_code == 0
    assert '"provider": "nso"' in collect.output
    assert show.exit_code == 0
    assert '"indicator_id": "vn_cpi_yoy"' in show.output
    assert calls == [
        ("status", False),
        ("collect", "nso"),
        ("show", "2026-08-18", 12),
    ]

    human = CliRunner().invoke(
        app, ["macro", "show", "--as-of", "2026-08-18"]
    )
    assert human.exit_code == 0
    assert "percent ×1,000,000" in human.output


def test_macro_collect_requires_once_and_valid_source():
    missing_once = CliRunner().invoke(app, ["macro", "collect"])
    invalid_source = CliRunner().invoke(
        app, ["macro", "collect", "--once", "--source", "fred"]
    )

    assert missing_once.exit_code != 0
    assert "--once is required" in missing_once.output
    assert invalid_source.exit_code != 0
    assert "--source must be" in invalid_source.output


def test_doctor_live_macro_is_explicit(monkeypatch):
    import cli.gx_main as gx_main

    calls = []
    monkeypatch.setattr(gx_main, "_check_llms", _ok_llms)
    monkeypatch.setattr(gx_main, "_check_gx", lambda config=None: (True, "fake"))
    monkeypatch.setattr(
        gx_main,
        "_check_macro",
        lambda config, live=False: (
            calls.append(live) or "OK",
            "vn_macro",
        ),
    )

    offline = CliRunner().invoke(app, ["doctor"])
    live = CliRunner().invoke(app, ["doctor", "--live-macro"])

    assert offline.exit_code == 0
    assert live.exit_code == 0
    assert calls == [False, True]


def test_macro_doctor_accepts_partial_sbv_when_nso_or_archive_is_usable():
    import cli.gx_main as gx_main

    outcome, detail = gx_main._macro_status_outcome(
        {
            "enabled": True,
            "status": "partial",
            "archive_ready": True,
            "observation_count": 8,
            "usable": True,
            "sources": [
                {"provider": "nso_sdmx", "status": "available"},
                {
                    "provider": "sbv_html",
                    "status": "unavailable",
                    "warnings": ["SBV returned 403"],
                },
            ],
            "warnings": ["SBV returned 403"],
            "issues": [],
        }
    )

    assert outcome == "OK"
    assert "partial" in detail
    assert "observations=8" in detail

    failed, failure_detail = gx_main._macro_status_outcome(
        {
            "enabled": True,
            "status": "available",
            "archive_ready": True,
            "observation_count": 0,
            "usable": False,
            "sources": [],
            "warnings": [],
            "issues": [],
        }
    )
    assert failed == "FAIL"
    assert "no usable" in failure_detail


def test_social_snapshot_requires_explicit_llm_and_hosted_authorization(monkeypatch):
    profile = {
        "llm_provider": "openai",
        "vn_social": {
            "provider": "fireant",
            "authorized": True,
            "hosted_llm_authorized": False,
            "tickers": "HPG",
        },
    }

    import cli.gx_main as gx_main

    monkeypatch.setattr(gx_main, "_runner", lambda ctx: None)
    without_opt_in = CliRunner().invoke(
        app, ["social", "snapshot", "--date", "2026-08-13"], obj={"config": profile}
    )
    assert without_opt_in.exit_code != 0
    assert "--live-llm is required" in without_opt_in.output

    monkeypatch.setenv("TRADINGAGENTS_FIREANT_AUTHORIZED", "true")
    monkeypatch.setenv("TRADINGAGENTS_VN_SOCIAL_TICKERS", "HPG")
    monkeypatch.delenv("TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED", raising=False)
    locked = CliRunner().invoke(
        app,
        ["social", "snapshot", "--date", "2026-08-13", "--live-llm"],
    )
    assert locked.exit_code != 0
    assert "hosted social content is locked" in locked.output


def test_social_purge_prints_non_secret_counts(monkeypatch):
    import cli.gx_main as gx_main

    class Service:
        def purge(self):
            return {"post_versions": 3, "authors": 2, "snapshots": 0}

    monkeypatch.setattr(gx_main, "_social_service", lambda: Service())
    result = CliRunner().invoke(app, ["social", "purge"])

    assert result.exit_code == 0
    assert '"post_versions": 3' in result.output


def test_snapshot_time_rejects_today_before_1515_and_future():
    import cli.gx_main as gx_main

    now = datetime(2026, 8, 13, 15, 14, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))
    with pytest.raises(RuntimeError, match="after 15:15"):
        gx_main._assert_snapshot_time(date(2026, 8, 13), now=now)
    with pytest.raises(RuntimeError, match="future"):
        gx_main._assert_snapshot_time(date(2026, 8, 14), now=now)

    gx_main._assert_snapshot_time(
        date(2026, 8, 13),
        now=datetime(2026, 8, 13, 15, 15, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")),
    )


def test_social_llm_locality_requires_loopback():
    import cli.gx_main as gx_main

    assert gx_main._is_local_llm_profile(
        {
            "llm_provider": "openai_compatible",
            "backend_url": "http://127.0.0.1:8000/v1",
        }
    )
    assert not gx_main._is_local_llm_profile(
        {
            "llm_provider": "ollama",
            "backend_url": "https://ollama.internal.example/v1",
        }
    )


def test_social_snapshot_skips_existing_identity_before_llm(monkeypatch):
    import cli.gx_main as gx_main

    calls = {"run_stage": 0, "live": []}

    class Session:
        social_profile = {"prompt_version": "vn-social-v1"}
        llm = {"provider": "openai", "quick_model": "unit-quick"}

        @staticmethod
        def input_fingerprint(stage):
            assert stage == "sentiment"
            return "fingerprint-1"

    class Runner:
        @staticmethod
        def create_session(*args, **kwargs):
            return Session()

        @staticmethod
        def run_stage(*args, **kwargs):
            calls["run_stage"] += 1

    existing = SimpleNamespace(
        model_profile="openai:unit-quick",
        fingerprint="fingerprint-1",
        created=False,
        snapshot_id="existing",
    )

    class Service:
        @staticmethod
        def status(*, live=False):
            calls["live"].append(live)
            return {
                "provider": "fireant",
                "enabled": True,
                "authorized": True,
                "archive_ready": True,
                "watchlist": ["HPG"],
                "issues": [],
            }

        @staticmethod
        def get_snapshot(ticker, analysis_date):
            assert (ticker, analysis_date) == ("HPG", "2026-08-12")
            return existing

        @staticmethod
        def claim_snapshot(*args, **kwargs):
            pytest.fail("existing snapshot must be detected before claiming work")

    monkeypatch.setenv("TRADINGAGENTS_FIREANT_AUTHORIZED", "true")
    monkeypatch.setenv("TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED", "true")
    monkeypatch.setenv("TRADINGAGENTS_VN_SOCIAL_TICKERS", "HPG")
    monkeypatch.setattr(gx_main, "_social_service", lambda: Service())
    monkeypatch.setattr(gx_main, "_runner", lambda ctx: Runner())
    monkeypatch.setattr(gx_main, "_assert_completed_gx_session", lambda *a, **k: None)

    result = CliRunner().invoke(
        app,
        ["social", "snapshot", "--date", "2026-08-12", "--live-llm"],
    )

    assert result.exit_code == 0, result.output
    assert calls == {"run_stage": 0, "live": [False]}
    assert '"skipped": true' in result.output


def test_social_snapshot_rejects_existing_mismatched_identity_before_claim(monkeypatch):
    import cli.gx_main as gx_main

    class Session:
        social_profile = {"prompt_version": "vn-social-v1"}
        media_profile = {
            "providers": ["cafef_rss"],
            "archive_id": "new-media-archive",
        }
        llm = {"provider": "openai", "quick_model": "new-model"}

        @staticmethod
        def input_fingerprint(stage):
            return "new-fingerprint"

    class Service:
        @staticmethod
        def status(*, live=False):
            return {
                "provider": "fireant",
                "enabled": True,
                "authorized": True,
                "archive_ready": True,
                "watchlist": ["HPG"],
                "issues": [],
            }

        @staticmethod
        def get_snapshot(*args, **kwargs):
            return SimpleNamespace(
                # The model is unchanged: the fingerprint mismatch represents
                # a changed media archive/prompt/alias profile.
                model_profile="openai:new-model",
                fingerprint="old-fingerprint",
            )

        @staticmethod
        def claim_snapshot(*args, **kwargs):
            pytest.fail("identity mismatch must fail before claiming snapshot work")

    class Runner:
        @staticmethod
        def create_session(*args, **kwargs):
            return Session()

    monkeypatch.setenv("TRADINGAGENTS_FIREANT_AUTHORIZED", "true")
    monkeypatch.setenv("TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED", "true")
    monkeypatch.setenv("TRADINGAGENTS_VN_SOCIAL_TICKERS", "HPG")
    monkeypatch.setattr(gx_main, "_social_service", lambda: Service())
    monkeypatch.setattr(gx_main, "_runner", lambda ctx: Runner())
    monkeypatch.setattr(gx_main, "_assert_completed_gx_session", lambda *a, **k: None)

    result = CliRunner().invoke(
        app,
        ["social", "snapshot", "--date", "2026-08-12", "--live-llm"],
    )

    assert result.exit_code == 1
    assert "snapshot identity mismatch for HPG" in result.output


def test_social_snapshot_claims_then_persists_retail_and_report_status(monkeypatch):
    import cli.gx_main as gx_main

    captured = {"released": False}

    class Session:
        social_profile = {"prompt_version": "vn-social-v1"}
        llm = {"provider": "openai", "quick_model": "unit-quick"}
        state = {}
        stage_metadata = {}

        @staticmethod
        def input_fingerprint(stage):
            return "fingerprint-2"

    session = Session()

    class Runner:
        @staticmethod
        def create_session(*args, **kwargs):
            return session

        @staticmethod
        def run_stage(current, stage):
            current.state = {
                "sentiment_report": "safe aggregate",
                "sentiment_source_metadata": {
                    "status": "partial",
                    "retail_social_signal": {
                        "status": "available",
                        "provider": "fireant",
                        "sample_size": 12,
                        "unique_authors": 6,
                        "point_in_time_quality": "proxy",
                        "warnings": [],
                    },
                    "media_tone": {
                        "status": "unavailable",
                        "provider": "news",
                    },
                },
            }
            current.stage_metadata = {
                "sentiment": {"input_fingerprint": "fingerprint-2"}
            }

    claim = SimpleNamespace(acquired=True)

    class Service:
        @staticmethod
        def status(*, live=False):
            return {
                "provider": "fireant",
                "enabled": True,
                "authorized": True,
                "archive_ready": True,
                "watchlist": ["HPG"],
                "issues": [],
            }

        @staticmethod
        def get_snapshot(*args, **kwargs):
            return None

        @staticmethod
        def claim_snapshot(*args, **kwargs):
            return claim

        @staticmethod
        def save_snapshot(*args, **kwargs):
            captured.update(kwargs)
            return {"snapshot_id": "new", "created": True}

        @staticmethod
        def release_snapshot_claim(value):
            assert value is claim
            captured["released"] = True

    monkeypatch.setenv("TRADINGAGENTS_FIREANT_AUTHORIZED", "true")
    monkeypatch.setenv("TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED", "true")
    monkeypatch.setenv("TRADINGAGENTS_VN_SOCIAL_TICKERS", "HPG")
    monkeypatch.setattr(gx_main, "_social_service", lambda: Service())
    monkeypatch.setattr(gx_main, "_runner", lambda ctx: Runner())
    monkeypatch.setattr(gx_main, "_assert_completed_gx_session", lambda *a, **k: None)

    result = CliRunner().invoke(
        app,
        ["social", "snapshot", "--date", "2026-08-12", "--live-llm"],
    )

    assert result.exit_code == 0, result.output
    assert captured["status"] == "available"
    assert captured["report_status"] == "partial"
    assert captured["fingerprint"] == "fingerprint-2"
    media_fingerprint = captured["statistics"]["media_profile_fingerprint"]
    assert len(media_fingerprint) == 64
    assert captured["report_payload"]["media_profile_fingerprint"] == media_fingerprint
    assert captured["released"] is True
