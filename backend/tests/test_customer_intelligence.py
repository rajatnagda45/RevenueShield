"""Unit tests for CustomerIntelligenceService historical feature calculation."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import uuid
import pytest
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.payment import Payment
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.services.customer_intelligence import CustomerIntelligenceService


def test_customer_features_with_no_history(db_session: Session):
    """Verify that a new customer with zero payments produces correct default metrics."""
    customer = Customer(
        name="New Account",
        email="new@example.com",
        segment="STANDARD",
    )
    db_session.add(customer)
    db_session.flush()

    features = CustomerIntelligenceService.get_customer_features(db_session, customer.id)

    assert features.customer_id == str(customer.id)
    assert features.total_attempts == 0
    assert features.successful_count == 0
    assert features.failed_count == 0
    assert features.success_rate == 0.0
    assert features.avg_transaction_amount == 0.0
    assert features.previous_recovery_cases_count == 0
    assert features.previous_recovered_amount == 0.0
    assert features.consecutive_failures == 0


def test_customer_features_with_mixed_payments_and_recoveries(db_session: Session):
    """Verify accurate metrics calculation for a customer with successful, failed, and recovered transactions."""
    now = datetime.now(timezone.utc)

    customer = Customer(
        name="Enterprise Client",
        email="enterprise@client.com",
        segment="ENTERPRISE",
    )
    db_session.add(customer)
    db_session.flush()

    # 3 Successful payments (1 old, 2 recent)
    p1 = Payment(
        customer_id=customer.id,
        amount=Decimal("1000.00"),
        currency="USD",
        status="SUCCESS",
        created_at=now - timedelta(days=60),
    )
    p2 = Payment(
        customer_id=customer.id,
        amount=Decimal("1200.00"),
        currency="USD",
        status="SUCCESS",
        created_at=now - timedelta(days=15),
    )
    p3 = Payment(
        customer_id=customer.id,
        amount=Decimal("800.00"),
        currency="USD",
        status="SUCCESS",
        created_at=now - timedelta(days=5),
    )

    # 1 Failed payment (recent)
    p_fail = Payment(
        customer_id=customer.id,
        amount=Decimal("1000.00"),
        currency="USD",
        status="FAILED",
        failure_code="insufficient_funds",
        created_at=now - timedelta(hours=2),
    )

    db_session.add_all([p1, p2, p3, p_fail])
    db_session.flush()

    # Add 1 recovered case
    dummy_event = Event(
        external_event_id=f"evt_hist_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        customer_id=customer.id,
        payload={},
        occurred_at=now - timedelta(days=20),
    )
    db_session.add(dummy_event)
    db_session.flush()

    rec_case = RecoveryCase(
        customer_id=customer.id,
        event_id=dummy_event.id,
        amount_at_risk=Decimal("500.00"),
        case_type="PAYMENT_FAILURE",
        status="RECOVERED",
        recovered_amount=Decimal("500.00"),
        created_at=now - timedelta(days=20),
    )
    db_session.add(rec_case)
    db_session.flush()

    features = CustomerIntelligenceService.get_customer_features(db_session, customer.id)

    assert features.total_attempts == 4
    assert features.successful_count == 3
    assert features.failed_count == 1
    assert features.success_rate == 0.75  # 3 / 4
    assert features.recent_success_count == 2
    assert features.recent_failure_count == 1
    assert features.avg_transaction_amount == 1000.0  # (1000 + 1200 + 800 + 1000) / 4
    assert features.previous_recovery_cases_count == 1
    assert features.previous_recovered_amount == 500.0
    assert features.consecutive_failures == 1  # most recent was failure
