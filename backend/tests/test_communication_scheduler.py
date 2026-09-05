"""Unit tests for CommunicationScheduler policy rules: DND, cooldown, attempt limits, consent, and Promise-to-Pay."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import uuid
from zoneinfo import ZoneInfo
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.communication import Communication
from app.models.promise_to_pay import PromiseToPay
from app.services.communication_scheduler import CommunicationScheduler, PolicyCheckResult


def _create_test_case(db: Session, phone: str = "+919876543210", whatsapp_allowed: bool = True, dnd_enabled: bool = False) -> RecoveryCase:
    cust = Customer(
        external_customer_id=f"cust_{uuid.uuid4()}",
        name="Ankit Kumar",
        phone=phone,
        email="ankit@example.com",
        whatsapp_allowed=whatsapp_allowed,
        dnd_enabled=dnd_enabled,
        timezone="Asia/Kolkata",
    )
    db.add(cust)
    db.flush()

    evt = Event(
        external_event_id=f"evt_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        customer_id=cust.id,
        processing_status="PROCESSED",
    )
    db.add(evt)
    db.flush()

    case = RecoveryCase(
        customer_id=cust.id,
        event_id=evt.id,
        amount_at_risk=Decimal("5000.00"),
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status="OPEN",
    )
    db.add(case)
    db.commit()
    return case


def test_policy_allows_eligible_case_during_business_hours(db_session: Session):
    """Verify policy approval during daytime business hours."""
    case = _create_test_case(db_session)
    # 14:00 IST (2:00 PM) -> 08:30 UTC
    daytime = datetime(2026, 8, 22, 8, 30, tzinfo=timezone.utc)

    res: PolicyCheckResult = CommunicationScheduler.evaluate_outreach_policy(
        db=db_session, recovery_case=case, reference_time=daytime
    )
    assert res.allowed is True
    assert res.blocking_rule is None


def test_policy_blocks_during_quiet_hours(db_session: Session):
    """Verify policy blocks WhatsApp outreach during quiet hours (20:00 - 08:00 IST)."""
    case = _create_test_case(db_session)
    # 22:00 IST (10:00 PM) -> 16:30 UTC
    night_time = datetime(2026, 8, 22, 16, 30, tzinfo=timezone.utc)

    res: PolicyCheckResult = CommunicationScheduler.evaluate_outreach_policy(
        db=db_session, recovery_case=case, reference_time=night_time
    )
    assert res.allowed is False
    assert res.blocking_rule == "QUIET_HOURS_DND_PROHIBITED"
    assert "Quiet hours" in res.reason


def test_policy_blocks_when_customer_opted_out(db_session: Session):
    """Verify policy blocks outreach if customer opted out of WhatsApp."""
    case = _create_test_case(db_session, whatsapp_allowed=False)
    daytime = datetime(2026, 8, 22, 8, 30, tzinfo=timezone.utc)

    res: PolicyCheckResult = CommunicationScheduler.evaluate_outreach_policy(
        db=db_session, recovery_case=case, reference_time=daytime
    )
    assert res.allowed is False
    assert res.blocking_rule == "WHATSAPP_OPT_OUT"


def test_policy_blocks_when_active_promise_to_pay(db_session: Session):
    """Verify policy pauses outreach when an active Promise-to-Pay exists."""
    case = _create_test_case(db_session)
    daytime = datetime(2026, 8, 22, 8, 30, tzinfo=timezone.utc)

    # Create active PromiseToPay
    ptp = PromiseToPay(
        customer_id=case.customer_id,
        recovery_case_id=case.id,
        status="ACTIVE",
        promised_amount=Decimal("5000.00"),
        promised_date=daytime + timedelta(days=3),
    )
    db_session.add(ptp)
    db_session.commit()

    res: PolicyCheckResult = CommunicationScheduler.evaluate_outreach_policy(
        db=db_session, recovery_case=case, reference_time=daytime
    )
    assert res.allowed is False
    assert res.blocking_rule == "PROMISE_TO_PAY_ACTIVE"


def test_policy_blocks_when_cooldown_active(db_session: Session):
    """Verify policy blocks repeated outreach within the 24-hour cooldown period."""
    case = _create_test_case(db_session)
    daytime_1 = datetime(2026, 8, 22, 8, 30, tzinfo=timezone.utc)

    # Record first communication
    comm = Communication(
        recovery_case_id=case.id,
        customer_id=case.customer_id,
        channel="WHATSAPP",
        recipient_reference="+919876543210",
        recipient_masked="+919*****3210",
        message_body="First message",
        status="SENT",
        idempotency_key=f"comm_{case.id}_WHATSAPP_1",
        attempt_number=1,
        sent_at=daytime_1,
        created_at=daytime_1,
    )
    db_session.add(comm)
    db_session.commit()

    # Attempt second outreach 2 hours later
    daytime_2 = daytime_1 + timedelta(hours=2)
    res: PolicyCheckResult = CommunicationScheduler.evaluate_outreach_policy(
        db=db_session, recovery_case=case, reference_time=daytime_2
    )
    assert res.allowed is False
    assert res.blocking_rule == "COOLDOWN_PERIOD_ACTIVE"
    assert "Cooldown active" in res.reason


def test_policy_blocks_when_max_attempts_exceeded(db_session: Session):
    """Verify policy blocks fourth attempt when MAX_WHATSAPP_ATTEMPTS (3) is reached."""
    case = _create_test_case(db_session)
    base_time = datetime(2026, 8, 15, 8, 30, tzinfo=timezone.utc)

    # Record 3 prior communications spaced out past cooldown
    for i in range(1, 4):
        t = base_time + timedelta(days=i)
        comm = Communication(
            recovery_case_id=case.id,
            customer_id=case.customer_id,
            channel="WHATSAPP",
            recipient_reference="+919876543210",
            recipient_masked="+919*****3210",
            message_body=f"Attempt {i}",
            status="SENT",
            idempotency_key=f"comm_{case.id}_WHATSAPP_{i}",
            attempt_number=i,
            sent_at=t,
            created_at=t,
        )
        db_session.add(comm)
    db_session.commit()

    # Attempt 4th outreach on day 4
    daytime_4 = base_time + timedelta(days=4)
    res: PolicyCheckResult = CommunicationScheduler.evaluate_outreach_policy(
        db=db_session, recovery_case=case, reference_time=daytime_4
    )
    assert res.allowed is False
    assert res.blocking_rule == "MAX_WHATSAPP_ATTEMPTS_EXCEEDED"
    assert res.attempt_count == 3
