"""Execution lifecycle orchestration service."""
from datetime import datetime, timezone
import logging
from typing import Dict
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.recovery_action import RecoveryAction
from app.models.execution import RecoveryExecution
from app.models.audit_log import AuditLog
from app.decision.base import ActionType
from app.execution.base import (
    ExecutionRequest,
    ExecutionResult,
    ExecutionStatus,
    RecoveryExecutor,
)
from app.execution.guard import ExecutionGuard, GuardEvaluationResult
from app.execution.executors.razorpay_payment_link import RazorpayPaymentLinkExecutor

logger = logging.getLogger(__name__)


class ExecutionService:
    """Orchestrates pre-flight guard checks, provider dispatch, idempotency, and audit trails."""

    _executors: Dict[str, RecoveryExecutor] = {
        ActionType.SEND_PAYMENT_LINK.value: RazorpayPaymentLinkExecutor(),
    }

    @classmethod
    def register_executor(cls, action_type: str, executor: RecoveryExecutor) -> None:
        """Register or override a recovery action executor."""
        cls._executors[action_type] = executor

    @classmethod
    def execute_action(
        cls,
        db: Session,
        recovery_case: RecoveryCase,
    ) -> RecoveryExecution:
        """Execute the latest approved recovery action for an open recovery case.

        Args:
            db: Database session.
            recovery_case: The target RecoveryCase.

        Returns:
            The persisted RecoveryExecution record.
        """
        # 1. Fetch latest approved/recommended action
        action = db.scalar(
            select(RecoveryAction)
            .where(
                RecoveryAction.recovery_case_id == recovery_case.id,
                RecoveryAction.status.in_(["APPROVED", "RECOMMENDED", "PLANNED"]),
            )
            .order_by(RecoveryAction.created_at.desc())
        )

        if not action:
            # Check if there is an already executed action to return safely
            last_executed = db.scalar(
                select(RecoveryExecution)
                .where(RecoveryExecution.recovery_case_id == recovery_case.id)
                .order_by(RecoveryExecution.created_at.desc())
            )
            if last_executed and last_executed.status == "SUCCEEDED":
                logger.info(f"[EXECUTION_IDEMPOTENT] Case {recovery_case.id} already has a succeeded execution.")
                return last_executed

            raise ValueError(f"No actionable approved recommendation found for case '{recovery_case.id}'.")

        # 2. Deterministic Idempotency Key
        idempotency_key = f"{recovery_case.id}_{action.id}_{action.action_type}"

        # 3. Check if execution for this key has already succeeded (Idempotency)
        existing_exec = db.scalar(
            select(RecoveryExecution).where(
                RecoveryExecution.idempotency_key == idempotency_key
            )
        )
        if existing_exec and existing_exec.status == ExecutionStatus.SUCCEEDED.value:
            logger.info(f"[EXECUTION_IDEMPOTENT] Idempotency key {idempotency_key} already succeeded. Returning existing record.")
            return existing_exec

        # 4. Pre-Flight Execution Guard (2nd Safety Layer)
        guard_result: GuardEvaluationResult = ExecutionGuard.validate_pre_flight(
            db=db,
            recovery_case=recovery_case,
            recovery_action=action,
            idempotency_key=idempotency_key,
        )

        if not guard_result.allowed:
            logger.warning(
                f"[EXECUTION_BLOCKED] Guard rejected execution for case {recovery_case.id}: "
                f"{guard_result.blocking_rule} - {guard_result.reason}"
            )
            execution = RecoveryExecution(
                recovery_case_id=recovery_case.id,
                recovery_action_id=action.id,
                action_type=action.action_type,
                provider="GUARD",
                status=ExecutionStatus.BLOCKED.value,
                idempotency_key=idempotency_key,
                error_code=guard_result.blocking_rule,
                error_message=guard_result.reason,
            )
            db.add(execution)
            action.status = "BLOCKED"
            db.flush()

            # Record audit log
            audit = AuditLog(
                recovery_case_id=recovery_case.id,
                actor_type="SYSTEM",
                actor_id="execution_guard_v1",
                action="EXECUTION_BLOCKED",
                entity_type="RecoveryExecution",
                entity_id=str(execution.id),
                audit_metadata={
                    "blocking_rule": guard_result.blocking_rule,
                    "reason": guard_result.reason,
                    "action_type": action.action_type,
                },
            )
            db.add(audit)
            db.flush()
            return execution

        # 5. Create or reuse Execution Record in EXECUTING state
        customer = recovery_case.customer
        exec_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        execution = RecoveryExecution(
            id=exec_id,
            recovery_case_id=recovery_case.id,
            recovery_action_id=action.id,
            action_type=action.action_type,
            provider="INITIALIZING",
            status=ExecutionStatus.EXECUTING.value,
            idempotency_key=idempotency_key,
            started_at=now,
        )
        db.add(execution)
        db.flush()

        audit_start = AuditLog(
            recovery_case_id=recovery_case.id,
            actor_type="SYSTEM",
            actor_id="execution_service_v1",
            action="EXECUTION_STARTED",
            entity_type="RecoveryExecution",
            entity_id=str(execution.id),
            audit_metadata={
                "idempotency_key": idempotency_key,
                "action_type": action.action_type,
            },
        )
        db.add(audit_start)
        db.flush()

        # 6. Locate Executor
        executor = cls._executors.get(action.action_type)
        if not executor:
            error_msg = f"No registered executor for action_type '{action.action_type}'."
            logger.error(f"[EXECUTION_ERROR] {error_msg}")
            execution.status = ExecutionStatus.FAILED.value
            execution.provider = "SYSTEM"
            execution.error_code = "UNSUPPORTED_ACTION_TYPE"
            execution.error_message = error_msg
            execution.completed_at = datetime.now(timezone.utc)
            action.status = "FAILED"
            db.flush()
            return execution

        # 7. Dispatch Execution Request
        req = ExecutionRequest(
            execution_id=str(execution.id),
            case_id=str(recovery_case.id),
            action_id=str(action.id),
            action_type=action.action_type,
            customer_id=str(recovery_case.customer_id),
            amount=recovery_case.amount_at_risk,
            currency=recovery_case.currency,
            idempotency_key=idempotency_key,
            customer_name=customer.name if customer else None,
            customer_email=customer.email if customer else None,
            customer_phone=customer.phone if customer else None,
            requested_at=now,
            engine_version=action.decision_engine_version,
            policy_version=action.policy_engine_version,
        )

        exec_res: ExecutionResult = executor.execute(req)

        # 8. Update Execution Record and Recovery Action
        execution.provider = exec_res.provider
        execution.status = exec_res.status.value
        execution.provider_reference = exec_res.provider_reference
        execution.provider_url = exec_res.provider_url
        execution.error_code = exec_res.error_code
        execution.error_message = exec_res.error_message
        execution.execution_metadata = exec_res.execution_metadata
        execution.completed_at = datetime.now(timezone.utc)

        if exec_res.status == ExecutionStatus.SUCCEEDED:
            action.status = "EXECUTED"
            action.executed_at = datetime.now(timezone.utc)
            # CRITICAL MONEY TRACKING RULE:
            # Creating a payment link leaves amount_at_risk intact.
            # recovery_case.recovered_amount remains None/0 until payment.captured webhook.
            audit_action = "EXECUTION_SUCCEEDED"
        else:
            action.status = "FAILED"
            audit_action = "EXECUTION_FAILED"

        db.flush()

        # 9. Record Completion AuditLog
        audit_end = AuditLog(
            recovery_case_id=recovery_case.id,
            actor_type="SYSTEM",
            actor_id=exec_res.provider,
            action=audit_action,
            entity_type="RecoveryExecution",
            entity_id=str(execution.id),
            audit_metadata={
                "status": exec_res.status.value,
                "provider": exec_res.provider,
                "provider_reference": exec_res.provider_reference,
                "provider_url": exec_res.provider_url,
                "error_code": exec_res.error_code,
            },
        )
        db.add(audit_end)
        db.flush()

        logger.info(
            f"[EXECUTION_COMPLETED] Case {recovery_case.id} execution {execution.id} "
            f"status={exec_res.status.value} provider_reference={exec_res.provider_reference}"
        )

        return execution
