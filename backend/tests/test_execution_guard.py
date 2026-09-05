"""Unit tests for the ExecutionGuard pre-flight safety layer."""
from datetime import datetime, timezone
from decimal import Decimal
import uuid
import pytest
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.recovery_action import RecoveryAction
from app.models.execution import RecoveryExecution
from app.models.promise_to_pay import PromiseToPay
from app.execution.guard import ExecutionGuard


@pytest.fixture
def sample_case_and_action(db_session: Session):
    customer = Customer(
        name="Guard User",
        email="guard@example.com",
        phone="+919876543210",
    )
    db_session.add(customer)
    db_session.flush()

    now = datetime.now(timezone.utc)
    event = Event(
        external_event_id=f"evt_guard_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        customer_id=customer.id,
        payload={},
        occurred_at=now,
    )
    db_session.add(event)
    db_session.flush()

    case = RecoveryCase(
        customer_id=customer.id,
        event_id=event.id,
        amount_at_risk=Decimal("1200.00"),
        case_type="PAYMENT_FAILURE",
        status="OPEN",
    )
    db_session.add(case)
    db_session.flush()

    action = RecoveryAction(
        recovery_case_id=case.id,
        action_type="SEND_PAYMENT_LINK",
        channel="EMAIL",
        status="APPROVED",
    )
    db_session.add(action)
    db_session.flush()

    return case, action


def test_guard_allows_valid_open_approved_case(db_session: Session, sample_case_and_action):
    """Verify that a valid OPEN case with APPROVED action passes all execution guard checks."""
    case, action = sample_case_and_action
    res = ExecutionGuard.validate_pre_flight(
        db=db_session,
        recovery_case=case,
        recovery_action=action,
        idempotency_key="key_valid_01",
    )
    assert res.allowed is True
    assert res.blocking_rule is None


def test_guard_blocks_when_case_is_recovered_or_closed(db_session: Session, sample_case_and_action):
    """Verify that guard blocks execution if case is already RECOVERED or CLOSED."""
    case, action = sample_case_and_action
    case.status = "RECOVERED"
    db_session.flush()

    res = ExecutionGuard.validate_pre_flight(
        db=db_session,
        recovery_case=case,
        recovery_action=action,
        idempotency_key="key_rec_01",
    )
    assert res.allowed is False
    assert res.blocking_rule == "CASE_INACTIVE"


def test_guard_blocks_when_payment_already_captured(db_session: Session, sample_case_and_action):
    """Verify that guard blocks execution if money is already recovered."""
    case, action = sample_case_and_action
    case.recovered_amount = Decimal("1200.00")
    db_session.flush()

    res = ExecutionGuard.validate_pre_flight(
        db=db_session,
        recovery_case=case,
        recovery_action=action,
        idempotency_key="key_cap_01",
    )
    assert res.allowed is False
    assert res.blocking_rule == "PAYMENT_ALREADY_CAPTURED"


def test_guard_blocks_when_action_is_not_approved(db_session: Session, sample_case_and_action):
    """Verify that guard blocks execution if action is in BLOCKED or FAILED state."""
    case, action = sample_case_and_action
    action.status = "BLOCKED"
    db_session.flush()

    res = ExecutionGuard.validate_pre_flight(
        db=db_session,
        recovery_case=case,
        recovery_action=action,
        idempotency_key="key_act_01",
    )
    assert res.allowed is False
    assert res.blocking_rule == "ACTION_NOT_APPROVED"


def test_guard_blocks_when_promise_to_pay_is_active(db_session: Session, sample_case_and_action):
    """Verify that active Promise-to-Pay blocks pre-flight execution."""
    case, action = sample_case_and_action

    ptp = PromiseToPay(
        recovery_case_id=case.id,
        customer_id=case.customer_id,
        promised_amount=Decimal("1200.00"),
        promised_date=case.created_at,
        status="ACTIVE",
    )
    db_session.add(ptp)
    db_session.flush()

    res = ExecutionGuard.validate_pre_flight(
        db=db_session,
        recovery_case=case,
        recovery_action=action,
        idempotency_key="key_ptp_01",
    )
    assert res.allowed is False
    assert res.blocking_rule == "PROMISE_TO_PAY_ACTIVE"


def test_guard_blocks_duplicate_successful_idempotency_key(db_session: Session, sample_case_and_action):
    """Verify that guard recognizes previously SUCCEEDED idempotency keys."""
    case, action = sample_case_and_action
    key = "key_dup_success_01"

    existing_exec = RecoveryExecution(
        recovery_case_id=case.id,
        recovery_action_id=action.id,
        action_type=action.action_type,
        provider="SIMULATED",
        status="SUCCEEDED",
        idempotency_key=key,
    )
    db_session.add(existing_exec)
    db_session.flush()

    res = ExecutionGuard.validate_pre_flight(
        db=db_session,
        recovery_case=case,
        recovery_action=action,
        idempotency_key=key,
    )
    assert res.allowed is False
    assert res.blocking_rule == "IDEMPOTENCY_ALREADY_SUCCEEDED"
