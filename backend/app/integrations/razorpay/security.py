"""Razorpay webhook security & cryptographic signature verification."""
import hmac
import hashlib
from typing import Union


class RazorpaySecurityError(Exception):
    """Base exception for Razorpay security and signature verification errors."""
    pass


def compute_razorpay_signature(body: Union[bytes, str], secret: str) -> str:
    """Compute the HMAC-SHA256 hex digest for a raw webhook payload.

    Args:
        body: The exact raw request body bytes (or string if already decoded).
        secret: The Razorpay webhook secret.

    Returns:
        The computed HMAC-SHA256 hexadecimal string.
    """
    if not secret:
        raise RazorpaySecurityError("Webhook secret cannot be empty.")

    if isinstance(body, str):
        payload_bytes = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray)):
        payload_bytes = bytes(body)
    else:
        raise RazorpaySecurityError("Payload body must be bytes or string.")

    secret_bytes = secret.encode("utf-8")
    return hmac.new(secret_bytes, payload_bytes, hashlib.sha256).hexdigest()


def verify_razorpay_signature(
    body: Union[bytes, str], signature: str, secret: str
) -> bool:
    """Verify that an incoming webhook request was genuinely signed by Razorpay.

    Uses timing-safe comparison (hmac.compare_digest) against the raw request body.

    Args:
        body: The exact raw binary bytes of the incoming HTTP request body.
        signature: The received value of the 'X-Razorpay-Signature' header.
        secret: The configured RAZORPAY_WEBHOOK_SECRET.

    Returns:
        True if the signature is authentic, False otherwise.
    """
    if not signature or not secret:
        return False

    try:
        expected_signature = compute_razorpay_signature(body, secret)
        return hmac.compare_digest(expected_signature, signature)
    except Exception:
        return False
