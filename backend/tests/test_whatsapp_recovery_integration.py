"""Integration tests for WhatsApp Recovery Agent end-to-end lifecycle, webhooks, and stopping rules."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.communication import Communication
from app.models.audit_log import AuditLog
from app.services.communication_orchestrator import CommunicationOrchestrator, WhatsAppOutreachResult
from app.services.event_processor import EventProcessor
from app.schemas.event import NormalizedEvent


client = TestClient(app)


def _setup_case(db: Session, amount: Decimal = Decimal("5000.00")) -> RecoveryCase:
    cust = Customer(
        external_customer_id=f"cust_{uuid.uuid4()}",
        name="Rohit Mehra",
        phone="+919876543210",
        email="rohit@example.com",
        whatsapp_allowed=True,
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
        status="OPEN",
    )
    db.add(case)
    db.commit()
    return case


def test_whatsapp_dispatch_lifecycle_and_stopping_rule(db_session: Session):
    """Verify end-to-end: Failed case -> Send WhatsApp -> Webhook payment.captured -> Stopping rule cancels and blocks."""
    case = _setup_case(db_session)
    daytime = datetime(2026, 8, 22, 8, 30, tzinfo=timezone.utc)

    # 1. Preview
    prev = CommunicationOrchestrator.preview_whatsapp_outreach(
        db=db_session, recovery_case_id=case.id, language="HINGLISH", reference_time=daytime
    )
    assert prev["policy_status"] == "APPROVED"
    assert prev["language"] == "HINGLISH"
    assert "aapka ₹5,000.00 ka payment" in prev["message_body"]

    # 2. Dispatch
    outreach: WhatsAppOutreachResult = CommunicationOrchestrator.queue_or_send_whatsapp_recovery(
        db=db_session,
        recovery_case_id=case.id,
        language="HINGLISH",
        dry_run=True,
        reference_time=daytime,
    )
    assert outreach.status == "SENT"
    assert outreach.communication_id is not None
    assert outreach.is_simulated is True
    assert "https://rzp.io/i/" in outreach.payment_link_url

    # 3. Simulate Provider Status Callback: DELIVERED
    comm_id = uuid.UUID(outreach.communication_id)
    comm = db_session.scalar(select(Communication).where(Communication.id == comm_id))
    assert comm.status == "SENT"

    updated = CommunicationOrchestrator.handle_status_webhook(
        db=db_session,
        provider_message_id=comm.provider_message_id,
        status="DELIVERED",
    )
    assert updated.status == "DELIVERED"
    assert updated.delivered_at is not None

    # 4. Ingest payment.captured Webhook
    cap_evt = NormalizedEvent(
        event_id=f"evt_cap_{uuid.uuid4().hex[:10]}",
        event_type="payment.captured",
        source="RAZORPAY",
        occurred_at=daytime + timedelta(minutes=15),
        external_customer_id=case.customer.external_customer_id,
        amount=Decimal("5000.00"),
        currency="INR",
        raw_payload={"id": "pay_test_cap_123", "amount": 500000, "status": "captured"},
    )
    proc_res = EventProcessor.process_normalized_event(db=db_session, event=cap_evt)
    assert proc_res.status == "processed"

    db_session.refresh(case)
    assert case.status == "RECOVERED"

    # 5. Verify Stopping Rule: Attempting WhatsApp on recovered case is strictly BLOCKED
    second_attempt = CommunicationOrchestrator.queue_or_send_whatsapp_recovery(
        db=db_session,
        recovery_case_id=case.id,
        language="ENGLISH",
        dry_run=True,
        reference_time=daytime + timedelta(minutes=30),
    )
    assert second_attempt.status == "BLOCKED"
    assert second_attempt.policy_blocking_rule == "CASE_ALREADY_RECOVERED_OR_CLOSED"


def test_whatsapp_api_endpoints(client: TestClient, db_session: Session):
    """Verify FastAPI endpoints for dispatch, preview, webhook, and dashboard."""
    case = _setup_case(db_session)

    # 1. Preview API
    res_prev = client.get(f"/recovery-cases/{case.id}/communications/whatsapp/preview?language=ENGLISH")
    assert res_prev.status_code == 200
    data_prev = res_prev.json()
    assert data_prev["case_id"] == str(case.id)
    assert data_prev["language"] == "ENGLISH"
    assert "₹5,000.00" in data_prev["message"]

    # 2. Webhook Callback API
    # Create communication
    comm = Communication(
        recovery_case_id=case.id,
        customer_id=case.customer_id,
        channel="WHATSAPP",
        recipient_reference="+919876543210",
        recipient_masked="+919*****3210",
        message_body="Test API message",
        status="SENT",
        provider_message_id="SM_test_webhook_cb_123",
        idempotency_key=f"comm_{case.id}_WHATSAPP_api_test",
        attempt_number=1,
    )
    db_session.add(comm)
    db_session.commit()

    res_cb = client.post(
        "/webhooks/whatsapp/status",
        json={"provider_message_id": "SM_test_webhook_cb_123", "status": "DELIVERED"},
    )
    assert res_cb.status_code == 200
    assert res_cb.json()["status"] == "processed"
    assert res_cb.json()["updated_status"] == "DELIVERED"

    # 3. Dashboard API
    res_dash = client.get("/admin/communications/whatsapp/dashboard")
    assert res_dash.status_code == 200
    data_dash = res_dash.json()
    assert "whatsapp_messages_sent" in data_dash
    assert "whatsapp_recovery_rate" in data_dash
