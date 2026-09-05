"""Deterministic planner used when no LLM is available (tests, offline demo, outage fallback).

It reproduces what the rule/ML Next-Best-Action engine would do, expressed through the same tool
interface and plan schema as the LLM, so the runner, gating, approvals and traces are identical.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from app.agent.schemas import AgentPlan, ProviderTurn, ToolCall, ToolSpec
from app.core.config import settings

NBA_TO_AGENT = {
    "EMAIL_PAYMENT_RECOVERY": "SEND_EMAIL",
    "EMAIL_FOLLOWUP": "SEND_EMAIL",
    "EMAIL": "SEND_EMAIL",
    "WHATSAPP_PAYMENT_RECOVERY": "SEND_WHATSAPP",
    "WHATSAPP": "SEND_WHATSAPP",
    "SEND_PAYMENT_LINK": "SEND_PAYMENT_LINK",
    "PAYMENT_LINK_RETRY": "SEND_PAYMENT_LINK",
    "VOICE_RECOVERY": "START_VOICE_CALL",
    "VOICE": "START_VOICE_CALL",
    "VOICE_OUTREACH": "START_VOICE_CALL",
    "PAYMENT_RETRY": "SCHEDULE_RETRY",
    "RETRY_PAYMENT": "SCHEDULE_RETRY",
    "B2B_RECEIVABLES_ESCALATION": "ESCALATE_TO_MERCHANT",
    "ESCALATE": "ESCALATE_TO_MERCHANT",
    "WAIT": "WAIT",
    "NO_ACTION": "NO_ACTION",
}

FALLBACK_ORDER = ["SEND_WHATSAPP", "SEND_EMAIL", "SEND_PAYMENT_LINK", "START_VOICE_CALL", "SCHEDULE_RETRY", "WAIT", "NO_ACTION"]


class NullLLMProvider:
    name = "null"
    model = "rules:next_best_action_v1"

    def plan(self, dossier: Dict[str, Any], tools: List[ToolSpec], transcript: List[Dict[str, Any]]) -> ProviderTurn:
        turn_index = sum(1 for t in transcript if t.get("role") == "assistant")
        case = dossier.get("case", {})
        verdicts = {v["action"]: v for v in dossier.get("constraints", {}).get("action_verdicts", [])}
        allowed = set(dossier.get("constraints", {}).get("allowed_actions", []))

        # Turn 0: demonstrate a bounded tool call - confirm the policy for the recommended action.
        rec = dossier.get("recommendation", {}) or {}
        candidate = NBA_TO_AGENT.get(str(rec.get("action_type") or "NO_ACTION"), "NO_ACTION")
        if turn_index == 0:
            return ProviderTurn(tool_calls=[ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name="check_policy", arguments={"action": candidate})], raw_text=f"Checking policy for recommended action {candidate}.")

        # Turn 1: decide.
        amount = float(case.get("outstanding_amount") or case.get("amount_at_risk") or 0)
        meta = case.get("metadata") or {}
        rationale_bits: List[str] = []

        if case.get("human_handoff"):
            return self._final("NO_ACTION", "Case is frozen for human handling; the agent takes no automated action until an operator releases it.", 0.99)
        if case.get("systemic_hold"):
            return self._final("WAIT", "An issuer/PSP degradation incident is active for this bank and method. The customer is not at fault; retrying or messaging now would waste a touch. Re-evaluate when the incident clears.", 0.9, wait_hours=max(1, settings.DEGRADATION_RECHECK_MINUTES // 60 or 1))
        if case.get("active_promise_to_pay"):
            return self._final("WAIT", "An active promise-to-pay exists; outreach stays paused until the promised date.", 0.95, wait_hours=24)

        surface = case.get("surface")
        if surface == "SUBSCRIPTION_MANDATE_FAILURE" and meta.get("halted"):
            candidate = "SEND_PAYMENT_LINK"
            rationale_bits.append("The mandate is halted, so gateway retries cannot succeed; the customer needs a fresh authorisation link.")
        if surface == "RECEIVABLE_OVERDUE" and meta.get("ageing_bucket") in ("31-60", "60+") and amount >= 100000 and case.get("touches_used", 0) >= 2:
            candidate = "ESCALATE_TO_MERCHANT"
            rationale_bits.append("Large receivable aged beyond 30 days with reminders already sent: escalate to the merchant's finance team for a direct conversation.")

        chosen = candidate if candidate in allowed else next((a for a in FALLBACK_ORDER if a in allowed), "NO_ACTION")
        if chosen != candidate:
            v = verdicts.get(candidate, {})
            rationale_bits.append(f"Recommended {candidate} is blocked ({v.get('rule')}); falling back to {chosen}.")
        rationale_bits.append(f"Rule/ML engine recommends {rec.get('action_type')} with expected value Rs {rec.get('expected_recovery_value', 0):,.2f} (p={rec.get('expected_recovery_probability', 0):.2f}) for diagnosis {dossier.get('diagnosis', {}).get('category')}.")

        requires_approval = False
        approval_reason: Optional[str] = None
        if chosen == "START_VOICE_CALL" and amount >= settings.APPROVAL_VOICE_AMOUNT_THRESHOLD:
            requires_approval, approval_reason = True, f"voice call on a Rs {amount:,.0f} case exceeds the approval threshold"
        if chosen == "ESCALATE_TO_MERCHANT":
            requires_approval, approval_reason = True, "merchant escalation always needs a human decision"

        wait_hours = None
        retry_hours = None
        if chosen == "WAIT":
            wait_hours = int(case.get("surface_profile", {}).get("reevaluation_hours", 24))
        if chosen == "SCHEDULE_RETRY":
            retry_hours = 24
        return self._final(chosen, " ".join(rationale_bits), 0.8 if chosen == candidate else 0.65, wait_hours=wait_hours, retry_after_hours=retry_hours, requires_human_approval=requires_approval, approval_reason=approval_reason)

    @staticmethod
    def _final(action: str, rationale: str, confidence: float, **kwargs) -> ProviderTurn:
        return ProviderTurn(final_plan=AgentPlan(action=action, rationale=rationale, confidence=confidence, **kwargs), raw_text="deterministic plan")
