"""Recovery Agent: bounded tools, deterministic validation, approvals, degradation to rules, traces."""
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.approvals import ApprovalService
from app.agent.dossier import DossierBuilder
from app.agent.providers.anthropic_provider import AnthropicProvider
from app.agent.providers.base import llm_breaker
from app.agent.providers.null_provider import NullLLMProvider
from app.agent.runner import RecoveryAgentRunner
from app.agent.schemas import AgentPlan, MessageDraft, PaymentPlanOffer, ProviderTurn, ProviderUnavailable, ToolCall
from app.agent.tools import AgentToolbox
from app.agent.validators import MessageValidator, NegotiationEnvelope, PlanValidator
from app.compliance.handoff import HandoffService
from app.core.config import settings
from app.experiments.assigner import ExperimentArm, ExperimentAssigner
from app.models.agent_run import AgentRun
from app.models.approval import Approval
from app.models.audit_log import AuditLog
from app.models.communication import Communication
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlanStep
from app.services.recovery_scheduler import RecoveryScheduler
from tests.helpers_v2 import make_case, make_customer

IST = __import__("zoneinfo").ZoneInfo("Asia/Kolkata")
DAYTIME = datetime(2026, 9, 7, 11, 0, tzinfo=IST).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", None)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "null")
    llm_breaker().reset()


class ScriptedProvider:
    """Test double: returns a scripted sequence of turns, or raises."""

    name = "scripted"
    model = "scripted-v1"

    def __init__(self, turns, raise_first=None):
        self._turns = list(turns)
        self._raise_first = raise_first
        self.calls = 0

    def plan(self, dossier, tools, transcript):
        self.calls += 1
        if self._raise_first is not None and self.calls == 1:
            raise self._raise_first
        return self._turns.pop(0)


def _open_case(db, amount=2500.0, **kw):
    customer = make_customer(db, **kw)
    case = make_case(db, customer, amount=amount)
    RecoveryScheduler.create_or_get_plan(db, case.id)
    return customer, case


# ------------------------------------------------------------------ validators

def test_message_validator_rules():
    good = MessageDraft(body="Hi Asha, your payment of ₹2,500.00 could not be completed. Complete it securely here: {payment_link}. Reply STOP to opt out.")
    assert MessageValidator.validate(good, outstanding_amount=Decimal("2500")).ok
    bad = MessageDraft(body="Pay ₹9,999.00 now or we will take legal action. Your risk score is high. BAD_REQUEST_ERROR")
    res = MessageValidator.validate(bad, outstanding_amount=Decimal("2500"))
    assert not res.ok
    joined = " ".join(res.errors).lower()
    assert "legal action" in joined and "risk score" in joined and "9,999.00" in joined and "opt-out" in joined and "{payment_link}" in joined
    email = MessageDraft(channel="EMAIL", body="Hello, your invoice of ₹2,500.00 is pending. {payment_link}")
    assert MessageValidator.validate(email, outstanding_amount=Decimal("2500")).ok  # no footer needed on email


def test_negotiation_envelope():
    ok = NegotiationEnvelope.validate(PaymentPlanOffer(installments=3, first_payment_pct=40, due_days=7), outstanding_amount=Decimal("3000"))
    assert ok.ok and NegotiationEnvelope.first_amount(PaymentPlanOffer(installments=3, first_payment_pct=40), Decimal("3000")) == Decimal("1200.00")
    bad = NegotiationEnvelope.validate(PaymentPlanOffer(installments=6, first_payment_pct=10, due_days=30, waiver_pct=5), outstanding_amount=Decimal("3000"))
    assert not bad.ok and len(bad.errors) == 4


def test_plan_validator_structural():
    res = PlanValidator.validate(AgentPlan(action="HANDOFF_TO_HUMAN", rationale="too short", confidence=0.5), outstanding_amount=Decimal("100"))
    assert not res.ok and any("handoff_reason" in e for e in res.errors) and any("rationale" in e for e in res.errors)


# ------------------------------------------------------------------ dossier & tools

def test_dossier_precomputes_verdicts(db_session: Session):
    customer, case = _open_case(db_session)
    d = DossierBuilder.build(db_session, case, now=DAYTIME)
    assert d["case"]["outstanding_amount"] == 2500.0 and d["hash"]
    verdicts = {v["action"]: v for v in d["constraints"]["action_verdicts"]}
    assert verdicts["SEND_WHATSAPP"]["allowed"] and verdicts["SEND_EMAIL"]["allowed"]
    assert d["customer"]["phone_masked"].startswith("+919") and "9876543210" not in json.dumps(d)
    # holdout: everything but WAIT / NO_ACTION blocked
    ExperimentAssigner.assign(db_session, case, force_arm=ExperimentArm.HOLDOUT)
    d2 = DossierBuilder.build(db_session, case, now=DAYTIME)
    assert set(d2["constraints"]["allowed_actions"]) == {"WAIT", "NO_ACTION"}


def test_toolbox_gates_and_executes(db_session: Session):
    customer, case = _open_case(db_session)
    box = AgentToolbox(db_session, case, dry_run=True, now=DAYTIME)
    names = {t.name for t in box.specs()}
    assert {"send_whatsapp", "offer_payment_plan", "handoff_to_human", "check_policy"} <= names
    unknown = box.execute(ToolCall(id="1", name="nope", arguments={}))
    assert not unknown.ok and "unknown tool" in unknown.error
    pol = box.execute(ToolCall(id="2", name="check_policy", arguments={"action": "SEND_WHATSAPP"}))
    assert pol.ok and pol.result["allowed"]
    sent = box.execute(ToolCall(id="3", name="send_whatsapp", arguments={"body": "Hi Test, ₹2,500.00 is pending: {payment_link}. Reply STOP to opt out."}))
    assert sent.ok and sent.result["status"] == "SENT"
    comm = db_session.scalar(select(Communication).where(Communication.recovery_case_id == case.id))
    assert "{payment_link}" not in comm.message_body and "http" in comm.message_body and comm.template_name == "AGENT_PERSONALISED_V1"
    # envelope violation never reaches the gateway
    plan = box.execute(ToolCall(id="4", name="offer_payment_plan", arguments={"installments": 8, "first_payment_pct": 5}))
    assert not plan.ok and plan.blocked_rule == "NEGOTIATION_ENVELOPE"
    # voice on a normal-value case: blocked only by policy state (min gap after the WhatsApp touch)
    voice = box.execute(ToolCall(id="5", name="start_voice_call", arguments={}))
    assert not voice.ok


# ------------------------------------------------------------------ runner with the deterministic planner

def test_agent_run_executes_recommended_action_with_trace(db_session: Session):
    customer, case = _open_case(db_session)
    run = RecoveryAgentRunner.run(db_session, case.id, dry_run=True, now=DAYTIME)
    assert run.provider == "null" and run.status == "EXECUTED" and not run.degraded_to_rules
    assert run.final_plan["action"] in ("SEND_WHATSAPP", "SEND_EMAIL", "SEND_PAYMENT_LINK", "SCHEDULE_RETRY")
    events = [t["event"] for t in run.trace]
    assert events.count("planner_turn") == 2 and "tool_result" in events and "executed" in events
    assert run.execution_result["ok"] is True
    assert run.dossier_hash and run.validation["ok"]
    assert db_session.scalar(select(AuditLog).where(AuditLog.recovery_case_id == case.id, AuditLog.action == "AGENT_RUN_COMPLETED")) is not None


def test_agent_respects_holdout_and_handoff(db_session: Session):
    customer, case = _open_case(db_session)
    ExperimentAssigner.assign(db_session, case, force_arm=ExperimentArm.HOLDOUT)
    run = RecoveryAgentRunner.run(db_session, case.id, dry_run=True, now=DAYTIME)
    assert run.final_plan["action"] in ("WAIT", "NO_ACTION") and run.status in ("EXECUTED", "NO_ACTION")
    assert db_session.scalar(select(Communication).where(Communication.recovery_case_id == case.id)) is None

    customer2, case2 = _open_case(db_session, phone="+919000000041")
    HandoffService.freeze(db_session, case=case2, reason="dispute", source="TEST")
    run2 = RecoveryAgentRunner.run(db_session, case2.id, dry_run=True, now=DAYTIME)
    assert run2.final_plan["action"] == "NO_ACTION" and run2.status == "NO_ACTION"


def test_high_value_voice_requires_approval_then_executes_on_approve(db_session: Session):
    customer, case = _open_case(db_session, amount=120000.0, phone="+919000000042")
    provider = ScriptedProvider([ProviderTurn(final_plan=AgentPlan(action="START_VOICE_CALL", rationale="High-value case with a reachable phone; a call is the most effective next step.", confidence=0.8))])
    run = RecoveryAgentRunner.run(db_session, case.id, dry_run=True, now=DAYTIME, provider=provider)
    assert run.status == "AWAITING_APPROVAL" and run.approval_id is not None
    approval = db_session.scalar(select(Approval).where(Approval.id == run.approval_id))
    assert approval.status == "PENDING" and approval.action == "START_VOICE_CALL"

    ApprovalService.approve(db_session, approval.id, operator="ops@merchant", note="ok", dry_run=True, now=DAYTIME)
    db_session.refresh(run)
    assert approval.status == "APPROVED" and approval.execution_result["ok"] is True
    assert run.status == "EXECUTED" and run.execution_result["result"]["status"] in ("QUEUED", "INITIATED")

    # a second identical request is idempotent; rejection path
    customer3, case3 = _open_case(db_session, amount=90000.0, phone="+919000000043")
    run3 = RecoveryAgentRunner.run(db_session, case3.id, dry_run=True, now=DAYTIME, provider=ScriptedProvider([ProviderTurn(final_plan=AgentPlan(action="ESCALATE_TO_MERCHANT", rationale="Large receivable, reminders exhausted; finance should talk to the buyer directly.", confidence=0.7))]))
    a3 = db_session.scalar(select(Approval).where(Approval.id == run3.approval_id))
    ApprovalService.reject(db_session, a3.id, operator="ops", note="not yet", now=DAYTIME)
    db_session.refresh(run3)
    assert a3.status == "REJECTED" and run3.status == "BLOCKED"
    with pytest.raises(ValueError):
        ApprovalService.reject(db_session, a3.id, operator="ops")


def test_approval_expiry(db_session: Session):
    customer, case = _open_case(db_session, amount=200000.0, phone="+919000000044")
    run = RecoveryAgentRunner.run(db_session, case.id, dry_run=True, now=DAYTIME, provider=ScriptedProvider([ProviderTurn(final_plan=AgentPlan(action="START_VOICE_CALL", rationale="High value, reachable by phone, best served by a direct conversation.", confidence=0.8))]))
    expired = ApprovalService.expire_due(db_session, now=DAYTIME + timedelta(hours=settings.APPROVAL_SLA_HOURS + 1))
    assert str(run.approval_id) in expired
    db_session.refresh(run)
    assert run.status == "BLOCKED" and run.execution_result["blocking_rule"] == "APPROVAL_EXPIRED"


def test_llm_outage_degrades_to_rules(db_session: Session):
    customer, case = _open_case(db_session, phone="+919000000045")
    provider = ScriptedProvider([], raise_first=ProviderUnavailable("429 rate limited"))
    run = RecoveryAgentRunner.run(db_session, case.id, dry_run=True, now=DAYTIME, provider=provider)
    assert run.degraded_to_rules is True and run.status in ("EXECUTED", "NO_ACTION")
    assert any(t["event"] == "provider_unavailable" for t in run.trace)
    assert run.final_plan is not None


def test_invalid_llm_message_falls_back_to_template(db_session: Session):
    customer, case = _open_case(db_session, phone="+919000000046")
    bad_msg = MessageDraft(body="Pay ₹99,999.00 immediately or face legal action. {payment_link}")
    provider = ScriptedProvider([ProviderTurn(final_plan=AgentPlan(action="SEND_WHATSAPP", rationale="Customer prefers WhatsApp and the amount is small; a friendly reminder with the link should do.", confidence=0.7, message=bad_msg))])
    run = RecoveryAgentRunner.run(db_session, case.id, dry_run=True, now=DAYTIME, provider=provider)
    assert run.status == "EXECUTED"
    assert any(t["event"] == "message_rejected_fallback_to_template" for t in run.trace)
    comm = db_session.scalar(select(Communication).where(Communication.recovery_case_id == case.id))
    assert "legal" not in comm.message_body.lower() and comm.template_name != "AGENT_PERSONALISED_V1"


def test_payment_plan_creates_partial_link(db_session: Session):
    customer, case = _open_case(db_session, amount=6000.0, phone="+919000000047")
    provider = ScriptedProvider([ProviderTurn(final_plan=AgentPlan(action="OFFER_PAYMENT_PLAN", rationale="Customer history shows repeated small failures; splitting ₹6,000 into three parts with 40% up front is inside the envelope.", confidence=0.7, payment_plan=PaymentPlanOffer(installments=3, first_payment_pct=40, due_days=7)))])
    run = RecoveryAgentRunner.run(db_session, case.id, dry_run=True, now=DAYTIME, provider=provider)
    assert run.status == "EXECUTED", run.execution_result
    db_session.refresh(case)
    assert case.case_metadata["payment_plan"]["first_amount"] == 2400.0
    assert db_session.scalar(select(AuditLog).where(AuditLog.recovery_case_id == case.id, AuditLog.action == "PAYMENT_PLAN_OFFERED")) is not None
    link_audit = db_session.scalar(select(AuditLog).where(AuditLog.recovery_case_id == case.id, AuditLog.action == "PAYMENT_LINK_CREATED"))
    assert link_audit.audit_metadata["accept_partial"] is True and link_audit.audit_metadata["first_min_partial_amount"] == 2400.0


def test_agent_tool_loop_with_scripted_calls_and_promise(db_session: Session):
    customer, case = _open_case(db_session, phone="+919000000048")
    promised = (DAYTIME + timedelta(days=3)).date()
    provider = ScriptedProvider([
        ProviderTurn(tool_calls=[ToolCall(id="a", name="get_customer_history", arguments={}), ToolCall(id="b", name="check_policy", arguments={"action": "START_VOICE_CALL"})], raw_text="looking"),
        ProviderTurn(final_plan=AgentPlan(action="RECORD_PROMISE_TO_PAY", rationale="Customer said on the last call they will pay after salary day; record the promise and pause outreach.", confidence=0.9, promise={"promised_date": promised.isoformat()})),
    ])
    run = RecoveryAgentRunner.run(db_session, case.id, dry_run=True, now=DAYTIME, provider=provider)
    assert run.status == "EXECUTED" and run.turns == 2
    assert sum(1 for t in run.trace if t["event"] == "tool_result") == 2
    assert run.execution_result["result"]["promise_id"]


def test_scheduler_can_be_driven_by_agent(db_session: Session, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_DRIVES_PLANS", True)
    customer, case = _open_case(db_session, phone="+919000000049")
    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)
    res = RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=DAYTIME, dry_run=True)
    assert res["agent_run_id"] and res["status"] in ("EXECUTED", "NO_ACTION")
    step = db_session.scalar(select(RecoveryPlanStep).where(RecoveryPlanStep.recovery_plan_id == plan.id))
    assert step.action_type.startswith("AGENT:") and step.step_metadata["agent_run_id"] == res["agent_run_id"]


def test_anthropic_provider_parses_tool_use_and_plan(monkeypatch):
    """No network: inject a fake client returning SDK-shaped blocks."""
    class FakeMessages:
        def __init__(self):
            self.kwargs = None
        def create(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                stop_reason="tool_use", model="claude-opus-5",
                usage=SimpleNamespace(input_tokens=1200, output_tokens=80, cache_read_input_tokens=900, cache_creation_input_tokens=0),
                content=[
                    SimpleNamespace(type="text", text="Checking policy first."),
                    SimpleNamespace(type="tool_use", id="toolu_1", name="check_policy", input={"action": "SEND_WHATSAPP"}),
                ],
            )
    fake = SimpleNamespace(messages=FakeMessages())
    provider = AnthropicProvider(client=fake, model="claude-opus-5")
    turn = provider.plan({"case": {}}, AgentToolbox.specs(), [])
    assert turn.tool_calls[0].name == "check_policy" and turn.usage["input_tokens"] == 1200
    sent = fake.messages.kwargs
    assert sent["model"] == "claude-opus-5" and sent["thinking"] == {"type": "adaptive"}
    assert any(t["name"] == "submit_plan" for t in sent["tools"]) and sent["system"][0]["cache_control"]["type"] == "ephemeral"

    class PlanMessages:
        def create(self, **kwargs):
            return SimpleNamespace(stop_reason="end_turn", model="claude-opus-5", usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0),
                                   content=[SimpleNamespace(type="tool_use", id="toolu_2", name="submit_plan", input={"action": "WAIT", "rationale": "Issuer degradation is active; waiting is the fair and effective choice.", "confidence": 0.9, "wait_hours": 4})])
    turn2 = AnthropicProvider(client=SimpleNamespace(messages=PlanMessages()), model="claude-opus-5").plan({}, [], [])
    assert turn2.final_plan.action == "WAIT" and turn2.final_plan.wait_hours == 4

    class Failing:
        def create(self, **kwargs):
            raise RuntimeError("503 overloaded")
    with pytest.raises(ProviderUnavailable):
        AnthropicProvider(client=SimpleNamespace(messages=Failing()), model="claude-opus-5").plan({}, [], [])
    assert llm_breaker().is_open


def test_agent_api(client: TestClient, db_session: Session):
    customer, case = _open_case(db_session, phone="+919000000050")
    res = client.post(f"/agent/cases/{case.id}/run", json={"dry_run": True, "provider": "null", "reference_time": DAYTIME.isoformat()})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] in ("EXECUTED", "NO_ACTION") and body["trace"]
    assert client.get(f"/agent/runs/{body['id']}").json()["dossier"]["case"]["id"] == str(case.id)
    assert any(r["id"] == body["id"] for r in client.get("/agent/runs", params={"case_id": str(case.id)}).json())
    assert len(client.get("/agent/tools").json()) >= 12
    assert client.get("/approvals").status_code == 200
