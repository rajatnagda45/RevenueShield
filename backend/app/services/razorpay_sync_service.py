"""Razorpay Historical and Batch Payment Synchronization Service."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.sync_checkpoint import SyncCheckpoint
from app.models.audit_log import AuditLog
from app.integrations.razorpay.payment_client import RazorpayPaymentClient
from app.integrations.razorpay.payment_normalizer import PaymentNormalizer, NormalizedPaymentData
from app.repositories.payment_repository import PaymentRepository
from app.schemas.event import NormalizedEvent
from app.diagnosis.service import DiagnosisService
from app.decision.service import DecisionService
from app.outcomes.engine import OutcomeEngine
from app.learning.service import LearningDataService

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """Structured result of a Razorpay payment synchronization run."""

    sync_id: str
    status: str
    records_fetched: int
    records_created: int
    records_updated: int
    from_timestamp: Optional[str]
    to_timestamp: Optional[str]
    started_at: str
    completed_at: Optional[str]
    error_message: Optional[str] = None


class RazorpayPaymentSyncService:
    """Orchestrates historical and batch payment synchronization from Razorpay API."""

    @classmethod
    def sync_payments(
        cls,
        db: Session,
        from_dt: Optional[datetime] = None,
        to_dt: Optional[datetime] = None,
        batch_size: int = 100,
        client: Optional[RazorpayPaymentClient] = None,
    ) -> SyncResult:
        """Synchronize payments from Razorpay API for an optional time window.

        Args:
            db: Database session.
            from_dt: Start datetime (inclusive).
            to_dt: End datetime (inclusive).
            batch_size: Number of records per API request (max 100).
            client: Optional injected RazorpayPaymentClient.
        """
        # Validate date range
        if from_dt and to_dt and from_dt > to_dt:
            raise ValueError(f"Start date ({from_dt}) cannot be after end date ({to_dt}).")

        # 1. Initialize SyncCheckpoint in database
        now = datetime.now(timezone.utc)
        checkpoint = SyncCheckpoint(
            source="RAZORPAY_API",
            started_at=now,
            from_timestamp=from_dt,
            to_timestamp=to_dt,
            status="RUNNING",
            records_fetched=0,
            records_created=0,
            records_updated=0,
        )
        db.add(checkpoint)
        db.commit()
        db.refresh(checkpoint)

        rz_client = client or RazorpayPaymentClient()

        from_epoch = int(from_dt.timestamp()) if from_dt else None
        to_epoch = int(to_dt.timestamp()) if to_dt else None

        records_fetched = 0
        records_created = 0
        records_updated = 0
        skip = 0

        try:
            logger.info(
                f"[RAZORPAY_SYNC_STARTED] Sync ID {checkpoint.id} range: {from_dt} to {to_dt}"
            )

            while True:
                response = rz_client.fetch_payments(
                    from_timestamp=from_epoch,
                    to_timestamp=to_epoch,
                    count=batch_size,
                    skip=skip,
                )

                items: List[Dict[str, Any]] = response.get("items", [])
                if not items:
                    logger.info(f"[RAZORPAY_SYNC] Reached end of records at skip={skip}.")
                    break

                for item in items:
                    records_fetched += 1
                    try:
                        norm_data = PaymentNormalizer.normalize_entity(item)
                        payment, is_new = PaymentRepository.upsert_payment(db, norm_data)

                        if is_new:
                            records_created += 1
                        else:
                            records_updated += 1

                        # Downstream Recovery Pipeline Integration
                        cls._reconcile_recovery_state(db=db, payment=payment, norm_data=norm_data)

                    except Exception as item_err:
                        logger.error(
                            f"[RAZORPAY_SYNC_ITEM_ERROR] Failed processing payment {item.get('id')}: {item_err}"
                        )

                # Pagination stopping condition
                if len(items) < batch_size:
                    break

                skip += len(items)

            # Mark Checkpoint as SUCCEEDED
            completed_time = datetime.now(timezone.utc)
            checkpoint.status = "SUCCEEDED"
            checkpoint.completed_at = completed_time
            checkpoint.records_fetched = records_fetched
            checkpoint.records_created = records_created
            checkpoint.records_updated = records_updated
            db.commit()

            logger.info(
                f"[RAZORPAY_SYNC_SUCCEEDED] Sync {checkpoint.id} fetched={records_fetched}, "
                f"created={records_created}, updated={records_updated}"
            )

            return SyncResult(
                sync_id=str(checkpoint.id),
                status="SUCCEEDED",
                records_fetched=records_fetched,
                records_created=records_created,
                records_updated=records_updated,
                from_timestamp=from_dt.isoformat() if from_dt else None,
                to_timestamp=to_dt.isoformat() if to_dt else None,
                started_at=checkpoint.started_at.isoformat(),
                completed_at=completed_time.isoformat(),
            )

        except Exception as exc:
            db.rollback()
            completed_time = datetime.now(timezone.utc)
            checkpoint.status = "FAILED"
            checkpoint.completed_at = completed_time
            checkpoint.records_fetched = records_fetched
            checkpoint.records_created = records_created
            checkpoint.records_updated = records_updated
            checkpoint.error_message = str(exc)[:1000]
            db.commit()

            logger.error(f"[RAZORPAY_SYNC_FAILED] Sync {checkpoint.id} failed: {exc}")
            raise

    @classmethod
    def _reconcile_recovery_state(
        cls,
        db: Session,
        payment: Payment,
        norm_data: NormalizedPaymentData,
    ) -> None:
        """Connect ingested payment with Diagnosis/RecoveryCase or OutcomeEngine without duplicate cases."""
        if payment.status == "FAILED":
            # Check if a RecoveryCase already exists for this payment
            existing_case = db.scalar(
                select(RecoveryCase).where(RecoveryCase.payment_id == payment.id)
            )
            if not existing_case:
                # Create Event record first
                db_event = Event(
                    external_event_id=f"sync_evt_{payment.external_payment_id}",
                    event_type="payment.failed",
                    source="RAZORPAY_SYNC",
                    customer_id=payment.customer_id,
                    payment_id=payment.id,
                    payload=payment.raw_payload or {},
                    occurred_at=payment.razorpay_created_at or payment.created_at,
                    processing_status="PROCESSED",
                    processed_at=datetime.now(timezone.utc),
                )
                db.add(db_event)
                db.flush()

                # Create RecoveryCase for this failed payment
                case = RecoveryCase(
                    customer_id=payment.customer_id,
                    event_id=db_event.id,
                    payment_id=payment.id,
                    amount_at_risk=payment.amount,
                    currency=payment.currency,
                    case_type="PAYMENT_FAILURE",
                    status="OPEN",
                )
                db.add(case)
                db.flush()

                # Record audit log
                db.add(
                    AuditLog(
                        recovery_case_id=case.id,
                        actor_type="SYSTEM",
                        actor_id="razorpay_sync_service",
                        action="RECOVERY_CASE_OPENED_FROM_SYNC",
                        entity_type="RecoveryCase",
                        entity_id=str(case.id),
                        audit_metadata={
                            "payment_id": str(payment.id),
                            "external_payment_id": payment.external_payment_id,
                            "amount_at_risk": str(payment.amount),
                        },
                    )
                )
                db.flush()

                # Trigger Diagnosis and Decision Pipeline
                simulated_event = NormalizedEvent(
                    event_id=f"sync_evt_{payment.external_payment_id}",
                    event_type="payment.failed",
                    source="RAZORPAY_SYNC",
                    amount=payment.amount,
                    currency=payment.currency,
                    external_payment_id=payment.external_payment_id,
                    payment_method=payment.payment_method,
                    failure_code=payment.failure_code,
                    failure_reason=payment.error_reason,
                    failure_description=payment.failure_description,
                )

                diag = DiagnosisService.diagnose_case(db=db, recovery_case=case, event=simulated_event)
                action = DecisionService.generate_recommendation(db=db, recovery_case=case, diagnosis=diag)
                if action:
                    LearningDataService.create_initial_example(db=db, recovery_case=case, action=action, diagnosis=diag)

        elif payment.status == "CAPTURED":
            # Check if there is an active RecoveryCase associated with this payment or customer
            active_case = db.scalar(
                select(RecoveryCase).where(
                    RecoveryCase.payment_id == payment.id,
                    RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PTP"]),
                )
            )
            if active_case:
                OutcomeEngine.process_payment_capture(
                    db=db,
                    recovery_case=active_case,
                    captured_amount=payment.amount,
                    captured_at=payment.paid_at or payment.created_at,
                    provider_event_id=payment.external_payment_id,
                )

    @classmethod
    def get_data_quality_summary(cls, db: Session) -> Dict[str, Any]:
        """Compute aggregate data quality metrics across all stored payments and sync activity."""
        total_payments = db.scalar(select(func.count(Payment.id))) or 0
        successful_payments = db.scalar(
            select(func.count(Payment.id)).where(Payment.status.in_(["CAPTURED", "SUCCESS"]))
        ) or 0
        failed_payments = db.scalar(
            select(func.count(Payment.id)).where(Payment.status == "FAILED")
        ) or 0
        unknown_status = db.scalar(
            select(func.count(Payment.id)).where(
                ~Payment.status.in_(["CAPTURED", "SUCCESS", "FAILED", "AUTHORIZED", "REFUNDED", "CREATED"])
            )
        ) or 0

        total_amount = db.scalar(select(func.coalesce(func.sum(Payment.amount), Decimal("0.00")))) or Decimal("0.00")
        failed_amount = db.scalar(
            select(func.coalesce(func.sum(Payment.amount), Decimal("0.00"))).where(Payment.status == "FAILED")
        ) or Decimal("0.00")
        captured_amount = db.scalar(
            select(func.coalesce(func.sum(Payment.amount), Decimal("0.00"))).where(Payment.status.in_(["CAPTURED", "SUCCESS"]))
        ) or Decimal("0.00")

        last_sync = db.scalar(
            select(SyncCheckpoint.completed_at)
            .where(SyncCheckpoint.status == "SUCCEEDED")
            .order_by(SyncCheckpoint.completed_at.desc())
            .limit(1)
        )

        last_webhook = db.scalar(
            select(Event.occurred_at)
            .where(Event.source == "RAZORPAY")
            .order_by(Event.occurred_at.desc())
            .limit(1)
        )

        return {
            "total_payments": total_payments,
            "successful_payments": successful_payments,
            "failed_payments": failed_payments,
            "unknown_status_payments": unknown_status,
            "total_amount": float(total_amount),
            "failed_amount": float(failed_amount),
            "captured_amount": float(captured_amount),
            "last_sync_time": last_sync.isoformat() if last_sync else None,
            "last_webhook_time": last_webhook.isoformat() if last_webhook else None,
        }
