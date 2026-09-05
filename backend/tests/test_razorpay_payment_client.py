"""Unit tests for RazorpayPaymentClient HTTP transport, retries, and pagination."""
from typing import Any, Dict
import pytest
import httpx

from app.integrations.razorpay.payment_client import RazorpayPaymentClient, RazorpayClientError


def test_fetch_payments_builds_correct_params(monkeypatch):
    """Verify that fetch_payments clamps count to 100 and passes epoch filters correctly."""
    captured_params = {}

    def mock_request(self, method, endpoint, params=None, json_data=None):
        nonlocal captured_params
        captured_params = params
        return {"entity": "collection", "count": 2, "items": [{"id": "pay_001"}, {"id": "pay_002"}]}

    monkeypatch.setattr(RazorpayPaymentClient, "_request_with_retry", mock_request)

    client = RazorpayPaymentClient(key_id="rzp_test_123", key_secret="secret123")
    res = client.fetch_payments(from_timestamp=1700000000, to_timestamp=1700086400, count=250, skip=100)

    assert res["count"] == 2
    assert captured_params["count"] == 100  # Clamped from 250
    assert captured_params["skip"] == 100
    assert captured_params["from"] == 1700000000
    assert captured_params["to"] == 1700086400


def test_client_raises_on_unconfigured_credentials():
    """Verify that client fails gracefully if API keys are missing."""
    client = RazorpayPaymentClient(key_id="", key_secret="")
    with pytest.raises(RazorpayClientError, match="Razorpay API credentials .* are missing"):
        client.fetch_payments()


def test_client_raises_non_retryable_on_401(monkeypatch):
    """Verify that 401 Unauthorized raises immediately without retrying."""
    def mock_request(self, method, url, auth=None, params=None, json=None):
        return httpx.Response(status_code=401, json={"error": {"description": "Invalid API key"}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "request", mock_request)

    client = RazorpayPaymentClient(key_id="rzp_test_bad", key_secret="secret")
    with pytest.raises(RazorpayClientError, match="Invalid API key") as exc_info:
        client.fetch_payment("pay_test_001")

    assert exc_info.value.status_code == 401
