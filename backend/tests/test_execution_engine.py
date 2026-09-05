"""Unit tests for RazorpayPaymentLinkExecutor and execution engine behaviors."""
from decimal import Decimal
import pytest
from unittest.mock import MagicMock

from app.core.config import settings
from app.execution.base import ExecutionRequest, ExecutionStatus
from app.execution.executors.razorpay_payment_link import RazorpayPaymentLinkExecutor
from app.integrations.razorpay.client import RazorpayClient, RazorpayAPIError


@pytest.fixture
def sample_request():
    return ExecutionRequest(
        execution_id="exec_test_01",
        case_id="case_test_01",
        action_id="act_test_01",
        action_type="SEND_PAYMENT_LINK",
        customer_id="cust_test_01",
        amount=Decimal("500.00"),
        currency="INR",
        idempotency_key="case_test_01_act_test_01_SEND_PAYMENT_LINK",
        customer_name="Priya Sharma",
        customer_email="priya@example.com",
        customer_phone="+919876500001",
    )


def test_dry_run_execution_simulates_success_without_external_call(sample_request: ExecutionRequest, monkeypatch):
    """Verify that dry_run mode returns SIMULATED_SUCCESS with simulated link URL."""
    monkeypatch.setattr(settings, "EXECUTION_MODE", "dry_run")

    mock_client = MagicMock(spec=RazorpayClient)
    executor = RazorpayPaymentLinkExecutor(client=mock_client)

    result = executor.execute(sample_request)

    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.provider == "DRY_RUN"
    assert result.provider_reference.startswith("sim_plink_")
    assert "https://simulated.pay/i/" in result.provider_url
    assert result.execution_metadata["simulated"] is True
    # Ensure no external client methods were invoked
    mock_client.create_payment_link_sync.assert_not_called()


def test_razorpay_test_mode_creates_link_with_paise_conversion(sample_request: ExecutionRequest, monkeypatch):
    """Verify that razorpay_test mode correctly converts rupees to paise and parses provider output."""
    monkeypatch.setattr(settings, "EXECUTION_MODE", "razorpay_test")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_validkey123")

    mock_client = MagicMock(spec=RazorpayClient)
    mock_client.create_payment_link_sync.return_value = {
        "id": "plink_test_998877",
        "short_url": "https://rzp.io/i/test9988",
        "status": "created",
        "amount": 50000,
        "currency": "INR",
    }

    executor = RazorpayPaymentLinkExecutor(client=mock_client)
    result = executor.execute(sample_request)

    assert result.status == ExecutionStatus.SUCCEEDED
    assert result.provider == "RAZORPAY"
    assert result.provider_reference == "plink_test_998877"
    assert result.provider_url == "https://rzp.io/i/test9988"

    # Verify amount was passed in paise (₹500.00 -> 50000)
    mock_client.create_payment_link_sync.assert_called_once_with(
        amount_paise=50000,
        currency="INR",
        description="Revenue recovery link for Case case_tes",
        customer_name="Priya Sharma",
        customer_email="priya@example.com",
        customer_phone="+919876500001",
        reference_id="case_test_01",
    )


def test_razorpay_api_failure_returns_failed_execution_result(sample_request: ExecutionRequest, monkeypatch):
    """Verify that gateway API errors gracefully translate into FAILED ExecutionResult without crashing."""
    monkeypatch.setattr(settings, "EXECUTION_MODE", "razorpay_test")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_validkey123")

    mock_client = MagicMock(spec=RazorpayClient)
    mock_client.create_payment_link_sync.side_effect = RazorpayAPIError(
        status_code=400,
        message="Currency not supported",
        error_payload={"error": {"code": "BAD_REQUEST_ERROR"}},
    )

    executor = RazorpayPaymentLinkExecutor(client=mock_client)
    result = executor.execute(sample_request)

    assert result.status == ExecutionStatus.FAILED
    assert result.provider == "RAZORPAY"
    assert result.error_code == "RAZORPAY_API_400"
    assert "Currency not supported" in result.error_message


def test_non_test_credentials_blocked_in_test_mode(sample_request: ExecutionRequest, monkeypatch):
    """Security check: Non-test credentials in test mode are rejected before execution."""
    monkeypatch.setattr(settings, "EXECUTION_MODE", "razorpay_test")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_live_dangerouskey")

    mock_client = MagicMock(spec=RazorpayClient)
    executor = RazorpayPaymentLinkExecutor(client=mock_client)

    result = executor.execute(sample_request)

    assert result.status == ExecutionStatus.FAILED
    assert result.error_code == "INVALID_TEST_CREDENTIALS"
    mock_client.create_payment_link_sync.assert_not_called()
