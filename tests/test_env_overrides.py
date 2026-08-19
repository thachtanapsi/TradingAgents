"""Tests for TRADINGAGENTS_* env-var overlay onto DEFAULT_CONFIG."""

from __future__ import annotations

import importlib

import pytest

import tradingagents.default_config as default_config_module


def _reload_with_env(monkeypatch, **overrides):
    """Set/clear env vars then reload default_config to re-evaluate DEFAULT_CONFIG."""
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return importlib.reload(default_config_module)


def test_no_env_uses_built_in_defaults(monkeypatch):
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["llm_provider"] == "openai"
    assert dc.DEFAULT_CONFIG["deep_think_llm"] == "gpt-5.5"
    assert dc.DEFAULT_CONFIG["quick_think_llm"] == "gpt-5.4-mini"
    assert dc.DEFAULT_CONFIG["backend_url"] is None
    assert dc.DEFAULT_CONFIG["quick_llm_provider"] is None
    assert dc.DEFAULT_CONFIG["deep_llm_provider"] is None
    assert dc.DEFAULT_CONFIG["quick_llm_base_url"] is None
    assert dc.DEFAULT_CONFIG["deep_llm_base_url"] is None
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 1
    assert dc.DEFAULT_CONFIG["checkpoint_enabled"] is False


def test_string_overrides(monkeypatch):
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="google",
        TRADINGAGENTS_DEEP_THINK_LLM="gemini-3-pro-preview",
        TRADINGAGENTS_QUICK_THINK_LLM="gemini-3-flash-preview",
        TRADINGAGENTS_LLM_BACKEND_URL="https://example.invalid/v1",
        TRADINGAGENTS_OUTPUT_LANGUAGE="Chinese",
    )
    assert dc.DEFAULT_CONFIG["llm_provider"] == "google"
    assert dc.DEFAULT_CONFIG["deep_think_llm"] == "gemini-3-pro-preview"
    assert dc.DEFAULT_CONFIG["quick_think_llm"] == "gemini-3-flash-preview"
    assert dc.DEFAULT_CONFIG["backend_url"] == "https://example.invalid/v1"
    assert dc.DEFAULT_CONFIG["output_language"] == "Chinese"


def test_role_specific_llm_overrides_are_independent(monkeypatch):
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="openai",
        TRADINGAGENTS_LLM_BACKEND_URL="https://legacy.example/v1",
        TRADINGAGENTS_QUICK_LLM_PROVIDER="ollama",
        TRADINGAGENTS_QUICK_LLM_BASE_URL="http://127.0.0.1:11434/v1",
        TRADINGAGENTS_DEEP_LLM_PROVIDER="anthropic",
        TRADINGAGENTS_DEEP_LLM_BASE_URL="https://anthropic-gateway.example/v1",
    )

    assert dc.DEFAULT_CONFIG["quick_llm_provider"] == "ollama"
    assert dc.DEFAULT_CONFIG["quick_llm_base_url"] == "http://127.0.0.1:11434/v1"
    assert dc.DEFAULT_CONFIG["deep_llm_provider"] == "anthropic"
    assert dc.DEFAULT_CONFIG["deep_llm_base_url"] == "https://anthropic-gateway.example/v1"
    assert dc.DEFAULT_CONFIG["llm_provider"] == "openai"


def test_int_coercion(monkeypatch):
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="3",
        TRADINGAGENTS_MAX_RISK_ROUNDS="2",
    )
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 3
    assert isinstance(dc.DEFAULT_CONFIG["max_debate_rounds"], int)
    assert dc.DEFAULT_CONFIG["max_risk_discuss_rounds"] == 2
    assert isinstance(dc.DEFAULT_CONFIG["max_risk_discuss_rounds"], int)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
    ],
)
def test_bool_coercion(monkeypatch, raw, expected):
    dc = _reload_with_env(monkeypatch, TRADINGAGENTS_CHECKPOINT_ENABLED=raw)
    assert dc.DEFAULT_CONFIG["checkpoint_enabled"] is expected


def test_reasoning_thinking_overrides(monkeypatch):
    """The provider reasoning/thinking knobs are env-configurable (non-interactive runs)."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_OPENAI_REASONING_EFFORT="high",
        TRADINGAGENTS_GOOGLE_THINKING_LEVEL="minimal",
        TRADINGAGENTS_ANTHROPIC_EFFORT="low",
    )
    assert dc.DEFAULT_CONFIG["openai_reasoning_effort"] == "high"
    assert dc.DEFAULT_CONFIG["google_thinking_level"] == "minimal"
    assert dc.DEFAULT_CONFIG["anthropic_effort"] == "low"


def test_reasoning_effort_defaults_to_none(monkeypatch):
    """Unset reasoning/thinking knobs stay None so each provider uses its own default."""
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["openai_reasoning_effort"] is None
    assert dc.DEFAULT_CONFIG["google_thinking_level"] is None
    assert dc.DEFAULT_CONFIG["anthropic_effort"] is None


def test_empty_env_value_is_passthrough(monkeypatch):
    """Empty TRADINGAGENTS_* values must not clobber the built-in default."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="",
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="",
    )
    assert dc.DEFAULT_CONFIG["llm_provider"] == "openai"
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 1


def test_invalid_int_raises(monkeypatch):
    """Garbage int values should surface a ValueError at import, not silently misconfigure."""
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "not-a-number")
    with pytest.raises(ValueError, match="TRADINGAGENTS_MAX_DEBATE_ROUNDS"):
        importlib.reload(default_config_module)
    # Restore module state for subsequent tests in this process
    monkeypatch.delenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", raising=False)
    importlib.reload(default_config_module)


@pytest.mark.parametrize("bad", ["treu", "flase", "maybe", "2", "enabled"])
def test_invalid_bool_raises(monkeypatch, bad):
    """A misspelled boolean must fail loudly (like ints) instead of silently False."""
    monkeypatch.setenv("TRADINGAGENTS_CHECKPOINT_ENABLED", bad)
    with pytest.raises(ValueError, match="TRADINGAGENTS_CHECKPOINT_ENABLED"):
        importlib.reload(default_config_module)
    monkeypatch.delenv("TRADINGAGENTS_CHECKPOINT_ENABLED", raising=False)
    importlib.reload(default_config_module)


def test_unknown_env_var_is_ignored(monkeypatch):
    """Env vars outside _ENV_OVERRIDES must not bleed into DEFAULT_CONFIG."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_NONEXISTENT_KEY="oops",
    )
    assert "nonexistent_key" not in dc.DEFAULT_CONFIG


def test_vietnam_social_env_overrides_are_typed_and_secrets_stay_out(monkeypatch):
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_VN_SOCIAL_PROVIDER="fireant",
        TRADINGAGENTS_FIREANT_AUTHORIZED="true",
        TRADINGAGENTS_FIREANT_HOSTED_LLM_AUTHORIZED="false",
        TRADINGAGENTS_VN_SOCIAL_TICKERS="HPG,FPT",
        TRADINGAGENTS_VN_SOCIAL_LOOKBACK_DAYS="5",
        TRADINGAGENTS_VN_SOCIAL_MIN_POSTS="12",
        TRADINGAGENTS_VN_SOCIAL_MIN_UNIQUE_AUTHORS="6",
        TRADINGAGENTS_VN_SOCIAL_POLL_SECONDS="180",
        TRADINGAGENTS_SOCIAL_RAW_RETENTION_DAYS="45",
    )

    social = dc.DEFAULT_CONFIG["vn_social"]
    assert social["provider"] == "fireant"
    assert social["authorized"] is True
    assert social["hosted_llm_authorized"] is False
    assert social["tickers"] == "HPG,FPT"
    assert social["lookback_days"] == 5
    assert social["min_posts"] == 12
    assert social["min_unique_authors"] == 6
    assert social["poll_seconds"] == 180
    assert social["raw_retention_days"] == 45
    assert "access_token" not in social
    assert "encryption_key" not in social


def test_gx_profile_uses_fireant_without_changing_upstream_social_defaults(monkeypatch):
    dc = _reload_with_env(monkeypatch)

    assert dc.DEFAULT_CONFIG["data_vendors"]["social_data"] == "legacy_social"
    assert dc.DEFAULT_CONFIG["vn_social"]["provider"] == "legacy"

    gx = dc.apply_gx_market_info_defaults(dc.DEFAULT_CONFIG)
    assert gx["data_vendors"]["social_data"] == "fireant"
    assert gx["vn_social"]["provider"] == "fireant"
    assert gx["vn_social"]["legacy_sources_enabled"] is False
    assert gx["tool_vendors"]["get_disclosures"] == "gx_market_info"
    assert gx["tool_vendors"]["get_editorial_news"] == "vn_media"
    assert gx["tool_vendors"]["get_vietnam_macro_context"] == "vn_macro"
    assert "get_news" not in gx["tool_vendors"]
    assert gx["vn_media"]["providers"] == "cafef_rss,vnexpress_rss"
    assert gx["vn_media"]["alias_policy_version"] == "vn-media-alias-v2"
    assert gx["vn_macro"]["enabled"] is True
    assert gx["vn_macro"]["providers"] == "nso_sdmx,nso_release,sbv_html"


def test_vietnam_media_env_overrides_are_typed_and_runtime_secrets_stay_out(
    monkeypatch,
):
    monkeypatch.setenv("TRADINGAGENTS_CAFEF_RSS_AUTHORIZED", "true")
    monkeypatch.setenv("TRADINGAGENTS_VNEXPRESS_RSS_AUTHORIZED", "true")
    monkeypatch.setenv("VN_MEDIA_ARCHIVE_ENCRYPTION_KEY", "must-not-persist")
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_VN_MEDIA_PROVIDERS="cafef_rss,vnexpress_rss",
        TRADINGAGENTS_VN_MEDIA_TICKERS="HPG,FPT",
        TRADINGAGENTS_VN_MEDIA_LOOKBACK_DAYS="5",
        TRADINGAGENTS_VN_MEDIA_MIN_ARTICLES="4",
        TRADINGAGENTS_VN_MEDIA_POLL_SECONDS="180",
        TRADINGAGENTS_VN_MEDIA_RAW_RETENTION_DAYS="21",
    )

    media = dc.DEFAULT_CONFIG["vn_media"]
    assert media["providers"] == "cafef_rss,vnexpress_rss"
    assert media["tickers"] == "HPG,FPT"
    assert media["lookback_days"] == 5
    assert media["min_articles"] == 4
    assert media["poll_seconds"] == 180
    assert media["raw_retention_days"] == 21
    assert "authorized" not in media
    assert "hosted_llm_authorized" not in media
    assert "encryption_key" not in media


def test_vietnam_macro_env_overrides_are_typed_and_upstream_stays_disabled(
    monkeypatch,
):
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_VN_MACRO_ENABLED="true",
        TRADINGAGENTS_VN_MACRO_PROVIDERS="nso_sdmx,sbv_html",
        TRADINGAGENTS_VN_MACRO_LOOKBACK_MONTHS="18",
        TRADINGAGENTS_VN_MACRO_STRICT_PIT="false",
        TRADINGAGENTS_VN_MACRO_TIMEOUT_SECONDS="9.5",
        TRADINGAGENTS_VN_MACRO_ARCHIVE_PATH="/tmp/vn-macro-test.sqlite3",
    )

    macro = dc.DEFAULT_CONFIG["vn_macro"]
    assert macro["enabled"] is True
    assert macro["providers"] == "nso_sdmx,sbv_html"
    assert macro["lookback_months"] == 18
    assert macro["strict_point_in_time"] is False
    assert macro["timeout_seconds"] == 9.5
    assert macro["archive_path"] == "/tmp/vn-macro-test.sqlite3"
    assert "api_key" not in macro


def test_gx_macro_default_can_be_explicitly_disabled(monkeypatch):
    dc = _reload_with_env(monkeypatch, TRADINGAGENTS_VN_MACRO_ENABLED="false")

    gx = dc.apply_gx_market_info_defaults(dc.DEFAULT_CONFIG)

    assert gx["vn_macro"]["enabled"] is False
