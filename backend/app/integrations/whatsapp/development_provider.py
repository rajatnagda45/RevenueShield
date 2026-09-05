"""Development & simulated WhatsApp provider for testing and dry-run environments."""
from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional
import uuid

from app.integrations.whatsapp.base import WhatsAppProvider, WhatsAppSendResult, WhatsAppStatusResult
from app.services.notification_service import mask_contact

logger = logging.getLogger(__name__)


class DevelopmentWhatsAppProvider(WhatsAppProvider):
    """Simulated WhatsApp provider that logs messages cleanly without calling external APIs."""

    def __init__(self, simulate_failure: bool = False):
        self.simulate_failure = simulate_failure
        self.dispatched_messages: Dict[str, Dict[str, Any]] = {}

    def send_message(
        self,
        recipient: str,
        message: str,
        template_name: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> WhatsAppSendResult:
        """Simulate WhatsApp message dispatch."""
        masked_phone = mask_contact(recipient)

        if self.simulate_failure:
            logger.warning(f"[DEVELOPMENT_WHATSAPP_SIMULATION_FAILED] Recipient={masked_phone} Template={template_name}")
            return WhatsAppSendResult(
                success=False,
                provider_message_id=f"wa_sim_fail_{uuid.uuid4().hex[:8]}",
                status="FAILED",
                is_simulated=True,
                error_code="SIMULATED_PROVIDER_ERROR",
                error_message="Development provider configured to simulate network failure.",
                raw_response={"simulated": True, "error": True},
            )

        provider_msg_id = f"wa_sim_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)

        self.dispatched_messages[provider_msg_id] = {
            "recipient": recipient,
            "recipient_masked": masked_phone,
            "message": message,
            "template_name": template_name,
            "context": context or {},
            "status": "SENT",
            "sent_at": now,
            "delivered_at": None,
            "read_at": None,
        }

        logger.info(
            f"[DEVELOPMENT_WHATSAPP_DISPATCH] id={provider_msg_id} "
            f"recipient={masked_phone} template={template_name} "
            f"simulated=true"
        )

        return WhatsAppSendResult(
            success=True,
            provider_message_id=provider_msg_id,
            status="SENT",
            is_simulated=True,
            raw_response={
                "provider": "DEVELOPMENT",
                "simulated": True,
                "message_id": provider_msg_id,
                "template": template_name,
            },
            dispatched_at=now,
        )

    def get_message_status(self, provider_message_id: str) -> WhatsAppStatusResult:
        """Retrieve simulated status."""
        msg = self.dispatched_messages.get(provider_message_id)
        if not msg:
            return WhatsAppStatusResult(
                provider_message_id=provider_message_id,
                status="UNDELIVERED",
                error_message="Message ID not found in development simulation memory.",
            )

        return WhatsAppStatusResult(
            provider_message_id=provider_message_id,
            status=msg["status"],
            delivered_at=msg.get("delivered_at"),
            read_at=msg.get("read_at"),
            raw_response=msg,
        )

    def verify_webhook_signature(
        self,
        payload_bytes: bytes,
        signature: Optional[str],
        url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Simulated signature verification for development testing."""
        if signature == "invalid_sig":
            return False
        return True
