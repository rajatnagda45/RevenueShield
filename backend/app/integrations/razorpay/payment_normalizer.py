"""Unified Payment Normalizer.

Transforms raw payment dictionaries (from both Razorpay REST API responses and Webhooks)
into a canonical NormalizedPaymentData structure.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class NormalizedPaymentData:
    """Canonical internal representation of a payment transaction."""

    external_payment_id: str
    razorpay_order_id: Optional[str]
    amount: Decimal
    currency: str
    status: str  # CREATED, AUTHORIZED, CAPTURED, REFUNDED, FAILED
    payment_method: str
    bank: Optional[str]
    wallet: Optional[str]
    vpa: Optional[str]
    international: bool
    captured: bool
    amount_refunded: Decimal
    refund_status: Optional[str]
    description: Optional[str]
    error_code: Optional[str]
    error_description: Optional[str]
    error_source: Optional[str]
    error_step: Optional[str]
    error_reason: Optional[str]
    paid_at: Optional[datetime]
    razorpay_created_at: datetime
    customer_id_ext: Optional[str]
    customer_email: Optional[str]
    customer_phone: Optional[str]
    customer_name: Optional[str]
    raw_payload: Dict[str, Any] = field(default_factory=dict)


class PaymentNormalizer:
    """Centralized normalizer for Razorpay payment entities."""

    @classmethod
    def normalize_entity(cls, entity: Dict[str, Any]) -> NormalizedPaymentData:
        """Normalize a raw Razorpay payment entity dictionary."""
        payment_id = entity.get("id")
        if not payment_id:
            raise ValueError("Payment entity is missing required 'id' field.")

        # 1. Amount normalization (subunits to decimal)
        raw_amount = entity.get("amount", 0)
        try:
            amount = (Decimal(str(raw_amount)) / Decimal("100")).quantize(Decimal("0.01"))
        except Exception:
            amount = Decimal("0.00")

        currency = (entity.get("currency") or "INR").upper()

        # 2. Status normalization
        raw_status = (entity.get("status") or "").lower()
        if raw_status in ("captured", "success"):
            status = "CAPTURED"
        elif raw_status == "failed":
            status = "FAILED"
        elif raw_status == "authorized":
            status = "AUTHORIZED"
        elif raw_status == "refunded":
            status = "REFUNDED"
        elif raw_status == "created":
            status = "CREATED"
        else:
            status = raw_status.upper() if raw_status else "UNKNOWN"

        captured = bool(entity.get("captured", False)) or (status == "CAPTURED")
        international = bool(entity.get("international", False))

        # 3. Refund normalization
        raw_refunded = entity.get("amount_refunded", 0)
        try:
            amount_refunded = (Decimal(str(raw_refunded)) / Decimal("100")).quantize(Decimal("0.01"))
        except Exception:
            amount_refunded = Decimal("0.00")
        refund_status = entity.get("refund_status")

        # 4. Method details
        raw_method = entity.get("method")
        payment_method = raw_method.upper() if raw_method else "CARD"
        bank = entity.get("bank")
        wallet = entity.get("wallet")
        vpa = entity.get("vpa")
        description = entity.get("description")

        # 5. Diagnostic / Error details
        error_code = entity.get("error_code")
        error_description = entity.get("error_description")
        error_source = entity.get("error_source")
        error_step = entity.get("error_step")
        error_reason = entity.get("error_reason")

        # 6. Timestamp handling
        epoch_created = entity.get("created_at")
        if epoch_created:
            razorpay_created_at = datetime.fromtimestamp(epoch_created, tz=timezone.utc)
        else:
            razorpay_created_at = datetime.now(timezone.utc)

        paid_at = razorpay_created_at if status == "CAPTURED" else None

        # 7. Customer & Order references
        notes = entity.get("notes", {}) or {}
        customer_id_ext = (
            notes.get("customer_id")
            or notes.get("external_customer_id")
            or entity.get("customer_id")
        )
        customer_email = entity.get("email") or notes.get("customer_email")
        customer_phone = entity.get("contact") or notes.get("customer_phone")
        customer_name = notes.get("customer_name") or entity.get("name")
        razorpay_order_id = entity.get("order_id") or notes.get("order_id")

        return NormalizedPaymentData(
            external_payment_id=payment_id,
            razorpay_order_id=razorpay_order_id,
            amount=amount,
            currency=currency,
            status=status,
            payment_method=payment_method,
            bank=bank,
            wallet=wallet,
            vpa=vpa,
            international=international,
            captured=captured,
            amount_refunded=amount_refunded,
            refund_status=refund_status,
            description=description,
            error_code=error_code,
            error_description=error_description,
            error_source=error_source,
            error_step=error_step,
            error_reason=error_reason,
            paid_at=paid_at,
            razorpay_created_at=razorpay_created_at,
            customer_id_ext=str(customer_id_ext) if customer_id_ext else None,
            customer_email=str(customer_email) if customer_email else None,
            customer_phone=str(customer_phone) if customer_phone else None,
            customer_name=str(customer_name) if customer_name else None,
            raw_payload=entity,
        )

    @classmethod
    def normalize_webhook(cls, payload: Dict[str, Any]) -> NormalizedPaymentData:
        """Extract payment entity from a webhook payload and normalize it."""
        payload_container = payload.get("payload", {})
        payment_container = payload_container.get("payment", {})
        payment_entity = payment_container.get("entity", {}) if isinstance(payment_container, dict) else {}

        if not payment_entity:
            # Check if payload itself is the payment entity
            if payload.get("entity") == "payment" or payload.get("id", "").startswith("pay_"):
                payment_entity = payload
            else:
                raise ValueError("Webhook payload does not contain a valid payment entity.")

        return cls.normalize_entity(payment_entity)
