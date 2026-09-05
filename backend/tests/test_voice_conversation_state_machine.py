"""Comprehensive unit and integration test suite for Multi-State Voice Recovery Conversation."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional
from unittest.mock import MagicMock, patch
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.event import Event
from app.models.promise_to_pay import PromiseToPay
from app.models.recovery_case import RecoveryCase
from app.models.voice_call import VoiceCall
from app.services.voice_conversation_state import (
    ConversationIntent,
    ConversationSafetyGuard,
    ConversationState,
    StructuredIntentResult,
)
from app.services.voice_conversation_manager import VoiceConversationManager
from app.services.voice_intent_extractor import VoiceIntentExtractor
from app.services.voice_recovery_service import VoiceRecoveryService


def _create_test_case(
    db: Session,
    case_status: str = "OPEN",
    phone: Optional[str] = "+919876543210",
    name: str = "Pooja Sharma",
    amount: Decimal = Decimal("4999.00"),
) -> tuple[Customer, RecoveryCase]:
    cust_id = uuid.uuid4()
    cust = Customer(
        id=cust_id,
        external_customer_id=f"cust_test_{cust_id.hex[:8]}",
        name=name,
        email="pooja.sharma@example.com",
        phone=phone,
        dnd_enabled=False,
        timezone="Asia/Kolkata",
    )
    db.add(cust)
    db.flush()

    evt = Event(
        id=uuid.uuid4(),
        external_event_id=f"evt_{uuid.uuid4().hex[:12]}",
        event_type="payment.failed",
        source="RAZORPAY",
        payload={},
    )
    db.add(evt)
    db.flush()

    case = RecoveryCase(
        id=uuid.uuid4(),
        customer_id=cust.id,
        event_id=evt.id,
        amount_at_risk=amount,
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status=case_status,
    )
    db.add(case)
    db.commit()
    db.refresh(cust)
    db.refresh(case)
    return cust, case


def _create_test_call(
    db: Session,
    case: RecoveryCase,
    cust: Customer,
    initial_state: ConversationState = ConversationState.PAYMENT_STATUS,
) -> VoiceCall:
    call_id = uuid.uuid4()
    voice_call = VoiceCall(
        id=call_id,
        recovery_case_id=case.id,
        customer_id=cust.id,
        provider="TWILIO",
        provider_call_id=f"CA_{call_id.hex[:16]}",
        from_number="+17372212163",
        to_number="+919876543210",
        status="IN_PROGRESS",
        attempt_number=1,
        call_metadata={
            "conversation_state": initial_state.value,
            "clarification_attempts": 0,
        },
    )
    db.add(voice_call)
    db.commit()
    db.refresh(voice_call)
    return voice_call


# 1. PAY_NOW test
def test_1_pay_now_path(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    voice_call = _create_test_call(db_session, case, cust)

    with patch("app.services.email_recovery_service.EmailRecoveryService.execute_recovery") as mock_email:
        res = client.post(
            f"/webhooks/twilio/voice/{voice_call.id}/gather",
            data={"SpeechResult": "I can pay now please send me the link", "Confidence": "0.95"},
        )
        assert res.status_code == 200
        assert "send you a secure payment link" in res.text
        assert "<Hangup/>" in res.text

        db_session.refresh(voice_call)
        assert voice_call.call_metadata.get("conversation_state") == ConversationState.COMPLETED.value


# 2. PROMISE_TO_PAY + Confirmation prompt test
def test_2_promise_to_pay_prompts_confirmation(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    voice_call = _create_test_call(db_session, case, cust)

    # Reference Wednesday Aug 26, 2026
    res = client.post(
        f"/webhooks/twilio/voice/{voice_call.id}/gather",
        data={"SpeechResult": "I will pay next monday", "Confidence": "0.92"},
    )
    assert res.status_code == 200
    assert "Just to confirm, you will make the Rupees 4,999.00 payment on Monday" in res.text
    assert "<Gather" in res.text

    db_session.refresh(voice_call)
    assert voice_call.call_metadata.get("conversation_state") == ConversationState.PROMISE_CONFIRMATION.value
    assert "pending_ptp" in voice_call.call_metadata

    # Assert PTP is NOT yet created in DB
    ptp = db_session.scalar(select(PromiseToPay).where(PromiseToPay.recovery_case_id == case.id))
    assert ptp is None


# 3. PROMISE_CONFIRMATION with "yes" -> PTP committed
def test_3_promise_confirmation_yes_creates_ptp(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    voice_call = _create_test_call(db_session, case, cust, initial_state=ConversationState.PROMISE_CONFIRMATION)

    # Set pending_ptp in metadata
    metadata = dict(voice_call.call_metadata or {})
    future_date = datetime.now(timezone.utc) + timedelta(days=5)
    metadata["pending_ptp"] = {
        "promised_date": future_date.isoformat(),
        "promised_display": "Monday, August 31, 2026",
    }
    voice_call.call_metadata = metadata
    db_session.commit()

    res = client.post(
        f"/webhooks/twilio/voice/{voice_call.id}/gather",
        data={"SpeechResult": "Yes that is correct", "Confidence": "0.96"},
    )
    assert res.status_code == 200
    assert "Your payment commitment has been recorded" in res.text
    assert "<Hangup/>" in res.text

    # Assert PTP is now created in DB
    ptp = db_session.scalar(select(PromiseToPay).where(PromiseToPay.recovery_case_id == case.id))
    assert ptp is not None
    assert ptp.status == "ACTIVE"
    assert ptp.source == "VOICE_ASSISTANT"


# 4. PROMISE_CONFIRMATION with "no" -> re-prompts for date
def test_4_promise_confirmation_no_reprompts(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    voice_call = _create_test_call(db_session, case, cust, initial_state=ConversationState.PROMISE_CONFIRMATION)

    res = client.post(
        f"/webhooks/twilio/voice/{voice_call.id}/gather",
        data={"SpeechResult": "No not that date", "Confidence": "0.92"},
    )
    assert res.status_code == 200
    assert "When would you be able to complete the payment?" in res.text
    assert "<Gather" in res.text

    db_session.refresh(voice_call)
    assert voice_call.call_metadata.get("conversation_state") == ConversationState.PROMISE_TO_PAY.value


# 5. Ambiguous date clarification
def test_5_ambiguous_date_reprompts(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    voice_call = _create_test_call(db_session, case, cust)

    res = client.post(
        f"/webhooks/twilio/voice/{voice_call.id}/gather",
        data={"SpeechResult": "I can pay later next month", "Confidence": "0.85"},
    )
    assert res.status_code == 200
    assert "When would you be able to complete the payment?" in res.text


# 6. ALREADY_PAID + case is RECOVERED
def test_6_already_paid_confirmed_when_recovered(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="RECOVERED")
    voice_call = _create_test_call(db_session, case, cust)

    res = client.post(
        f"/webhooks/twilio/voice/{voice_call.id}/gather",
        data={"SpeechResult": "I already paid yesterday", "Confidence": "0.93"},
    )
    assert res.status_code == 200
    assert "We have confirmed your payment. No further action is required." in res.text
    assert "<Hangup/>" in res.text


# 7. ALREADY_PAID + case is OPEN (unconfirmed)
def test_7_already_paid_unconfirmed_when_open(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    voice_call = _create_test_call(db_session, case, cust)

    res = client.post(
        f"/webhooks/twilio/voice/{voice_call.id}/gather",
        data={"SpeechResult": "I already made the payment", "Confidence": "0.93"},
    )
    assert res.status_code == 200
    assert "I cannot confirm the payment yet. We will verify the transaction" in res.text
    assert "<Hangup/>" in res.text
    # Case must NOT be marked recovered
    db_session.refresh(case)
    assert case.status == "OPEN"


# 8. REFUSAL_TO_PAY polite exit
def test_8_refusal_to_pay_polite_exit(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    voice_call = _create_test_call(db_session, case, cust)

    res = client.post(
        f"/webhooks/twilio/voice/{voice_call.id}/gather",
        data={"SpeechResult": "I will not pay this amount", "Confidence": "0.95"},
    )
    assert res.status_code == 200
    assert "I understand. I won't pressure you. We can stop here." in res.text
    assert "<Hangup/>" in res.text

    audit = db_session.scalar(
        select(AuditLog).where(
            AuditLog.recovery_case_id == case.id,
            AuditLog.action == "VOICE_CUSTOMER_REFUSAL_TO_PAY",
        )
    )
    assert audit is not None


# 9. DISPUTE logging
def test_9_dispute_logged_gracefully(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    voice_call = _create_test_call(db_session, case, cust)

    res = client.post(
        f"/webhooks/twilio/voice/{voice_call.id}/gather",
        data={"SpeechResult": "I dispute this bill I never ordered this", "Confidence": "0.92"},
    )
    assert res.status_code == 200
    assert "record this as a payment dispute so it can be reviewed" in res.text
    assert "<Hangup/>" in res.text


# 10. WRONG_NUMBER privacy protection
def test_10_wrong_number_reported(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    voice_call = _create_test_call(db_session, case, cust)

    res = client.post(
        f"/webhooks/twilio/voice/{voice_call.id}/gather",
        data={"SpeechResult": "Wrong number who is this", "Confidence": "0.91"},
    )
    assert res.status_code == 200
    assert "this number may not belong to the intended customer" in res.text
    assert "<Hangup/>" in res.text


# 11. HUMAN_REQUEST logging
def test_11_human_request_logged(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    voice_call = _create_test_call(db_session, case, cust)

    res = client.post(
        f"/webhooks/twilio/voice/{voice_call.id}/gather",
        data={"SpeechResult": "Can I talk to a human customer care agent please", "Confidence": "0.94"},
    )
    assert res.status_code == 200
    assert "record your request for human assistance" in res.text
    assert "<Hangup/>" in res.text

    audit = db_session.scalar(
        select(AuditLog).where(
            AuditLog.recovery_case_id == case.id,
            AuditLog.action == "HUMAN_REQUESTED",
        )
    )
    assert audit is not None


# 12. UNKNOWN 1st attempt asks clarification
def test_12_unknown_first_attempt_clarifies(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    voice_call = _create_test_call(db_session, case, cust)

    res = client.post(
        f"/webhooks/twilio/voice/{voice_call.id}/gather",
        data={"SpeechResult": "blabla whispering noise", "Confidence": "0.3"},
    )
    assert res.status_code == 200
    assert "I didn't quite catch that. Are you able to make the payment today" in res.text
    assert "<Gather" in res.text

    db_session.refresh(voice_call)
    assert voice_call.call_metadata.get("clarification_attempts") == 1


# 13. UNKNOWN 2nd attempt cutoff
def test_13_unknown_second_attempt_cutoff(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    voice_call = _create_test_call(db_session, case, cust)

    # Set clarification_attempts = 1
    metadata = dict(voice_call.call_metadata or {})
    metadata["clarification_attempts"] = 1
    voice_call.call_metadata = metadata
    db_session.commit()

    res = client.post(
        f"/webhooks/twilio/voice/{voice_call.id}/gather",
        data={"SpeechResult": "still incomprehensible background noise", "Confidence": "0.2"},
    )
    assert res.status_code == 200
    assert "I don't want to misunderstand you. We'll stop here and arrange follow-up" in res.text
    assert "<Hangup/>" in res.text

    db_session.refresh(voice_call)
    assert voice_call.call_metadata.get("conversation_state") == ConversationState.COMPLETED.value


# 14. Deterministic NLP fallback
def test_14_deterministic_fallback():
    res = VoiceIntentExtractor.classify_intent("I will pay tomorrow evening")
    assert res.intent == ConversationIntent.PROMISE_TO_PAY
    assert res.promised_date is not None


# 15. Invalid LLM JSON resilience
def test_15_invalid_llm_json_resilience():
    # Parsing malformed dict returns UNKNOWN without raising
    res = StructuredIntentResult.from_dict({"intent": "NON_EXISTENT_INTENT", "confidence": "invalid"})
    assert res.intent == ConversationIntent.UNKNOWN
    assert res.confidence == 0.0


# 16. Safety Filter credential protection
def test_16_safety_filter_sanitizes_credentials():
    bad_prompt = "Please tell me your credit card number and OTP to proceed."
    sanitized = ConversationSafetyGuard.sanitize_speech_output(bad_prompt)
    assert "credit card" not in sanitized
    assert "OTP" not in sanitized
    assert "secure payment link" in sanitized


# 17. Duplicate PTP prevention
def test_17_duplicate_ptp_prevention(db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    from app.services.promise_to_pay_service import PromiseToPayService

    future1 = datetime.now(timezone.utc) + timedelta(days=2)
    p1 = PromiseToPayService.create_promise(
        db=db_session,
        recovery_case_id=case.id,
        promised_amount=Decimal("4999.00"),
        promised_date=future1,
        source="VOICE_ASSISTANT",
    )
    assert p1.status == "ACTIVE"

    # Create 2nd promise for same case
    future2 = datetime.now(timezone.utc) + timedelta(days=5)
    p2 = PromiseToPayService.create_promise(
        db=db_session,
        recovery_case_id=case.id,
        promised_amount=Decimal("4999.00"),
        promised_date=future2,
        source="VOICE_ASSISTANT",
    )
    assert p2.status == "ACTIVE"

    # p1 should now be superseded/cancelled
    db_session.refresh(p1)
    assert p1.status in ["SUPERSEDED", "CANCELLED", "INACTIVE"]


# 18. Existing PTP stopping rule blocks new voice call
def test_18_existing_ptp_blocks_voice_call(client: TestClient, db_session: Session):
    cust, case = _create_test_case(db_session, case_status="OPEN")
    from app.services.promise_to_pay_service import PromiseToPayService

    future_dt = datetime.now(timezone.utc) + timedelta(days=3)
    PromiseToPayService.create_promise(
        db=db_session,
        recovery_case_id=case.id,
        promised_amount=Decimal("4999.00"),
        promised_date=future_dt,
        source="VOICE_ASSISTANT",
    )

    res = client.post(
        f"/recovery-cases/{case.id}/voice-recovery",
        json={"dry_run": True},
    )
    assert res.status_code == 400
    assert "Promise-to-Pay" in res.json()["detail"]
