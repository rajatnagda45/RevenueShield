"""Unit tests for PaymentRepository idempotency and state transition guards."""
from datetime import datetime, timezone
from decimal import Decimal
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.integrations.razorpay.payment_normalizer import NormalizedPaymentData
from app.repositories.payment_repository import PaymentRepository


def test_upsert_payment_creates_new_record_and_customer(db_session: Session):
    """Verify that upserting a non-existent payment creates both Customer and Payment rows."""
    norm = NormalizedPaymentData(
        external_payment_id="pay_repo_001",
        razorpay_order_id="order_repo_001",
        amount=Decimal("1500.00"),
        currency="INR",
        status="CAPTURED",
        payment_method="CARD",
        bank="ICICI",
        wallet=None,
        vpa=None,
        international=False,
        captured=True,
        amount_refunded=Decimal("0.00"),
        refund_status=None,
        description="Subscription payment",
        error_code=None,
        error_description=None,
        error_source=None,
        error_step=None,
        error_reason=None,
        paid_at=datetime.now(timezone.utc),
        razorpay_created_at=datetime.now(timezone.utc),
        customer_id_ext="cust_ext_99",
        customer_email="sharma@example.com",
        customer_phone="+919876500011",
        customer_name="Sharma Ji",
        raw_payload={"id": "pay_repo_001"},
    )

    payment, is_created = PaymentRepository.upsert_payment(db_session, norm)

    assert is_created is True
    assert payment.external_payment_id == "pay_repo_001"
    assert payment.status == "CAPTURED"
    assert payment.amount == Decimal("1500.00")
    assert payment.customer is not None
    assert payment.customer.email == "sharma@example.com"


def test_upsert_existing_payment_updates_metadata_without_duplicate(db_session: Session):
    """Verify that calling upsert on existing payment updates fields without creating a duplicate row."""
    norm1 = NormalizedPaymentData(
        external_payment_id="pay_repo_002",
        razorpay_order_id=None,
        amount=Decimal("800.00"),
        currency="INR",
        status="AUTHORIZED",
        payment_method="UPI",
        bank=None,
        wallet=None,
        vpa="test@upi",
        international=False,
        captured=False,
        amount_refunded=Decimal("0.00"),
        refund_status=None,
        description=None,
        error_code=None,
        error_description=None,
        error_source=None,
        error_step=None,
        error_reason=None,
        paid_at=None,
        razorpay_created_at=datetime.now(timezone.utc),
        customer_id_ext=None,
        customer_email="user02@example.com",
        customer_phone=None,
        customer_name="User 02",
        raw_payload={"id": "pay_repo_002"},
    )

    pay1, created1 = PaymentRepository.upsert_payment(db_session, norm1)
    assert created1 is True

    # Now simulate payment captured update
    norm2 = NormalizedPaymentData(
        external_payment_id="pay_repo_002",
        razorpay_order_id="order_repo_002",
        amount=Decimal("800.00"),
        currency="INR",
        status="CAPTURED",
        payment_method="UPI",
        bank=None,
        wallet=None,
        vpa="test@upi",
        international=False,
        captured=True,
        amount_refunded=Decimal("0.00"),
        refund_status=None,
        description="Captured UPI",
        error_code=None,
        error_description=None,
        error_source=None,
        error_step=None,
        error_reason=None,
        paid_at=datetime.now(timezone.utc),
        razorpay_created_at=datetime.now(timezone.utc),
        customer_id_ext=None,
        customer_email="user02@example.com",
        customer_phone=None,
        customer_name="User 02",
        raw_payload={"id": "pay_repo_002", "status": "captured"},
    )

    pay2, created2 = PaymentRepository.upsert_payment(db_session, norm2)
    assert created2 is False
    assert pay2.id == pay1.id
    assert pay2.status == "CAPTURED"
    assert pay2.captured is True
    assert pay2.razorpay_order_id == "order_repo_002"

    # Verify count in DB is exactly 1
    total = db_session.scalars(select(Payment).where(Payment.external_payment_id == "pay_repo_002")).all()
    assert len(total) == 1


def test_state_transition_guard_prevents_downgrade_from_captured_to_failed(db_session: Session):
    """Verify that late arrival of a FAILED webhook cannot downgrade a CAPTURED payment."""
    norm_captured = NormalizedPaymentData(
        external_payment_id="pay_repo_003",
        razorpay_order_id="order_003",
        amount=Decimal("2000.00"),
        currency="INR",
        status="CAPTURED",
        payment_method="CARD",
        bank="SBI",
        wallet=None,
        vpa=None,
        international=False,
        captured=True,
        amount_refunded=Decimal("0.00"),
        refund_status=None,
        description=None,
        error_code=None,
        error_description=None,
        error_source=None,
        error_step=None,
        error_reason=None,
        paid_at=datetime.now(timezone.utc),
        razorpay_created_at=datetime.now(timezone.utc),
        customer_id_ext=None,
        customer_email="user03@example.com",
        customer_phone=None,
        customer_name="User 03",
        raw_payload={"id": "pay_repo_003", "status": "captured"},
    )
    payment, _ = PaymentRepository.upsert_payment(db_session, norm_captured)
    assert payment.status == "CAPTURED"

    # Late arrival of FAILED payload
    norm_failed = NormalizedPaymentData(
        external_payment_id="pay_repo_003",
        razorpay_order_id="order_003",
        amount=Decimal("2000.00"),
        currency="INR",
        status="FAILED",
        payment_method="CARD",
        bank="SBI",
        wallet=None,
        vpa=None,
        international=False,
        captured=False,
        amount_refunded=Decimal("0.00"),
        refund_status=None,
        description=None,
        error_code="BAD_REQUEST_ERROR",
        error_description="Card declined",
        error_source="customer",
        error_step="payment_authorization",
        error_reason="incorrect_otp",
        paid_at=None,
        razorpay_created_at=datetime.now(timezone.utc),
        customer_id_ext=None,
        customer_email="user03@example.com",
        customer_phone=None,
        customer_name="User 03",
        raw_payload={"id": "pay_repo_003", "status": "failed"},
    )
    updated_payment, is_new = PaymentRepository.upsert_payment(db_session, norm_failed)

    assert is_new is False
    # CRITICAL: Status must remain CAPTURED!
    assert updated_payment.status == "CAPTURED"
    assert updated_payment.captured is True
