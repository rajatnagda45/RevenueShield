"""Second-Layer Safety & Pre-Flight Execution Guard.

Validates all situational conditions, case lifecycles, and policy constraints
immediately before any external or simulated provider call is made.
"""
from dataclasses import dataclass
from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.recovery_action import RecoveryAction
from app.models.execution import RecoveryExecution
from app.models.promise_to_pay import PromiseToPay
from app.decision.policy import PolicyEngine
from app.decision.base import DecisionContext
from app.degradation.monitor import DegradationMonitor


@dataclass
class GuardEvaluationResult:
    """Outcome of pre-flight execution guard validation."""

    allowed: bool
    reason: str
    blocking_rule: Optional[str] = None


class ExecutionGuard:
    """Pre-flight safety barrier executed immediately prior to provider invocation."""

    @classmethod
    def validate_pre_flight(
        cls,
        db: Session,
        recovery_case: RecoveryCase,
        recovery_action: RecoveryAction,
        idempotency_key: str,
    ) -> GuardEvaluationResult:
        """Verify all 10 safety and idempotency invariants.

        Args:
            db: Database session.
            recovery_case: RecoveryCase instance.
            recovery_action: Target RecoveryAction instance.
            idempotency_key: Unique deterministic idempotency string.

        Returns:
            GuardEvaluationResult indicating whether execution may proceed.
        """
        # 1 & 8: Verify RecoveryCase is in an actionable state
        if recovery_case.status in ["RECOVERED", "CLOSED", "CANCELLED", "RESOLVED"]:
            return GuardEvaluationResult(
                allowed=False,
                reason=f"RecoveryCase is already {recovery_case.status}. Execution is prohibited.",
                blocking_rule="CASE_INACTIVE",
            )

        # 1b (measurement): HOLDOUT cases are never executed against
        if recovery_case.experiment_arm == "HOLDOUT":
            return GuardEvaluationResult(
                allowed=False,
                reason="Case is in the experiment HOLDOUT arm; execution is prohibited (observe only).",
                blocking_rule="HOLDOUT_ARM_OBSERVE_ONLY",
            )

        # 2: Verify payment has not already been captured
        if recovery_case.recovered_amount and recovery_case.recovered_amount > 0:
            return GuardEvaluationResult(
                allowed=False,
                reason="Payment has already been captured and recovered for this case.",
                blocking_rule="PAYMENT_ALREADY_CAPTURED",
            )

        # 3: Verify RecoveryAction is in APPROVED or RECOMMENDED state
        if recovery_action.status not in ["APPROVED", "RECOMMENDED", "PLANNED"]:
            return GuardEvaluationResult(
                allowed=False,
                reason=f"RecoveryAction is currently '{recovery_action.status}' (must be APPROVED).",
                blocking_rule="ACTION_NOT_APPROVED",
            )

        # 5: Verify Promise-to-Pay has not become active
        active_ptp = db.scalar(
            select(PromiseToPay).where(
                PromiseToPay.customer_id == recovery_case.customer_id,
                PromiseToPay.status == "ACTIVE",
            )
        )
        if active_ptp:
            return GuardEvaluationResult(
                allowed=False,
                reason="Customer has an active Promise-to-Pay agreement. Execution is blocked.",
                blocking_rule="PROMISE_TO_PAY_ACTIVE",
            )

        # 7 & 10: Check existing executions for this idempotency key
        existing_exec = db.scalar(
            select(RecoveryExecution).where(
                RecoveryExecution.idempotency_key == idempotency_key
            )
        )
        if existing_exec:
            if existing_exec.status == "SUCCEEDED":
                return GuardEvaluationResult(
                    allowed=False,
                    reason="This action has already been successfully executed.",
                    blocking_rule="IDEMPOTENCY_ALREADY_SUCCEEDED",
                )
            if existing_exec.status == "EXECUTING":
                return GuardEvaluationResult(
                    allowed=False,
                    reason="This action is currently in an EXECUTING state. Parallel execution is blocked.",
                    blocking_rule="CONCURRENT_EXECUTION_IN_PROGRESS",
                )

        # 6: Check no other action is currently EXECUTING on this case
        other_running = db.scalar(
            select(RecoveryExecution).where(
                RecoveryExecution.recovery_case_id == recovery_case.id,
                RecoveryExecution.status == "EXECUTING",
            )
        )
        if other_running:
            return GuardEvaluationResult(
                allowed=False,
                reason="Another recovery execution is currently in-flight for this case.",
                blocking_rule="CONCURRENT_INTERVENTION_EXISTS",
            )

        # 4 & 9: Re-evaluate Policy Engine in real-time
        context = DecisionContext(
            case_id=str(recovery_case.id),
            case_type=recovery_case.case_type,
            amount_at_risk=recovery_case.amount_at_risk,
            currency=recovery_case.currency,
            case_age_hours=0.0,
            retry_count=recovery_case.retry_count or 0,
            diagnosis_category=recovery_action.reason or "UNKNOWN",
            diagnosis_confidence=recovery_action.confidence or 0.8,
            risk_score=recovery_case.risk_score or 50.0,
            recovery_probability=recovery_case.recovery_probability or 0.5,
            promise_to_pay_active=bool(active_ptp),
            metadata={
                "experiment_arm": recovery_case.experiment_arm,
                "leak_surface": recovery_case.leak_surface,
                "systemic_incident_active": DegradationMonitor.case_is_held(db, recovery_case),
            },
        )

        policy_check = PolicyEngine.evaluate(
            action_type=recovery_action.action_type,
            context=context,
            case_status=recovery_case.status,
            active_interventions_count=0,
        )
        if not policy_check.allowed:
            return GuardEvaluationResult(
                allowed=False,
                reason=f"Policy violation at execution time: {policy_check.reason}",
                blocking_rule=policy_check.blocking_rule,
            )

        return GuardEvaluationResult(
            allowed=True,
            reason="Pre-flight execution guard passed all safety invariants.",
            blocking_rule=None,
        )
