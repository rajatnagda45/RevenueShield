"""The agent's toolbox: thin, typed, policy-gated wrappers over the deterministic services.

Read tools return facts. Mutating tools are gated three times before anything happens:
1. holdout arm / human handoff freeze,
2. PolicyEngine (case status, PTP, caps, windows, surface profile, degradation hold),
3. the channel service itself (which applies ContactPolicy and its own idempotency).
A blocked call is returned to the planner as an error result with the blocking rule, never raised.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.schemas import ACTION_TO_POLICY_TYPE, ToolCall, ToolResult, ToolSpec
from app.agent.validators import NegotiationEnvelope
from app.compliance.handoff import HandoffService
from app.decision.base import ActionType, DecisionContext
from app.decision.policy import PolicyEngine
from app.degradation.monitor import DegradationMonitor
from app.experiments.assigner import ExperimentAssigner
from app.ledger.service import LedgerService
from app.models.audit_log import AuditLog
from app.models.promise_to_pay import PromiseToPay
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan

logger = logging.getLogger(__name__)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class AgentToolbox:
    """Tools bound to one case, one session and one clock."""

    def __init__(self, db: Session, case: RecoveryCase, *, dry_run: bool = True, now: Optional[datetime] = None, actor: str = "recovery_agent_v1"):
        self.db = db
        self.case = case
        self.dry_run = dry_run
        self.now = now or datetime.now(timezone.utc)
        self.actor = actor
        self._handlers: Dict[str, Callable[..., Dict[str, Any]]] = {
            "get_case_dossier": self.get_case_dossier,
            "get_customer_history": self.get_customer_history,
            "check_policy": self.check_policy,
            "draft_message_template": self.draft_message_template,
            "send_whatsapp": self.send_whatsapp,
            "send_email": self.send_email,
            "send_payment_link": self.send_payment_link,
            "offer_payment_plan": self.offer_payment_plan,
            "start_voice_call": self.start_voice_call,
            "schedule_retry": self.schedule_retry,
            "record_promise_to_pay": self.record_promise_to_pay,
            "wait": self.wait,
            "handoff_to_human": self.handoff_to_human,
            "escalate_to_merchant": self.escalate_to_merchant,
            "close_case": self.close_case,
        }

    # ------------------------------------------------------------------ specs

    @staticmethod
    def specs() -> List[ToolSpec]:
        obj = {"type": "object", "properties": {}, "additionalProperties": False}
        return [
            ToolSpec(name="get_case_dossier", description="Return the full case dossier: surface, outstanding amount, diagnosis, customer consent and history, previous steps, precomputed policy verdicts per action.", input_schema=obj),
            ToolSpec(name="get_customer_history", description="Return the customer's payment history features and cross-channel touches in the last 30 days.", input_schema=obj),
            ToolSpec(name="check_policy", description="Dry-run the policy engine and contact policy for one agent action; returns allowed, blocking rule and reason.", input_schema={"type": "object", "properties": {"action": {"type": "string", "description": "An agent action name, e.g. SEND_WHATSAPP"}}, "required": ["action"], "additionalProperties": False}),
            ToolSpec(name="draft_message_template", description="Return the merchant-approved template text for a channel and language, to personalise. Keep {payment_link} where the link goes.", input_schema={"type": "object", "properties": {"channel": {"type": "string", "enum": ["WHATSAPP", "EMAIL"]}, "language": {"type": "string", "enum": ["ENGLISH", "HINGLISH"]}}, "required": ["channel"], "additionalProperties": False}),
            ToolSpec(name="send_whatsapp", description="Send a WhatsApp payment reminder with a secure payment link. Optional personalised body (must keep {payment_link} and an opt-out line).", input_schema={"type": "object", "properties": {"body": {"type": "string"}, "language": {"type": "string", "enum": ["ENGLISH", "HINGLISH"]}}, "additionalProperties": False}, mutating=True),
            ToolSpec(name="send_email", description="Send a payment recovery email with a secure payment link. Optional personalised plain-text body (must keep {payment_link}).", input_schema={"type": "object", "properties": {"body": {"type": "string"}}, "additionalProperties": False}, mutating=True),
            ToolSpec(name="send_payment_link", description="Create a Razorpay payment link for the outstanding amount and notify the customer on their best channel.", input_schema=obj, mutating=True),
            ToolSpec(name="offer_payment_plan", description="Offer instalments inside the negotiation envelope: creates a partial-payment link with the first instalment as the minimum.", input_schema={"type": "object", "properties": {"installments": {"type": "integer", "minimum": 1, "maximum": 12}, "first_payment_pct": {"type": "number", "minimum": 1, "maximum": 100}, "due_days": {"type": "integer", "minimum": 1, "maximum": 90}, "waiver_pct": {"type": "number", "minimum": 0, "maximum": 100}}, "required": ["installments", "first_payment_pct"], "additionalProperties": False}, mutating=True),
            ToolSpec(name="start_voice_call", description="Place a recovery call through the voice agent (RBI window 08:00-19:00, 1 call / 3 days).", input_schema=obj, mutating=True),
            ToolSpec(name="schedule_retry", description="Schedule the next automated re-evaluation / gateway retry after N hours (mandates and bank failures).", input_schema={"type": "object", "properties": {"hours": {"type": "integer", "minimum": 1, "maximum": 720}, "reason": {"type": "string"}}, "required": ["hours"], "additionalProperties": False}, mutating=True),
            ToolSpec(name="record_promise_to_pay", description="Record a customer's promise to pay on a date; pauses outreach until then.", input_schema={"type": "object", "properties": {"promised_date": {"type": "string", "description": "YYYY-MM-DD"}, "amount": {"type": "number"}, "note": {"type": "string"}}, "required": ["promised_date"], "additionalProperties": False}, mutating=True),
            ToolSpec(name="wait", description="Do nothing now and re-evaluate after N hours.", input_schema={"type": "object", "properties": {"hours": {"type": "integer", "minimum": 1, "maximum": 336}, "reason": {"type": "string"}}, "required": ["hours"], "additionalProperties": False}, mutating=True),
            ToolSpec(name="handoff_to_human", description="Freeze automation and hand the case to a human (disputes, distress, identity doubt, anything sensitive).", input_schema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"], "additionalProperties": False}, mutating=True),
            ToolSpec(name="escalate_to_merchant", description="Escalate a B2B receivable to the merchant's finance team (always requires human approval).", input_schema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"], "additionalProperties": False}, mutating=True),
            ToolSpec(name="close_case", description="Close the case as not recoverable (requires approval above a small amount).", input_schema={"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"], "additionalProperties": False}, mutating=True),
        ]

    # ------------------------------------------------------------------ dispatch

    def execute(self, call: ToolCall) -> ToolResult:
        handler = self._handlers.get(call.name)
        if handler is None:
            return ToolResult(tool_call_id=call.id, name=call.name, ok=False, error=f"unknown tool '{call.name}'")
        try:
            result = handler(**(call.arguments or {}))
        except TypeError as exc:
            return ToolResult(tool_call_id=call.id, name=call.name, ok=False, error=f"bad arguments: {exc}")
        except ValueError as exc:
            return ToolResult(tool_call_id=call.id, name=call.name, ok=False, error=str(exc))
        blocked_rule = result.get("blocking_rule") if isinstance(result, dict) else None
        ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
        return ToolResult(tool_call_id=call.id, name=call.name, ok=ok and not blocked_rule, result=result, blocked_rule=blocked_rule)

    # ------------------------------------------------------------------ gating

    def _gate(self, agent_action: str) -> Optional[Dict[str, Any]]:
        """Return a blocking payload if the action may not run now."""
        case = self.case
        if case.status in ("RECOVERED", "CLOSED"):
            return {"ok": False, "blocking_rule": "CASE_ALREADY_RECOVERED_OR_CLOSED", "reason": f"case is {case.status}"}
        if ExperimentAssigner.is_holdout(case) and agent_action not in ("WAIT", "NO_ACTION"):
            return {"ok": False, "blocking_rule": "HOLDOUT_ARM_OBSERVE_ONLY", "reason": "holdout arm: observe only"}
        if HandoffService.is_frozen(case) and agent_action not in ("WAIT", "NO_ACTION", "HANDOFF_TO_HUMAN"):
            return {"ok": False, "blocking_rule": "HUMAN_HANDOFF_FREEZE", "reason": "case is frozen for a human"}
        ptype = ACTION_TO_POLICY_TYPE.get(agent_action, "NO_ACTION")
        customer = case.customer
        plan = self.db.scalar(select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case.id))
        prev = [s.action_type for s in (plan.steps if plan else []) if s.action_type in {t.value for t in ActionType}]
        ctx = DecisionContext(
            case_id=str(case.id), case_type=case.case_type, amount_at_risk=case.amount_at_risk or Decimal("0"), currency=case.currency or "INR",
            case_age_hours=0.0, retry_count=case.retry_count or 0, diagnosis_category="UNKNOWN", diagnosis_confidence=0.8,
            risk_score=case.risk_score or 50.0, recovery_probability=case.recovery_probability or 0.5,
            customer_phone_available=bool(customer and customer.phone), customer_email_available=bool(customer and customer.email),
            promise_to_pay_active=bool(self.db.scalar(select(PromiseToPay).where(PromiseToPay.recovery_case_id == case.id, PromiseToPay.status == "ACTIVE"))),
            current_time=self.now, previous_action_types=prev,
            metadata={"experiment_arm": case.experiment_arm, "leak_surface": case.leak_surface, "timezone": getattr(customer, "timezone", None), "systemic_incident_active": DegradationMonitor.case_is_held(self.db, case)},
        )
        res = PolicyEngine.evaluate(action_type=ActionType(ptype), context=ctx, case_status=case.status)
        if not res.allowed:
            return {"ok": False, "blocking_rule": res.blocking_rule, "reason": res.reason}
        return None

    def _audit(self, action: str, metadata: Dict[str, Any]) -> None:
        self.db.add(AuditLog(recovery_case_id=self.case.id, actor_type="AGENT", actor_id=self.actor, action=action, entity_type="RecoveryCase", entity_id=str(self.case.id), audit_metadata=metadata))
        self.db.flush()

    # ------------------------------------------------------------------ read tools

    def get_case_dossier(self) -> Dict[str, Any]:
        from app.agent.dossier import DossierBuilder
        return DossierBuilder.build(self.db, self.case, now=self.now)

    def get_customer_history(self) -> Dict[str, Any]:
        from app.compliance.contact_policy import ContactPolicy
        from app.services.customer_intelligence import CustomerIntelligenceService
        feats = CustomerIntelligenceService.get_customer_features(self.db, self.case.customer_id, reference_time=self.now)
        return {"features": feats.to_dict(), "touches_last_30d": ContactPolicy.touches(self.db, self.case.customer_id, days=30)}

    def check_policy(self, action: str) -> Dict[str, Any]:
        action = (action or "").upper()
        if action not in ACTION_TO_POLICY_TYPE:
            return {"ok": False, "allowed": False, "reason": f"unknown action '{action}'"}
        gate = self._gate(action)
        if gate:
            return {"ok": True, "allowed": False, "blocking_rule_preview": gate["blocking_rule"], "reason": gate["reason"]}
        from app.agent.schemas import ACTION_TO_CHANNEL
        from app.compliance.contact_policy import ContactPolicy
        channel = ACTION_TO_CHANNEL.get(action)
        if channel and self.case.customer:
            c = ContactPolicy.evaluate(self.db, case=self.case, customer=self.case.customer, channel=channel, now=self.now, record=False)
            if not c.allowed:
                return {"ok": True, "allowed": False, "blocking_rule_preview": c.rule, "reason": c.reason, "next_eligible_at": c.next_eligible_at.isoformat() if c.next_eligible_at else None}
        return {"ok": True, "allowed": True, "reason": "permitted"}

    def draft_message_template(self, channel: str = "WHATSAPP", language: str = "ENGLISH") -> Dict[str, Any]:
        from app.services.recovery_message_generator import RecoveryMessageGenerator
        draft = RecoveryMessageGenerator.generate(self.case, payment_link_url="{payment_link}", language=language)
        body = draft.message_body
        if channel.upper() in ("WHATSAPP", "SMS"):
            body += " Reply STOP to opt out."
        return {"ok": True, "channel": channel.upper(), "language": draft.language, "template": draft.template_name, "body": body}

    # ------------------------------------------------------------------ mutating tools

    def send_whatsapp(self, body: Optional[str] = None, language: str = "ENGLISH") -> Dict[str, Any]:
        gate = self._gate("SEND_WHATSAPP")
        if gate:
            return gate
        from app.services.whatsapp_recovery_service import WhatsAppRecoveryService
        res = WhatsAppRecoveryService.execute_recovery(self.db, self.case.id, language=language, dry_run=self.dry_run, reference_time=self.now, message_override=body)
        return {"ok": res.status == "SENT", "status": res.status, "blocking_rule": res.policy_blocking_rule, "reason": res.reason, "communication": res.communication, "payment_link": res.payment_link}

    def send_email(self, body: Optional[str] = None) -> Dict[str, Any]:
        gate = self._gate("SEND_EMAIL")
        if gate:
            return gate
        from app.services.email_recovery_service import EmailRecoveryService
        res = EmailRecoveryService.execute_recovery(self.db, str(self.case.id), body_override=body, dry_run=self.dry_run, reference_time=self.now)
        return {"ok": bool(res.get("success")), "status": res.get("status"), "blocking_rule": res.get("blocking_rule"), "reason": res.get("reason") or res.get("error"), "communication_id": res.get("communication_id")}

    def send_payment_link(self) -> Dict[str, Any]:
        gate = self._gate("SEND_PAYMENT_LINK")
        if gate:
            return gate
        from app.services.intervention_service import InterventionService
        res = InterventionService.execute_intervention(self.db, self.case.id, action_override="SEND_PAYMENT_LINK", dry_run=self.dry_run, reference_time=self.now)
        return {"ok": res.status == "SENT", "status": res.status, "reason": res.reason, "blocking_rule": None if res.status == "SENT" else (res.reason or "BLOCKED").split(":")[0][:60], "payment_link": res.payment_link.__dict__ if res.payment_link else None}

    def offer_payment_plan(self, installments: int, first_payment_pct: float, due_days: int = 7, waiver_pct: float = 0.0) -> Dict[str, Any]:
        from app.agent.schemas import PaymentPlanOffer
        offer = PaymentPlanOffer(installments=installments, first_payment_pct=first_payment_pct, due_days=due_days, waiver_pct=waiver_pct)
        outstanding = Decimal(str(LedgerService.case_balance(self.db, self.case.id)["outstanding"] or self.case.amount_at_risk or 0))
        env = NegotiationEnvelope.validate(offer, outstanding_amount=outstanding)
        if not env.ok:
            return {"ok": False, "blocking_rule": "NEGOTIATION_ENVELOPE", "reason": "; ".join(env.errors)}
        gate = self._gate("OFFER_PAYMENT_PLAN")
        if gate:
            return gate
        first_amount = NegotiationEnvelope.first_amount(offer, outstanding)
        from app.services.intervention_service import InterventionService
        res = InterventionService.execute_intervention(
            self.db, self.case.id, action_override="SEND_PAYMENT_LINK", dry_run=self.dry_run,
            accept_partial=True, first_min_partial_amount=first_amount, reference_time=self.now,
        )
        if res.status == "SENT":
            meta = dict(self.case.case_metadata or {})
            meta["payment_plan"] = {"installments": installments, "first_payment_pct": first_payment_pct, "first_amount": float(first_amount), "due_days": due_days, "waiver_pct": waiver_pct, "offered_at": self.now.isoformat(), "payment_link_id": res.payment_link.razorpay_payment_link_id if res.payment_link else None}
            self.case.case_metadata = meta
            self._audit("PAYMENT_PLAN_OFFERED", meta["payment_plan"])
        return {"ok": res.status == "SENT", "status": res.status, "reason": res.reason, "first_amount": float(first_amount), "payment_link": res.payment_link.__dict__ if res.payment_link else None}

    def start_voice_call(self) -> Dict[str, Any]:
        gate = self._gate("START_VOICE_CALL")
        if gate:
            return gate
        from app.services.voice_recovery_service import VoiceRecoveryService
        try:
            res = VoiceRecoveryService.start_recovery_call(self.db, self.case.id, dry_run=self.dry_run, reference_time=self.now)
            return {"ok": True, **res}
        except ValueError as exc:
            msg = str(exc)
            return {"ok": False, "blocking_rule": "VOICE_ELIGIBILITY", "reason": msg}

    def schedule_retry(self, hours: int, reason: str = "") -> Dict[str, Any]:
        gate = self._gate("SCHEDULE_RETRY")
        if gate:
            return gate
        return self._defer(hours, reason or "agent scheduled retry", action="RETRY_SCHEDULED")

    def wait(self, hours: int, reason: str = "") -> Dict[str, Any]:
        return self._defer(hours, reason or "agent chose to wait", action="WAIT_SCHEDULED")

    def _defer(self, hours: int, reason: str, action: str) -> Dict[str, Any]:
        hours = int(max(1, min(hours, 24 * 30)))
        plan = self.db.scalar(select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == self.case.id))
        next_at = self.now + timedelta(hours=hours)
        if plan and plan.status not in ("COMPLETED", "RECOVERED", "CANCELLED", "EXPIRED"):
            plan.status = "WAITING"
            plan.next_evaluation_at = next_at
        self._audit(action, {"hours": hours, "reason": reason, "next_evaluation_at": next_at.isoformat()})
        return {"ok": True, "next_evaluation_at": next_at.isoformat(), "reason": reason}

    def record_promise_to_pay(self, promised_date: str, amount: Optional[float] = None, note: Optional[str] = None) -> Dict[str, Any]:
        from app.services.promise_to_pay_service import PromiseToPayService
        outstanding = Decimal(str(LedgerService.case_balance(self.db, self.case.id)["outstanding"] or self.case.amount_at_risk or 0))
        try:
            when = datetime.fromisoformat(promised_date)
        except ValueError:
            return {"ok": False, "reason": f"invalid date '{promised_date}'"}
        if when.tzinfo is None:
            when = when.replace(hour=17, tzinfo=timezone.utc)
        try:
            promise = PromiseToPayService.create_promise(self.db, self.case.id, promised_amount=Decimal(str(amount)) if amount else outstanding, promised_date=when, source="AGENT", notes=note)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": True, "promise_id": str(promise.id), "promised_date": _aware(promise.promised_date).isoformat(), "amount": float(promise.promised_amount)}

    def handoff_to_human(self, reason: str) -> Dict[str, Any]:
        HandoffService.freeze(self.db, case=self.case, reason=reason, source="AGENT")
        return {"ok": True, "human_handoff": True, "reason": reason}

    def escalate_to_merchant(self, reason: str) -> Dict[str, Any]:
        gate = self._gate("ESCALATE_TO_MERCHANT")
        if gate:
            return gate
        meta = dict(self.case.case_metadata or {})
        meta["merchant_escalation"] = {"reason": reason, "at": self.now.isoformat(), "by": self.actor}
        self.case.case_metadata = meta
        self._audit("ESCALATED_TO_MERCHANT", {"reason": reason})
        return {"ok": True, "escalated": True}

    def close_case(self, reason: str) -> Dict[str, Any]:
        if self.case.status in ("RECOVERED", "CLOSED"):
            return {"ok": False, "blocking_rule": "CASE_ALREADY_RECOVERED_OR_CLOSED", "reason": f"case is {self.case.status}"}
        from app.services.recovery_scheduler import RecoveryScheduler
        self.case.status = "CLOSED"
        self.case.closed_at = self.now
        LedgerService.post_write_off(self.db, self.case, reason=reason)
        RecoveryScheduler.stop_plan_on_recovery(self.db, self.case.id, reason=f"CLOSED_BY_AGENT: {reason}")
        self._audit("RECOVERY_CASE_CLOSED", {"reason": reason})
        return {"ok": True, "status": "CLOSED", "reason": reason}
