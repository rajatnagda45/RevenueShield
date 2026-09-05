"""Unit tests for Razorpay webhook signature verification & security module."""
import pytest
from app.integrations.razorpay.security import (
    compute_razorpay_signature,
    verify_razorpay_signature,
    RazorpaySecurityError,
)

SAMPLE_SECRET = "super_secret_webhook_key_123"
SAMPLE_BODY = b'{"event":"payment.failed","account_id":"acc_test","payload":{"payment":{"entity":{"id":"pay_001"}}}}'


def test_signature_verification_succeeds_with_valid_signature():
    """Verify that a genuine HMAC-SHA256 signature generated with the secret validates successfully."""
    expected_sig = compute_razorpay_signature(SAMPLE_BODY, SAMPLE_SECRET)
    assert verify_razorpay_signature(SAMPLE_BODY, expected_sig, SAMPLE_SECRET) is True


def test_signature_verification_fails_with_modified_body():
    """Verify that any tampering with the body payload fails verification."""
    original_sig = compute_razorpay_signature(SAMPLE_BODY, SAMPLE_SECRET)
    tampered_body = b'{"event":"payment.captured","account_id":"acc_test","payload":{"payment":{"entity":{"id":"pay_001"}}}}'
    assert verify_razorpay_signature(tampered_body, original_sig, SAMPLE_SECRET) is False


def test_signature_verification_fails_with_invalid_signature():
    """Verify that an arbitrary or forged signature is rejected."""
    forged_sig = "a" * 64
    assert verify_razorpay_signature(SAMPLE_BODY, forged_sig, SAMPLE_SECRET) is False


def test_signature_verification_fails_with_wrong_secret():
    """Verify that signature generated with a different secret fails."""
    sig = compute_razorpay_signature(SAMPLE_BODY, "wrong_secret")
    assert verify_razorpay_signature(SAMPLE_BODY, sig, SAMPLE_SECRET) is False


def test_signature_verification_handles_empty_inputs_safely():
    """Verify that empty signature, empty body, or missing secret returns False without raising unhandled errors."""
    assert verify_razorpay_signature(SAMPLE_BODY, "", SAMPLE_SECRET) is False
    assert verify_razorpay_signature(SAMPLE_BODY, None, SAMPLE_SECRET) is False  # type: ignore[arg-type]
    assert verify_razorpay_signature(b"", "somesig", SAMPLE_SECRET) is False
    assert verify_razorpay_signature(SAMPLE_BODY, "somesig", "") is False


def test_compute_signature_raises_on_empty_secret():
    """Verify compute_razorpay_signature raises RazorpaySecurityError if secret is missing."""
    with pytest.raises(RazorpaySecurityError, match="Webhook secret cannot be empty"):
        compute_razorpay_signature(SAMPLE_BODY, "")
