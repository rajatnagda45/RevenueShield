"""Unit tests for the OutcomeEngine, recovery calculation, and attribution logic."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import uuid
import pytest
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.recovery_action import RecoveryAction
from app.models.execution import RecoveryExecution
from app.outcomes.base import OutcomeType, AttributionType
from app.outcomes.attribution import AttributionClassifier
from app.outcomes.engine import OutcomeEngine


@pytest.fixture
def base_case_and_execution(db_session: Session):
    customer = Customer(
        name="Outcome User",
        email="outcome@example.com",
        phone="+919876500099",
    )
    db_session.add(customer)
    db_session.flush()

    event = Event(
        external_event_id=f"evt_out_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        customer_id=customer.id,
        payload={},
        occurred_at=datetime.now(timezone.utc),
    )
    db_session.add(event)
    db_session.flush()

    case = RecoveryCase(
        customer_id=customer.id,
        event_id=event.id,
        amount_at_risk=Decimal("1000.00"),
        case_type="PAYMENT_FAILURE",
        status="OPEN",
    )
    db_session.add(case)
    db_session.flush()

    action = RecoveryAction(
        recovery_case_id=case.id,
        action_type="SEND_PAYMENT_LINK",
        channel="EMAIL",
        status="EXECUTED",
    )
    db_session.add(action)
    db_session.flush()

    now = datetime.now(timezone.utc)
    execution = RecoveryExecution(
        recovery_case_id=case.id,
        recovery_action_id=action.id,
        action_type="SEND_PAYMENT_LINK",
        provider="DRY_RUN",
        status="SUCCEEDED",
        idempotency_key=f"key_out_{uuid.uuid4()}",
        completed_at=now - timedelta(hours=2),
    )
    db_session.add(execution)
    db_session.flush()

    return case, action, execution


def test_full_payment_capture_creates_recovered_outcome(db_session: Session, base_case_and_execution):
    """Verify that full payment capture creates RECOVERED outcome with 100% recovery and DIRECT attribution."""
    case, action, execution = base_case_and_execution
    captured_at = datetime.now(timezone.utc)

    outcome = OutcomeEngine.process_payment_capture(
        db=db_session,
        recovery_case=case,
        captured_amount=Decimal("1000.00"),
        captured_at=captured_at,
        provider_event_id="evt_cap_test_01",
    )

    assert outcome.outcome_type == OutcomeType.RECOVERED.value
    assert outcome.amount_recovered == Decimal("1000.00")
    assert outcome.recovery_percentage == 100.0
    assert outcome.attribution == AttributionType.DIRECT.value
    assert outcome.time_to_recovery_seconds is not None
    assert 7100.0 <= outcome.time_to_recovery_seconds <= 7300.0  # Approx 2 hours = 7200s
    assert case.status == "RECOVERED"
    assert case.recovered_amount == Decimal("1000.00")


def test_partial_payment_capture_creates_partially_recovered_outcome(db_session: Session, base_case_and_execution):
    """Verify that partial payment capture computes correct recovery percentage (e.g. ₹600 of ₹1000 = 60%)."""
    case, action, execution = base_case_and_execution

    outcome = OutcomeEngine.process_payment_capture(
        db=db_session,
        recovery_case=case,
        captured_amount=Decimal("600.00"),
        captured_at=datetime.now(timezone.utc),
    )

    assert outcome.outcome_type == OutcomeType.PARTIALLY_RECOVERED.value
    assert outcome.amount_recovered == Decimal("600.00")
    assert outcome.recovery_percentage == 60.0
    assert case.status == "RECOVERED"
    assert case.recovered_amount == Decimal("600.00")


def test_payment_captured_without_execution_classified_as_organic(db_session: Session):
    """Verify that payment capture without prior executed action is classified as ORGANIC."""
    customer = Customer(name="Organic User", email="organic@example.com")
    db_session.add(customer)
    db_session.flush()

    event = Event(
        external_event_id=f"evt_org_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        customer_id=customer.id,
        payload={},
        occurred_at=datetime.now(timezone.utc),
    )
    db_session.add(event)
    db_session.flush()

    case = RecoveryCase(
        customer_id=customer.id,
        event_id=event.id,
        amount_at_risk=Decimal("500.00"),
        case_type="PAYMENT_FAILURE",
        status="OPEN",
    )
    db_session.add(case)
    db_session.flush()

    outcome = OutcomeEngine.process_payment_capture(
        db=db_session,
        recovery_case=case,
        captured_amount=Decimal("500.00"),
        captured_at=datetime.now(timezone.utc),
    )

    assert outcome.outcome_type == OutcomeType.RECOVERED.value
    assert outcome.attribution == AttributionType.ORGANIC.value
    assert outcome.time_to_recovery_seconds is None


def test_negative_captured_amount_raises_error(db_session: Session, base_case_and_execution):
    """Verify that negative captured amounts are rejected."""
    case, _, _ = base_case_and_execution

    with pytest.raises(ValueError, match="Captured amount cannot be negative"):
        OutcomeEngine.process_payment_capture(
            db=db_session,
            recovery_case=case,
            captured_amount=Decimal("-100.00"),
        )
