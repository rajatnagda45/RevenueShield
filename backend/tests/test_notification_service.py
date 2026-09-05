"""Unit tests for NotificationService, masking, and message generation."""
from decimal import Decimal
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.communication_log import CommunicationLog
from app.services.notification_service import (
    NotificationService,
    DevelopmentNotificationProvider,
    mask_contact,
)


def test_contact_masking_utility():
    """Verify privacy masking for phone numbers and email addresses."""
    assert mask_contact("+919876543210") == "+919*****3210"
    assert mask_contact("+14155552671") == "+141****2671"
    assert mask_contact("alice@example.com") == "a***e@example.com"
    assert mask_contact("me@co.in") == "m*@co.in"
    assert mask_contact("") == "[NOT_PROVIDED]"
    assert mask_contact(None) == "[NOT_PROVIDED]"


def test_notification_message_formatting_and_communication_log(db_session: Session):
    """Verify that notification contains clean customer copy and creates CommunicationLog."""
    cust = Customer(
        external_customer_id=f"cust_{uuid.uuid4()}",
        name="Priya Sharma",
        phone="+919876543210",
        email="priya@example.com",
    )
    db_session.add(cust)
    db_session.flush()

    evt = Event(
        external_event_id=f"evt_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        customer_id=cust.id,
        processing_status="PROCESSED",
    )
    db_session.add(evt)
    db_session.flush()

    case = RecoveryCase(
        customer_id=cust.id,
        event_id=evt.id,
        amount_at_risk=Decimal("4500.00"),
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status="OPEN",
    )
    db_session.add(case)
    db_session.commit()

    # Dispatch notification
    result = NotificationService.send_recovery_notification(
        db=db_session,
        recovery_case_id=case.id,
        customer_id=cust.id,
        recipient=cust.phone,
        amount=Decimal("4500.00"),
        currency="INR",
        payment_url="https://rzp.io/i/plink_test_123",
        channel="WHATSAPP",
    )

    assert result.status == "SENT"
    assert result.channel == "WHATSAPP"
    assert result.recipient_masked == "+919*****3210"
    assert "₹4,500.00" in result.message_content
    assert "https://rzp.io/i/plink_test_123" in result.message_content

    # Strict check: No AI internal diagnosis leakage
    assert "risk_score" not in result.message_content
    assert "prediction" not in result.message_content
    assert "diagnosis" not in result.message_content

    # Check CommunicationLog in DB
    comm_log = db_session.scalar(
        select(CommunicationLog).where(CommunicationLog.recovery_case_id == case.id)
    )
    assert comm_log is not None
    assert comm_log.channel == "WHATSAPP"
    assert comm_log.direction == "OUTBOUND"
    assert comm_log.status == "SENT"
    assert comm_log.provider_message_id == result.provider_message_id
