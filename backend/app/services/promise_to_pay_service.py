"""Promise-to-Pay Service managing customer commitments and outreach pausing."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, Optional
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.promise_to_pay import PromiseToPay
from app.models.recovery_case import RecoveryCase
from app.services.recovery_scheduler import RecoveryScheduler

logger = logging.getLogger(__name__)


class PromiseToPayService:
    """Manages the Promise-to-Pay lifecycle and coordinates outreach halting."""

    @classmethod
    def create_promise(
        cls,
        db: Session,
        recovery_case_id: uuid.UUID,
        promised_amount: Decimal,
        promised_date: datetime,
        promised_time: Optional[str] = "17:00",
        source: str = "CUSTOMER",
        notes: Optional[str] = None,
    ) -> PromiseToPay:
        """Validate and record a customer commitment, immediately pausing active recovery plans."""
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == recovery_case_id))
        if not case:
            raise ValueError(f"RecoveryCase '{recovery_case_id}' not found.")

        # 1. Validation Pre-Flight Checks
        if case.status in ["RECOVERED", "CLOSED"]:
            raise ValueError(f"Cannot create Promise-to-Pay on already {case.status} case.")

        now = datetime.now(timezone.utc)
        if promised_date.tzinfo is None:
            p_date = promised_date.replace(tzinfo=timezone.utc)
        else:
            p_date = promised_date

        if p_date <= now:
            raise ValueError(f"Promised date must be strictly in the future. Received {p_date.isoformat()}.")

        max_ahead = now + timedelta(days=settings.PROMISE_MAX_DAYS_AHEAD)
        if p_date > max_ahead:
            raise ValueError(f"Promised date exceeds maximum allowable window ({settings.PROMISE_MAX_DAYS_AHEAD} days).")

        amount_due = case.amount_at_risk or Decimal("0.00")
        if promised_amount <= Decimal("0.00"):
            raise ValueError(f"Promised amount must be greater than 0. Received Rs. {promised_amount}.")

        if promised_amount > amount_due:
            raise ValueError(f"Promised amount (Rs. {promised_amount}) cannot exceed total amount due (Rs. {amount_due}).")

        # 2. Cancel any existing ACTIVE promise on the case
        active_existing = db.scalars(
            select(PromiseToPay).where(
                PromiseToPay.recovery_case_id == case.id,
                PromiseToPay.status == "ACTIVE",
            )
        ).all()
        for p in active_existing:
            p.status = "CANCELLED"
            p.cancelled_at = now

        # 3. Create new PromiseToPay record
        promise = PromiseToPay(
            recovery_case_id=case.id,
            customer_id=case.customer_id,
            amount_due=amount_due,
            promised_amount=promised_amount,
            promised_date=p_date,
            promised_time=promised_time or "17:00",
            currency=case.currency or "INR",
            status="ACTIVE",
            source=source,
            confidence=1.0 if source in ["CUSTOMER", "AGENT"] else 0.85,
            notes=notes,
        )
        db.add(promise)
        db.flush()

        # 4. Stopping Rule: Pause Recovery Plan & Cancel Pending Outreach
        RecoveryScheduler.pause_plan(
            db=db,
            case_id=case.id,
            reason="PROMISE_TO_PAY",
        )

        # 5. Emit Structured Audit Logs
        db.add(
            AuditLog(
                recovery_case_id=case.id,
                actor_type=source,
                actor_id="promise_to_pay_service",
                action="PROMISE_TO_PAY_CREATED",
                entity_type="PromiseToPay",
                entity_id=str(promise.id),
                audit_metadata={
                    "promised_amount": float(promised_amount),
                    "amount_due": float(amount_due),
                    "promised_date": p_date.isoformat(),
                    "source": source,
                },
            )
        )
        db.add(
            AuditLog(
                recovery_case_id=case.id,
                actor_type="SYSTEM",
                actor_id="promise_to_pay_service",
                action="RECOVERY_PLAN_PAUSED_FOR_PROMISE",
                entity_type="RecoveryPlan",
                entity_id=str(case.recovery_plan.id) if case.recovery_plan else str(case.id),
                audit_metadata={"promise_id": str(promise.id), "status": "PAUSED"},
            )
        )
        db.commit()
        db.refresh(promise)

        logger.info(
            f"[PROMISE_CREATED] Case={case.id} Promise={promise.id} Amount=Rs.{promised_amount} Date={p_date.isoformat()}"
        )
        return promise

    @classmethod
    def has_active_promise(cls, db: Session, recovery_case_id: uuid.UUID) -> bool:
        """Fast index-backed check to verify if a case currently has an active commitment."""
        promise = db.scalar(
            select(PromiseToPay).where(
                PromiseToPay.recovery_case_id == recovery_case_id,
                PromiseToPay.status == "ACTIVE",
            ).limit(1)
        )
        return promise is not None

    @classmethod
    def cancel_promise(
        cls,
        db: Session,
        promise_id: uuid.UUID,
        reason: str = "CANCELLED_BY_OPERATOR",
    ) -> PromiseToPay:
        """Cancel an active promise and resume automated recovery sequencing."""
        promise = db.scalar(select(PromiseToPay).where(PromiseToPay.id == promise_id))
        if not promise:
            raise ValueError(f"PromiseToPay '{promise_id}' not found.")

        now = datetime.now(timezone.utc)
        promise.status = "CANCELLED"
        promise.cancelled_at = now

        # Resume Recovery Plan
        RecoveryScheduler.resume_plan(db=db, case_id=promise.recovery_case_id)

        db.add(
            AuditLog(
                recovery_case_id=promise.recovery_case_id,
                actor_type="OPERATOR",
                actor_id="promise_to_pay_service",
                action="PROMISE_TO_PAY_CANCELLED",
                entity_type="PromiseToPay",
                entity_id=str(promise.id),
                audit_metadata={"reason": reason, "cancelled_at": now.isoformat()},
            )
        )
        db.commit()
        db.refresh(promise)
        logger.info(f"[PROMISE_CANCELLED] Promise {promise.id} cancelled: {reason}")
        return promise

    @classmethod
    def get_customer_promise_history(cls, db: Session, customer_id: uuid.UUID) -> Dict[str, Any]:
        """Compute customer historical commitment stats and fulfillment reliability rate."""
        promises = db.scalars(
            select(PromiseToPay).where(PromiseToPay.customer_id == customer_id)
        ).all()

        total = len(promises)
        fulfilled = sum(1 for p in promises if p.status == "FULFILLED")
        missed = sum(1 for p in promises if p.status == "MISSED")
        expired = sum(1 for p in promises if p.status == "EXPIRED")
        completed = fulfilled + missed + expired

        fulfillment_rate = (fulfilled / completed) if completed > 0 else 1.0

        return {
            "total_promises": total,
            "fulfilled": fulfilled,
            "missed": missed,
            "expired": expired,
            "fulfillment_rate": round(fulfillment_rate, 4),
        }
