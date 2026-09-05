"""Unit tests for unified PaymentNormalizer logic."""
from decimal import Decimal
import pytest

from app.integrations.razorpay.payment_normalizer import PaymentNormalizer, NormalizedPaymentData


def test_normalize_api_captured_card_payment():
    """Verify normalizing a captured card payment from Razorpay API entity format."""
    raw_entity = {
        "id": "pay_test_card_01",
        "entity": "payment",
        "amount": 250000,  # 2500.00 INR in paise
        "currency": "INR",
        "status": "captured",
        "order_id": "order_test_99",
        "method": "card",
        "captured": True,
        "bank": "HDFC",
        "wallet": None,
        "vpa": None,
        "email": "rohit@example.com",
        "contact": "+919876543210",
        "notes": {
            "customer_name": "Rohit Kumar",
            "customer_id": "cust_internal_44",
        },
        "error_code": None,
        "error_description": None,
        "created_at": 1716300000,
    }

    norm = PaymentNormalizer.normalize_entity(raw_entity)

    assert norm.external_payment_id == "pay_test_card_01"
    assert norm.razorpay_order_id == "order_test_99"
    assert norm.amount == Decimal("2500.00")
    assert norm.currency == "INR"
    assert norm.status == "CAPTURED"
    assert norm.payment_method == "CARD"
    assert norm.bank == "HDFC"
    assert norm.captured is True
    assert norm.customer_email == "rohit@example.com"
    assert norm.customer_phone == "+919876543210"
    assert norm.customer_name == "Rohit Kumar"
    assert norm.customer_id_ext == "cust_internal_44"
    assert norm.paid_at is not None


def test_normalize_api_failed_upi_payment():
    """Verify normalizing a failed UPI payment with diagnostic error codes."""
    raw_entity = {
        "id": "pay_test_upi_02",
        "entity": "payment",
        "amount": 100000,  # 1000.00 INR
        "currency": "INR",
        "status": "failed",
        "method": "upi",
        "vpa": "rohit@okhdfcbank",
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Payment was declined by customer bank.",
        "error_source": "customer",
        "error_step": "payment_authorization",
        "error_reason": "insufficient_funds",
        "created_at": 1716301000,
    }

    norm = PaymentNormalizer.normalize_entity(raw_entity)

    assert norm.external_payment_id == "pay_test_upi_02"
    assert norm.amount == Decimal("1000.00")
    assert norm.status == "FAILED"
    assert norm.payment_method == "UPI"
    assert norm.vpa == "rohit@okhdfcbank"
    assert norm.error_code == "BAD_REQUEST_ERROR"
    assert norm.error_reason == "insufficient_funds"
    assert norm.error_source == "customer"
    assert norm.error_step == "payment_authorization"
    assert norm.captured is False
    assert norm.paid_at is None


def test_normalize_missing_id_raises_value_error():
    """Verify that entity without an ID raises ValueError."""
    with pytest.raises(ValueError, match="missing required 'id' field"):
        PaymentNormalizer.normalize_entity({"amount": 500})
