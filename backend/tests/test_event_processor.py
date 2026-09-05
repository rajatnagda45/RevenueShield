"""Integration tests for EventProcessor service and RecoveryCase lifecycle transitions."""
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.payment import Payment
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.audit_log import AuditLog
from app.schemas.event import NormalizedEvent
from app.services.event_processor import EventProcessor


def test_payment_failed_opens_recovery_case(db_session: Session):
    """Verify that a payment.failed event opens a RecoveryCase with amount_at_risk and creates an AuditLog."""
    event = NormalizedEvent(
        event_id="evt_test_failed_001",
        event_type="payment.failed",
        source="RAZORPAY",
        amount=Decimal("499.00"),
        currency="INR",
        external_customer_id="cust_corp_01",
        customer_email="finance@corp01.com",
        customer_name="Corp 01",
        external_payment_id="pay_fail_001",
        payment_method="CARD",
        payment_status="FAILED",
        failure_code="INSUFFICIENT_FUNDS",
        failure_description="Transaction declined due to insufficient balance",
        raw_payload={"mock": "data", "event_id": "evt_test_failed_001"},
    )

    result = EventProcessor.process_normalized_event(db_session, event)

    assert result.status == "processed"
    assert result.event_id == "evt_test_failed_001"
    assert result.recovery_case_id is not None

    # Verify Database State
    db_event = db_session.scalar(select(Event).where(Event.external_event_id == "evt_test_failed_001"))
    assert db_event is not None
    assert db_event.event_type == "payment.failed"
    assert db_event.payload == {"mock": "data", "event_id": "evt_test_failed_001"}

    db_case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == result.recovery_case_id))
    assert db_case is not None
    assert db_case.status == "OPEN"
    assert db_case.case_type == "PAYMENT_FAILURE"
    assert db_case.amount_at_risk == Decimal("499.00")
    assert db_case.currency == "INR"

    # Verify AuditLog
    db_audit = db_session.scalar(select(AuditLog).where(AuditLog.recovery_case_id == db_case.id))
    assert db_audit is not None
    assert db_audit.action == "RECOVERY_CASE_OPENED"
    assert db_audit.actor_type == "SYSTEM"


def test_payment_captured_recovers_open_recovery_case(db_session: Session):
    """Verify that a subsequent payment.captured event marks the open RecoveryCase as RECOVERED."""
    # 1. First ingest failure event
    fail_event = NormalizedEvent(
        event_id="evt_test_failed_002",
        event_type="payment.failed",
        source="RAZORPAY",
        amount=Decimal("1500.00"),
        currency="INR",
        external_customer_id="cust_corp_02",
        customer_email="billing@corp02.com",
        customer_name="Corp 02",
        external_payment_id="pay_flow_002",
        payment_method="CARD",
        payment_status="FAILED",
        failure_code="CARD_DECLINED",
        failure_description="Do not honor",
    )
    fail_result = EventProcessor.process_normalized_event(db_session, fail_event)
    case_id = fail_result.recovery_case_id

    # 2. Later ingest capture event for the same payment
    cap_event = NormalizedEvent(
        event_id="evt_test_captured_002",
        event_type="payment.captured",
        source="RAZORPAY",
        amount=Decimal("1500.00"),
        currency="INR",
        external_customer_id="cust_corp_02",
        customer_email="billing@corp02.com",
        customer_name="Corp 02",
        external_payment_id="pay_flow_002",
        payment_method="CARD",
        payment_status="SUCCESS",
    )
    cap_result = EventProcessor.process_normalized_event(db_session, cap_event)

    assert cap_result.status == "processed"
    assert cap_result.recovery_case_id == case_id

    # Verify RecoveryCase is now RECOVERED
    recovered_case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    assert recovered_case.status == "RECOVERED"
    assert recovered_case.recovered_amount == Decimal("1500.00")
    assert recovered_case.closed_at is not None

    # Verify Payment is marked CAPTURED
    payment = db_session.scalar(select(Payment).where(Payment.external_payment_id == "pay_flow_002"))
    assert payment.status in ("SUCCESS", "CAPTURED")

    # Verify recovery audit log exists
    audits = db_session.scalars(select(AuditLog).where(AuditLog.recovery_case_id == case_id)).all()
    actions = [a.action for a in audits]
    assert "RECOVERY_CASE_OPENED" in actions
    assert "RECOVERY_CASE_RECOVERED" in actions


def test_idempotent_duplicate_event_handling(db_session: Session):
    """Verify that submitting the same event_id twice returns status='duplicate' and prevents duplicates."""
    event = NormalizedEvent(
        event_id="evt_duplicate_id_999",
        event_type="payment.failed",
        source="RAZORPAY",
        amount=Decimal("300.00"),
        currency="INR",
        customer_email="user999@test.com",
        external_payment_id="pay_dup_999",
    )

    # First delivery
    res1 = EventProcessor.process_normalized_event(db_session, event)
    assert res1.status == "processed"

    # Second duplicate delivery
    res2 = EventProcessor.process_normalized_event(db_session, event)
    assert res2.status == "duplicate"
    assert res2.internal_event_id == res1.internal_event_id

    # Ensure only 1 Event record exists in database
    events = db_session.scalars(select(Event).where(Event.external_event_id == "evt_duplicate_id_999")).all()
    assert len(events) == 1

    # Ensure only 1 RecoveryCase was created
    cases = db_session.scalars(select(RecoveryCase).where(RecoveryCase.event_id == events[0].id)).all()
    assert len(cases) == 1
