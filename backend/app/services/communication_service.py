"""CommunicationService provider abstraction dispatching actions across channels."""
from datetime import datetime
from enum import Enum
import logging
from typing import Any, Dict, Optional
import uuid

from sqlalchemy.orm import Session

from app.services.email_recovery_service import EmailRecoveryService

logger = logging.getLogger(__name__)


class RecoveryChannel(str, Enum):
    """Supported recovery outreach channels."""

    EMAIL = "EMAIL"
    WHATSAPP = "WHATSAPP"
    VOICE = "VOICE"
    NONE = "NONE"


class ActionType(str, Enum):
    """Supported recovery intervention action types."""

    EMAIL_PAYMENT_RECOVERY = "EMAIL_PAYMENT_RECOVERY"
    EMAIL_FOLLOWUP = "EMAIL_FOLLOWUP"
    WHATSAPP_PAYMENT_RECOVERY = "WHATSAPP_PAYMENT_RECOVERY"
    VOICE_RECOVERY = "VOICE_RECOVERY"
    PROMISE_TO_PAY_FOLLOWUP = "PROMISE_TO_PAY_FOLLOWUP"
    PAYMENT_LINK_RETRY = "PAYMENT_LINK_RETRY"
    B2B_RECEIVABLES_ESCALATION = "B2B_RECEIVABLES_ESCALATION"
    NO_ACTION = "NO_ACTION"


class CommunicationService:
    """Unified service dispatching recovery communications to underlying channel handlers."""

    @classmethod
    def dispatch_action(
        cls,
        db: Session,
        case_id: str,
        action_type: str,
        channel: str,
        recipient: Optional[str] = None,
        dry_run: Optional[bool] = None,
        reference_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Dispatch action to appropriate communication provider."""
        channel_upper = channel.upper()
        action_upper = action_type.upper()

        logger.info(f"[COMMUNICATION_DISPATCH] Case={case_id} Action={action_upper} Channel={channel_upper}")

        if action_upper == ActionType.NO_ACTION.value or channel_upper == RecoveryChannel.NONE.value:
            return {
                "success": True,
                "status": "NO_ACTION",
                "reason": "No customer outreach executed.",
            }

        if channel_upper == RecoveryChannel.EMAIL.value or action_upper in [
            ActionType.EMAIL_PAYMENT_RECOVERY.value,
            ActionType.EMAIL_FOLLOWUP.value,
        ]:
            if dry_run:
                return {
                    "success": True,
                    "status": "SENT",
                    "provider": "DRY_RUN",
                    "payment_link_url": "https://rzp.io/rzp/dryrun_link",
                }
            return EmailRecoveryService.execute_recovery(
                db=db,
                case_id=case_id,
                recipient_email=recipient,
                reference_time=reference_time,
            )

        if channel_upper == RecoveryChannel.WHATSAPP.value or action_upper == ActionType.WHATSAPP_PAYMENT_RECOVERY.value:
            from app.services.whatsapp_recovery_service import WhatsAppRecoveryService
            wa_res = WhatsAppRecoveryService.execute_recovery(
                db=db,
                recovery_case_id=uuid.UUID(case_id),
                dry_run=dry_run,
                reference_time=reference_time,
            )
            return {
                "success": wa_res.status == "SENT",
                "status": wa_res.status,
                "communication": wa_res.communication,
                "payment_link": wa_res.payment_link,
                "reason": wa_res.reason,
                "policy_blocking_rule": wa_res.policy_blocking_rule,
            }

        return {
            "success": False,
            "status": "FAILED",
            "error": f"Unsupported channel '{channel}' for action '{action_type}'.",
        }
