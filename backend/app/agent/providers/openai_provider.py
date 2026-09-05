"""GPT planner via the official OpenAI SDK (Chat Completions + function calling).

Same contract as the Anthropic provider: the model sees the dossier and the bounded tool list, may call a
few read-only tools, and must end by calling `submit_plan`. `tool_choice="required"` keeps GPT-4o from
answering in prose (a prose-only turn would end the run with no plan and fall back to rules). Any transport,
rate-limit or refusal problem is raised as ProviderUnavailable so the runner degrades to the deterministic
planner and records that it did.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from app.agent.providers.base import llm_breaker
from app.agent.providers.prompt import PROMPT_VERSION, SUBMIT_PLAN_DESCRIPTION, SUBMIT_PLAN_NAME, SYSTEM_PROMPT, plan_schema
from app.agent.schemas import AgentPlan, ProviderTurn, ProviderUnavailable, ToolCall, ToolSpec
from app.core.config import settings

__all__ = ["OpenAIProvider", "PROMPT_VERSION"]

logger = logging.getLogger(__name__)


def default_openai_model() -> str:
    """`LLM_MODEL_PLANNER` if it names an OpenAI model, else the OpenAI-specific default (gpt-4o)."""
    planner = (settings.LLM_MODEL_PLANNER or "").strip()
    if planner and not planner.lower().startswith("claude"):
        return planner
    return settings.LLM_MODEL_PLANNER_OPENAI


class OpenAIProvider:
    name = "openai"

    def __init__(self, client: Optional[Any] = None, model: Optional[str] = None):
        self.model = model or default_openai_model()
        self._client = client
        if self._client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("openai SDK is not installed") from exc
            api_key = settings.OPENAI_API_KEY or os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is not configured")
            kwargs: Dict[str, Any] = {"api_key": api_key, "timeout": settings.LLM_TIMEOUT_SECONDS, "max_retries": 1}
            base_url = settings.OPENAI_BASE_URL or os.environ.get("OPENAI_BASE_URL")
            if base_url:
                kwargs["base_url"] = base_url
            self._client = openai.OpenAI(**kwargs)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _tools_payload(tools: List[ToolSpec]) -> List[Dict[str, Any]]:
        out = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
            for t in tools
        ]
        out.append({"type": "function", "function": {"name": SUBMIT_PLAN_NAME, "description": SUBMIT_PLAN_DESCRIPTION, "parameters": plan_schema()}})
        return out

    @staticmethod
    def _messages(dossier: Dict[str, Any], transcript: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Case dossier (JSON). Decide the next move and finish with submit_plan.\n\n" + json.dumps(dossier, default=str, indent=1)},
        ]
        for entry in transcript:
            if entry.get("role") == "assistant":
                content = entry.get("content")
                if isinstance(content, dict) and content.get("role") == "assistant":
                    messages.append(content)  # our own previous turn, replayed verbatim (includes tool_calls)
                elif isinstance(content, str):
                    messages.append({"role": "assistant", "content": content})
                else:  # blocks from another provider (only after a mid-run switch): flatten to text
                    messages.append({"role": "assistant", "content": json.dumps(content, default=str)[:4000]})
            elif entry.get("role") == "tool_results":
                for r in entry["results"]:
                    payload = r["result"] if r["ok"] else {"ok": False, "error": r.get("error"), "blocked_rule": r.get("blocked_rule")}
                    messages.append({"role": "tool", "tool_call_id": r["tool_call_id"], "content": json.dumps(payload, default=str)[:6000]})
        return messages

    # ------------------------------------------------------------------ plan

    def plan(self, dossier: Dict[str, Any], tools: List[ToolSpec], transcript: List[Dict[str, Any]]) -> ProviderTurn:
        breaker = llm_breaker()
        if breaker.is_open:
            raise ProviderUnavailable("LLM circuit breaker is open")
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=self._messages(dossier, transcript),
                tools=self._tools_payload(tools),
                tool_choice="required",
                temperature=0.2,
                max_completion_tokens=2048,
            )
        except Exception as exc:
            breaker.record_failure()
            status = getattr(exc, "status_code", None)
            logger.warning(f"[AGENT_LLM_UNAVAILABLE] provider=openai model={self.model} status={status} error={exc}")
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc

        choices = getattr(response, "choices", None) or []
        if not choices:
            breaker.record_failure()
            raise ProviderUnavailable("empty completion")
        choice = choices[0]
        message = choice.message
        if getattr(message, "refusal", None):
            breaker.record_failure()
            raise ProviderUnavailable("model refused the request")
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "content_filter":
            breaker.record_failure()
            raise ProviderUnavailable("completion blocked by content filter")
        breaker.record_success()

        usage = getattr(response, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        usage_dict = {
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(details, "cached_tokens", 0) or 0,
            "cache_creation_input_tokens": 0,
            "model": getattr(response, "model", self.model),
            "finish_reason": finish_reason,
        }

        text = message.content if isinstance(getattr(message, "content", None), str) else None
        texts: List[str] = [text] if text else []
        tool_calls: List[ToolCall] = []
        final_plan: Optional[AgentPlan] = None
        replay_calls: List[Dict[str, Any]] = []
        for tc in getattr(message, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            fn_name = getattr(fn, "name", None) or ""
            raw_args = getattr(fn, "arguments", None) or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError as exc:
                args = {}
                texts.append(f"[{fn_name} arguments were not valid JSON: {exc}]")
            replay_calls.append({"id": tc.id, "type": "function", "function": {"name": fn_name, "arguments": raw_args if isinstance(raw_args, str) else json.dumps(raw_args)}})
            if fn_name == SUBMIT_PLAN_NAME:
                try:
                    final_plan = AgentPlan.model_validate(args)
                except Exception as exc:
                    texts.append(f"[submit_plan rejected: {exc}]")
                    tool_calls.append(ToolCall(id=tc.id, name="__invalid_plan__", arguments={"error": str(exc)}))
            else:
                tool_calls.append(ToolCall(id=tc.id, name=fn_name, arguments=args))

        assistant_message: Dict[str, Any] = {"role": "assistant", "content": text}
        if replay_calls:
            assistant_message["tool_calls"] = replay_calls

        turn = ProviderTurn(tool_calls=tool_calls, final_plan=final_plan, raw_text="\n".join(texts) or None, usage=usage_dict)
        turn.usage["assistant_content"] = assistant_message  # replayed verbatim on the next turn
        return turn
