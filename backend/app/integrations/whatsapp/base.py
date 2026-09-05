"""Abstract base class and data transfer objects for WhatsApp communication providers."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class WhatsAppSendResult:
    """Standardized response from WhatsApp provider dispatch."""

    success: bool
    provider_message_id: str
    status: str  # "QUEUED", "SENT", "FAILED"
    is_simulated: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    raw_response: Dict[str, Any] = field(default_factory=dict)
    dispatched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class WhatsAppStatusResult:
    """Standardized message delivery status query result."""

    provider_message_id: str
    status: str  # "SENT", "DELIVERED", "READ", "FAILED", "UNDELIVERED"
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    raw_response: Dict[str, Any] = field(default_factory=dict)


class WhatsAppProvider(ABC):
    """Abstract interface decoupling recovery business logic from specific WhatsApp gateways (Twilio, Meta, etc.)."""

    @abstractmethod
    def send_message(
        self,
        recipient: str,
        message: str,
        template_name: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> WhatsAppSendResult:
        """Send or simulate sending a WhatsApp message to a customer.

        Args:
            recipient: E.164 formatted customer phone number.
            message: Fully rendered, customer-safe message content.
            template_name: Auditable template identifier (e.g. PAYMENT_RECOVERY_EN_V1).
            context: Additional metadata.

        Returns:
            WhatsAppSendResult indicating provider acceptance status.
        """
        pass

    @abstractmethod
    def get_message_status(self, provider_message_id: str) -> WhatsAppStatusResult:
        """Retrieve delivery status for a previously dispatched message."""
        pass

    @abstractmethod
    def verify_webhook_signature(
        self,
        payload_bytes: bytes,
        signature: Optional[str],
        url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Verify the authenticity of an incoming status callback webhook."""
        pass
