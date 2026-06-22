"""LLM provider registry.

Two service "kinds" cover essentially every model vendor:

  - ``anthropic`` -> native AnthropicLLMService (Claude).
  - ``openai``    -> OpenAILLMService pointed at any OpenAI-compatible /v1
                     endpoint. That single path covers OpenAI, Ollama, vLLM,
                     Together/Groq/etc., **and Gemini** (Google exposes an
                     OpenAI-compatible endpoint), plus any custom server.

Each entry is a *preset* that fills in sensible defaults (kind, base URL, the
env var its API key comes from, model suggestions). A call's effective settings
(provider/model/base_url/api_key) are turned into a concrete spec by ``resolve``.
The API key can come from the admin settings (per-deployment override) or the
provider's env var; for keyless local servers (Ollama/vLLM) it's optional.
"""

from __future__ import annotations

import os

from config import config

# kind: how to instantiate the Pipecat service. label/models are UI hints.
PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "kind": "anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "base_url": "",
        "needs_base_url": False,
        "models": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"],
    },
    "openai": {
        "label": "OpenAI",
        "kind": "openai",
        "env_key": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "needs_base_url": False,
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o4-mini"],
    },
    "gemini": {
        "label": "Google Gemini",
        "kind": "openai",  # via Gemini's OpenAI-compatible endpoint
        "env_key": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "needs_base_url": False,
        "models": ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
    },
    "ollama": {
        "label": "Ollama (self-hosted)",
        "kind": "openai",
        "env_key": "",  # keyless
        "base_url": "",  # derived from OLLAMA_BASE_URL when left blank
        "needs_base_url": True,
        "models": [],  # discovered live (see admin._ollama_models)
    },
    "vllm": {
        "label": "vLLM (self-hosted)",
        "kind": "openai",
        "env_key": "VLLM_API_KEY",  # usually any/empty
        "base_url": "",
        "needs_base_url": True,
        "models": [],
    },
    "custom": {
        "label": "OpenAI-compatible (custom)",
        "kind": "openai",
        "env_key": "LLM_API_KEY",
        "base_url": "",
        "needs_base_url": True,
        "models": [],
    },
}

PROVIDER_IDS = tuple(PROVIDERS.keys())


def preset(provider_id: str) -> dict:
    return PROVIDERS.get(provider_id, PROVIDERS["custom"])


def effective_base_url(provider_id: str, override: str) -> str:
    """The base URL a call will actually use for this provider."""
    override = (override or "").strip()
    if override:
        return override
    if provider_id == "ollama":
        return config.ollama_base_url.rstrip("/") + "/v1"
    return preset(provider_id)["base_url"]


def env_key_present(provider_id: str) -> bool:
    p = preset(provider_id)
    return bool(p["env_key"] and os.getenv(p["env_key"], ""))


def resolve(eff: dict) -> dict:
    """Turn effective settings into a concrete service spec.

    Returns {kind, model, base_url, api_key, system_in_context}.
    """
    pid = eff.get("llm_provider", "anthropic")
    p = preset(pid)
    base = effective_base_url(pid, eff.get("llm_base_url", ""))

    key = (eff.get("llm_api_key") or "").strip()
    if not key and p["env_key"]:
        key = os.getenv(p["env_key"], "")
    if not key and p["kind"] == "openai":
        key = "x"  # OpenAI client requires a non-empty key; local servers ignore it

    return {
        "kind": p["kind"],
        "model": eff["llm_model"],
        "base_url": base,
        "api_key": key,
        "system_in_context": p["kind"] != "anthropic",
    }
