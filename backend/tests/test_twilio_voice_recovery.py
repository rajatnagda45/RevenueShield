"""Unit and Integration test suite for Twilio Voice Recovery with personalized TwiML & stopping rules."""
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
from app.services.voice_recovery_service import VoiceRecoveryService


def _create_test_case(
    db: Session,
    case_status: str = "OPEN",
    phone: Optional[str] = "+919876543210",
    dnd_enabled: bool = False,
    amount: Decimal = Decimal("2499.00"),
    name: str = "Vikram Malhotra",
) -> tuple[Customer, RecoveryCase]:
    """Helper to create test Customer and RecoveryCase."""
    cust_id = uuid.uuid4()
    cust = Customer(
        id=cust_id,
        external_customer_id=f"cust_test_{cust_id.hex[:8]}",
        name=name,
        email=f"{name.lower().replace(' ', '.')}@example.com",
        phone=phone,
        dnd_enabled=dnd_enabled,
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
    db.refresh(case)
    db.refresh(cust)
    return cust, case


def test_start_recovery_call_eligible_case(client: TestClient, db_session: Session, monkeypatch):
    """Test 1: Eligible recovery case initiates outbound Twilio call and records VoiceCall and AuditLog."""
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC_test_mock_sid")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "test_auth_token_xyz")
    monkeypatch.setattr(settings, "TWILIO_PHONE_NUMBER", "+17372212163")
    monkeypatch.setattr(settings, "EXECUTION_MODE", "real")

    # Mock time outside DND quiet hours (14:00 IST / 08:30 UTC)
    daytime = datetime(2026, 8, 23, 8, 30, tzinfo=timezone.utc)

    cust, case = _create_test_case(db_session, case_status="OPEN", phone="+919876543210")

    with patch("app.integrations.voice.twilio_client.Client") as mock_client_cls, \
         patch("app.services.voice_recovery_service.datetime") as mock_dt:
        mock_dt.now.return_value = daytime
        mock_instance = MagicMock()
        mock_call = MagicMock()
        mock_call.sid = "CA111222333444555666777888999000aa"
        mock_call.status = "queued"
        mock_instance.calls.create.return_value = mock_call
        mock_client_cls.return_value = mock_instance

        res = client.post(f"/recovery-cases/{case.id}/voice-recovery")

        assert res.status_code == 200
        data = res.json()
        assert data["case_id"] == str(case.id)
        assert data["call_sid"] == "CA111222333444555666777888999000aa"
        assert data["status"] == "QUEUED"
        assert data["provider"] == "TWILIO"
        assert data["voice_call_id"] is not None

        # Verify VoiceCall in DB
        call_rec = db_session.scalar(select(VoiceCall).where(VoiceCall.id == uuid.UUID(data["voice_call_id"])))
        assert call_rec is not None
        assert call_rec.provider == "TWILIO"
        assert call_rec.provider_call_id == "CA111222333444555666777888999000aa"
        assert call_rec.to_number == "+919876543210"
        assert call_rec.status == "QUEUED"
        assert call_rec.attempt_number == 1
        assert call_rec.dynamic_variables["customer_name"] == "Vikram Malhotra"
        assert call_rec.dynamic_variables["amount_due"] == 2499.0

        # Verify Audit Logs
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.recovery_case_id == case.id).order_by(AuditLog.timestamp)
        ).all()
        actions = [a.action for a in audits]
        assert "VOICE_CALL_REQUESTED" in actions
        assert "VOICE_CALL_QUEUED" in actions


def test_missing_recovery_case_returns_404(client: TestClient):
    """Test 2: Missing recovery case UUID returns 404."""
    non_existent_id = uuid.uuid4()
    res = client.post(f"/recovery-cases/{non_existent_id}/voice-recovery")
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_already_recovered_case_blocks_voice_call(client: TestClient, db_session: Session):
    """Test 3: Already recovered case is strictly blocked."""
    cust, case = _create_test_case(db_session, case_status="RECOVERED")

    res = client.post(f"/recovery-cases/{case.id}/voice-recovery")
    assert res.status_code == 400
    assert "already RECOVERED" in res.json()["detail"]

    # Verify audit log
    audit = db_session.scalar(
        select(AuditLog)
        .where(AuditLog.recovery_case_id == case.id, AuditLog.action == "VOICE_CALL_BLOCKED")
    )
    assert audit is not None
    assert audit.audit_metadata["blocking_rule"] == "CASE_ALREADY_RECOVERED"


def test_active_promise_to_pay_blocks_voice_call(client: TestClient, db_session: Session):
    """Test 4: Active Promise-to-Pay agreement blocks routine recovery calls."""
    daytime = datetime(2026, 8, 23, 8, 30, tzinfo=timezone.utc)
    cust, case = _create_test_case(db_session, case_status="OPEN")

    ptp = PromiseToPay(
        id=uuid.uuid4(),
        recovery_case_id=case.id,
        customer_id=cust.id,
        amount_due=Decimal("2499.00"),
        promised_amount=Decimal("2499.00"),
        promised_date=daytime + timedelta(days=2),
        currency="INR",
        status="ACTIVE",
        source="CUSTOMER",
        confidence=0.9,
    )
    db_session.add(ptp)
    db_session.commit()

    with patch("app.services.voice_recovery_service.datetime") as mock_dt:
        mock_dt.now.return_value = daytime
        res = client.post(f"/recovery-cases/{case.id}/voice-recovery")
        assert res.status_code == 400
        assert "Active Promise-to-Pay exists" in res.json()["detail"]

        audit = db_session.scalar(
            select(AuditLog)
            .where(AuditLog.recovery_case_id == case.id, AuditLog.action == "VOICE_CALL_BLOCKED")
        )
        assert audit is not None
        assert audit.audit_metadata["blocking_rule"] == "PROMISE_TO_PAY_ACTIVE"


def test_dnd_quiet_hours_blocks_voice_call(client: TestClient, db_session: Session):
    """Test 5: Night time quiet hours (e.g. 23:00 IST) block voice calls."""
    cust, case = _create_test_case(db_session, case_status="OPEN")

    # 23:00 IST is 17:30 UTC
    night_time = datetime(2026, 8, 23, 17, 30, tzinfo=timezone.utc)

    with patch("app.services.voice_recovery_service.datetime") as mock_dt:
        mock_dt.now.return_value = night_time

        res = client.post(f"/recovery-cases/{case.id}/voice-recovery")
        assert res.status_code == 400
        assert "quiet hours" in res.json()["detail"]

        audit = db_session.scalar(
            select(AuditLog)
            .where(AuditLog.recovery_case_id == case.id, AuditLog.action == "VOICE_CALL_BLOCKED")
        )
        assert audit is not None
        assert audit.audit_metadata["blocking_rule"] == "DND_QUIET_HOURS"


def test_max_attempts_cap_blocks_voice_call(client: TestClient, db_session: Session):
    """Test 6: Exceeding 3 voice recovery attempts blocks further calls."""
    daytime = datetime(2026, 8, 23, 8, 30, tzinfo=timezone.utc)
    cust, case = _create_test_case(db_session, case_status="OPEN")

    # Add 3 previous calls
    for i in range(3):
        call = VoiceCall(
            id=uuid.uuid4(),
            recovery_case_id=case.id,
            customer_id=cust.id,
            provider="TWILIO",
            from_number="+17372212163",
            to_number="+919876543210",
            status="COMPLETED",
            attempt_number=i + 1,
            created_at=daytime - timedelta(hours=i + 2),
        )
        db_session.add(call)
    db_session.commit()

    with patch("app.services.voice_recovery_service.datetime") as mock_dt:
        mock_dt.now.return_value = daytime

        res = client.post(f"/recovery-cases/{case.id}/voice-recovery")
        assert res.status_code == 400
        assert "Maximum voice recovery attempts" in res.json()["detail"]

        audit = db_session.scalar(
            select(AuditLog)
            .where(AuditLog.recovery_case_id == case.id, AuditLog.action == "VOICE_CALL_BLOCKED")
        )
        assert audit is not None
        assert audit.audit_metadata["blocking_rule"] == "MAX_ATTEMPTS_EXCEEDED"


def test_cooldown_period_blocks_voice_call(client: TestClient, db_session: Session):
    """Test 7: Voice call attempted within 60-minute cooldown is blocked."""
    daytime = datetime(2026, 8, 23, 8, 30, tzinfo=timezone.utc)
    cust, case = _create_test_case(db_session, case_status="OPEN")

    # Call placed 15 minutes ago
    recent_call = VoiceCall(
        id=uuid.uuid4(),
        recovery_case_id=case.id,
        customer_id=cust.id,
        provider="TWILIO",
        from_number="+17372212163",
        to_number="+919876543210",
        status="COMPLETED",
        attempt_number=1,
        created_at=daytime - timedelta(minutes=15),
    )
    db_session.add(recent_call)
    db_session.commit()

    with patch("app.services.voice_recovery_service.datetime") as mock_dt:
        mock_dt.now.return_value = daytime

        res = client.post(f"/recovery-cases/{case.id}/voice-recovery")
        assert res.status_code == 400
        assert "cooldown active" in res.json()["detail"].lower()

        audit = db_session.scalar(
            select(AuditLog)
            .where(AuditLog.recovery_case_id == case.id, AuditLog.action == "VOICE_CALL_BLOCKED")
        )
        assert audit is not None
        assert audit.audit_metadata["blocking_rule"] == "COOLDOWN_ACTIVE"


def test_missing_customer_phone_blocks_call(client: TestClient, db_session: Session):
    """Test 8: Customer with missing phone number is blocked."""
    cust, case = _create_test_case(db_session, case_status="OPEN", phone="")
    cust.phone = None
    db_session.commit()

    res = client.post(f"/recovery-cases/{case.id}/voice-recovery")
    assert res.status_code == 400
    assert "phone number is missing" in res.json()["detail"].lower()


def test_personalized_english_twiml_generation(client: TestClient, db_session: Session):
    """Test 9: Webhook /webhooks/twilio/voice/{call_id} returns valid personalized English XML."""
    cust, case = _create_test_case(db_session, case_status="OPEN", amount=Decimal("4999.00"))

    call_id = uuid.uuid4()
    voice_call = VoiceCall(
        id=call_id,
        recovery_case_id=case.id,
        customer_id=cust.id,
        provider="TWILIO",
        from_number="+17372212163",
        to_number="+919876543210",
        status="QUEUED",
        attempt_number=1,
        dynamic_variables={
            "customer_name": "Pooja Sharma",
            "amount_due": 4999.0,
            "currency": "INR",
            "due_date": "August 20, 2026",
        },
    )
    db_session.add(voice_call)
    db_session.commit()

    res = client.get(f"/webhooks/twilio/voice/{call_id}")
    assert res.status_code == 200
    assert "application/xml" in res.headers["content-type"]

    xml_text = res.text
    assert "<Response>" in xml_text
    assert "Hello Pooja Sharma." in xml_text
    assert "4,999.00" in xml_text
    assert "August 20, 2026" in xml_text
    assert "Polly.Aditi" in xml_text
    assert "<Gather" in xml_text

    # Verify no Hinglish/Hindi phrases
    assert "Namaste" not in xml_text
    assert "taraf se" not in xml_text
    assert "chahte hain" not in xml_text


def test_twilio_status_callback_updates_voice_call(client: TestClient, db_session: Session):
    """Test 10: Status webhook updates VoiceCall status and duration without closing case."""
    cust, case = _create_test_case(db_session, case_status="OPEN")

    call_id = uuid.uuid4()
    call_sid = "CA999888777666555444333222111000bb"
    voice_call = VoiceCall(
        id=call_id,
        recovery_case_id=case.id,
        customer_id=cust.id,
        provider="TWILIO",
        provider_call_id=call_sid,
        from_number="+17372212163",
        to_number="+919876543210",
        status="QUEUED",
        attempt_number=1,
    )
    db_session.add(voice_call)
    db_session.commit()

    # Post Twilio status callback
    callback_payload = {
        "CallSid": call_sid,
        "CallStatus": "completed",
        "CallDuration": "45",
    }
    res = client.post("/webhooks/twilio/status", data=callback_payload)
    assert res.status_code == 200
    assert res.json()["status"] == "received"

    # Verify VoiceCall in DB
    db_session.refresh(voice_call)
    assert voice_call.status == "COMPLETED"
    assert voice_call.duration_seconds == 45
    assert voice_call.ended_at is not None

    # Verify RecoveryCase is NOT closed or marked recovered
    db_session.refresh(case)
    assert case.status == "OPEN"

    # Verify Audit Log
    audit = db_session.scalar(
        select(AuditLog)
        .where(AuditLog.recovery_case_id == case.id, AuditLog.action == "VOICE_CALL_COMPLETED")
    )
    assert audit is not None


def test_invalid_twilio_webhook_signature_rejected(client: TestClient, monkeypatch):
    """Test 11: Invalid X-Twilio-Signature returns 403 Forbidden."""
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "secret_auth_token_123")

    with patch.object(VoiceRecoveryService, "validate_twilio_webhook_signature", return_value=False):
        res = client.post(
            f"/webhooks/twilio/voice/{uuid.uuid4()}",
            headers={"X-Twilio-Signature": "invalid_sig_abc"},
            data={"CallSid": "CA12345"},
        )
        assert res.status_code == 403
        assert "Invalid Twilio signature" in res.json()["detail"]


def test_voice_gather_creates_promise_to_pay(client: TestClient, db_session: Session):
    """Test 12: Speech recognition of payment date and multi-turn confirmation creates PromiseToPay."""
    daytime = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
    cust, case = _create_test_case(db_session, case_status="OPEN", name="Rohan Verma")

    call_id = uuid.uuid4()
    call_sid = "CA111222333444555666777888999000gg"
    voice_call = VoiceCall(
        id=call_id,
        recovery_case_id=case.id,
        customer_id=cust.id,
        provider="TWILIO",
        provider_call_id=call_sid,
        from_number="+17372212163",
        to_number="+919876543210",
        status="IN_PROGRESS",
        attempt_number=1,
    )
    db_session.add(voice_call)
    db_session.commit()

    # Turn 1: Customer states "I will pay next Monday" -> asks confirmation
    res1 = client.post(
        f"/webhooks/twilio/voice/{call_id}/gather",
        data={
            "CallSid": call_sid,
            "SpeechResult": "I will pay next Monday",
            "Confidence": "0.95",
        },
    )
    assert res1.status_code == 200
    assert "application/xml" in res1.headers["content-type"]
    xml_text1 = res1.text
    assert "<Response>" in xml_text1
    assert "Just to confirm" in xml_text1
    assert "Monday" in xml_text1

    # Turn 2: Customer confirms "Yes that is correct" -> records PromiseToPay
    res2 = client.post(
        f"/webhooks/twilio/voice/{call_id}/gather",
        data={
            "CallSid": call_sid,
            "SpeechResult": "Yes that is correct",
            "Confidence": "0.96",
        },
    )
    assert res2.status_code == 200
    xml_text2 = res2.text
    assert "payment commitment has been recorded" in xml_text2

    # Verify PromiseToPay in database
    ptp = db_session.scalar(
        select(PromiseToPay).where(PromiseToPay.recovery_case_id == case.id)
    )
    assert ptp is not None
    assert ptp.status == "ACTIVE"
    assert ptp.source == "VOICE_ASSISTANT"
    assert ptp.promised_amount == case.amount_at_risk

    # Verify Audit Log
    audit = db_session.scalar(
        select(AuditLog).where(
            AuditLog.recovery_case_id == case.id,
            AuditLog.action == "VOICE_PROMISE_TO_PAY_RECORDED",
        )
    )
    assert audit is not None


def test_voice_gather_handles_refusal_or_unclear(client: TestClient, db_session: Session):
    """Test 13: Customer refusal or unclear speech returns polite guidance without failing."""
    cust, case = _create_test_case(db_session, case_status="OPEN")

    call_id = uuid.uuid4()
    voice_call = VoiceCall(
        id=call_id,
        recovery_case_id=case.id,
        customer_id=cust.id,
        provider="TWILIO",
        provider_call_id="CA_test_refusal_123",
        from_number="+17372212163",
        to_number="+919876543210",
        status="IN_PROGRESS",
        attempt_number=1,
    )
    db_session.add(voice_call)
    db_session.commit()

    res = client.post(
        f"/webhooks/twilio/voice/{call_id}/gather",
        data={
            "CallSid": "CA_test_refusal_123",
            "SpeechResult": "I cannot pay right now I have no money",
            "Confidence": "0.91",
        },
    )
    assert res.status_code == 200
    assert "won't pressure you" in res.text
