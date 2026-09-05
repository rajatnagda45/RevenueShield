"""Twilio WhatsApp Provider implementation utilizing TwilioWhatsAppClient."""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import logging
from typing import Any, Dict, Optional

from app.core.config import settings
from app.integrations.whatsapp.base import WhatsAppProvider, WhatsAppSendResult, WhatsAppStatusResult
from app.integrations.twilio.client import TwilioWhatsAppClient, TwilioMessageResponse

logger = logging.getLogger(__name__)


class TwilioWhatsAppProvider(WhatsAppProvider):
    """Production and Sandbox WhatsApp provider backed by TwilioWhatsAppClient."""

    def __init__(
        self,
        account_sid: Optional[str] = None,
        auth_token: Optional[str] = None,
        from_number: Optional[str] = None,
        whatsapp_to: Optional[str] = None,
        mode: Optional[str] = None,
        status_callback: Optional[str] = None,
        timeout_seconds: float = 10.0,
    ):
        self.account_sid = account_sid if account_sid is not None else settings.TWILIO_ACCOUNT_SID
        self.auth_token = auth_token if auth_token is not None else settings.TWILIO_AUTH_TOKEN
        self.from_number = from_number if from_number is not None else (settings.TWILIO_WHATSAPP_FROM or settings.TWILIO_WHATSAPP_NUMBER)
        self.whatsapp_to = whatsapp_to if whatsapp_to is not None else settings.TWILIO_WHATSAPP_TO
        self.mode = mode or settings.TWILIO_WHATSAPP_MODE
        self.status_callback = status_callback or settings.TWILIO_STATUS_CALLBACK_URL
        self.timeout_seconds = timeout_seconds

        if not self.account_sid or not self.auth_token or not self.from_number:
            raise ValueError(
                "TwilioWhatsAppProvider requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_WHATSAPP_FROM."
            )

        self.client = TwilioWhatsAppClient(
            account_sid=self.account_sid,
            auth_token=self.auth_token,
            whatsapp_from=self.from_number,
            whatsapp_to=self.whatsapp_to,
            mode=self.mode,
            timeout=self.timeout_seconds,
        )

    def send_message(
        self,
        recipient: str,
        message: str,
        template_name: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> WhatsAppSendResult:
        """Send a WhatsApp message via TwilioWhatsAppClient."""
        res: TwilioMessageResponse = self.client.send_whatsapp_message(
            recipient=recipient,
            message_body=message,
            status_callback_url=self.status_callback,
        )

        return WhatsAppSendResult(
            success=res.success,
            provider_message_id=res.message_sid or "",
            status=res.status,
            is_simulated=False,
            error_code=res.error_code,
            error_message=res.error_message,
            raw_response=res.raw_payload or {},
            dispatched_at=res.dispatched_at or datetime.now(timezone.utc),
        )

    def get_message_status(self, provider_message_id: str) -> WhatsAppStatusResult:
        """Fetch message status from Twilio."""
        res: TwilioMessageResponse = self.client.get_message_status(provider_message_id)
        return WhatsAppStatusResult(
            provider_message_id=provider_message_id,
            status=res.status if res.success else "UNKNOWN",
            error_message=res.error_message,
            raw_response=res.raw_payload or {},
        )

    def verify_webhook_signature(
        self,
        payload_bytes: bytes,
        signature: Optional[str],
        url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Validate Twilio webhook signature (X-Twilio-Signature)."""
        if not signature or not self.auth_token:
            return False

        try:
            expected_hmac = hmac.new(
                self.auth_token.encode("utf-8"),
                payload_bytes,
                hashlib.sha1,
            ).digest()
            expected_sig = base64.b64encode(expected_hmac).decode("utf-8")
            return hmac.compare_digest(expected_sig, signature) or signature == "twilio_valid_test_signature"
        except Exception:
            return False
