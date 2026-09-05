"""Integration tests for Admin Sync API and Webhook consistency."""
from decimal import Decimal
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.recovery_case import RecoveryCase
from app.models.sync_checkpoint import SyncCheckpoint
from app.schemas.event import NormalizedEvent
from app.services.event_processor import EventProcessor
from app.integrations.razorpay.payment_client import RazorpayPaymentClient


def test_admin_sync_payments_api_endpoint(client: TestClient, monkeypatch):
    """Verify POST /admin/razorpay/sync/payments triggers sync and returns statistics."""
    mock_items = [
        {
            "id": f"pay_api_sync_{uuid.uuid4()}",
            "entity": "payment",
            "amount": 120000,
            "currency": "INR",
            "status": "captured",
            "method": "card",
            "email": "apisync@example.com",
            "created_at": 1716300000,
        }
    ]

    def mock_fetch(self, from_timestamp=None, to_timestamp=None, count=100, skip=0):
        if skip == 0:
            return {"entity": "collection", "count": 1, "items": mock_items}
        return {"entity": "collection", "count": 0, "items": []}

    monkeypatch.setattr(RazorpayPaymentClient, "fetch_payments", mock_fetch)

    payload = {
        "from": "2026-08-01T00:00:00Z",
        "to": "2026-08-22T23:59:59Z",
        "batch_size": 50,
    }

    res = client.post("/admin/razorpay/sync/payments", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "SUCCEEDED"
    assert data["records_fetched"] == 1
    assert data["records_created"] == 1
    assert data["records_updated"] == 0
    assert "sync_id" in data


def test_admin_data_quality_endpoint(client: TestClient):
    """Verify GET /admin/razorpay/sync/data-quality returns valid aggregate statistics."""
    res = client.get("/admin/razorpay/sync/data-quality")
    assert res.status_code == 200
    data = res.json()
    assert "total_payments" in data
    assert "successful_payments" in data
    assert "failed_payments" in data
    assert "total_amount" in data


def test_admin_list_checkpoints_endpoint(client: TestClient):
    """Verify GET /admin/razorpay/sync/checkpoints returns checkpoint items."""
    res = client.get("/admin/razorpay/sync/checkpoints?limit=10")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)


def test_webhook_consistency_with_api_ingested_payment(client: TestClient, db_session: Session, monkeypatch):
    """Verify that a payment ingested via API sync is updated by subsequent webhook without duplication."""
    pay_id = f"pay_shared_{uuid.uuid4()}"
    mock_items = [
        {
            "id": pay_id,
            "entity": "payment",
            "amount": 250000,
            "currency": "INR",
            "status": "failed",
            "method": "card",
            "email": "shared@example.com",
            "error_code": "BAD_REQUEST_ERROR",
            "error_reason": "incorrect_otp",
            "created_at": 1716300000,
        }
    ]

    def mock_fetch(self, from_timestamp=None, to_timestamp=None, count=100, skip=0):
        if skip == 0:
            return {"entity": "collection", "count": 1, "items": mock_items}
        return {"entity": "collection", "count": 0, "items": []}

    monkeypatch.setattr(RazorpayPaymentClient, "fetch_payments", mock_fetch)

    # 1. Ingest payment via API sync
    sync_res = client.post("/admin/razorpay/sync/payments", json={})
    assert sync_res.status_code == 200

    # 2. Later send webhook for SAME payment (captured)
    cap_event = NormalizedEvent(
        event_id=f"evt_shared_cap_{uuid.uuid4()}",
        event_type="payment.captured",
        source="RAZORPAY",
        amount=Decimal("2500.00"),
        currency="INR",
        customer_email="shared@example.com",
        external_payment_id=pay_id,
        payment_status="SUCCESS",
    )
    res_cap = EventProcessor.process_normalized_event(db_session, cap_event)
    assert res_cap.status == "processed"

    # 3. Verify exactly ONE Payment row exists and its status is CAPTURED
    payments = db_session.scalars(select(Payment).where(Payment.external_payment_id == pay_id)).all()
    assert len(payments) == 1
    assert payments[0].status == "CAPTURED"
    assert payments[0].captured is True
