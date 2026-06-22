"""LLM provider factory.

The brain is pluggable across vendors via the registry in providers.py. Anthropic
(Claude) uses the native service; everything else — OpenAI, Google Gemini, Ollama,
vLLM, or any custom OpenAI-compatible server — goes through OpenAILLMService with
the appropriate base URL and key.

System-prompt handling differs by kind, so this returns a flag telling the caller
whether the prompt is already attached (Anthropic's system_instruction) or must be
added to the conversation context as a system message (OpenAI-compatible).
"""

from __future__ import annotations

from loguru import logger
from pipecat.services.anthropic.llm import AnthropicLLMService

from providers import resolve


def build_llm(effective: dict, system_text: str):
    """Return (llm_service, system_in_context: bool) for the effective settings.

    `system_text` is the final, guardrail-wrapped system prompt (see guardrails.py).
    """
    spec = resolve(effective)
    provider = effective.get("llm_provider", "anthropic")

    if spec["kind"] == "anthropic":
        logger.info(f"LLM: anthropic model={spec['model']}")
        llm = AnthropicLLMService(
            api_key=spec["api_key"],
            settings=AnthropicLLMService.Settings(
                model=spec["model"],
                enable_prompt_caching=True,
                system_instruction=system_text,
            ),
        )
        return llm, False

    # OpenAI-compatible: OpenAI, Gemini, Ollama, vLLM, custom — only URL/key vary.
    from pipecat.services.openai.llm import OpenAILLMService

    logger.info(
        f"LLM: {provider} (openai-compatible) model={spec['model']} "
        f"base_url={spec['base_url'] or 'default'}"
    )
    kwargs: dict = {"api_key": spec["api_key"], "model": spec["model"]}
    if spec["base_url"]:
        kwargs["base_url"] = spec["base_url"]
    llm = OpenAILLMService(**kwargs)
    return llm, spec["system_in_context"]
