"""Notification Service & Provider Abstraction for Customer Recovery Outreach.

Generates customer-friendly recovery messages and dispatches via configured provider.
Maintains audit logs in communication_logs table without exposing internal AI signals.
"""
from dataclasses import dataclass
from decimal import Decimal
import logging
from typing import Optional, Protocol
import uuid
from sqlalchemy.orm import Session

from app.models.communication_log import CommunicationLog

logger = logging.getLogger(__name__)


def mask_contact(contact: Optional[str]) -> str:
    """Mask phone numbers or emails for safe, privacy-compliant application logging.

    Examples:
        "+919876543210" -> "+9198****3210"
        "user@example.com" -> "u***r@example.com"
    """
    if not contact:
        return "[NOT_PROVIDED]"

    s = contact.strip()
    if "@" in s:
        # Email masking
        parts = s.split("@", 1)
        user, domain = parts[0], parts[1]
        if len(user) <= 2:
            masked_user = user[0] + "*"
        else:
            masked_user = user[0] + "*" * (len(user) - 2) + user[-1]
        return f"{masked_user}@{domain}"
    else:
        # Phone masking
        if len(s) <= 4:
            return "****"
        return s[:4] + "*" * max(0, len(s) - 8) + s[-4:]


@dataclass
class NotificationDispatchResult:
    """Result of a customer notification attempt."""

    status: str  # "GENERATED", "SENT", "DELIVERED", "FAILED"
    provider: str
    channel: str
    recipient_masked: str
    message_content: str
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None


class NotificationProvider(Protocol):
    """Protocol defining notification provider contract."""

    def send_payment_recovery_notification(
        self,
        customer_id: Optional[uuid.UUID],
        recipient: Optional[str],
        amount: Decimal,
        currency: str,
        payment_url: str,
        case_id: uuid.UUID,
        channel: str = "WHATSAPP",
    ) -> NotificationDispatchResult:
        ...


class DevelopmentNotificationProvider:
    """Development notification provider that generates and logs messages without external side-effects."""

    def __init__(self, provider_name: str = "DEVELOPMENT_SIMULATOR"):
        self.provider_name = provider_name

    def send_payment_recovery_notification(
        self,
        customer_id: Optional[uuid.UUID],
        recipient: Optional[str],
        amount: Decimal,
        currency: str,
        payment_url: str,
        case_id: uuid.UUID,
        channel: str = "WHATSAPP",
    ) -> NotificationDispatchResult:
        """Format recovery message and simulate dispatch."""
        curr_symbol = "₹" if currency.upper() == "INR" else f"{currency.upper()} "
        formatted_amount = f"{amount:,.2f}"

        # Clean customer-facing copy (strictly no AI diagnosis / risk metrics)
        message = (
            f"Your recent payment of {curr_symbol}{formatted_amount} could not be completed.\n"
            f"You can securely complete the payment here:\n"
            f"{payment_url}"
        )

        masked = mask_contact(recipient)
        msg_id = f"sim_msg_{uuid.uuid4().hex[:12]}"

        logger.info(
            f"[NOTIFICATION_DISPATCHED] Provider={self.provider_name} Channel={channel} "
            f"Case={case_id} Recipient={masked} MsgID={msg_id}"
        )

        return NotificationDispatchResult(
            status="SENT",
            provider=self.provider_name,
            channel=channel.upper(),
            recipient_masked=masked,
            message_content=message,
            provider_message_id=msg_id,
        )


class NotificationService:
    """Service facade for generating customer recovery notifications and recording logs."""

    _provider: NotificationProvider = DevelopmentNotificationProvider()

    @classmethod
    def set_provider(cls, provider: NotificationProvider) -> None:
        """Configure active notification provider."""
        cls._provider = provider

    @classmethod
    def get_provider(cls) -> NotificationProvider:
        """Get active notification provider."""
        return cls._provider

    @classmethod
    def send_recovery_notification(
        cls,
        db: Session,
        recovery_case_id: uuid.UUID,
        customer_id: Optional[uuid.UUID],
        recipient: Optional[str],
        amount: Decimal,
        currency: str,
        payment_url: str,
        channel: str = "WHATSAPP",
    ) -> NotificationDispatchResult:
        """Dispatch customer notification and persist CommunicationLog record."""
        result = cls._provider.send_payment_recovery_notification(
            customer_id=customer_id,
            recipient=recipient,
            amount=amount,
            currency=currency,
            payment_url=payment_url,
            case_id=recovery_case_id,
            channel=channel,
        )

        # Persist communication log entry in DB
        comm_log = CommunicationLog(
            recovery_case_id=recovery_case_id,
            customer_id=customer_id,
            channel=channel.upper(),
            direction="OUTBOUND",
            provider_message_id=result.provider_message_id,
            content=result.message_content,
            status=result.status,
        )
        db.add(comm_log)
        db.flush()

        return result
