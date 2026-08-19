"""Resolve independent Quick and Deep LLM runtime profiles.

The public config intentionally contains only provider/model/base URL. API keys
are read from the environment at the last possible moment and are excluded from
the profile's representation and equality so they cannot accidentally become
part of a durable session identity.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from .api_key_env import get_api_key_env

LLMRole = Literal["quick", "deep"]

_ROLE_FIELDS = {
    "quick": {
        "provider": "quick_llm_provider",
        "model": "quick_think_llm",
        "base_url": "quick_llm_base_url",
        "api_key_env": "TRADINGAGENTS_QUICK_LLM_API_KEY",
    },
    "deep": {
        "provider": "deep_llm_provider",
        "model": "deep_think_llm",
        "base_url": "deep_llm_base_url",
        "api_key_env": "TRADINGAGENTS_DEEP_LLM_API_KEY",
    },
}
_DEFAULT_MODELS = {"quick": "gpt-5.4-mini", "deep": "gpt-5.5"}

# These mirror the actual provider client defaults. Providers whose SDK owns
# endpoint discovery (Google and Bedrock), Azure, and a generic compatible
# endpoint intentionally remain ``None`` here.
_PROVIDER_DEFAULT_BASE_URLS: dict[str, str | None] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/",
    "google": None,
    "azure": None,
    "bedrock": None,
    "xai": "https://api.x.ai/v1",
    "deepseek": "https://api.deepseek.com",
    "qwen": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "qwen-cn": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "glm": "https://api.z.ai/api/paas/v4/",
    "glm-cn": "https://open.bigmodel.cn/api/paas/v4/",
    "minimax": "https://api.minimax.io/v1",
    "minimax-cn": "https://api.minimaxi.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "mistral": "https://api.mistral.ai/v1",
    "kimi": "https://api.moonshot.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "ollama": "http://localhost:11434/v1",
    "openai_compatible": None,
}


def _nonempty(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _validate_base_url(value: str | None, *, role: LLMRole) -> str | None:
    if value is None:
        return None
    if len(value) > 2048:
        raise ValueError(f"{role} LLM base URL is too long")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise ValueError(f"{role} LLM base URL is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{role} LLM base URL must be HTTP(S) without credentials, query, or fragment"
        )
    return value.rstrip("/")


def provider_default_base_url(provider: str) -> str | None:
    """Return the provider endpoint used when no role/legacy URL is configured."""
    normalized = provider.strip().lower()
    if normalized == "ollama":
        return _nonempty(os.environ.get("OLLAMA_BASE_URL")) or _PROVIDER_DEFAULT_BASE_URLS[
            "ollama"
        ]
    if normalized == "azure":
        return _nonempty(os.environ.get("AZURE_OPENAI_ENDPOINT"))
    return _PROVIDER_DEFAULT_BASE_URLS.get(normalized)


@dataclass(frozen=True)
class ResolvedLLMProfile:
    """One role's public runtime profile; credentials are never stored here."""

    role: LLMRole
    provider: str
    model: str
    # Only an explicit role/legacy override. Provider defaults are resolved by
    # ``effective_base_url`` and by the provider client itself.
    base_url: str | None
    has_role_overrides: bool = False

    @property
    def effective_base_url(self) -> str | None:
        return _validate_base_url(
            self.base_url or provider_default_base_url(self.provider), role=self.role
        )

    def public_identity(self) -> dict[str, str | None]:
        return {
            "provider": self.provider,
            "model": self.model,
            # New role profiles lock their effective endpoint even when they
            # rely on a provider default/env. Pure legacy profiles retain the
            # old explicit-only identity so v1-v3 sessions remain resumable.
            "base_url": self.effective_base_url
            if self.has_role_overrides
            else self.base_url,
        }


def resolve_llm_profile(
    config: Mapping[str, Any], role: LLMRole
) -> ResolvedLLMProfile:
    """Resolve role-specific config, then legacy config, then provider defaults.

    Role-specific API keys are never copied into ``config``. A non-empty role
    key overrides the provider's canonical environment key; an empty role key
    is treated as absent.
    """
    if role not in _ROLE_FIELDS:
        raise ValueError(f"unknown LLM role: {role}")
    fields = _ROLE_FIELDS[role]
    role_provider = _nonempty(config.get(fields["provider"]))
    role_base_url = _nonempty(config.get(fields["base_url"]))
    provider = (
        role_provider or _nonempty(config.get("llm_provider")) or "openai"
    ).lower()
    model = _nonempty(config.get(fields["model"])) or _DEFAULT_MODELS[role]

    base_url = _validate_base_url(
        role_base_url or _nonempty(config.get("backend_url")), role=role
    )
    # Validate provider endpoint env/defaults too (not just explicit role URLs)
    # before any client can consume them.
    _validate_base_url(base_url or provider_default_base_url(provider), role=role)
    if provider == "bedrock" and base_url:
        raise ValueError(
            f"{role} Bedrock profile does not support a generic LLM base URL"
        )

    return ResolvedLLMProfile(
        role=role,
        provider=provider,
        model=model,
        base_url=base_url,
        has_role_overrides=bool(role_provider or role_base_url),
    )


def resolve_llm_api_key(
    profile: ResolvedLLMProfile,
) -> tuple[str | None, str | None]:
    """Return a transient key and its env-var name for one resolved profile."""
    role_key_env = _ROLE_FIELDS[profile.role]["api_key_env"]
    role_key = _nonempty(os.environ.get(role_key_env))
    if profile.provider == "bedrock":
        if role_key:
            raise ValueError(
                f"{role_key_env} is not supported by Bedrock; use the AWS credential chain"
            )
        return None, None
    provider_key_env = get_api_key_env(profile.provider)
    provider_key = (
        _nonempty(os.environ.get(provider_key_env)) if provider_key_env else None
    )
    if role_key:
        return role_key, role_key_env
    if provider_key:
        return provider_key, provider_key_env
    return None, None


def is_local_llm_profile(profile: ResolvedLLMProfile) -> bool:
    """Whether a profile is a loopback Ollama/OpenAI-compatible runtime."""
    if profile.provider not in {"ollama", "openai_compatible"}:
        return False
    base_url = profile.effective_base_url
    if not base_url:
        return False
    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


__all__ = [
    "LLMRole",
    "ResolvedLLMProfile",
    "is_local_llm_profile",
    "provider_default_base_url",
    "resolve_llm_api_key",
    "resolve_llm_profile",
]
