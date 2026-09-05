"""Recovery Decision orchestration service."""
from datetime import datetime, timezone
import logging
from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.diagnosis import Diagnosis
from app.models.recovery_action import RecoveryAction
from app.models.promise_to_pay import PromiseToPay
from app.models.audit_log import AuditLog
from app.decision.base import (
    ActionStatus,
    DecisionContext,
    RecommendationResult,
    RecoveryDecisionEngine,
)
from app.decision.engine import RuleBasedRecoveryDecisionEngine
from app.decision.policy import PolicyEngine, PolicyEvaluationResult
from app.degradation.monitor import DegradationMonitor

logger = logging.getLogger(__name__)


class DecisionService:
    """Orchestrates next best action recommendation, policy evaluation, deduplication, and persistence."""

    _engine: RecoveryDecisionEngine = RuleBasedRecoveryDecisionEngine()

    @classmethod
    def set_engine(cls, engine: RecoveryDecisionEngine) -> None:
        """Swap the active decision engine (e.g. for ML models or testing)."""
        cls._engine = engine

    @classmethod
    def generate_recommendation(
        cls,
        db: Session,
        recovery_case: RecoveryCase,
        diagnosis: Optional[Diagnosis] = None,
        evaluation_time: Optional[datetime] = None,
    ) -> RecoveryAction:
        """Evaluate open recovery case and record approved/blocked next best action.

        Args:
            db: Database session.
            recovery_case: Open RecoveryCase instance.
            diagnosis: Associated Diagnosis record (fetched if not provided).
            evaluation_time: Optional evaluation timestamp (defaults to current UTC time).

        Returns:
            The persisted RecoveryAction record.
        """
        from app.services.customer_intelligence import CustomerIntelligenceService, _to_utc_aware

        now = _to_utc_aware(evaluation_time) or datetime.now(timezone.utc)
        case_created_at = _to_utc_aware(recovery_case.created_at) or now
        case_age_hours = max(0.0, (now - case_created_at).total_seconds() / 3600.0)

        # 1. Fetch latest Diagnosis if not passed
        if not diagnosis:
            diagnosis = db.scalar(
                select(Diagnosis)
                .where(Diagnosis.recovery_case_id == recovery_case.id)
                .order_by(Diagnosis.created_at.desc())
            )

        category = diagnosis.category if diagnosis else "UNKNOWN"
        confidence = diagnosis.confidence if diagnosis else 0.30
        risk_score = recovery_case.risk_score or 50.0
        recovery_prob = recovery_case.recovery_probability or 0.50

        # 2. Fetch Customer features and channel contact info
        customer = recovery_case.customer
        customer_phone = bool(customer and customer.phone)
        customer_email = bool(customer and customer.email)

        customer_features = CustomerIntelligenceService.get_customer_features(
            db=db,
            customer_id=recovery_case.customer_id,
            reference_time=now,
        )

        # 3. Check for active Promise-to-Pay agreements
        active_ptp = db.scalar(
            select(PromiseToPay).where(
                PromiseToPay.customer_id == recovery_case.customer_id,
                PromiseToPay.status == "ACTIVE",
            )
        )
        promise_to_pay_active = active_ptp is not None

        # 4. Fetch prior recovery actions for this case
        prior_actions: List[RecoveryAction] = db.scalars(
            select(RecoveryAction)
            .where(RecoveryAction.recovery_case_id == recovery_case.id)
            .order_by(RecoveryAction.created_at.asc())
        ).all()

        previous_action_types = [a.action_type for a in prior_actions]
        previous_outcomes = []
        for a in prior_actions:
            for out in a.outcomes:
                previous_outcomes.append(out.outcome_status)

        # Check existing active recommendations (Idempotency check)
        existing_active = [
            a for a in prior_actions if a.status in [ActionStatus.RECOMMENDED, ActionStatus.APPROVED]
        ]
        if existing_active:
            latest_active = existing_active[-1]
            logger.info(
                f"[DECISION_SKIPPED] Case {recovery_case.id} already has active recommendation "
                f"id={latest_active.id} type={latest_active.action_type}. Returning existing."
            )
            return latest_active

        # 5. Build DecisionContext
        context = DecisionContext(
            case_id=str(recovery_case.id),
            case_type=recovery_case.case_type,
            amount_at_risk=recovery_case.amount_at_risk,
            currency=recovery_case.currency,
            case_age_hours=round(case_age_hours, 2),
            retry_count=recovery_case.retry_count or 0,
            diagnosis_category=category,
            diagnosis_confidence=confidence,
            risk_score=risk_score,
            recovery_probability=recovery_prob,
            customer_features=customer_features,
            customer_phone_available=customer_phone,
            customer_email_available=customer_email,
            promise_to_pay_active=promise_to_pay_active,
            current_time=now,
            previous_action_types=previous_action_types,
            previous_action_outcomes=previous_outcomes,
            metadata={
                "experiment_arm": recovery_case.experiment_arm,
                "timezone": getattr(customer, "timezone", None),
                "leak_surface": recovery_case.leak_surface,
                "systemic_incident_active": DegradationMonitor.case_is_held(db, recovery_case),
            },
        )

        # 6. Execute Recovery Decision Engine & Predictive Recovery Service
        try:
            from app.ml.prediction_service import PredictionService
            predictions_res = PredictionService.predict_for_case(
                db=db,
                recovery_case=recovery_case,
                save_predictions=True,
            )
            # If an ML model is active and produced predictions, incorporate the highest Expected Value action
            if predictions_res.strategy == "ML" and predictions_res.predictions:
                top_pred = predictions_res.predictions[0]
                rec_result = cls._engine.recommend(context)
                # If the heuristic engine returned a different action, provide the ML top action with expected value context
                rec_result.supporting_factors.append(
                    f"Predictive ML Model ({predictions_res.model_version}) highest Expected Value action: {top_pred.action} (₹{top_pred.expected_recovered_value:.2f}, P={top_pred.probability:.2f})."
                )
            else:
                rec_result: RecommendationResult = cls._engine.recommend(context)
        except Exception as exc:
            logger.warning(f"Predictive scoring skipped or failed ({exc}); using decision engine baseline.")
            rec_result: RecommendationResult = cls._engine.recommend(context)

        # 7. Evaluate Policy / Compliance Engine
        active_interventions_count = len(existing_active)
        policy_result: PolicyEvaluationResult = PolicyEngine.evaluate(
            action_type=rec_result.recommended_action,
            context=context,
            case_status=recovery_case.status,
            active_interventions_count=active_interventions_count,
        )

        # Action status: APPROVED if policy allows, BLOCKED if rejected by safety rule
        final_status = ActionStatus.APPROVED if policy_result.allowed else ActionStatus.BLOCKED

        # 8. Persist RecoveryAction record
        recovery_action = RecoveryAction(
            recovery_case_id=recovery_case.id,
            action_type=rec_result.recommended_action,
            channel=rec_result.channel,
            status=final_status.value,
            reason=rec_result.reason,
            confidence=rec_result.confidence,
            decision_score=rec_result.score,
            decision_confidence=rec_result.confidence,
            decision_engine_version=rec_result.decision_engine_version,
            policy_engine_version=PolicyEngine.version,
            policy_result=policy_result.to_dict(),
            alternatives=rec_result.alternatives,
            supporting_factors=rec_result.supporting_factors,
        )
        db.add(recovery_action)
        db.flush()

        # 9. Record immutable AuditLog
        audit = AuditLog(
            recovery_case_id=recovery_case.id,
            actor_type="SYSTEM",
            actor_id=rec_result.decision_engine_version,
            action="RECOVERY_ACTION_RECOMMENDED",
            entity_type="RecoveryCase",
            entity_id=str(recovery_case.id),
            audit_metadata={
                "action_id": str(recovery_action.id),
                "recommended_action": rec_result.recommended_action,
                "channel": rec_result.channel,
                "status": final_status.value,
                "decision_score": rec_result.score,
                "decision_confidence": rec_result.confidence,
                "decision_engine_version": rec_result.decision_engine_version,
                "policy_engine_version": PolicyEngine.version,
                "policy_allowed": policy_result.allowed,
                "policy_blocking_rule": policy_result.blocking_rule,
                "policy_reason": policy_result.reason,
                "reason": rec_result.reason,
            },
        )
        db.add(audit)
        db.flush()

        logger.info(
            f"[DECISION_RECORDED] Case {recovery_case.id} action={rec_result.recommended_action} "
            f"status={final_status.value} allowed={policy_result.allowed}"
        )

        return recovery_action

    @classmethod
    def cancel_pending_actions(
        cls,
        db: Session,
        recovery_case: RecoveryCase,
        cancellation_reason: str = "Payment successfully captured; case recovered.",
    ) -> int:
        """Cancel and invalidate all pending/approved recovery actions upon case recovery (Stopping Rule).

        Args:
            db: Database session.
            recovery_case: The recovered RecoveryCase.
            cancellation_reason: Reason for cancelling the actions.

        Returns:
            Number of actions cancelled.
        """
        pending_actions: List[RecoveryAction] = db.scalars(
            select(RecoveryAction).where(
                RecoveryAction.recovery_case_id == recovery_case.id,
                RecoveryAction.status.in_([ActionStatus.RECOMMENDED.value, ActionStatus.APPROVED.value, "PLANNED"]),
            )
        ).all()

        cancelled_count = 0
        for action in pending_actions:
            action.status = ActionStatus.CANCELLED.value
            action.reason = cancellation_reason
            cancelled_count += 1

            audit = AuditLog(
                recovery_case_id=recovery_case.id,
                actor_type="SYSTEM",
                actor_id="policy_engine_v1",
                action="RECOVERY_ACTION_CANCELLED",
                entity_type="RecoveryAction",
                entity_id=str(action.id),
                audit_metadata={
                    "cancelled_action_type": action.action_type,
                    "reason": cancellation_reason,
                },
            )
            db.add(audit)

        if cancelled_count > 0:
            db.flush()
            logger.info(
                f"[DECISION_CANCELLED] Cancelled {cancelled_count} pending actions for Case {recovery_case.id}"
            )

        return cancelled_count
