"""Recovery outcome processing and correlation engine."""
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.recovery_action import RecoveryAction
from app.models.execution import RecoveryExecution
from app.models.outcome import RecoveryOutcome
from app.models.audit_log import AuditLog
from app.outcomes.base import OutcomeType, AttributionType
from app.outcomes.attribution import AttributionClassifier
from app.decision.service import DecisionService
from app.learning.service import LearningDataService

logger = logging.getLogger(__name__)


class OutcomeEngine:
    """Evaluates business outcomes, determines attribution, calculates recovery timing, and closes cases."""

    @classmethod
    def process_payment_capture(
        cls,
        db: Session,
        recovery_case: RecoveryCase,
        captured_amount: Decimal,
        captured_at: Optional[datetime] = None,
        provider_event_id: Optional[str] = None,
        provider_payment_id: Optional[str] = None,
    ) -> RecoveryOutcome:
        """Process an incoming verified payment capture event.

        Args:
            db: Database session.
            recovery_case: The associated open/in-progress RecoveryCase.
            captured_amount: Verified amount captured by the gateway.
            captured_at: Timestamp when capture occurred at gateway.
            provider_event_id: External gateway event identifier.

        Returns:
            The persisted RecoveryOutcome record.
        """
        now = captured_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        amount_at_risk = recovery_case.amount_at_risk or Decimal("0.00")
        if captured_amount < Decimal("0.00"):
            raise ValueError(f"Captured amount cannot be negative ({captured_amount}).")

        # 1. Calculate recovery percentage and outcome type
        if amount_at_risk > Decimal("0.00"):
            raw_pct = (float(captured_amount) / float(amount_at_risk)) * 100.0
            recovery_percentage = round(min(raw_pct, 100.0), 2)
        else:
            recovery_percentage = 100.0 if captured_amount > 0 else 0.0

        if captured_amount >= amount_at_risk and amount_at_risk > 0:
            outcome_type = OutcomeType.RECOVERED
        elif captured_amount > Decimal("0.00"):
            outcome_type = OutcomeType.PARTIALLY_RECOVERED
        else:
            outcome_type = OutcomeType.NOT_RECOVERED

        # 2. Find latest execution, intervention, and action
        latest_exec = db.scalar(
            select(RecoveryExecution)
            .where(RecoveryExecution.recovery_case_id == recovery_case.id)
            .order_by(RecoveryExecution.created_at.desc())
        )

        from app.models.intervention import Intervention
        latest_intervention = db.scalar(
            select(Intervention)
            .where(
                Intervention.recovery_case_id == recovery_case.id,
                Intervention.status.in_(["SENT", "SUCCEEDED", "EXECUTING"]),
            )
            .order_by(Intervention.created_at.desc())
        )

        latest_action = db.scalar(
            select(RecoveryAction)
            .where(RecoveryAction.recovery_case_id == recovery_case.id)
            .order_by(RecoveryAction.created_at.desc())
        )

        # 3. Determine attribution
        exec_time = None
        if latest_exec and latest_exec.status == "SUCCEEDED":
            attribution: AttributionType = AttributionClassifier.classify(
                execution=latest_exec,
                captured_at=now,
            )
            exec_time = latest_exec.completed_at or latest_exec.started_at
        elif latest_intervention:
            interv_time = latest_intervention.started_at
            if interv_time.tzinfo is None:
                interv_time = interv_time.replace(tzinfo=timezone.utc)
            elapsed = (now - interv_time).total_seconds()
            if elapsed <= 24 * 3600:
                attribution = AttributionType.DIRECT
            elif elapsed <= 72 * 3600:
                attribution = AttributionType.LIKELY
            else:
                attribution = AttributionType.ORGANIC
            exec_time = interv_time
        else:
            # Agent/scheduler outreach lands in `communications` (WhatsApp / email); count it the same way.
            from app.models.communication import Communication
            latest_comm = db.scalar(
                select(Communication)
                .where(
                    Communication.recovery_case_id == recovery_case.id,
                    Communication.status.in_(["SENT", "DELIVERED", "READ"]),
                    Communication.sent_at.isnot(None),
                )
                .order_by(Communication.sent_at.desc())
            )
            if latest_comm is not None:
                comm_time = latest_comm.sent_at if latest_comm.sent_at.tzinfo else latest_comm.sent_at.replace(tzinfo=timezone.utc)
                elapsed = (now - comm_time).total_seconds()
                if elapsed < 0:
                    attribution = AttributionType.ORGANIC
                elif elapsed <= 24 * 3600:
                    attribution = AttributionType.DIRECT
                elif elapsed <= 72 * 3600:
                    attribution = AttributionType.LIKELY
                else:
                    attribution = AttributionType.ORGANIC
                exec_time = comm_time if attribution != AttributionType.ORGANIC else None
            else:
                attribution = AttributionType.ORGANIC

        # 4. Calculate time to recovery (only if attributable)
        time_to_recovery_seconds: Optional[float] = None
        if attribution in [AttributionType.DIRECT, AttributionType.LIKELY] and exec_time:
            if exec_time.tzinfo is None:
                exec_time = exec_time.replace(tzinfo=timezone.utc)
            diff = (now - exec_time).total_seconds()
            time_to_recovery_seconds = round(max(diff, 0.0), 2)

        # 5. Create RecoveryOutcome record
        outcome = RecoveryOutcome(
            recovery_case_id=recovery_case.id,
            recovery_action_id=latest_action.id if latest_action else None,
            execution_id=latest_exec.id if latest_exec else None,
            outcome_type=outcome_type.value,
            attribution=attribution.value,
            amount_at_risk=amount_at_risk,
            amount_recovered=captured_amount,
            recovery_percentage=recovery_percentage,
            occurred_at=now,
            time_to_recovery_seconds=time_to_recovery_seconds,
            provider_event_id=provider_event_id,
            outcome_metadata={
                "captured_amount": float(captured_amount),
                "amount_at_risk": float(amount_at_risk),
                "recovery_percentage": recovery_percentage,
                "attribution": attribution.value,
                "time_to_recovery_seconds": time_to_recovery_seconds,
            },
        )
        db.add(outcome)
        db.flush()

        # 6. Update RecoveryCase state
        recovery_case.status = "RECOVERED"
        recovery_case.recovered_amount = captured_amount
        recovery_case.closed_at = now
        db.flush()

        # 6b. Ledger: recovered rupees keyed by the gateway payment id (used for settlement recon)
        from app.ledger.service import LedgerService
        LedgerService.post_capture(
            db, recovery_case, captured_amount,
            provider_reference=provider_payment_id or provider_event_id or f"capture_{outcome.id}",
            occurred_at=now,
            metadata={"attribution": attribution.value, "provider_event_id": provider_event_id, "outcome_id": str(outcome.id)},
        )

        # 7. Stopping Rule: Cancel all pending/planned actions, executions, active interventions, and queued communications
        from app.services.intervention_service import InterventionService
        from app.services.communication_orchestrator import CommunicationOrchestrator
        from app.services.recovery_scheduler import RecoveryScheduler
        from app.models.promise_to_pay import PromiseToPay

        # Fulfill any active Promise-to-Pay commitments
        active_promises = db.scalars(
            select(PromiseToPay).where(
                PromiseToPay.recovery_case_id == recovery_case.id,
                PromiseToPay.status.in_(["ACTIVE", "PARTIAL"]),
            )
        ).all()
        for prom in active_promises:
            prom.status = "FULFILLED"
            prom.fulfilled_at = now

        RecoveryScheduler.stop_plan_on_recovery(
            db=db,
            case_id=recovery_case.id,
            reason="PAYMENT_CAPTURED",
        )
        InterventionService.stop_intervention_on_recovery(
            db=db,
            recovery_case=recovery_case,
            captured_amount=captured_amount,
            captured_at=now,
            provider_payment_id=provider_event_id,
        )
        CommunicationOrchestrator.stop_whatsapp_on_recovery(
            db=db,
            recovery_case=recovery_case,
        )

        DecisionService.cancel_pending_actions(
            db=db,
            recovery_case=recovery_case,
            cancellation_reason="Payment captured; case successfully recovered.",
        )

        pending_execs = db.scalars(
            select(RecoveryExecution).where(
                RecoveryExecution.recovery_case_id == recovery_case.id,
                RecoveryExecution.status.in_(["PENDING", "APPROVED"]),
            )
        ).all()
        for ex in pending_execs:
            ex.status = "CANCELLED"

        db.flush()

        # 8. Finalize Learning Dataset Example & Resolve Closed-Loop Attribution
        from app.services.recovery_outcome_resolver import RecoveryOutcomeResolver
        RecoveryOutcomeResolver.resolve_outcome(
            db=db,
            case=recovery_case,
            outcome_status="RECOVERED",
            amount_recovered=captured_amount,
        )

        LearningDataService.finalize_learning_example(
            db=db,
            recovery_case=recovery_case,
            outcome=outcome,
        )

        # 9. Audit Logging
        db.add(
            AuditLog(
                recovery_case_id=recovery_case.id,
                actor_type="SYSTEM",
                actor_id="outcome_engine_v1",
                action="OUTCOME_CREATED",
                entity_type="RecoveryOutcome",
                entity_id=str(outcome.id),
                audit_metadata={
                    "outcome_type": outcome_type.value,
                    "attribution": attribution.value,
                    "amount_recovered": float(captured_amount),
                    "recovery_percentage": recovery_percentage,
                    "time_to_recovery_seconds": time_to_recovery_seconds,
                },
            )
        )
        db.add(
            AuditLog(
                recovery_case_id=recovery_case.id,
                actor_type="SYSTEM",
                actor_id="outcome_engine_v1",
                action="RECOVERY_CASE_RECOVERED",
                entity_type="RecoveryCase",
                entity_id=str(recovery_case.id),
                audit_metadata={
                    "recovered_amount": float(captured_amount),
                    "status": "RECOVERED",
                },
            )
        )
        db.flush()

        logger.info(
            f"[OUTCOME_RECORDED] Case {recovery_case.id} outcome={outcome_type.value} "
            f"recovered=₹{captured_amount} ({recovery_percentage}%) attribution={attribution.value}"
        )
        return outcome
