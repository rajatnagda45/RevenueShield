"""Unit tests for RazorpayPaymentLinkClient."""
import httpx
import pytest
from app.integrations.razorpay.payment_link_client import (
    RazorpayPaymentLinkClient,
    RazorpayPaymentLinkError,
    PaymentLinkResponse,
)


def test_client_initialization_and_configuration():
    """Verify credentials configuration checks."""
    client = RazorpayPaymentLinkClient(key_id="rzp_test_123", key_secret="secret_123")
    assert client.is_configured is True
    assert "rzp_test..." in repr(client)

    unconfigured = RazorpayPaymentLinkClient(key_id="", key_secret="")
    assert unconfigured.is_configured is False
    with pytest.raises(RazorpayPaymentLinkError) as exc_info:
        unconfigured.create_payment_link(amount_paise=1000, currency="INR", description="Test")
    assert exc_info.value.error_code == "CREDENTIALS_MISSING"


def test_create_payment_link_success(monkeypatch):
    """Verify successful payment link creation and parameter serialization."""
    captured_payload = {}

    def mock_request(self, method, url, auth=None, params=None, json=None):
        nonlocal captured_payload
        captured_payload = json
        return httpx.Response(
            status_code=200,
            json={
                "id": "plink_test_999",
                "short_url": "https://rzp.io/i/plink_test_999",
                "status": "created",
                "amount": 500000,
                "currency": "INR",
                "reference_id": "case_abc_123",
                "created_at": 1724310000,
            },
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(httpx.Client, "request", mock_request)

    client = RazorpayPaymentLinkClient(key_id="rzp_test_123", key_secret="secret_123")
    res: PaymentLinkResponse = client.create_payment_link(
        amount_paise=500000,
        currency="INR",
        description="Recovery payment for invoice",
        customer_name="John Doe",
        customer_email="john@example.com",
        customer_phone="+919876543210",
        reference_id="case_abc_123",
    )

    assert res.payment_link_id == "plink_test_999"
    assert res.short_url == "https://rzp.io/i/plink_test_999"
    assert res.amount == 5000.0
    assert res.currency == "INR"
    assert res.status == "CREATED"

    # Verify notification flags are disabled (our system handles notification)
    assert captured_payload["notify"]["sms"] is False
    assert captured_payload["notify"]["email"] is False
    assert captured_payload["customer"]["name"] == "John Doe"
    assert captured_payload["customer"]["contact"] == "+919876543210"


def test_get_and_cancel_payment_link(monkeypatch):
    """Verify fetching and cancelling payment links."""
    def mock_request(self, method, url, auth=None, params=None, json=None):
        if "cancel" in url:
            return httpx.Response(status_code=200, json={"id": "plink_test_999", "status": "cancelled"})
        return httpx.Response(
            status_code=200,
            json={
                "id": "plink_test_999",
                "short_url": "https://rzp.io/i/plink_test_999",
                "status": "paid",
                "amount": 250000,
                "currency": "INR",
            },
        )

    monkeypatch.setattr(httpx.Client, "request", mock_request)

    client = RazorpayPaymentLinkClient(key_id="rzp_test_123", key_secret="secret_123")
    get_res = client.get_payment_link("plink_test_999")
    assert get_res.status == "PAID"
    assert get_res.amount == 2500.0

    cancel_res = client.cancel_payment_link("plink_test_999")
    assert cancel_res["status"] == "cancelled"


def test_client_raises_non_retryable_on_400(monkeypatch):
    """Verify that 400 Bad Request raises immediately without retrying."""
    call_count = 0

    def mock_request(self, method, url, auth=None, params=None, json=None):
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            status_code=400,
            json={"error": {"code": "BAD_REQUEST_ERROR", "description": "Invalid amount"}},
            headers={"content-type": "application/json"},
        )

    monkeypatch.setattr(httpx.Client, "request", mock_request)

    client = RazorpayPaymentLinkClient(key_id="rzp_test_123", key_secret="secret_123", max_retries=3)
    with pytest.raises(RazorpayPaymentLinkError) as exc_info:
        client.create_payment_link(amount_paise=0, currency="INR", description="Test")

    assert call_count == 1
    assert exc_info.value.status_code == 400
    assert exc_info.value.is_retryable is False
