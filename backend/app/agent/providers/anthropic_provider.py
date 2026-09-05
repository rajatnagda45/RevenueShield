"""Claude planner via the official Anthropic SDK.

The model sees the dossier and a bounded tool list; it ends its turn by calling `submit_plan`
with a strict schema. Prompt caching is applied to the stable system prompt and tool list. Any
transport/rate-limit/refusal problem is surfaced as ProviderUnavailable so the runner can degrade
to the deterministic planner and record that it did.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from app.agent.schemas import AgentPlan, ProviderTurn, ProviderUnavailable, ToolCall, ToolSpec
from app.agent.providers.base import llm_breaker
from app.agent.providers.prompt import PROMPT_VERSION, SUBMIT_PLAN_DESCRIPTION, SUBMIT_PLAN_NAME, SYSTEM_PROMPT, plan_schema
from app.core.config import settings

logger = logging.getLogger(__name__)

__all__ = ["AnthropicProvider", "PROMPT_VERSION", "SYSTEM_PROMPT"]


def _plan_tool_spec() -> Dict[str, Any]:
    return {"name": SUBMIT_PLAN_NAME, "description": SUBMIT_PLAN_DESCRIPTION, "input_schema": plan_schema()}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: Optional[Any] = None, model: Optional[str] = None):
        self.model = model or settings.LLM_MODEL_PLANNER
        self._client = client
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("anthropic SDK is not installed") from exc
            api_key = settings.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not configured")
            self._client = anthropic.Anthropic(api_key=api_key, timeout=settings.LLM_TIMEOUT_SECONDS, max_retries=1)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _tools_payload(tools: List[ToolSpec]) -> List[Dict[str, Any]]:
        out = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
        out.append(_plan_tool_spec())
        out[-1]["cache_control"] = {"type": "ephemeral"}
        return out

    @staticmethod
    def _messages(dossier: Dict[str, Any], transcript: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = [{
            "role": "user",
            "content": "Case dossier (JSON). Decide the next move and finish with submit_plan.\n\n" + json.dumps(dossier, default=str, indent=1),
        }]
        for entry in transcript:
            if entry.get("role") == "assistant":
                messages.append({"role": "assistant", "content": entry["content"]})
            elif entry.get("role") == "tool_results":
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["tool_call_id"], "content": json.dumps(r["result"], default=str)[:6000], "is_error": not r["ok"]}
                    for r in entry["results"]
                ]})
        return messages

    # ------------------------------------------------------------------ plan

    def plan(self, dossier: Dict[str, Any], tools: List[ToolSpec], transcript: List[Dict[str, Any]]) -> ProviderTurn:
        breaker = llm_breaker()
        if breaker.is_open:
            raise ProviderUnavailable("LLM circuit breaker is open")
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                tools=self._tools_payload(tools),
                messages=self._messages(dossier, transcript),
                thinking={"type": "adaptive"},
                output_config={"effort": settings.LLM_EFFORT},
            )
        except Exception as exc:
            breaker.record_failure()
            status = getattr(exc, "status_code", None)
            logger.warning(f"[AGENT_LLM_UNAVAILABLE] model={self.model} status={status} error={exc}")
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            breaker.record_failure()
            raise ProviderUnavailable("model refused the request")
        breaker.record_success()

        usage = getattr(response, "usage", None)
        usage_dict = {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
            "model": getattr(response, "model", self.model),
        }

        content = getattr(response, "content", []) or []
        tool_calls: List[ToolCall] = []
        final_plan: Optional[AgentPlan] = None
        texts: List[str] = []
        assistant_blocks: List[Dict[str, Any]] = []
        for block in content:
            btype = getattr(block, "type", None)
            if btype == "text":
                texts.append(block.text)
                assistant_blocks.append({"type": "text", "text": block.text})
            elif btype == "tool_use":
                args = block.input if isinstance(block.input, dict) else json.loads(block.input or "{}")
                assistant_blocks.append({"type": "tool_use", "id": block.id, "name": block.name, "input": args})
                if block.name == "submit_plan":
                    try:
                        final_plan = AgentPlan.model_validate(args)
                    except Exception as exc:
                        texts.append(f"[submit_plan rejected: {exc}]")
                        tool_calls.append(ToolCall(id=block.id, name="__invalid_plan__", arguments={"error": str(exc)}))
                else:
                    tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=args))
            elif btype == "thinking":
                assistant_blocks.append({"type": "thinking", "thinking": getattr(block, "thinking", ""), "signature": getattr(block, "signature", "")})

        turn = ProviderTurn(tool_calls=tool_calls, final_plan=final_plan, raw_text="\n".join(texts) or None, usage=usage_dict)
        turn.usage["assistant_content"] = assistant_blocks  # replayed verbatim on the next turn
        return turn
