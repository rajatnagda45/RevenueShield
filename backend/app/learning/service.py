"""Learning data service for managing point-in-time training examples and anti-leakage validation."""
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.recovery_action import RecoveryAction
from app.models.diagnosis import Diagnosis
from app.models.outcome import RecoveryOutcome
from app.models.learning import LearningExample
from app.models.audit_log import AuditLog

logger = logging.getLogger(__name__)


class DataQualityError(Exception):
    """Exception raised when a learning example fails quality or anti-leakage checks."""
    pass


class LearningDataService:
    """Manages the creation, snapshotting, anti-leakage validation, and finalization of learning dataset examples."""

    @classmethod
    def create_feature_snapshot(
        cls,
        recovery_case: RecoveryCase,
        action: RecoveryAction,
        diagnosis: Optional[Diagnosis] = None,
    ) -> Dict[str, Any]:
        """Construct an immutable snapshot of features strictly known prior to action execution.

        GUARANTEE: No future outcome variables (e.g. amount_recovered, capture_time) are included.
        """
        customer = recovery_case.customer
        evidence = diagnosis.evidence if diagnosis and diagnosis.evidence else {}
        history = evidence.get("customer_history", {})

        snapshot = {
            "case_id": str(recovery_case.id),
            "amount_at_risk": float(recovery_case.amount_at_risk or Decimal("0.00")),
            "currency": recovery_case.currency,
            "case_type": recovery_case.case_type,
            "retry_count": recovery_case.retry_count or 0,
            "diagnosis_category": diagnosis.category if diagnosis else "UNKNOWN",
            "diagnosis_confidence": float(diagnosis.confidence) if diagnosis else 0.8,
            "risk_score": float(recovery_case.risk_score or (diagnosis.risk_score if diagnosis else 50.0) or 50.0),
            "recovery_probability": float(recovery_case.recovery_probability or (diagnosis.recovery_probability if diagnosis else 0.5) or 0.5),
            "payment_method": evidence.get("payment_method", "UNKNOWN"),
            "bank": evidence.get("bank"),
            "error_code": evidence.get("error_code"),
            "error_reason": evidence.get("error_reason"),
            "customer_total_attempts": history.get("total_attempts", 0),
            "customer_success_rate": history.get("success_rate", 0.0),
            "customer_failure_count": history.get("failed_count", 0),
            "customer_consecutive_failures": history.get("consecutive_failures", 0),
            "customer_avg_transaction_amount": history.get("avg_transaction_amount", 0.0),
            "action_type": action.action_type,
            "channel": action.channel,
            "decision_score": float(action.decision_score or 0.8),
            "decision_confidence": float(action.decision_confidence or 0.8),
            "policy_allowed": action.status not in ["BLOCKED", "PROHIBITED"],
        }

        # Anti-Leakage Assertion: Verify no outcome fields are accidentally included
        leaked_keys = [k for k in snapshot if k in ["amount_recovered", "time_to_recovery", "time_to_recovery_seconds", "captured_at", "outcome_type", "label"]]
        if leaked_keys:
            raise DataQualityError(f"CRITICAL: Future information leakage detected in pre-decision feature snapshot: {leaked_keys}")

        return snapshot

    @classmethod
    def create_initial_example(
        cls,
        db: Session,
        recovery_case: RecoveryCase,
        action: RecoveryAction,
        diagnosis: Optional[Diagnosis] = None,
    ) -> LearningExample:
        """Create a pending LearningExample record with pre-decision features."""
        snapshot = cls.create_feature_snapshot(
            recovery_case=recovery_case,
            action=action,
            diagnosis=diagnosis,
        )

        history = (diagnosis.evidence if diagnosis and diagnosis.evidence else {}).get("customer_history", {})

        example = LearningExample(
            recovery_case_id=recovery_case.id,
            recovery_action_id=action.id,
            diagnosis_category=snapshot["diagnosis_category"],
            diagnosis_confidence=snapshot["diagnosis_confidence"],
            risk_score=snapshot["risk_score"],
            recovery_probability=snapshot["recovery_probability"],
            amount_at_risk=recovery_case.amount_at_risk or Decimal("0.00"),
            case_age_at_decision_hours=0.0,
            customer_success_rate_at_decision=history.get("success_rate", 0.0),
            customer_failure_count_at_decision=history.get("failed_count", 0),
            previous_recovery_attempts=recovery_case.retry_count or 0,
            payment_method=snapshot["payment_method"],
            bank=snapshot["bank"],
            action_type=action.action_type,
            decision_score=snapshot["decision_score"],
            decision_confidence=snapshot["decision_confidence"],
            policy_allowed=snapshot["policy_allowed"],
            feature_snapshot=snapshot,
            is_finalized=False,
        )
        db.add(example)
        db.flush()

        db.add(
            AuditLog(
                recovery_case_id=recovery_case.id,
                actor_type="SYSTEM",
                actor_id="learning_data_service_v1",
                action="LEARNING_EXAMPLE_CREATED",
                entity_type="LearningExample",
                entity_id=str(example.id),
                audit_metadata={"action_type": action.action_type, "diagnosis_category": snapshot["diagnosis_category"]},
            )
        )
        db.flush()
        logger.info(f"[LEARNING_DATA] Created pending LearningExample {example.id} for case {recovery_case.id}")
        return example

    @classmethod
    def finalize_learning_example(
        cls,
        db: Session,
        recovery_case: RecoveryCase,
        outcome: RecoveryOutcome,
    ) -> Optional[LearningExample]:
        """Attach outcome realization targets and assign binary training label to the learning example."""
        example = db.scalar(
            select(LearningExample)
            .where(
                LearningExample.recovery_case_id == recovery_case.id,
                LearningExample.is_finalized == False,  # noqa: E712
            )
            .order_by(LearningExample.created_at.desc())
        )

        if not example:
            # Check if an already finalized example exists
            example = db.scalar(
                select(LearningExample)
                .where(LearningExample.recovery_case_id == recovery_case.id)
                .order_by(LearningExample.created_at.desc())
            )
            if not example:
                logger.warning(f"[LEARNING_DATA] No LearningExample found to finalize for case {recovery_case.id}.")
                return None

        # Binary Label Assignment Rule:
        # 1 = Successful recovery attributable to the recovery intervention (DIRECT or LIKELY)
        # 0 = Non-recovery, failed action, expired window, or organic recovery
        if outcome.outcome_type in ["RECOVERED", "PARTIALLY_RECOVERED"] and outcome.attribution in ["DIRECT", "LIKELY"]:
            label = 1
        else:
            label = 0

        example.execution_id = outcome.execution_id
        example.outcome_type = outcome.outcome_type
        example.amount_recovered = outcome.amount_recovered
        example.recovery_percentage = outcome.recovery_percentage
        example.time_to_recovery_seconds = outcome.time_to_recovery_seconds
        example.attribution = outcome.attribution
        example.label = label
        example.is_finalized = True
        example.finalized_at = datetime.now(timezone.utc)
        db.flush()

        # Validate data quality
        quality_issues = cls.validate_data_quality(example)
        if quality_issues:
            logger.error(f"[LEARNING_DATA_QUALITY_ISSUE] Example {example.id} has quality issues: {quality_issues}")

        db.add(
            AuditLog(
                recovery_case_id=recovery_case.id,
                actor_type="SYSTEM",
                actor_id="learning_data_service_v1",
                action="LEARNING_EXAMPLE_FINALIZED",
                entity_type="LearningExample",
                entity_id=str(example.id),
                audit_metadata={
                    "label": label,
                    "outcome_type": outcome.outcome_type,
                    "attribution": outcome.attribution,
                    "recovery_percentage": outcome.recovery_percentage,
                },
            )
        )
        db.flush()
        logger.info(f"[LEARNING_DATA] Finalized LearningExample {example.id} with label={label}")
        return example

    @classmethod
    def validate_data_quality(cls, example: LearningExample) -> List[str]:
        """Validate data quality, anti-leakage invariants, and sanity bounds for a LearningExample."""
        issues: List[str] = []

        if example.amount_at_risk is None or example.amount_at_risk < Decimal("0.00"):
            issues.append(f"Invalid amount_at_risk: {example.amount_at_risk}")

        if example.amount_recovered is not None and example.amount_recovered < Decimal("0.00"):
            issues.append(f"Negative amount_recovered: {example.amount_recovered}")

        if (
            example.amount_recovered is not None
            and example.amount_at_risk is not None
            and example.amount_recovered > example.amount_at_risk
        ):
            issues.append(f"amount_recovered ({example.amount_recovered}) exceeds amount_at_risk ({example.amount_at_risk})")

        if example.recovery_percentage is not None and not (0.0 <= example.recovery_percentage <= 100.0):
            issues.append(f"Invalid recovery_percentage: {example.recovery_percentage}")

        if example.time_to_recovery_seconds is not None and example.time_to_recovery_seconds < 0.0:
            issues.append(f"Impossible negative time_to_recovery_seconds: {example.time_to_recovery_seconds}")

        if not example.diagnosis_category:
            issues.append("Missing diagnosis_category")

        if not example.action_type:
            issues.append("Missing action_type")

        # Anti-leakage validation
        snapshot = example.feature_snapshot or {}
        for leaked in ["amount_recovered", "time_to_recovery", "time_to_recovery_seconds", "captured_at", "outcome_type", "label"]:
            if leaked in snapshot:
                issues.append(f"Leakage: Outcome target '{leaked}' found in pre-decision feature snapshot")

        return issues
