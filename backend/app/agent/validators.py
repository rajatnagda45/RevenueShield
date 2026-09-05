"""Deterministic validators applied to everything the planner produces.

The LLM may write the words; these rules decide whether the words may leave the building.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from app.agent.schemas import AgentPlan, MessageDraft, PaymentPlanOffer
from app.core.config import settings

# Internal vocabulary that must never reach a customer.
INTERNAL_TOKENS = [
    "bad_request_error", "gateway_error", "authentication_failure", "insufficient_funds", "error_code",
    "risk score", "risk_score", "probability", "prediction", "model_version", "recovery_case", "case_id",
    "diagnosis", "holdout", "treatment arm", "experiment", "expected recovery", "erv", "ml model", "algorithm",
    "razorpay_payment_link_id", "plink_", "pay_", "uuid",
]

# RBI Fair Practices Code: no intimidation, harassment or misrepresentation by recovery agents.
INTIMIDATION_TOKENS = [
    "arrest", "police", "jail", "prison", "criminal", "court", "legal action", "lawsuit", "sue you", "cibil",
    "blacklist", "credit score will", "consequences", "warning", "final notice", "immediately or", "last chance",
    "we will visit", "your employer", "your family", "shame", "fraudster", "cheat",
]

# Phrases that promise something policy cannot guarantee.
FORBIDDEN_PROMISES = ["guarantee", "guaranteed", "no charges ever", "free forever", "we will refund everything"]

OPT_OUT_FOOTERS = ["reply stop", "reply 'stop'", "reply \"stop\"", "stop to opt out", "stop to unsubscribe", "unsubscribe"]

MAX_LENGTH = {"WHATSAPP": 700, "SMS": 320, "EMAIL": 2500}

AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)


@dataclass
class ValidationResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self):
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


class MessageValidator:
    """Checks a customer-facing draft against amount, disclosure, tone and formatting rules."""

    @staticmethod
    def _amounts_in(text: str) -> List[Decimal]:
        out = []
        for m in AMOUNT_RE.finditer(text):
            try:
                out.append(Decimal(m.group(1).replace(",", "")))
            except Exception:
                continue
        return out

    @classmethod
    def validate(
        cls,
        draft: MessageDraft,
        *,
        outstanding_amount: Decimal,
        first_name: Optional[str] = None,
        require_link: bool = True,
        allowed_amounts: Optional[List[Decimal]] = None,
    ) -> ValidationResult:
        errors: List[str] = []
        warnings: List[str] = []
        body = draft.body or ""
        lower = body.lower()

        if not body.strip():
            errors.append("message body is empty")
            return ValidationResult(False, errors, warnings)

        limit = MAX_LENGTH.get(draft.channel, 700)
        if len(body) > limit:
            errors.append(f"message is {len(body)} characters; limit for {draft.channel} is {limit}")

        for tok in INTERNAL_TOKENS:
            if tok in lower:
                errors.append(f"internal vocabulary must not reach the customer: '{tok}'")
        for tok in INTIMIDATION_TOKENS:
            if tok in lower:
                errors.append(f"intimidating or threatening language is prohibited (RBI Fair Practices Code): '{tok}'")
        for tok in FORBIDDEN_PROMISES:
            if tok in lower:
                errors.append(f"message promises something policy cannot guarantee: '{tok}'")

        if require_link and "{payment_link}" not in body and "http" not in lower:
            errors.append("message must include the secure payment link placeholder {payment_link}")

        if draft.channel in ("WHATSAPP", "SMS") and not any(f in lower for f in OPT_OUT_FOOTERS):
            errors.append("WhatsApp/SMS messages must carry an opt-out instruction (e.g. 'Reply STOP to opt out')")

        amounts = cls._amounts_in(body)
        permitted = set(Decimal(str(a)).quantize(Decimal("0.01")) for a in (allowed_amounts or [])) | {Decimal(str(outstanding_amount)).quantize(Decimal("0.01"))}
        for amt in amounts:
            if amt.quantize(Decimal("0.01")) not in permitted:
                errors.append(f"amount ₹{amt:,.2f} does not match the outstanding amount ₹{Decimal(str(outstanding_amount)):,.2f} or an approved instalment")
        if not amounts:
            warnings.append("message does not state the amount; the template will include it")

        if first_name and first_name.lower() not in lower:
            warnings.append("message does not use the customer's first name")

        if re.search(r"[A-Z]{6,}", body):
            warnings.append("avoid shouting (long all-caps runs)")

        return ValidationResult(not errors, errors, warnings)


class NegotiationEnvelope:
    """Merchant-configured limits on what the agent may offer without a human."""

    @classmethod
    def validate(cls, offer: PaymentPlanOffer, *, outstanding_amount: Decimal) -> ValidationResult:
        errors: List[str] = []
        warnings: List[str] = []
        if offer.installments > settings.NEGOTIATION_MAX_INSTALLMENTS:
            errors.append(f"{offer.installments} instalments exceeds the envelope maximum of {settings.NEGOTIATION_MAX_INSTALLMENTS}")
        if offer.first_payment_pct < settings.NEGOTIATION_MIN_FIRST_PAYMENT_PCT:
            errors.append(f"first payment {offer.first_payment_pct:.0f}% is below the envelope minimum of {settings.NEGOTIATION_MIN_FIRST_PAYMENT_PCT:.0f}%")
        if offer.due_days > settings.NEGOTIATION_MAX_EXTENSION_DAYS:
            errors.append(f"{offer.due_days} days extension exceeds the envelope maximum of {settings.NEGOTIATION_MAX_EXTENSION_DAYS}")
        if offer.waiver_pct > settings.NEGOTIATION_MAX_WAIVER_PCT:
            errors.append(f"waiver {offer.waiver_pct:.1f}% exceeds the envelope maximum of {settings.NEGOTIATION_MAX_WAIVER_PCT:.1f}% (requires human approval)")
        first_amount = (Decimal(str(outstanding_amount)) * Decimal(str(offer.first_payment_pct)) / Decimal("100")).quantize(Decimal("0.01"))
        if first_amount < Decimal("1.00"):
            errors.append("first instalment would be below ₹1.00")
        return ValidationResult(not errors, errors, warnings)

    @staticmethod
    def first_amount(offer: PaymentPlanOffer, outstanding_amount: Decimal) -> Decimal:
        return (Decimal(str(outstanding_amount)) * Decimal(str(offer.first_payment_pct)) / Decimal("100")).quantize(Decimal("0.01"))


class PlanValidator:
    """Structural checks on the final plan before anything executes."""

    @classmethod
    def validate(cls, plan: AgentPlan, *, outstanding_amount: Decimal, first_name: Optional[str] = None) -> ValidationResult:
        errors: List[str] = []
        warnings: List[str] = []
        if plan.action in ("SEND_WHATSAPP", "SEND_EMAIL") and plan.message is not None:
            res = MessageValidator.validate(plan.message, outstanding_amount=outstanding_amount, first_name=first_name)
            errors += [f"message: {e}" for e in res.errors]
            warnings += res.warnings
        if plan.action == "OFFER_PAYMENT_PLAN":
            if plan.payment_plan is None:
                errors.append("OFFER_PAYMENT_PLAN requires payment_plan")
            else:
                res = NegotiationEnvelope.validate(plan.payment_plan, outstanding_amount=outstanding_amount)
                errors += [f"payment_plan: {e}" for e in res.errors]
                if plan.message is not None:
                    first = NegotiationEnvelope.first_amount(plan.payment_plan, outstanding_amount)
                    mres = MessageValidator.validate(plan.message, outstanding_amount=outstanding_amount, first_name=first_name, allowed_amounts=[first])
                    errors += [f"message: {e}" for e in mres.errors]
        if plan.action == "RECORD_PROMISE_TO_PAY" and plan.promise is None:
            errors.append("RECORD_PROMISE_TO_PAY requires promise")
        if plan.action == "HANDOFF_TO_HUMAN" and not (plan.handoff_reason or "").strip():
            errors.append("HANDOFF_TO_HUMAN requires handoff_reason")
        if plan.action == "WAIT" and not plan.wait_hours:
            warnings.append("WAIT without wait_hours; defaulting to the surface re-evaluation interval")
        if len((plan.rationale or "").strip()) < 20:
            errors.append("rationale is too short to be auditable")
        return ValidationResult(not errors, errors, warnings)
