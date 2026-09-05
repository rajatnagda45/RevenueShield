"""WhatsApp integrations package with provider factory."""
import logging
from typing import Optional

from app.core.config import settings
from app.integrations.whatsapp.base import WhatsAppProvider, WhatsAppSendResult, WhatsAppStatusResult
from app.integrations.whatsapp.development_provider import DevelopmentWhatsAppProvider
from app.integrations.whatsapp.twilio_provider import TwilioWhatsAppProvider

logger = logging.getLogger(__name__)


def get_whatsapp_provider(mode_override: Optional[str] = None) -> WhatsAppProvider:
    """Factory to instantiate the appropriate WhatsApp provider based on configuration.

    Resolution logic:
    1. If mode is "dry_run" or "development" -> DevelopmentWhatsAppProvider.
    2. If mode is "twilio", check credentials:
       - If valid credentials present -> TwilioWhatsAppProvider.
       - Otherwise -> log warning and fallback to DevelopmentWhatsAppProvider.
    """
    mode = (mode_override or settings.COMMUNICATION_MODE or "dry_run").lower()

    if mode == "twilio":
        if (
            settings.TWILIO_ACCOUNT_SID
            and settings.TWILIO_AUTH_TOKEN
            and settings.TWILIO_WHATSAPP_NUMBER
        ):
            try:
                return TwilioWhatsAppProvider()
            except Exception as exc:
                logger.warning(
                    f"[WHATSAPP_PROVIDER_INIT_FAILED] Failed to initialize TwilioWhatsAppProvider: {exc}. "
                    "Falling back to DevelopmentWhatsAppProvider."
                )
        else:
            logger.warning(
                "[WHATSAPP_PROVIDER_CONFIG_MISSING] Twilio credentials missing. "
                "Falling back to DevelopmentWhatsAppProvider."
            )

    return DevelopmentWhatsAppProvider()


__all__ = [
    "WhatsAppProvider",
    "WhatsAppSendResult",
    "WhatsAppStatusResult",
    "DevelopmentWhatsAppProvider",
    "TwilioWhatsAppProvider",
    "get_whatsapp_provider",
]
