from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.profiles import (
    is_local_llm_profile,
    resolve_llm_api_key,
    resolve_llm_profile,
)


def test_role_profile_precedence_and_api_key_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "provider-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-provider-key")
    monkeypatch.setenv("TRADINGAGENTS_QUICK_LLM_API_KEY", "quick-role-key")
    monkeypatch.delenv("TRADINGAGENTS_DEEP_LLM_API_KEY", raising=False)
    config = {
        "llm_provider": "openai",
        "backend_url": "https://legacy.example/v1",
        "quick_llm_provider": "openai",
        "quick_llm_base_url": "https://quick.example/v1",
        "quick_think_llm": "quick-model",
        "deep_llm_provider": "anthropic",
        "deep_llm_base_url": "https://deep.example/v1",
        "deep_think_llm": "deep-model",
    }

    quick = resolve_llm_profile(config, "quick")
    deep = resolve_llm_profile(config, "deep")
    quick_key, quick_key_source = resolve_llm_api_key(quick)
    deep_key, _ = resolve_llm_api_key(deep)

    assert quick.provider == "openai"
    assert quick.base_url == "https://quick.example/v1"
    assert quick_key == "quick-role-key"
    assert quick_key_source == "TRADINGAGENTS_QUICK_LLM_API_KEY"
    assert deep.provider == "anthropic"
    assert deep.base_url == "https://deep.example/v1"
    assert deep_key == "anthropic-provider-key"
    assert "quick-role-key" not in repr(quick)


def test_empty_role_values_fall_back_to_legacy(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_QUICK_LLM_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-key")
    profile = resolve_llm_profile(
        {
            "llm_provider": "openai",
            "backend_url": "https://legacy.example/v1",
            "quick_llm_provider": "",
            "quick_llm_base_url": "",
            "quick_think_llm": "gpt-5.4-mini",
        },
        "quick",
    )

    assert profile.provider == "openai"
    assert profile.base_url == "https://legacy.example/v1"
    assert resolve_llm_api_key(profile)[0] == "provider-key"


def test_durable_llm_identity_is_nested_and_never_contains_keys(monkeypatch):
    from tradingagents.graph.stage_runner import llm_identity

    monkeypatch.setenv("TRADINGAGENTS_QUICK_LLM_API_KEY", "quick-secret")
    monkeypatch.setenv("TRADINGAGENTS_DEEP_LLM_API_KEY", "deep-secret")
    identity = llm_identity(
        {
            "quick_llm_provider": "ollama",
            "quick_llm_base_url": "http://127.0.0.1:11434/v1",
            "quick_think_llm": "qwen3:8b",
            "deep_llm_provider": "openai",
            "deep_llm_base_url": "https://api.openai.com/v1",
            "deep_think_llm": "gpt-5.5",
        }
    )

    assert identity["quick"]["provider"] == "ollama"
    assert identity["deep"]["provider"] == "openai"
    encoded = json.dumps(identity)
    assert "quick-secret" not in encoded
    assert "deep-secret" not in encoded
    assert "api_key" not in encoded


def test_new_role_identity_locks_default_base_url_but_legacy_stays_compatible():
    from tradingagents.graph.stage_runner import llm_identity

    role_identity = llm_identity(
        {
            "quick_llm_provider": "openai",
            "quick_think_llm": "gpt-5.4-mini",
            "deep_llm_provider": "openai",
            "deep_think_llm": "gpt-5.5",
        }
    )
    legacy_identity = llm_identity(
        {
            "llm_provider": "openai",
            "quick_think_llm": "gpt-5.4-mini",
            "deep_think_llm": "gpt-5.5",
        }
    )

    assert role_identity["quick"]["base_url"] == "https://api.openai.com/v1"
    assert role_identity["deep"]["base_url"] == "https://api.openai.com/v1"
    assert legacy_identity["quick"]["base_url"] is None
    assert legacy_identity["deep"]["base_url"] is None


def test_role_keys_are_redacted_from_stage_and_cli_errors(monkeypatch):
    import cli.gx_main as gx_main
    from tradingagents.graph.stage_runner import TradingAgentsStageRunner

    secret = "role-key-must-not-leak"
    monkeypatch.setenv("TRADINGAGENTS_QUICK_LLM_API_KEY", secret)
    error = RuntimeError(f"provider rejected {secret}")

    assert secret not in TradingAgentsStageRunner._safe_error(error)
    assert secret not in gx_main._safe_runtime_error(error)


def test_locality_uses_role_endpoint_and_rejects_remote_ollama():
    local = resolve_llm_profile(
        {
            "quick_llm_provider": "ollama",
            "quick_llm_base_url": "http://127.0.0.1:11434/v1",
            "quick_think_llm": "qwen3:8b",
        },
        "quick",
    )
    remote = resolve_llm_profile(
        {
            "quick_llm_provider": "ollama",
            "quick_llm_base_url": "https://ollama.internal.example/v1",
            "quick_think_llm": "qwen3:8b",
        },
        "quick",
    )

    assert is_local_llm_profile(local)
    assert not is_local_llm_profile(remote)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@llm.example/v1",
        "https://llm.example/v1?api_key=secret",
        "not-a-url",
    ],
)
def test_profile_rejects_non_public_base_url(url):
    with pytest.raises(ValueError, match="base URL"):
        resolve_llm_profile(
            {
                "quick_llm_provider": "openai_compatible",
                "quick_llm_base_url": url,
                "quick_think_llm": "model",
            },
            "quick",
        )


def test_bedrock_rejects_generic_base_url():
    with pytest.raises(ValueError, match="Bedrock.*base URL"):
        resolve_llm_profile(
            {
                "deep_llm_provider": "bedrock",
                "deep_llm_base_url": "https://bedrock.example/v1",
                "deep_think_llm": "model-id",
            },
            "deep",
        )


def test_bedrock_rejects_generic_role_key(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_DEEP_LLM_API_KEY", "not-a-bedrock-token")
    profile = resolve_llm_profile(
        {"deep_llm_provider": "bedrock", "deep_think_llm": "model-id"},
        "deep",
    )

    with pytest.raises(ValueError, match="not supported by Bedrock"):
        resolve_llm_api_key(profile)


def test_graph_constructs_independent_clients(monkeypatch, tmp_path):
    import tradingagents.graph.trading_graph as graph_module

    calls = []

    class FakeClient:
        def get_llm(self):
            return MagicMock()

    def fake_create_llm_client(**kwargs):
        calls.append(kwargs)
        return FakeClient()

    monkeypatch.setattr(graph_module, "create_llm_client", fake_create_llm_client)
    monkeypatch.setenv("TRADINGAGENTS_QUICK_LLM_API_KEY", "quick-secret")
    monkeypatch.setenv("TRADINGAGENTS_DEEP_LLM_API_KEY", "deep-secret")
    config = deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "results_dir": str(tmp_path / "results"),
            "data_cache_dir": str(tmp_path / "cache"),
            "memory_log_path": str(tmp_path / "memory.md"),
            "quick_llm_provider": "openai_compatible",
            "quick_llm_base_url": "http://127.0.0.1:8000/v1",
            "quick_think_llm": "quick-model",
            "deep_llm_provider": "anthropic",
            "deep_llm_base_url": "https://deep.example/v1",
            "deep_think_llm": "deep-model",
        }
    )

    graph_module.TradingAgentsGraph(selected_analysts=("market",), config=config)

    deep_call, quick_call = calls
    assert deep_call["provider"] == "anthropic"
    assert deep_call["model"] == "deep-model"
    assert deep_call["base_url"] == "https://deep.example/v1"
    assert deep_call["api_key"] == "deep-secret"
    assert quick_call["provider"] == "openai_compatible"
    assert quick_call["model"] == "quick-model"
    assert quick_call["base_url"] == "http://127.0.0.1:8000/v1"
    assert quick_call["api_key"] == "quick-secret"


def test_shared_knobs_are_filtered_per_role_provider():
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {
        "openai_reasoning_effort": "high",
        "anthropic_effort": "low",
        "google_thinking_level": "minimal",
        "temperature": 0.2,
        "llm_max_retries": 4,
    }

    openai = graph._get_provider_kwargs("openai")
    anthropic = graph._get_provider_kwargs("anthropic")
    google = graph._get_provider_kwargs("google")

    assert openai["reasoning_effort"] == "high"
    assert "effort" not in openai and "thinking_level" not in openai
    assert anthropic["effort"] == "low"
    assert "reasoning_effort" not in anthropic
    assert google["thinking_level"] == "minimal"
    for kwargs in (openai, anthropic, google):
        assert kwargs["temperature"] == 0.2
        assert kwargs["max_retries"] == 4


def test_azure_role_base_url_and_deployment_are_forwarded(monkeypatch):
    import tradingagents.llm_clients.azure_client as azure_module

    captured = {}

    def fake_chat(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(azure_module, "NormalizedAzureChatOpenAI", fake_chat)
    client = azure_module.AzureOpenAIClient(
        "quick-deployment",
        "https://quick-resource.openai.azure.com",
        api_key="role-key",
        azure_deployment="quick-deployment",
    )

    client.get_llm()

    assert captured["azure_endpoint"] == "https://quick-resource.openai.azure.com"
    assert captured["azure_deployment"] == "quick-deployment"
    assert captured["api_key"] == "role-key"
