"""Payment Repository for Idempotent Persistence & State Transition Control."""
from datetime import datetime, timezone
import logging
from typing import Optional, Tuple
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.integrations.razorpay.payment_normalizer import NormalizedPaymentData
from app.services.customer_service import CustomerService

logger = logging.getLogger(__name__)


class PaymentRepository:
    """Repository managing internal Payment records with idempotency and state protection."""

    # Terminal states where regression to non-terminal/failed is prohibited
    TERMINAL_SUCCESS_STATES = {"CAPTURED", "REFUNDED"}

    @classmethod
    def get_by_external_id(
        cls,
        db: Session,
        external_payment_id: str,
    ) -> Optional[Payment]:
        """Fetch payment by Razorpay payment ID (e.g. 'pay_xxx')."""
        if not external_payment_id:
            return None
        return db.scalar(
            select(Payment).where(Payment.external_payment_id == external_payment_id)
        )

    @classmethod
    def upsert_payment(
        cls,
        db: Session,
        data: NormalizedPaymentData,
        customer_id: Optional[uuid.UUID] = None,
    ) -> Tuple[Payment, bool]:
        """Idempotently insert or update a normalized payment record.

        Enforces State Monotonicity: A payment that has reached CAPTURED or REFUNDED
        cannot be downgraded to FAILED or CREATED due to delayed or out-of-order deliveries.

        Returns:
            Tuple of (Payment, is_created_boolean)
        """
        # 1. Resolve or reconcile customer
        if customer_id:
            resolved_customer_id = customer_id
        else:
            customer = CustomerService.reconcile_customer(
                db=db,
                email=data.customer_email,
                phone=data.customer_phone,
                name=data.customer_name,
                external_id=data.customer_id_ext,
            )
            resolved_customer_id = customer.id

        existing = cls.get_by_external_id(db, data.external_payment_id)

        if not existing:
            # 2. Insert new Payment
            payment = Payment(
                external_payment_id=data.external_payment_id,
                razorpay_payment_id=data.external_payment_id,
                razorpay_order_id=data.razorpay_order_id,
                customer_id=resolved_customer_id,
                amount=data.amount,
                currency=data.currency,
                status=data.status,
                payment_method=data.payment_method,
                bank=data.bank,
                wallet=data.wallet,
                vpa=data.vpa,
                international=data.international,
                captured=data.captured,
                amount_refunded=data.amount_refunded,
                refund_status=data.refund_status,
                description=data.description,
                failure_code=data.error_code,
                failure_description=data.error_description,
                error_source=data.error_source,
                error_step=data.error_step,
                error_reason=data.error_reason,
                paid_at=data.paid_at,
                razorpay_created_at=data.razorpay_created_at,
                raw_payload=data.raw_payload,
            )
            db.add(payment)
            db.flush()
            logger.info(
                f"[PAYMENT_CREATED] Created payment {payment.id} for {data.external_payment_id} "
                f"status={payment.status} amount={payment.amount}"
            )
            return payment, True

        # 3. Update existing Payment with State Transition Protection
        if existing.status in cls.TERMINAL_SUCCESS_STATES and data.status not in cls.TERMINAL_SUCCESS_STATES:
            logger.warning(
                f"[STATE_TRANSITION_GUARD] Ignored attempt to downgrade payment {existing.external_payment_id} "
                f"from {existing.status} to {data.status}."
            )
            # Retain terminal status and captured flag, update auxiliary attributes
            existing.amount_refunded = data.amount_refunded or existing.amount_refunded
            existing.refund_status = data.refund_status or existing.refund_status
        else:
            existing.status = data.status
            existing.captured = data.captured or existing.captured
            if data.status == "CAPTURED" and not existing.paid_at:
                existing.paid_at = data.paid_at or datetime.now(timezone.utc)

        # Update metadata and gateway attributes
        existing.razorpay_payment_id = data.external_payment_id or existing.razorpay_payment_id
        existing.razorpay_order_id = data.razorpay_order_id or existing.razorpay_order_id
        existing.payment_method = data.payment_method or existing.payment_method
        existing.bank = data.bank or existing.bank
        existing.wallet = data.wallet or existing.wallet
        existing.vpa = data.vpa or existing.vpa
        existing.description = data.description or existing.description
        existing.failure_code = data.error_code or existing.failure_code
        existing.failure_description = data.error_description or existing.failure_description
        existing.error_source = data.error_source or existing.error_source
        existing.error_step = data.error_step or existing.error_step
        existing.error_reason = data.error_reason or existing.error_reason
        existing.raw_payload = data.raw_payload

        db.flush()
        logger.info(
            f"[PAYMENT_UPDATED] Updated payment {existing.id} ({data.external_payment_id}) "
            f"status={existing.status}"
        )
        return existing, False
