"""Unit tests for WhatsApp provider abstraction (Development and Twilio)."""
from datetime import datetime, timezone
import pytest

from app.integrations.whatsapp.base import WhatsAppProvider, WhatsAppSendResult, WhatsAppStatusResult
from app.integrations.whatsapp.development_provider import DevelopmentWhatsAppProvider
from app.integrations.whatsapp.twilio_provider import TwilioWhatsAppProvider
from app.integrations.whatsapp import get_whatsapp_provider


def test_development_whatsapp_provider_lifecycle():
    """Verify DevelopmentWhatsAppProvider records simulated dispatch and maintains internal state."""
    provider = DevelopmentWhatsAppProvider()
    result = provider.send_message(
        recipient="+919876543210",
        message="Hi Rahul, please pay here: https://rzp.io/i/plink_123",
        template_name="PAYMENT_RECOVERY_EN_V1",
    )

    assert result.success is True
    assert result.status == "SENT"
    assert result.is_simulated is True
    assert result.provider_message_id.startswith("wa_sim_")
    assert result.provider_message_id in provider.dispatched_messages

    # Query status
    status_res = provider.get_message_status(result.provider_message_id)
    assert status_res.status == "SENT"


def test_development_whatsapp_provider_simulated_failure():
    """Verify DevelopmentWhatsAppProvider handles simulated provider failure."""
    provider = DevelopmentWhatsAppProvider(simulate_failure=True)
    result = provider.send_message(
        recipient="+919876543210",
        message="Hi Rahul, please pay here: https://rzp.io/i/plink_123",
        template_name="PAYMENT_RECOVERY_EN_V1",
    )

    assert result.success is False
    assert result.status == "FAILED"
    assert result.error_code == "SIMULATED_PROVIDER_ERROR"


def test_twilio_provider_requires_credentials():
    """Verify TwilioWhatsAppProvider validates credential presence."""
    with pytest.raises(ValueError, match="TwilioWhatsAppProvider requires"):
        TwilioWhatsAppProvider(account_sid="", auth_token="", from_number="")


def test_provider_factory_fallback():
    """Verify get_whatsapp_provider falls back safely to DevelopmentWhatsAppProvider."""
    # When mode is dry_run or development
    p_dev = get_whatsapp_provider(mode_override="dry_run")
    assert isinstance(p_dev, DevelopmentWhatsAppProvider)

    # When mode is twilio but credentials missing
    p_tw = get_whatsapp_provider(mode_override="twilio")
    assert isinstance(p_tw, DevelopmentWhatsAppProvider)
