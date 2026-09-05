"""Single place where a RecoveryCase is born, regardless of surface.

Opening a case always means: persist it, audit it, assign an experiment arm, post the AT_RISK
ledger entry, diagnose, recommend the first action, snapshot a learning example and create the
bounded recovery plan. Detectors call this instead of re-implementing the sequence.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.decision.service import DecisionService
from app.diagnosis.service import DiagnosisService
from app.domain.surfaces import LeakSurface, profile_for
from app.experiments.assigner import ExperimentAssigner
from app.learning.service import LearningDataService
from app.ledger.service import LedgerService
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.event import Event
from app.models.invoice import Invoice
from app.models.payment import Payment
from app.models.recovery_case import RecoveryCase
from app.models.subscription import Subscription
from app.schemas.event import NormalizedEvent

logger = logging.getLogger(__name__)


class CaseFactory:
    """Opens recovery cases with the full v2 bootstrap sequence."""

    @classmethod
    def open_case(
        cls,
        db: Session,
        *,
        customer: Customer,
        db_event: Event,
        surface: LeakSurface,
        amount: Decimal,
        currency: str,
        diagnosis_event: NormalizedEvent,
        case_type: Optional[str] = None,
        payment: Optional[Payment] = None,
        subscription: Optional[Subscription] = None,
        invoice: Optional[Invoice] = None,
        batch_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        actor_id: str = "leak_detector",
        ledger_reference: Optional[str] = None,
    ) -> RecoveryCase:
        profile = profile_for(surface.value)

        # Replayed events carry their own timeline; production events use the database clock.
        created_at_override = None
        if (diagnosis_event.metadata or {}).get("replay") and diagnosis_event.occurred_at:
            created_at_override = diagnosis_event.occurred_at

        case = RecoveryCase(
            customer_id=customer.id,
            event_id=db_event.id,
            payment_id=payment.id if payment else None,
            subscription_id=subscription.id if subscription else None,
            invoice_id=invoice.id if invoice else None,
            amount_at_risk=Decimal(str(amount)).quantize(Decimal("0.01")),
            currency=(currency or "INR").upper(),
            case_type=case_type or surface.value,
            status="OPEN",
            leak_surface=surface.value,
            batch_id=batch_id,
            case_metadata=dict(metadata or {}),
        )
        if created_at_override is not None:
            case.created_at = created_at_override
            case.updated_at = created_at_override
        db.add(case)
        db.flush()

        db.add(
            AuditLog(
                recovery_case_id=case.id,
                actor_type="SYSTEM",
                actor_id=actor_id,
                action="RECOVERY_CASE_OPENED",
                entity_type="RecoveryCase",
                entity_id=str(case.id),
                audit_metadata={
                    "event_id": db_event.external_event_id,
                    "leak_surface": surface.value,
                    "amount_at_risk": str(case.amount_at_risk),
                    "currency": case.currency,
                    "failure_code": diagnosis_event.failure_code,
                    "failure_description": diagnosis_event.failure_description,
                    **({"surface_metadata": metadata} if metadata else {}),
                },
            )
        )
        db.flush()

        ExperimentAssigner.assign(db, case, surface=surface.value)
        LedgerService.post_at_risk(db, case, provider_reference=ledger_reference or db_event.external_event_id)

        diagnosis = DiagnosisService.diagnose_case(db=db, recovery_case=case, event=diagnosis_event)
        action = DecisionService.generate_recommendation(db=db, recovery_case=case, diagnosis=diagnosis)
        if action:
            LearningDataService.create_initial_example(db=db, recovery_case=case, action=action, diagnosis=diagnosis)

        from app.services.recovery_scheduler import RecoveryScheduler
        RecoveryScheduler.create_or_get_plan(db=db, case_id=case.id, max_steps=profile.max_touches)

        # Outbox: the first plan evaluation is a job committed with the case itself.
        from app.jobs.queue import JobQueue
        JobQueue.enqueue(db, "plan.evaluate", {"case_id": str(case.id), "surface": surface.value}, idempotency_key=f"plan.evaluate:first:{case.id}")

        from app.observability.metrics import metrics
        metrics.case_opened(surface.value, case.experiment_arm)
        logger.info(f"[CASE_OPENED] surface={surface.value} case={case.id} amount={case.amount_at_risk} arm={case.experiment_arm}")
        return case

    @staticmethod
    def synthetic_event(
        db: Session,
        *,
        customer: Customer,
        event_type: str,
        entity_id: str,
        payload: Dict[str, Any],
        occurred_at: Optional[datetime] = None,
        subscription: Optional[Subscription] = None,
        invoice: Optional[Invoice] = None,
    ) -> Event:
        """Internal (non-webhook) event row for detector sweeps; RecoveryCase.event_id is NOT NULL."""
        now = occurred_at or datetime.now(timezone.utc)
        evt = Event(
            external_event_id=f"rs_{event_type}_{entity_id}_{int(now.timestamp())}",
            event_type=event_type,
            source="REVENUESHIELD",
            customer_id=customer.id,
            subscription_id=subscription.id if subscription else None,
            invoice_id=invoice.id if invoice else None,
            payload=payload,
            occurred_at=now,
            processing_status="PROCESSED",
            processed_at=datetime.now(timezone.utc),
        )
        db.add(evt)
        db.flush()
        return evt
