"""Provider protocol, circuit breaker and resolution."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Protocol

from app.agent.schemas import ProviderTurn, ToolSpec
from app.core.config import settings
from app.core.resilience import CircuitBreaker

logger = logging.getLogger(__name__)


class LLMProvider(Protocol):
    name: str
    model: str

    def plan(self, dossier: Dict[str, Any], tools: List[ToolSpec], transcript: List[Dict[str, Any]]) -> ProviderTurn: ...


_breaker = CircuitBreaker(cooldown_seconds=settings.LLM_CIRCUIT_BREAKER_SECONDS, failure_threshold=1)


def llm_breaker() -> CircuitBreaker:
    return _breaker


def anthropic_key_configured() -> bool:
    return bool(settings.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY"))


def openai_key_configured() -> bool:
    return bool(settings.OPENAI_API_KEY or os.environ.get("OPENAI_API_KEY"))


def effective_provider_name(name: Optional[str] = None) -> str:
    """What `auto` resolves to right now: anthropic if that key exists, else openai if that key exists, else null."""
    choice = (name or settings.LLM_PROVIDER or "auto").lower()
    if choice == "auto":
        if anthropic_key_configured():
            return "anthropic"
        if openai_key_configured():
            return "openai"
        return "null"
    return choice


def resolve_provider(name: Optional[str] = None) -> LLMProvider:
    """Pick the planner: explicit name, else settings, else auto (a hosted LLM if a key exists, otherwise rules)."""
    from app.agent.providers.null_provider import NullLLMProvider

    choice = effective_provider_name(name)
    if choice == "anthropic":
        try:
            from app.agent.providers.anthropic_provider import AnthropicProvider
            return AnthropicProvider()
        except Exception as exc:  # SDK missing or misconfigured -> rules
            logger.warning(f"[AGENT_PROVIDER] Anthropic provider unavailable ({exc}); using NullLLM.")
            return NullLLMProvider()
    if choice == "openai":
        try:
            from app.agent.providers.openai_provider import OpenAIProvider
            return OpenAIProvider()
        except Exception as exc:
            logger.warning(f"[AGENT_PROVIDER] OpenAI provider unavailable ({exc}); using NullLLM.")
            return NullLLMProvider()
    if choice not in ("null", "rules"):
        logger.warning(f"[AGENT_PROVIDER] Unknown LLM provider '{choice}'; using NullLLM.")
    return NullLLMProvider()
