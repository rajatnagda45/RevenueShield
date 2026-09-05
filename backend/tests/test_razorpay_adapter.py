"""Unit tests for Razorpay adapter payload normalization."""
from decimal import Decimal
from datetime import datetime, timezone
from app.integrations.razorpay.adapter import RazorpayAdapter


def test_normalize_payment_failed_event():
    """Verify that a Razorpay payment.failed payload is accurately mapped to NormalizedEvent."""
    raw_payload = {
        "entity": "event",
        "account_id": "acc_test_123",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_failed_test_99",
                    "entity": "payment",
                    "amount": 75000,  # 750.00 INR
                    "currency": "INR",
                    "status": "failed",
                    "order_id": "order_test_456",
                    "invoice_id": "inv_test_789",
                    "international": False,
                    "method": "card",
                    "amount_refunded": 0,
                    "captured": False,
                    "description": "Subscription Renewal",
                    "card_id": "card_test_11",
                    "bank": "HDFC",
                    "email": "customer@apexlabs.com",
                    "contact": "+919876543210",
                    "notes": {
                        "customer_id": "cust_apex_01",
                        "customer_name": "Apex Labs",
                    },
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "Payment failed due to insufficient balance in your account.",
                    "error_source": "customer",
                    "error_step": "payment_authorization",
                    "error_reason": "insufficient_funds",
                    "created_at": 1716300000,
                }
            }
        },
        "created_at": 1716300000,
    }

    event = RazorpayAdapter.normalize(raw_payload, event_id_header="evt_header_id_101")

    assert event.event_id == "evt_header_id_101"
    assert event.event_type == "payment.failed"
    assert event.source == "RAZORPAY"
    assert event.amount == Decimal("750.00")
    assert event.currency == "INR"
    assert event.external_customer_id == "cust_apex_01"
    assert event.customer_email == "customer@apexlabs.com"
    assert event.customer_phone == "+919876543210"
    assert event.customer_name == "Apex Labs"
    assert event.external_payment_id == "pay_failed_test_99"
    assert event.external_order_id == "order_test_456"
    assert event.external_invoice_id == "inv_test_789"
    assert event.payment_method == "CARD"
    assert event.payment_status == "FAILED"
    assert event.failure_code == "BAD_REQUEST_ERROR"
    assert event.failure_description == "Payment failed due to insufficient balance in your account."
    assert event.failure_source == "customer"
    assert event.failure_reason == "insufficient_funds"
    assert event.raw_payload == raw_payload


def test_normalize_payment_captured_event():
    """Verify that a Razorpay payment.captured payload is normalized correctly."""
    raw_payload = {
        "entity": "event",
        "account_id": "acc_test_123",
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_captured_test_88",
                    "entity": "payment",
                    "amount": 120000,  # 1200.00 USD
                    "currency": "USD",
                    "status": "captured",
                    "order_id": "order_test_111",
                    "method": "upi",
                    "email": "user@gmail.com",
                    "contact": "+14155550199",
                    "created_at": 1716301000,
                }
            }
        },
        "created_at": 1716301000,
    }

    event = RazorpayAdapter.normalize(raw_payload, event_id_header="evt_header_cap_202")

    assert event.event_id == "evt_header_cap_202"
    assert event.event_type == "payment.captured"
    assert event.amount == Decimal("1200.00")
    assert event.currency == "USD"
    assert event.payment_method == "UPI"
    assert event.payment_status == "SUCCESS"
    assert event.customer_email == "user@gmail.com"
    assert event.external_payment_id == "pay_captured_test_88"


def test_normalize_fallback_event_id_generation():
    """Verify that when no header or payload event_id is supplied, a deterministic fallback is created."""
    raw_payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_fallback_001",
                    "amount": 5000,
                    "currency": "INR",
                    "created_at": 1716300000,
                }
            }
        },
    }

    event = RazorpayAdapter.normalize(raw_payload, event_id_header=None)
    assert event.event_id.startswith("rzp_evt_payment.failed_pay_fallback_001_")
