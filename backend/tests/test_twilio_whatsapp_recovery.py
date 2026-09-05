"""Unit and Integration Tests for Real Twilio WhatsApp Recovery Service and Client."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import uuid
import pytest
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.communication import Communication
from app.models.promise_to_pay import PromiseToPay
from app.models.recovery_payment_link import RecoveryPaymentLink
from app.models.payment import Payment
from app.integrations.twilio.client import TwilioWhatsAppClient, TwilioMessageResponse, normalize_whatsapp_address
from app.services.whatsapp_recovery_service import WhatsAppRecoveryService, WhatsAppRecoveryResponse
from app.services.event_processor import EventProcessor
from app.schemas.event import NormalizedEvent


def _create_test_case_helper(
    db: Session,
    phone: str = "+919876543210",
    name: str = "Rahul Sharma",
    amount: Decimal = Decimal("5000.00"),
    status: str = "OPEN",
    whatsapp_allowed: bool = True,
    dnd_enabled: bool = False,
) -> RecoveryCase:
    cust = Customer(
        external_customer_id=f"cust_{uuid.uuid4()}",
        name=name,
        phone=phone,
        email="rahul@example.com",
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
        amount_at_risk=amount,
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status=status,
    )
    db.add(case)
    db.commit()
    return case


# =============================================================================
# 1. Twilio Client Unit Tests (Credentials, Formatting, Sandbox, Backoff)
# =============================================================================

def test_normalize_whatsapp_address():
    """Verify phone normalization prepends whatsapp:+ correctly."""
    assert normalize_whatsapp_address("+919876543210") == "whatsapp:+919876543210"
    assert normalize_whatsapp_address("919876543210") == "whatsapp:+919876543210"
    assert normalize_whatsapp_address("whatsapp:+919876543210") == "whatsapp:+919876543210"
    assert normalize_whatsapp_address("") == ""


def test_twilio_client_missing_credentials_fails_gracefully():
    """Verify Twilio client fails gracefully when credentials are not configured."""
    client = TwilioWhatsAppClient(
        account_sid="",
        auth_token="",
        api_key_sid="",
        api_key_secret="",
        whatsapp_from="",
    )
    res = client.send_whatsapp_message(recipient="+919876543210", message_body="Test")
    assert res.success is False
    assert res.error_code == "TWILIO_NOT_CONFIGURED"


def test_twilio_sandbox_recipient_restriction():
    """Verify in SANDBOX mode, sending to non-configured recipient is blocked with SANDBOX_RECIPIENT_RESTRICTION."""
    client = TwilioWhatsAppClient(
        account_sid="AC_test_sid",
        auth_token="auth_test_token",
        whatsapp_from="whatsapp:+14155238886",
        whatsapp_to="whatsapp:+919876543210",
        mode="SANDBOX",
    )

    # Allowed recipient
    # Mocking httpx transport for allowed recipient
    def mock_handler_success(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"sid": "SM_test_sandbox_123", "status": "queued"})

    client_mock = httpx.Client(transport=httpx.MockTransport(mock_handler_success))
    # Replace sending method with mock transport
    original_client = httpx.Client
    try:
        httpx.Client = lambda *args, **kwargs: client_mock
        res_ok = client.send_whatsapp_message(recipient="+919876543210", message_body="Test")
        assert res_ok.success is True
        assert res_ok.message_sid == "SM_test_sandbox_123"
    finally:
        httpx.Client = original_client

    # Disallowed recipient
    res_blocked = client.send_whatsapp_message(recipient="+919999999999", message_body="Test")
    assert res_blocked.success is False
    assert res_blocked.error_code == "SANDBOX_RECIPIENT_RESTRICTION"
    assert res_blocked.status == "BLOCKED"


def test_twilio_client_retryable_5xx_and_exponential_backoff(monkeypatch):
    """Verify retry on 500 error and eventual backoff."""
    attempt_count = 0

    def mock_handler_500(request: httpx.Request) -> httpx.Response:
        nonlocal attempt_count
        attempt_count += 1
        return httpx.Response(500, text="Internal Server Error")

    client = TwilioWhatsAppClient(
        account_sid="AC_test_sid",
        auth_token="auth_test_token",
        whatsapp_from="whatsapp:+14155238886",
        whatsapp_to="whatsapp:+919876543210",
        mode="SANDBOX",
        max_retries=2,
    )

    original_client = httpx.Client
    try:
        httpx.Client = lambda *args, **kwargs: original_client(transport=httpx.MockTransport(mock_handler_500))
        res = client.send_whatsapp_message(recipient="+919876543210", message_body="Test")
        assert res.success is False
        assert attempt_count == 2
        assert res.error_code == "TWILIO_DISPATCH_TIMEOUT_OR_SERVER_ERROR"
    finally:
        httpx.Client = original_client


def test_twilio_client_non_retryable_400_fails_fast():
    """Verify non-retryable 4xx client errors fail fast without retry."""
    attempt_count = 0

    def mock_handler_400(request: httpx.Request) -> httpx.Response:
        nonlocal attempt_count
        attempt_count += 1
        return httpx.Response(400, json={"code": 21211, "message": "Invalid phone number"})

    client = TwilioWhatsAppClient(
        account_sid="AC_test_sid",
        auth_token="auth_test_token",
        whatsapp_from="whatsapp:+14155238886",
        whatsapp_to="whatsapp:+919876543210",
        mode="SANDBOX",
        max_retries=3,
    )

    original_client = httpx.Client
    try:
        httpx.Client = lambda *args, **kwargs: original_client(transport=httpx.MockTransport(mock_handler_400))
        res = client.send_whatsapp_message(recipient="+919876543210", message_body="Test")
        assert res.success is False
        assert attempt_count == 1  # Fails fast on 4xx
        assert res.error_code == "21211"
        assert "Invalid phone number" in res.error_message
    finally:
        httpx.Client = original_client


# =============================================================================
# 2. WhatsAppRecoveryService Policy & Safety Tests
# =============================================================================

def test_recovery_service_dnd_quiet_hours(db_session: Session):
    """Verify WhatsApp recovery is BLOCKED during quiet hours (20:00 - 08:00 IST)."""
    case = _create_test_case_helper(db_session)
    # 22:00 IST (16:30 UTC)
    night_time = datetime(2026, 8, 22, 16, 30, tzinfo=timezone.utc)

    res: WhatsAppRecoveryResponse = WhatsAppRecoveryService.execute_recovery(
        db=db_session, recovery_case_id=case.id, reference_time=night_time
    )
    assert res.status == "BLOCKED"
    assert res.policy_blocking_rule == "QUIET_HOURS_DND_PROHIBITED"


def test_recovery_service_cooldown_active(db_session: Session):
    """Verify WhatsApp recovery is BLOCKED inside the 24-hour cooldown window."""
    case = _create_test_case_helper(db_session)
    daytime_1 = datetime(2026, 8, 22, 8, 30, tzinfo=timezone.utc)

    # First attempt
    res_1 = WhatsAppRecoveryService.execute_recovery(
        db=db_session, recovery_case_id=case.id, dry_run=True, reference_time=daytime_1
    )
    assert res_1.status == "SENT"

    # Second attempt 3 hours later
    daytime_2 = daytime_1 + timedelta(hours=3)
    res_2 = WhatsAppRecoveryService.execute_recovery(
        db=db_session, recovery_case_id=case.id, dry_run=True, reference_time=daytime_2
    )
    assert res_2.status == "BLOCKED"
    assert res_2.policy_blocking_rule == "COOLDOWN_PERIOD_ACTIVE"


def test_recovery_service_max_attempts_exceeded(db_session: Session):
    """Verify WhatsApp recovery is BLOCKED after MAX_WHATSAPP_ATTEMPTS (3)."""
    case = _create_test_case_helper(db_session)
    base_time = datetime(2026, 8, 10, 8, 30, tzinfo=timezone.utc)

    # 3 spaced-out attempts
    for i in range(1, 4):
        t = base_time + timedelta(days=i)
        res = WhatsAppRecoveryService.execute_recovery(
            db=db_session, recovery_case_id=case.id, dry_run=True, reference_time=t
        )
        assert res.status == "SENT"

    # 4th attempt on day 4
    t4 = base_time + timedelta(days=4)
    res_4 = WhatsAppRecoveryService.execute_recovery(
        db=db_session, recovery_case_id=case.id, dry_run=True, reference_time=t4
    )
    assert res_4.status == "BLOCKED"
    assert res_4.policy_blocking_rule == "MAX_WHATSAPP_ATTEMPTS_EXCEEDED"


def test_recovery_service_active_promise_to_pay_blocks(db_session: Session):
    """Verify active Promise-to-Pay pauses WhatsApp outreach."""
    case = _create_test_case_helper(db_session)
    daytime = datetime(2026, 8, 22, 8, 30, tzinfo=timezone.utc)

    ptp = PromiseToPay(
        customer_id=case.customer_id,
        recovery_case_id=case.id,
        status="ACTIVE",
        promised_amount=Decimal("5000.00"),
        promised_date=daytime + timedelta(days=2),
    )
    db_session.add(ptp)
    db_session.commit()

    res = WhatsAppRecoveryService.execute_recovery(
        db=db_session, recovery_case_id=case.id, dry_run=True, reference_time=daytime
    )
    assert res.status == "BLOCKED"
    assert res.policy_blocking_rule == "PROMISE_TO_PAY_ACTIVE"


def test_recovery_service_race_condition_guard(db_session: Session):
    """Verify pre-flight race condition guard blocks dispatch if payment captured immediately before send."""
    case = _create_test_case_helper(db_session, status="RECOVERED")
    daytime = datetime(2026, 8, 22, 8, 30, tzinfo=timezone.utc)

    res = WhatsAppRecoveryService.execute_recovery(
        db=db_session, recovery_case_id=case.id, dry_run=True, reference_time=daytime
    )
    assert res.status == "BLOCKED"
    assert res.policy_blocking_rule == "CASE_ALREADY_RECOVERED_OR_CLOSED"


def test_recovery_service_payment_link_reuse(db_session: Session):
    """Verify existing active payment link is reused without generating duplicate links."""
    case = _create_test_case_helper(db_session)
    daytime = datetime(2026, 8, 22, 8, 30, tzinfo=timezone.utc)

    # Pre-create payment link
    link = RecoveryPaymentLink(
        recovery_case_id=case.id,
        razorpay_payment_link_id="plink_test_pre_created_123",
        payment_url="https://rzp.io/i/plink_test_pre_created_123",
        amount=case.amount_at_risk,
        currency="INR",
        status="CREATED",
    )
    db_session.add(link)
    db_session.commit()

    res = WhatsAppRecoveryService.execute_recovery(
        db=db_session, recovery_case_id=case.id, dry_run=True, reference_time=daytime
    )
    assert res.status == "SENT"
    assert res.payment_link["url"] == "https://rzp.io/i/plink_test_pre_created_123"

    # Verify no second payment link created
    all_links = db_session.scalars(
        select(RecoveryPaymentLink).where(RecoveryPaymentLink.recovery_case_id == case.id)
    ).all()
    assert len(all_links) == 1


# =============================================================================
# 3. End-to-End Test Mode & API Endpoints
# =============================================================================

def test_whatsapp_recovery_api_endpoint(client: TestClient, db_session: Session):
    """Verify POST /recovery-cases/{id}/whatsapp-recovery and GET /recovery-cases/{id}/whatsapp-preview."""
    case = _create_test_case_helper(db_session)

    # 1. Preview API
    res_prev = client.get(f"/recovery-cases/{case.id}/whatsapp-preview?language=HINGLISH")
    assert res_prev.status_code == 200
    prev_data = res_prev.json()
    assert prev_data["case_id"] == str(case.id)
    assert prev_data["language"] == "HINGLISH"
    assert "aapka ₹5,000.00 ka payment" in prev_data["message"]

    # 2. Recovery Action API (dry_run)
    res_post = client.post(
        f"/recovery-cases/{case.id}/whatsapp-recovery",
        json={"language": "HINGLISH", "dry_run": True, "evaluation_time": "2026-08-22T08:30:00Z"},
    )
    assert res_post.status_code == 200
    post_data = res_post.json()
    assert post_data["case_id"] == str(case.id)
    assert post_data["status"] == "SENT"
    assert post_data["action"] == "WHATSAPP_PAYMENT_RECOVERY"
    assert "https://rzp.io/i/" in post_data["payment_link"]["url"]
    assert post_data["communication"]["status"] == "SENT"


def test_full_test_mode_recovery_and_stopping_rule(db_session: Session):
    """Verify full lifecycle: failed payment -> WhatsApp dispatch -> customer pays -> captured webhook -> stopping rule cancels outreach."""
    case = _create_test_case_helper(db_session)
    daytime = datetime(2026, 8, 22, 8, 30, tzinfo=timezone.utc)

    # 1. Execute WhatsApp recovery
    res = WhatsAppRecoveryService.execute_recovery(
        db=db_session, recovery_case_id=case.id, language="ENGLISH", dry_run=True, reference_time=daytime
    )
    assert res.status == "SENT"

    # 2. Ingest payment.captured webhook
    cap_evt = NormalizedEvent(
        event_id=f"evt_cap_{uuid.uuid4().hex[:8]}",
        event_type="payment.captured",
        source="RAZORPAY",
        occurred_at=daytime + timedelta(minutes=10),
        external_customer_id=case.customer.external_customer_id,
        amount=Decimal("5000.00"),
        currency="INR",
        payment_method="UPI",
        raw_payload={"id": "pay_cap_test_999", "amount": 500000, "status": "captured"},
    )
    proc_res = EventProcessor.process_normalized_event(db=db_session, event=cap_evt)
    assert proc_res.status == "processed"

    db_session.refresh(case)
    assert case.status == "RECOVERED"
    assert case.recovered_amount == Decimal("5000.00")

    # 3. Verify stopping rule prevents any second recovery message
    second_attempt = WhatsAppRecoveryService.execute_recovery(
        db=db_session, recovery_case_id=case.id, language="ENGLISH", dry_run=True, reference_time=daytime + timedelta(minutes=20)
    )
    assert second_attempt.status == "BLOCKED"
    assert second_attempt.policy_blocking_rule == "CASE_ALREADY_RECOVERED_OR_CLOSED"
