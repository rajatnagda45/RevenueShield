"""Typed contracts between the planner (LLM or rules), the toolbox and the runner."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

AgentAction = Literal[
    "SEND_WHATSAPP",
    "SEND_EMAIL",
    "SEND_PAYMENT_LINK",
    "OFFER_PAYMENT_PLAN",
    "START_VOICE_CALL",
    "SCHEDULE_RETRY",
    "RECORD_PROMISE_TO_PAY",
    "WAIT",
    "HANDOFF_TO_HUMAN",
    "ESCALATE_TO_MERCHANT",
    "CLOSE_CASE",
    "NO_ACTION",
]

AGENT_ACTIONS: List[str] = list(AgentAction.__args__)  # type: ignore[attr-defined]

# Agent action -> PolicyEngine ActionType used for gating.
ACTION_TO_POLICY_TYPE: Dict[str, str] = {
    "SEND_WHATSAPP": "SEND_WHATSAPP_REMINDER",
    "SEND_EMAIL": "SEND_PAYMENT_LINK",
    "SEND_PAYMENT_LINK": "SEND_PAYMENT_LINK",
    "OFFER_PAYMENT_PLAN": "SEND_PAYMENT_LINK",
    "START_VOICE_CALL": "VOICE_OUTREACH",
    "SCHEDULE_RETRY": "RETRY_PAYMENT",
    "RECORD_PROMISE_TO_PAY": "NO_ACTION",
    "WAIT": "WAIT",
    "HANDOFF_TO_HUMAN": "NO_ACTION",
    "ESCALATE_TO_MERCHANT": "ESCALATE",
    "CLOSE_CASE": "NO_ACTION",
    "NO_ACTION": "NO_ACTION",
}

# Agent action -> ContactPolicy channel (None = no customer contact).
ACTION_TO_CHANNEL: Dict[str, Optional[str]] = {
    "SEND_WHATSAPP": "WHATSAPP",
    "SEND_EMAIL": "EMAIL",
    "SEND_PAYMENT_LINK": "WHATSAPP",
    "OFFER_PAYMENT_PLAN": "WHATSAPP",
    "START_VOICE_CALL": "VOICE",
}

MUTATING_ACTIONS = {
    "SEND_WHATSAPP", "SEND_EMAIL", "SEND_PAYMENT_LINK", "OFFER_PAYMENT_PLAN", "START_VOICE_CALL",
    "SCHEDULE_RETRY", "RECORD_PROMISE_TO_PAY", "HANDOFF_TO_HUMAN", "ESCALATE_TO_MERCHANT", "CLOSE_CASE", "WAIT",
}


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: Dict[str, Any]
    mutating: bool = False


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    tool_call_id: str
    name: str
    ok: bool
    result: Dict[str, Any] = Field(default_factory=dict)
    blocked_rule: Optional[str] = None
    error: Optional[str] = None


class MessageDraft(BaseModel):
    channel: Literal["WHATSAPP", "EMAIL", "SMS"] = "WHATSAPP"
    language: Literal["ENGLISH", "HINGLISH", "HINDI"] = "ENGLISH"
    tone: Literal["warm", "neutral", "firm"] = "warm"
    body: str = Field(..., description="Customer-facing text. Use {payment_link} where the secure link should appear.")


class PaymentPlanOffer(BaseModel):
    installments: int = Field(..., ge=1, le=12)
    first_payment_pct: float = Field(..., ge=1, le=100, description="Percent of the outstanding amount due in the first instalment")
    due_days: int = Field(7, ge=1, le=90, description="Days until the first instalment is due")
    waiver_pct: float = Field(0.0, ge=0, le=100, description="Goodwill waiver on the outstanding amount, percent")


class PromiseToPayDraft(BaseModel):
    promised_date: date
    amount: Optional[float] = Field(None, description="Rupees; defaults to the full outstanding amount")
    note: Optional[str] = None


class AgentPlan(BaseModel):
    """The planner's final, structured decision for one evaluation of one case."""

    action: AgentAction
    rationale: str = Field(..., description="Two or three sentences a compliance officer could read")
    confidence: float = Field(..., ge=0, le=1)
    message: Optional[MessageDraft] = None
    payment_plan: Optional[PaymentPlanOffer] = None
    promise: Optional[PromiseToPayDraft] = None
    wait_hours: Optional[int] = Field(None, ge=1, le=24 * 14)
    retry_after_hours: Optional[int] = Field(None, ge=1, le=24 * 30)
    handoff_reason: Optional[str] = None
    requires_human_approval: bool = False
    approval_reason: Optional[str] = None


class ProviderTurn(BaseModel):
    """One planner step: either tool calls to make, or the final plan."""

    tool_calls: List[ToolCall] = Field(default_factory=list)
    final_plan: Optional[AgentPlan] = None
    raw_text: Optional[str] = None
    usage: Dict[str, Any] = Field(default_factory=dict)


class ProviderUnavailable(Exception):
    """Raised by a provider when the model cannot be reached (timeout, 429, 5xx, refusal)."""
