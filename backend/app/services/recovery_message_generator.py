"""Deterministic recovery message generator supporting English and Hinglish templates."""
from dataclasses import dataclass
from decimal import Decimal
import logging
from typing import Optional

from app.models.customer import Customer
from app.models.recovery_case import RecoveryCase
from app.services.notification_service import mask_contact

logger = logging.getLogger(__name__)


@dataclass
class MessageDraft:
    """Standardized recovery message draft ready for policy verification and dispatch."""

    template_name: str
    template_version: str
    language: str
    message_body: str
    recipient_raw: str
    recipient_masked: str
    payment_link_url: str
    amount: Decimal
    currency: str
    customer_first_name: Optional[str] = None


class RecoveryMessageGenerator:
    """Generates customer-safe, personalized, deterministic payment recovery messages."""

    TEMPLATE_VERSION = "v1.0"

    TEMPLATES = {
        "ENGLISH": {
            "name": "PAYMENT_RECOVERY_EN_V1",
            "with_name": "Hi {first_name}, your payment of {formatted_amount} could not be completed. You can securely complete it here: {payment_link}",
            "generic": "Your payment of {formatted_amount} could not be completed. You can securely complete it here: {payment_link}",
        },
        "HINGLISH": {
            "name": "PAYMENT_RECOVERY_HI_V1",
            "with_name": "Hi {first_name}, aapka {formatted_amount} ka payment complete nahi ho paya. Aap yahan se securely payment complete kar sakte hain: {payment_link}",
            "generic": "Aapka {formatted_amount} ka payment complete nahi ho paya. Aap yahan se securely payment complete kar sakte hain: {payment_link}",
        },
    }

    @classmethod
    def generate(
        cls,
        recovery_case: RecoveryCase,
        payment_link_url: str,
        language: str = "ENGLISH",
        customer_override: Optional[Customer] = None,
    ) -> MessageDraft:
        """Generate an auditable, customer-safe MessageDraft for WhatsApp outreach.

        GUARANTEE: No technical failure codes (e.g. BAD_REQUEST_ERROR), diagnosis details,
        or internal AI prediction metrics are ever exposed in the customer-facing message.
        """
        customer = customer_override or recovery_case.customer
        lang_key = language.upper()
        if lang_key not in cls.TEMPLATES:
            lang_key = "ENGLISH"

        # 1. Format Currency Amount
        amount = recovery_case.amount_at_risk or Decimal("0.00")
        currency = (recovery_case.currency or "INR").upper()
        curr_symbol = "₹" if currency == "INR" else f"{currency} "
        formatted_amount = f"{curr_symbol}{amount:,.2f}"

        # 2. Extract Customer First Name safely
        raw_name = customer.name.strip() if (customer and customer.name) else ""
        first_name = raw_name.split()[0].title() if raw_name else None

        # 3. Select Template and Render
        template_cfg = cls.TEMPLATES[lang_key]
        template_name = template_cfg["name"]

        if first_name:
            body = template_cfg["with_name"].format(
                first_name=first_name,
                formatted_amount=formatted_amount,
                payment_link=payment_link_url,
            )
        else:
            body = template_cfg["generic"].format(
                formatted_amount=formatted_amount,
                payment_link=payment_link_url,
            )

        # 4. Resolve Recipient
        recipient_raw = (customer.phone if customer else "") or ""
        recipient_masked = mask_contact(recipient_raw)

        # 5. Anti-Leakage Safety Verification
        forbidden_tokens = [
            "BAD_REQUEST_ERROR",
            "AUTHENTICATION_FAILURE",
            "risk_score",
            "prediction",
            "model_version",
            "recovery_case_id",
            "diagnosis",
        ]
        for token in forbidden_tokens:
            if token.lower() in body.lower():
                raise ValueError(f"CRITICAL: Forbidden internal token '{token}' detected in customer message draft.")

        return MessageDraft(
            template_name=template_name,
            template_version=cls.TEMPLATE_VERSION,
            language=lang_key,
            message_body=body,
            recipient_raw=recipient_raw,
            recipient_masked=recipient_masked,
            payment_link_url=payment_link_url,
            amount=amount,
            currency=currency,
            customer_first_name=first_name,
        )
