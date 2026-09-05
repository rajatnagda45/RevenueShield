"""OpenAI (GPT-4o) planner provider: same contract as the Anthropic one, no network."""
import json
from types import SimpleNamespace

import pytest

from app.agent.providers.anthropic_provider import AnthropicProvider
from app.agent.providers.base import effective_provider_name, llm_breaker, resolve_provider
from app.agent.providers.openai_provider import OpenAIProvider, default_openai_model
from app.agent.providers.prompt import SYSTEM_PROMPT
from app.agent.schemas import ProviderUnavailable
from app.agent.tools import AgentToolbox
from app.core.config import settings


def _completion(tool_calls=None, content=None, finish_reason="tool_calls", refusal=None, prompt_tokens=900, completion_tokens=60, cached=300):
    message = SimpleNamespace(content=content, refusal=refusal, tool_calls=tool_calls or [])
    return SimpleNamespace(
        model="gpt-4o-2024-11-20",
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, prompt_tokens_details=SimpleNamespace(cached_tokens=cached)),
    )


def _tc(id_, name, args):
    return SimpleNamespace(id=id_, type="function", function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _client(*responses):
    completions = _FakeCompletions(responses)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def test_openai_provider_sends_shared_prompt_and_parses_tool_call():
    llm_breaker().record_success()
    client, completions = _client(_completion(tool_calls=[_tc("call_1", "check_policy", {"action": "SEND_WHATSAPP"})], content="Checking policy first."))
    provider = OpenAIProvider(client=client, model="gpt-4o")

    turn = provider.plan({"case": {"id": "c1"}}, AgentToolbox.specs(), [])

    assert turn.tool_calls[0].name == "check_policy" and turn.tool_calls[0].arguments == {"action": "SEND_WHATSAPP"}
    assert turn.final_plan is None and turn.raw_text == "Checking policy first."
    assert turn.usage["input_tokens"] == 900 and turn.usage["output_tokens"] == 60 and turn.usage["cache_read_input_tokens"] == 300
    sent = completions.calls[0]
    assert sent["model"] == "gpt-4o" and sent["tool_choice"] == "required"
    assert sent["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    names = [t["function"]["name"] for t in sent["tools"]]
    assert "submit_plan" in names and "check_policy" in names
    assert all(t["type"] == "function" for t in sent["tools"])
    # replayable assistant message for the next turn
    assistant = turn.usage["assistant_content"]
    assert assistant["role"] == "assistant" and assistant["tool_calls"][0]["function"]["name"] == "check_policy"


def test_openai_provider_replays_transcript_as_tool_messages():
    client, completions = _client(_completion(tool_calls=[_tc("call_2", "submit_plan", {
        "action": "WAIT", "rationale": "Issuer degradation is active; waiting is the fair and effective choice.", "confidence": 0.9, "wait_hours": 4,
    })]))
    provider = OpenAIProvider(client=client, model="gpt-4o")
    transcript = [
        {"role": "assistant", "content": {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "check_policy", "arguments": "{}"}}]}},
        {"role": "tool_results", "results": [{"tool_call_id": "call_1", "name": "check_policy", "ok": True, "result": {"allowed": False}, "blocked_rule": None, "error": None}]},
    ]

    turn = provider.plan({}, [], transcript)

    assert turn.final_plan is not None and turn.final_plan.action == "WAIT" and turn.final_plan.wait_hours == 4
    msgs = completions.calls[0]["messages"]
    assert msgs[2]["role"] == "assistant" and msgs[2]["tool_calls"][0]["id"] == "call_1"
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_1", "content": json.dumps({"allowed": False})}


def test_openai_provider_rejects_invalid_plan_and_surfaces_error_to_model():
    client, _ = _client(_completion(tool_calls=[_tc("call_3", "submit_plan", {"action": "FLY_TO_MOON", "rationale": "x", "confidence": 2})]))
    turn = OpenAIProvider(client=client, model="gpt-4o").plan({}, [], [])
    assert turn.final_plan is None
    assert turn.tool_calls[0].name == "__invalid_plan__" and "error" in turn.tool_calls[0].arguments
    assert "submit_plan rejected" in (turn.raw_text or "")


def test_openai_provider_failures_open_the_breaker():
    llm_breaker().record_success()
    client, _ = _client(RuntimeError("429 rate limited"))
    with pytest.raises(ProviderUnavailable):
        OpenAIProvider(client=client, model="gpt-4o").plan({}, [], [])
    assert llm_breaker().is_open
    # while open, no call is attempted at all
    client2, completions2 = _client(_completion())
    with pytest.raises(ProviderUnavailable):
        OpenAIProvider(client=client2, model="gpt-4o").plan({}, [], [])
    assert completions2.calls == []
    llm_breaker().record_success()

    client3, _ = _client(_completion(refusal="I cannot help with that.", tool_calls=[]))
    with pytest.raises(ProviderUnavailable):
        OpenAIProvider(client=client3, model="gpt-4o").plan({}, [], [])
    llm_breaker().record_success()


def test_provider_resolution_prefers_configured_keys(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "auto")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert effective_provider_name() == "null" and resolve_provider().name == "null"

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    assert effective_provider_name() == "openai"
    provider = resolve_provider()
    assert isinstance(provider, OpenAIProvider) and provider.model == "gpt-4o"

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "sk-ant-test")
    assert effective_provider_name() == "anthropic"
    assert isinstance(resolve_provider(), AnthropicProvider)

    # explicit choice wins over auto; unknown names degrade to rules
    assert resolve_provider("openai").name == "openai"
    assert resolve_provider("null").name == "null"
    assert resolve_provider("bogus").name == "null"


def test_openai_model_defaulting(monkeypatch):
    monkeypatch.setattr(settings, "LLM_MODEL_PLANNER", "claude-opus-5")
    monkeypatch.setattr(settings, "LLM_MODEL_PLANNER_OPENAI", "gpt-4o")
    assert default_openai_model() == "gpt-4o"
    monkeypatch.setattr(settings, "LLM_MODEL_PLANNER", "gpt-4o-mini")
    assert default_openai_model() == "gpt-4o-mini"


def test_openai_runs_cost_accounting():
    from app.agent.runner import RecoveryAgentRunner
    assert RecoveryAgentRunner._cost("gpt-4o-2024-11-20", 1_000_000, 100_000) == pytest.approx(2.5 + 1.0)
    assert RecoveryAgentRunner._cost("gpt-4o-mini", 1_000_000, 0) == pytest.approx(0.15)
