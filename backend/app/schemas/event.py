"""Internal normalized event schemas decoupled from external payment gateways."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class NormalizedEvent(BaseModel):
    """Canonical internal representation of an incoming payment or billing lifecycle event."""

    event_id: str = Field(..., description="Unique event identifier (idempotency key)")
    event_type: str = Field(..., description="Normalized event type, e.g. payment.failed, payment.captured")
    source: str = Field(default="RAZORPAY", description="Originating provider/gateway")
    occurred_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Event creation timestamp",
    )

    # Financial details (in standard currency units, e.g. 500.00)
    amount: Decimal = Field(default=Decimal("0.00"), description="Transaction amount in standard units")
    currency: str = Field(default="INR", description="Three-letter ISO currency code")

    # Customer identifiers
    external_customer_id: Optional[str] = Field(None, description="Gateway or external customer ID")
    customer_email: Optional[str] = Field(None, description="Customer email address")
    customer_phone: Optional[str] = Field(None, description="Customer phone / contact number")
    customer_name: Optional[str] = Field(None, description="Customer display name")

    # Entity associations
    external_payment_id: Optional[str] = Field(None, description="Gateway payment ID (e.g. pay_xxx)")
    external_order_id: Optional[str] = Field(None, description="Gateway order ID (e.g. order_xxx)")
    external_invoice_id: Optional[str] = Field(None, description="Gateway or internal invoice ID")
    external_subscription_id: Optional[str] = Field(None, description="Gateway subscription ID")

    # Transaction metadata
    payment_method: Optional[str] = Field(None, description="Payment method: CARD, UPI, NETBANKING, etc.")
    payment_status: Optional[str] = Field(None, description="Normalized status: SUCCESS, FAILED, CAPTURED")

    # Failure diagnostic details (for payment.failed)
    failure_code: Optional[str] = Field(None, description="Error or decline code from gateway")
    failure_description: Optional[str] = Field(None, description="Human-readable failure description")
    failure_source: Optional[str] = Field(None, description="Error origin: customer, gateway, bank, etc.")
    failure_step: Optional[str] = Field(None, description="Step in the lifecycle where failure occurred")
    failure_reason: Optional[str] = Field(None, description="Detailed gateway error reason")

    # Batch / surface tagging (v2 measurement)
    batch_id: Optional[str] = Field(None, description="Batch identifier for scorecard grouping (from notes.batch_id)")

    # Context & Audit
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Normalized supplementary context")
    raw_payload: Dict[str, Any] = Field(default_factory=dict, description="Preserved raw gateway payload")


class WebhookProcessingResult(BaseModel):
    """Result of webhook event processing."""

    status: str = Field(..., description="Processing outcome: 'processed', 'duplicate', or 'ignored'")
    event_id: str = Field(..., description="External event identifier")
    internal_event_id: Optional[str] = Field(None, description="Database UUID of the stored Event")
    recovery_case_id: Optional[str] = Field(None, description="Associated RecoveryCase UUID if created/updated")
    message: str = Field(default="Event processed successfully")
