"""Unit tests for RecoveryMessageGenerator, English, and Hinglish templates."""
from decimal import Decimal
import uuid
import pytest
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.services.recovery_message_generator import RecoveryMessageGenerator, MessageDraft


def _create_test_case_for_msg(db_session: Session, name: str = "Vikram Aditya Singh", amount: Decimal = Decimal("5000.00")) -> RecoveryCase:
    cust = Customer(
        external_customer_id=f"cust_{uuid.uuid4()}",
        name=name,
        phone="+919876543210",
        email="user@example.com",
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
        amount_at_risk=amount,
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status="OPEN",
    )
    db_session.add(case)
    db_session.commit()
    return case


def test_english_template_with_customer_name(db_session: Session):
    """Verify English message template correctly personalizes with first name and formatted amount."""
    case = _create_test_case_for_msg(db_session, name="Vikram Aditya Singh", amount=Decimal("5000.00"))

    draft: MessageDraft = RecoveryMessageGenerator.generate(
        recovery_case=case,
        payment_link_url="https://rzp.io/i/plink_test_vikram",
        language="ENGLISH",
    )

    assert draft.language == "ENGLISH"
    assert draft.template_name == "PAYMENT_RECOVERY_EN_V1"
    assert draft.template_version == "v1.0"
    assert "Hi Vikram," in draft.message_body
    assert "₹5,000.00" in draft.message_body
    assert "https://rzp.io/i/plink_test_vikram" in draft.message_body
    assert draft.recipient_masked == "+919*****3210"

    # Anti-leakage guarantees
    assert "BAD_REQUEST_ERROR" not in draft.message_body
    assert "risk_score" not in draft.message_body
    assert "diagnosis" not in draft.message_body


def test_hinglish_template_with_customer_name(db_session: Session):
    """Verify Hinglish message template generates natural Hindi/English copy."""
    case = _create_test_case_for_msg(db_session, name="Pooja Sharma", amount=Decimal("7500.50"))

    draft: MessageDraft = RecoveryMessageGenerator.generate(
        recovery_case=case,
        payment_link_url="https://rzp.io/i/plink_test_pooja",
        language="HINGLISH",
    )

    assert draft.language == "HINGLISH"
    assert draft.template_name == "PAYMENT_RECOVERY_HI_V1"
    assert "Hi Pooja, aapka ₹7,500.50 ka payment complete nahi ho paya." in draft.message_body
    assert "Aap yahan se securely payment complete kar sakte hain: https://rzp.io/i/plink_test_pooja" in draft.message_body


def test_generic_template_when_name_missing(db_session: Session):
    """Verify generic fallback template when customer name is not available."""
    case = _create_test_case_for_msg(db_session, name="", amount=Decimal("3000.00"))

    draft: MessageDraft = RecoveryMessageGenerator.generate(
        recovery_case=case,
        payment_link_url="https://rzp.io/i/plink_test_anon",
        language="ENGLISH",
    )

    assert "Your payment of ₹3,000.00 could not be completed." in draft.message_body
    assert not draft.message_body.startswith("Hi")
