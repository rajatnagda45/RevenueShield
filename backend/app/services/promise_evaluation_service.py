"""Promise Evaluation Service assessing payment satisfaction and triggering adaptive sequencers."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.promise_to_pay import PromiseToPay
from app.models.recovery_case import RecoveryCase
from app.services.next_best_action_engine import NextBestActionEngine
from app.services.recovery_scheduler import RecoveryScheduler

logger = logging.getLogger(__name__)


class PromiseEvaluationService:
    """Evaluates maturity of customer payment commitments and orchestrates plan resumption."""

    @classmethod
    def evaluate_promise(
        cls,
        db: Session,
        promise_id: uuid.UUID,
        reference_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Evaluate whether a Promise-to-Pay has been fulfilled, partially settled, missed, or expired."""
        promise = db.scalar(select(PromiseToPay).where(PromiseToPay.id == promise_id))
        if not promise:
            raise ValueError(f"PromiseToPay '{promise_id}' not found.")

        if promise.status not in ["ACTIVE", "PARTIAL"]:
            return {
                "promise_id": str(promise.id),
                "status": promise.status,
                "message": f"Promise is already in terminal state '{promise.status}'.",
            }

        case = promise.recovery_case or db.scalar(
            select(RecoveryCase).where(RecoveryCase.id == promise.recovery_case_id)
        )
        now = reference_time or datetime.now(timezone.utc)
        p_date = promise.promised_date
        if p_date.tzinfo is None:
            p_date = p_date.replace(tzinfo=timezone.utc)

        recovered_amt = case.recovered_amount if case and case.recovered_amount else Decimal("0.00")
        promised_amt = promise.promised_amount or Decimal("0.00")
        grace_expiry = p_date + timedelta(hours=settings.PROMISE_EXPIRATION_GRACE_HOURS)

        # ---------------------------------------------------------------------
        # 1. Full Payment Check (FULFILLED)
        # ---------------------------------------------------------------------
        if (case and case.status == "RECOVERED") or (recovered_amt >= promised_amt and promised_amt > 0):
            promise.status = "FULFILLED"
            promise.fulfilled_at = now
            RecoveryScheduler.stop_plan_on_recovery(
                db=db,
                case_id=promise.recovery_case_id,
                reason="PROMISE_FULFILLED",
            )
            db.add(
                AuditLog(
                    recovery_case_id=promise.recovery_case_id,
                    actor_type="SYSTEM",
                    actor_id="promise_evaluation_service",
                    action="PROMISE_TO_PAY_FULFILLED",
                    entity_type="PromiseToPay",
                    entity_id=str(promise.id),
                    audit_metadata={
                        "amount_recovered": float(recovered_amt),
                        "promised_amount": float(promised_amt),
                        "fulfilled_at": now.isoformat(),
                    },
                )
            )
            db.commit()
            db.refresh(promise)
            logger.info(f"[PROMISE_FULFILLED] Promise {promise.id} fulfilled with Rs.{recovered_amt}.")
            return {
                "promise_id": str(promise.id),
                "status": "FULFILLED",
                "amount_recovered": float(recovered_amt),
                "fulfilled_at": now.isoformat(),
            }

        # ---------------------------------------------------------------------
        # 2. Before Deadline Check (Still Active)
        # ---------------------------------------------------------------------
        if now < p_date:
            return {
                "promise_id": str(promise.id),
                "status": "ACTIVE",
                "message": f"Promise deadline has not yet arrived (Due: {p_date.isoformat()}).",
            }

        # ---------------------------------------------------------------------
        # 3. Partial Payment Check (PARTIAL)
        # ---------------------------------------------------------------------
        if recovered_amt > Decimal("0.00") and recovered_amt < promised_amt:
            promise.status = "PARTIAL"
            remaining = promised_amt - recovered_amt
            logger.info(f"[PROMISE_PARTIAL] Promise {promise.id} partially settled (Rs.{recovered_amt}/{promised_amt}).")
            db.commit()
            return {
                "promise_id": str(promise.id),
                "status": "PARTIAL",
                "amount_recovered": float(recovered_amt),
                "remaining_promised_amount": float(remaining),
            }

        # ---------------------------------------------------------------------
        # 4. Past Grace Expiry Check (EXPIRED)
        # ---------------------------------------------------------------------
        if now >= grace_expiry:
            promise.status = "EXPIRED"
            promise.expired_at = now
            RecoveryScheduler.resume_plan(db=db, case_id=promise.recovery_case_id)
            db.add(
                AuditLog(
                    recovery_case_id=promise.recovery_case_id,
                    actor_type="SYSTEM",
                    actor_id="promise_evaluation_service",
                    action="PROMISE_TO_PAY_EXPIRED",
                    entity_type="PromiseToPay",
                    entity_id=str(promise.id),
                    audit_metadata={"expired_at": now.isoformat(), "grace_hours": settings.PROMISE_EXPIRATION_GRACE_HOURS},
                )
            )
            db.commit()
            db.refresh(promise)
            return {
                "promise_id": str(promise.id),
                "status": "EXPIRED",
                "message": "Promise exceeded grace period without payment. Recovery plan resumed.",
            }

        # ---------------------------------------------------------------------
        # 5. Missed Deadline Check (MISSED)
        # ---------------------------------------------------------------------
        promise.status = "MISSED"
        # Resume Plan & Re-evaluate via NextBestActionEngine
        RecoveryScheduler.resume_plan(db=db, case_id=promise.recovery_case_id)
        next_action = NextBestActionEngine.compute_next_best_action(db=db, case=case) if case else None

        db.add(
            AuditLog(
                recovery_case_id=promise.recovery_case_id,
                actor_type="SYSTEM",
                actor_id="promise_evaluation_service",
                action="PROMISE_TO_PAY_MISSED",
                entity_type="PromiseToPay",
                entity_id=str(promise.id),
                audit_metadata={
                    "promised_date": p_date.isoformat(),
                    "evaluated_at": now.isoformat(),
                    "next_action": next_action.action_type if next_action else "NONE",
                },
            )
        )
        db.add(
            AuditLog(
                recovery_case_id=promise.recovery_case_id,
                actor_type="SYSTEM",
                actor_id="promise_evaluation_service",
                action="RECOVERY_PLAN_RESUMED_AFTER_PROMISE",
                entity_type="RecoveryPlan",
                entity_id=str(case.recovery_plan.id) if case and case.recovery_plan else str(promise.recovery_case_id),
                audit_metadata={"reason": "PROMISE_MISSED"},
            )
        )
        db.commit()
        db.refresh(promise)

        logger.info(
            f"[PROMISE_MISSED] Promise {promise.id} missed deadline. Resumed plan. NBA: {next_action.action_type if next_action else 'NONE'}"
        )
        return {
            "promise_id": str(promise.id),
            "status": "MISSED",
            "next_best_action": next_action.action_type if next_action else "NONE",
            "expected_recovery_value": float(next_action.expected_recovery_value) if next_action else 0.0,
        }

    @classmethod
    def evaluate_due_promises(
        cls,
        db: Session,
        reference_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Batch evaluate all active promises that have reached or passed their commitment deadline."""
        now = reference_time or datetime.now(timezone.utc)
        due_promises = db.scalars(
            select(PromiseToPay).where(
                PromiseToPay.status.in_(["ACTIVE", "PARTIAL"]),
                PromiseToPay.promised_date <= now,
            )
        ).all()

        results = []
        for p in due_promises:
            try:
                res = cls.evaluate_promise(db=db, promise_id=p.id, reference_time=now)
                results.append(res)
            except Exception as exc:
                logger.error(f"[PROMISE_EVAL_ERROR] Error evaluating promise {p.id}: {exc}")
                results.append({"promise_id": str(p.id), "status": "UNKNOWN", "error": str(exc)})

        return results
