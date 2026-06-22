"""Runtime-editable settings, overlaid on the env-driven defaults in config.

The admin console writes here; each new call reads the effective values when its
pipeline is built (changes apply to new calls, not ones already in progress).
Durable overrides live in Postgres (the `app_settings` table — the system of
record), with an in-process fallback when Postgres is unavailable.
"""

from __future__ import annotations

from loguru import logger

from config import config
from personas import PERSONA_IDS
from providers import PROVIDER_IDS

# Only these keys are user-editable. Everything else (infra URLs, Whisper
# device/compute) stays env-driven for safety.
EDITABLE_KEYS = (
    "llm_provider",
    "llm_model",
    "llm_base_url",  # OpenAI-compatible endpoint (ollama/vllm/custom/etc.)
    "llm_api_key",   # optional per-deployment key override (sensitive)
    "agent_persona",  # "what type of agent" (preset id); system_prompt is effective
    "system_prompt",
    "tts_voice",
    "whisper_model",
    "memory_enabled",        # reload this caller's prior turns at call start
    "memory_max_messages",   # how many prior messages to reload
    "history_max_messages",  # cap live context to last N messages (0 = unlimited)
)

# These may be blank (e.g. a keyless local server, or "use the env/default").
_OPTIONAL_EMPTY = {"llm_base_url", "llm_api_key"}


def _defaults() -> dict:
    return {
        "llm_provider": config.llm_provider,
        "llm_model": config.llm_model,
        "llm_base_url": "",
        "llm_api_key": "",
        "agent_persona": "assistant",
        "system_prompt": config.system_prompt,
        "tts_voice": config.tts_voice,
        "whisper_model": config.whisper_model,
        "memory_enabled": "false",
        "memory_max_messages": "20",
        "history_max_messages": "0",
    }


def _validate(patch: dict) -> dict:
    """Keep only known keys; reject bad providers and empty required values."""
    clean: dict[str, str] = {}
    for k, v in patch.items():
        if k not in EDITABLE_KEYS:
            continue
        if v is None:
            continue
        v = str(v).strip()
        if not v and k not in _OPTIONAL_EMPTY:
            raise ValueError(f"{k} cannot be empty")
        if k == "llm_provider" and v not in PROVIDER_IDS:
            raise ValueError(f"llm_provider must be one of {', '.join(PROVIDER_IDS)}")
        if k == "agent_persona" and v not in PERSONA_IDS:
            raise ValueError(f"agent_persona must be one of {', '.join(PERSONA_IDS)}")
        if k == "memory_enabled":
            v = "true" if v.lower() in ("true", "1", "yes", "on") else "false"
        if k in ("memory_max_messages", "history_max_messages"):
            if not v.isdigit():
                raise ValueError(f"{k} must be a non-negative integer")
        clean[k] = v
    return clean


class Settings:
    def __init__(self) -> None:
        self._p = None  # Persistence, bound at startup

    def bind(self, persistence) -> None:
        self._p = persistence

    async def _overrides(self) -> dict:
        if self._p is None:
            return {}
        stored = await self._p.settings_get_all()
        return {k: v for k, v in stored.items() if k in EDITABLE_KEYS}

    async def get(self) -> dict:
        """Effective settings: defaults overlaid with persisted overrides."""
        merged = _defaults()
        merged.update(await self._overrides())
        return merged

    async def get_for_agent(self, slug: str | None) -> dict:
        """Effective settings for a named agent: global config with the agent's
        non-empty fields overlaid. Unknown/disabled agent -> global config."""
        base = await self.get()
        if not slug or self._p is None:
            return base
        agent = await self._p.get_agent(slug)
        if not agent or not agent.get("enabled", True):
            return base
        for k in EDITABLE_KEYS:
            v = agent.get(k)
            if v not in (None, ""):
                base[k] = v
        return base

    async def update(self, patch: dict) -> dict:
        """Validate + persist a partial update; return the new effective set."""
        clean = _validate(patch)
        if clean and self._p is not None:
            await self._p.settings_set(clean)
            logger.info(f"settings: updated {sorted(clean)}")
        return await self.get()

    async def reset(self) -> dict:
        """Drop agent-config overrides, returning to env defaults. Leaves other
        stored settings (e.g. SMTP) untouched."""
        if self._p is not None:
            await self._p.settings_delete(list(EDITABLE_KEYS))
        return await self.get()


# Module singleton; persistence is bound in bot.py at startup.
settings = Settings()
