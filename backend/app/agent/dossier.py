"""Case dossier: everything the planner is allowed to know, with the constraints precomputed.

Precomputing policy verdicts per action keeps the planner honest and cheap: it sees which moves
are already blocked (and why) before it spends a token proposing them.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.schemas import ACTION_TO_CHANNEL, ACTION_TO_POLICY_TYPE, AGENT_ACTIONS
from app.compliance.consent import ConsentService
from app.compliance.contact_policy import ContactPolicy
from app.compliance.handoff import HandoffService
from app.core.config import settings
from app.decision.base import ActionType, DecisionContext
from app.decision.policy import PolicyEngine
from app.degradation.monitor import DegradationMonitor
from app.domain.surfaces import profile_for
from app.experiments.assigner import ExperimentAssigner
from app.ledger.service import LedgerService
from app.models.diagnosis import Diagnosis
from app.models.promise_to_pay import PromiseToPay
from app.models.recovery_case import RecoveryCase
from app.models.recovery_payment_link import RecoveryPaymentLink
from app.models.recovery_plan import RecoveryPlan
from app.services.customer_intelligence import CustomerIntelligenceService
from app.services.notification_service import mask_contact


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class DossierBuilder:
    @classmethod
    def build(cls, db: Session, case: RecoveryCase, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        customer = case.customer
        profile = profile_for(case.leak_surface)
        balance = LedgerService.case_balance(db, case.id)
        outstanding = Decimal(str(balance["outstanding"])) if balance["outstanding"] else Decimal(str(case.amount_at_risk or 0))

        diag = db.scalar(select(Diagnosis).where(Diagnosis.recovery_case_id == case.id).order_by(Diagnosis.created_at.desc()))
        plan = db.scalar(select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case.id))
        active_ptp = db.scalar(select(PromiseToPay).where(PromiseToPay.recovery_case_id == case.id, PromiseToPay.status == "ACTIVE"))
        link = db.scalar(select(RecoveryPaymentLink).where(RecoveryPaymentLink.recovery_case_id == case.id, RecoveryPaymentLink.status.in_(["CREATED", "SENT"])).order_by(RecoveryPaymentLink.created_at.desc()))
        features = CustomerIntelligenceService.get_customer_features(db, case.customer_id, reference_time=now) if customer else None

        created = _aware(case.created_at) or now
        age_hours = round(max((now - created).total_seconds() / 3600.0, 0.0), 1)
        previous_actions = [s.action_type for s in (plan.steps if plan else [])]

        # Rule/ML recommendation (the deterministic baseline the planner may agree with or override)
        recommendation = cls._recommendation(db, case, now)

        # Precomputed verdicts per agent action
        verdicts = cls._verdicts(db, case, customer, previous_actions, now)

        dossier: Dict[str, Any] = {
            "as_of": now.isoformat(),
            "case": {
                "id": str(case.id),
                "surface": case.leak_surface or case.case_type,
                "surface_profile": {
                    "max_touches": profile.max_touches, "voice_allowed": profile.voice_allowed, "retry_allowed": profile.retry_allowed,
                    "escalation_allowed": profile.escalation_allowed, "reevaluation_hours": profile.reevaluation_hours, "description": profile.description,
                },
                "status": case.status,
                "currency": case.currency,
                "amount_at_risk": float(case.amount_at_risk or 0),
                "outstanding_amount": float(outstanding),
                "recovered_so_far": balance["recovered_net_of_refunds"],
                "age_hours": age_hours,
                "experiment_arm": case.experiment_arm,
                "touches_used": len([a for a in previous_actions if a.replace("AGENT:", "") not in ("OBSERVE", "WAIT", "NO_ACTION", "HANDOFF_TO_HUMAN", "CLOSE_CASE")]),
                "previous_steps": previous_actions,
                "metadata": {k: v for k, v in (case.case_metadata or {}).items() if k not in ("systemic_tagged_at",)},
                "systemic_hold": DegradationMonitor.case_is_held(db, case),
                "human_handoff": HandoffService.is_frozen(case),
                "active_payment_link": bool(link),
                "active_promise_to_pay": {
                    "promised_date": _aware(active_ptp.promised_date).isoformat(), "amount": float(active_ptp.promised_amount)
                } if active_ptp else None,
            },
            "customer": {
                "first_name": (customer.name or "").split()[0] if customer and customer.name else None,
                "segment": customer.segment if customer else None,
                "preferred_language": customer.preferred_language if customer else "ENGLISH",
                "preferred_channel": customer.preferred_channel if customer else None,
                "timezone": customer.timezone if customer else settings.DEFAULT_TIMEZONE,
                "phone_masked": mask_contact(customer.phone) if customer and customer.phone else None,
                "email_masked": mask_contact(customer.email) if customer and customer.email else None,
                "consent": ConsentService.summary(db, customer.id)["effective"] if customer else {},
                "touches_last_30d": ContactPolicy.touches(db, customer.id, days=30)[:10] if customer else [],
                "history": features.to_dict() if features else None,
            },
            "diagnosis": {
                "category": diag.category if diag else "UNKNOWN",
                "explanation": diag.explanation if diag else None,
                "confidence": diag.confidence if diag else None,
                "recovery_probability": case.recovery_probability,
                "risk_score": case.risk_score,
                "evidence": {k: (diag.evidence or {}).get(k) for k in ("payment_method", "bank", "error_reason", "error_source")} if diag else {},
            },
            "recommendation": recommendation,
            "constraints": {
                "negotiation_envelope": {
                    "max_installments": settings.NEGOTIATION_MAX_INSTALLMENTS,
                    "min_first_payment_pct": settings.NEGOTIATION_MIN_FIRST_PAYMENT_PCT,
                    "max_extension_days": settings.NEGOTIATION_MAX_EXTENSION_DAYS,
                    "max_waiver_pct_without_approval": settings.NEGOTIATION_MAX_WAIVER_PCT,
                },
                "approval_thresholds": {
                    "voice_call_amount": settings.APPROVAL_VOICE_AMOUNT_THRESHOLD,
                    "close_case_amount": settings.APPROVAL_CLOSE_CASE_AMOUNT_THRESHOLD,
                    "always": ["ESCALATE_TO_MERCHANT", "waiver > envelope"],
                },
                "action_verdicts": verdicts,
                "allowed_actions": [v["action"] for v in verdicts if v["allowed"]],
            },
        }
        dossier["hash"] = hashlib.sha256(json.dumps(dossier, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return dossier

    @classmethod
    def _recommendation(cls, db: Session, case: RecoveryCase, now: datetime) -> Dict[str, Any]:
        try:
            from app.services.next_best_action_engine import NextBestActionEngine
            nba = NextBestActionEngine.compute_next_best_action(db=db, case=case, reference_time=now)
            return {
                "engine": "next_best_action",
                "action_type": nba.action_type, "channel": nba.channel,
                "expected_recovery_probability": round(float(nba.expected_recovery_probability), 4),
                "expected_recovery_value": float(nba.expected_recovery_value),
                "reason": nba.reason, "confidence": round(float(nba.confidence), 4),
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {"engine": "unavailable", "error": str(exc)}

    @classmethod
    def _verdicts(cls, db: Session, case: RecoveryCase, customer, previous_actions: List[str], now: datetime) -> List[Dict[str, Any]]:
        ctx = DecisionContext(
            case_id=str(case.id), case_type=case.case_type, amount_at_risk=case.amount_at_risk or Decimal("0"), currency=case.currency or "INR",
            case_age_hours=0.0, retry_count=case.retry_count or 0, diagnosis_category="UNKNOWN", diagnosis_confidence=0.8,
            risk_score=case.risk_score or 50.0, recovery_probability=case.recovery_probability or 0.5,
            customer_phone_available=bool(customer and customer.phone), customer_email_available=bool(customer and customer.email),
            promise_to_pay_active=bool(db.scalar(select(PromiseToPay).where(PromiseToPay.recovery_case_id == case.id, PromiseToPay.status == "ACTIVE"))),
            current_time=now, previous_action_types=[a for a in previous_actions if a in {t.value for t in ActionType}],
            metadata={
                "experiment_arm": case.experiment_arm, "leak_surface": case.leak_surface,
                "timezone": getattr(customer, "timezone", None), "systemic_incident_active": DegradationMonitor.case_is_held(db, case),
            },
        )
        out: List[Dict[str, Any]] = []
        for action in AGENT_ACTIONS:
            allowed, rule, reason = True, None, "permitted"
            if ExperimentAssigner.is_holdout(case) and action not in ("WAIT", "NO_ACTION"):
                allowed, rule, reason = False, "HOLDOUT_ARM_OBSERVE_ONLY", "holdout arm: observe only"
            elif HandoffService.is_frozen(case) and action not in ("NO_ACTION", "WAIT"):
                allowed, rule, reason = False, "HUMAN_HANDOFF_FREEZE", "case is with a human"
            else:
                ptype = ACTION_TO_POLICY_TYPE.get(action, "NO_ACTION")
                pres = PolicyEngine.evaluate(action_type=ActionType(ptype), context=ctx, case_status=case.status)
                if not pres.allowed:
                    allowed, rule, reason = False, pres.blocking_rule, pres.reason
                else:
                    channel = ACTION_TO_CHANNEL.get(action)
                    if channel and customer is not None:
                        cres = ContactPolicy.evaluate(db, case=case, customer=customer, channel=channel, now=now, record=False)
                        if not cres.allowed:
                            allowed, rule, reason = False, cres.rule, cres.reason
                    if action == "START_VOICE_CALL" and not (customer and customer.phone):
                        allowed, rule, reason = False, "CUSTOMER_PHONE_MISSING", "no phone number"
                    if action == "SEND_EMAIL" and not (customer and customer.email and "@" in customer.email):
                        allowed, rule, reason = False, "CUSTOMER_EMAIL_MISSING", "no email address"
            out.append({"action": action, "allowed": allowed, "rule": rule, "reason": reason})
        return out
