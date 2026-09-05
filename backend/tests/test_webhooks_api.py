"""End-to-End HTTP tests for POST /webhooks/razorpay endpoint."""
import json
import pytest
from fastapi.testclient import TestClient
from app.core.config import settings
from app.integrations.razorpay.security import compute_razorpay_signature


@pytest.fixture(autouse=True)
def setup_webhook_secret(monkeypatch):
    """Ensure a deterministic webhook secret is configured for tests."""
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret_key_456")


def test_webhook_endpoint_success_with_valid_signature(client: TestClient):
    """Test full HTTP webhook ingestion with valid HMAC-SHA256 signature."""
    payload_dict = {
        "entity": "event",
        "account_id": "acc_test_api",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_api_test_001",
                    "amount": 250000,  # 2500.00 INR
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "email": "customer.api@example.com",
                    "contact": "+919988776655",
                    "notes": {"customer_name": "API Tester Corp"},
                    "error_code": "GATEWAY_TIMEOUT",
                    "error_description": "Issuing bank timed out during authorization",
                    "created_at": 1716300500,
                }
            }
        },
        "created_at": 1716300500,
    }
    raw_body = json.dumps(payload_dict).encode("utf-8")
    signature = compute_razorpay_signature(raw_body, settings.RAZORPAY_WEBHOOK_SECRET)

    response = client.post(
        "/webhooks/razorpay",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "x-razorpay-event-id": "evt_api_hdr_001",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "processed"
    assert data["event_id"] == "evt_api_hdr_001"
    assert data["recovery_case_id"] is not None


def test_webhook_endpoint_rejects_missing_signature_header(client: TestClient):
    """Test that requests lacking the X-Razorpay-Signature header are rejected with 400 Bad Request."""
    response = client.post(
        "/webhooks/razorpay",
        json={"event": "payment.failed"},
    )
    assert response.status_code == 400
    assert "Missing required header: X-Razorpay-Signature" in response.json()["detail"]


def test_webhook_endpoint_rejects_invalid_signature(client: TestClient):
    """Test that requests with an invalid/forged signature return 401 Unauthorized."""
    raw_body = b'{"event":"payment.failed"}'
    response = client.post(
        "/webhooks/razorpay",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": "invalid_forged_hex_signature_string",
        },
    )
    assert response.status_code == 401
    assert "Invalid webhook signature" in response.json()["detail"]


def test_webhook_endpoint_rejects_tampered_body(client: TestClient):
    """Test that tampering with the body after signature generation causes 401 rejection."""
    original_body = b'{"event":"payment.failed","amount":100}'
    signature = compute_razorpay_signature(original_body, settings.RAZORPAY_WEBHOOK_SECRET)

    tampered_body = b'{"event":"payment.failed","amount":99999}'
    response = client.post(
        "/webhooks/razorpay",
        content=tampered_body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
        },
    )
    assert response.status_code == 401


def test_webhook_endpoint_duplicate_delivery_is_safe_and_idempotent(client: TestClient):
    """Test that sending duplicate webhook deliveries returns 200 with duplicate status."""
    payload_dict = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_dup_http_101",
                    "amount": 10000,
                    "currency": "INR",
                    "status": "failed",
                    "email": "dup@test.com",
                }
            }
        },
    }
    raw_body = json.dumps(payload_dict).encode("utf-8")
    signature = compute_razorpay_signature(raw_body, settings.RAZORPAY_WEBHOOK_SECRET)

    headers = {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature,
        "x-razorpay-event-id": "evt_dup_http_test_101",
    }

    # First request -> 200 OK, processed
    res1 = client.post("/webhooks/razorpay", content=raw_body, headers=headers)
    assert res1.status_code == 200
    assert res1.json()["status"] == "processed"

    # Second request -> 200 OK, duplicate
    res2 = client.post("/webhooks/razorpay", content=raw_body, headers=headers)
    assert res2.status_code == 200
    assert res2.json()["status"] == "duplicate"
