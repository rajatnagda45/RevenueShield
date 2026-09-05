"""Recovery Outcome Resolver resolving terminal case states into attributed learning examples."""
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.diagnosis import Diagnosis
from app.models.learning import LearningExample
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep
from app.services.recovery_attribution_engine import RecoveryAttributionEngine

logger = logging.getLogger(__name__)

VALID_OUTCOMES = {
    "RECOVERED",
    "NOT_RECOVERED",
    "EXPIRED",
    "BLOCKED",
    "DELIVERY_FAILED",
    "CUSTOMER_DECLINED",
    "PROMISE_TO_PAY",
    "UNKNOWN",
}


class RecoveryOutcomeResolver:
    """Resolves real-world payment and communication outcomes into validated learning records."""

    @classmethod
    def resolve_outcome(
        cls,
        db: Session,
        case: RecoveryCase,
        outcome_status: str,
        amount_recovered: Optional[Decimal] = None,
        is_manual_override: bool = False,
        environment_type: str = "TEST",
    ) -> LearningExample:
        """Resolve case outcome, invoke credit attribution, compute prediction error, and persist learning example."""
        outcome = outcome_status if outcome_status in VALID_OUTCOMES else "UNKNOWN"
        now = datetime.now(timezone.utc)
        recovered_amt = amount_recovered or (case.recovered_amount if case.recovered_amount else Decimal("0.00"))

        # 1. Attribute Recovery Credit if RECOVERED
        attribution_record = None
        if outcome == "RECOVERED":
            attribution_record = RecoveryAttributionEngine.attribute_recovery(
                db=db,
                recovery_case_id=case.id,
                amount_recovered=recovered_amt,
                captured_at=now,
            )

        # 2. Extract Associated Plan & Latest Step
        plan = case.recovery_plan or db.scalar(
            select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case.id)
        )
        plan_id = plan.id if plan else None
        latest_step: Optional[RecoveryPlanStep] = None
        if plan and plan.steps:
            completed_steps = sorted(plan.steps, key=lambda s: s.step_number, reverse=True)
            latest_step = completed_steps[0] if completed_steps else None

        step_id = latest_step.id if latest_step else None
        action_type = latest_step.action_type if latest_step else "EMAIL_PAYMENT_RECOVERY"
        channel = latest_step.channel if latest_step else "EMAIL"
        pred_score = float(latest_step.prediction_score) if latest_step and latest_step.prediction_score is not None else 0.50
        ev = Decimal(str(latest_step.expected_recovery_value)) if latest_step and latest_step.expected_recovery_value is not None else (case.amount_at_risk or Decimal("0.00")) * Decimal(str(pred_score))

        # 3. Label & Prediction Error Calculation
        label = 1 if outcome == "RECOVERED" else 0
        pred_error = round(float(label) - float(pred_score), 4)

        # 4. Training Eligibility Evaluation
        training_eligible = True
        exclusion_reason = None
        if is_manual_override:
            training_eligible = False
            exclusion_reason = "MANUAL_OPERATOR_OVERRIDE"
        elif outcome == "UNKNOWN":
            training_eligible = False
            exclusion_reason = "OUTCOME_UNKNOWN"

        # 5. Build and Persist LearningExample
        diag = db.scalar(
            select(Diagnosis).where(Diagnosis.recovery_case_id == case.id).order_by(Diagnosis.created_at.desc())
        )

        learning_ex = LearningExample(
            recovery_case_id=case.id,
            recovery_plan_id=plan_id,
            recovery_step_id=step_id,
            channel=channel,
            model_version="calibrated_v1",
            feature_version="v1",
            diagnosis_category=diag.category if diag and diag.category else "UNKNOWN",
            diagnosis_confidence=float(diag.confidence) if diag and diag.confidence is not None else 0.50,
            risk_score=float(diag.risk_score) if diag and diag.risk_score is not None else 50.0,
            recovery_probability=pred_score,
            amount_at_risk=case.amount_at_risk or Decimal("0.00"),
            case_age_at_decision_hours=0.5,
            customer_success_rate_at_decision=0.75,
            customer_failure_count_at_decision=1,
            previous_recovery_attempts=case.retry_count or 0,
            payment_method=case.payment.payment_method if case.payment and case.payment.payment_method else "CARD",
            bank=case.payment.bank if case.payment and case.payment.bank else "UNKNOWN",
            action_type=action_type,
            decision_score=0.75,
            decision_confidence=0.85,
            policy_allowed=True,
            feature_snapshot={"action_type": action_type, "channel": channel},
            outcome_type=outcome,
            amount_recovered=recovered_amt if outcome == "RECOVERED" else Decimal("0.00"),
            recovery_percentage=100.0 if outcome == "RECOVERED" else 0.0,
            attribution=attribution_record.attribution_type if attribution_record else "NONE",
            label=label,
            is_finalized=True,
            finalized_at=now,
            expected_recovery_value=ev,
            prediction_error=pred_error,
            training_eligible=training_eligible,
            training_exclusion_reason=exclusion_reason,
            environment_type=environment_type,
        )
        db.add(learning_ex)

        # 6. Audit Event
        audit = AuditLog(
            actor_type="SYSTEM",
            actor_id="OUTCOME_RESOLVER",
            action="OUTCOME_RESOLVED",
            entity_type="RECOVERY_CASE",
            entity_id=str(case.id),
            audit_metadata={
                "outcome": outcome,
                "amount_recovered": float(recovered_amt),
                "prediction_error": pred_error,
                "training_eligible": training_eligible,
                "attribution": attribution_record.attribution_type if attribution_record else "NONE",
            },
        )
        db.add(audit)
        db.commit()
        db.refresh(learning_ex)

        logger.info(
            f"[OUTCOME_RESOLVED] Case={case.id} Outcome={outcome} Error={pred_error} Eligible={training_eligible}"
        )
        return learning_ex
