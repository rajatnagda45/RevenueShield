"""WhatsApp communication orchestrator managing message lifecycles, provider dispatch, idempotency, and stopping rules."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, Optional
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.recovery_case import RecoveryCase
from app.models.communication import Communication
from app.models.recovery_payment_link import RecoveryPaymentLink
from app.models.audit_log import AuditLog
from app.integrations.whatsapp import get_whatsapp_provider, WhatsAppProvider, WhatsAppSendResult
from app.integrations.razorpay.payment_link_client import RazorpayPaymentLinkClient
from app.services.communication_scheduler import CommunicationScheduler
from app.services.recovery_message_generator import RecoveryMessageGenerator
from app.services.notification_service import mask_contact

logger = logging.getLogger(__name__)


@dataclass
class WhatsAppOutreachResult:
    """Standardized response from WhatsApp outreach execution."""

    case_id: str
    communication_id: Optional[str]
    channel: str
    status: str  # "QUEUED", "SENT", "DELIVERED", "BLOCKED", "FAILED", "ALREADY_RECOVERED"
    template_name: str
    template_version: str
    language: str
    recipient_masked: str
    message_body: str
    payment_link_url: Optional[str]
    is_simulated: bool
    provider: str
    provider_message_id: Optional[str] = None
    reason: Optional[str] = None
    policy_blocking_rule: Optional[str] = None


class CommunicationOrchestrator:
    """Orchestrates WhatsApp recovery communications with strict policy compliance, idempotency, and stopping rules."""

    @classmethod
    def preview_whatsapp_outreach(
        cls,
        db: Session,
        recovery_case_id: uuid.UUID,
        language: str = "ENGLISH",
        reference_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Generate a side-effect-free preview of proposed WhatsApp outreach."""
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == recovery_case_id))
        if not case:
            raise ValueError(f"RecoveryCase '{recovery_case_id}' not found.")

        # 1. Evaluate Policy
        policy_res = CommunicationScheduler.evaluate_outreach_policy(
            db=db,
            recovery_case=case,
            reference_time=reference_time,
        )

        # 2. Get existing or placeholder payment link
        active_link = db.scalar(
            select(RecoveryPaymentLink)
            .where(
                RecoveryPaymentLink.recovery_case_id == case.id,
                RecoveryPaymentLink.status.in_(["CREATED", "SENT"]),
            )
            .order_by(RecoveryPaymentLink.created_at.desc())
        )
        payment_url = active_link.payment_url if active_link else "https://rzp.io/i/plink_preview_placeholder"

        # 3. Generate Draft Message
        draft = RecoveryMessageGenerator.generate(
            recovery_case=case,
            payment_link_url=payment_url,
            language=language,
        )

        return {
            "case_id": str(case.id),
            "channel": "WHATSAPP",
            "language": draft.language,
            "template_name": draft.template_name,
            "template_version": draft.template_version,
            "recipient_masked": draft.recipient_masked,
            "message_body": draft.message_body,
            "payment_link_url": payment_url,
            "policy_status": "APPROVED" if policy_res.allowed else "BLOCKED",
            "policy_reasons": [policy_res.reason],
            "policy_blocking_rule": policy_res.blocking_rule,
            "attempt_number": policy_res.attempt_count + 1,
            "max_attempts": policy_res.max_attempts,
            "next_eligible_at": policy_res.next_eligible_at.isoformat() if policy_res.next_eligible_at else None,
        }

    @classmethod
    def queue_or_send_whatsapp_recovery(
        cls,
        db: Session,
        recovery_case_id: uuid.UUID,
        language: str = "ENGLISH",
        provider_override: Optional[WhatsAppProvider] = None,
        dry_run: Optional[bool] = None,
        reference_time: Optional[datetime] = None,
    ) -> WhatsAppOutreachResult:
        """Execute or simulate WhatsApp recovery outreach enforcing all policy and idempotency guards."""
        now = reference_time or datetime.now(timezone.utc)
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == recovery_case_id))
        if not case:
            raise ValueError(f"RecoveryCase '{recovery_case_id}' not found.")

        # 1. Evaluate Outreach Policy
        policy_res = CommunicationScheduler.evaluate_outreach_policy(
            db=db,
            recovery_case=case,
            reference_time=now,
        )

        cls._audit(
            db=db,
            case_id=case.id,
            action="WHATSAPP_POLICY_CHECKED",
            metadata={
                "allowed": policy_res.allowed,
                "reason": policy_res.reason,
                "blocking_rule": policy_res.blocking_rule,
                "attempt_count": policy_res.attempt_count,
            },
        )

        if not policy_res.allowed:
            cls._audit(
                db=db,
                case_id=case.id,
                action="WHATSAPP_BLOCKED",
                metadata={"reason": policy_res.reason, "blocking_rule": policy_res.blocking_rule},
            )
            return WhatsAppOutreachResult(
                case_id=str(case.id),
                communication_id=None,
                channel="WHATSAPP",
                status="BLOCKED",
                template_name="NONE",
                template_version="NONE",
                language=language.upper(),
                recipient_masked=mask_contact(case.customer.phone if case.customer else ""),
                message_body="",
                payment_link_url=None,
                is_simulated=True,
                provider="NONE",
                reason=policy_res.reason,
                policy_blocking_rule=policy_res.blocking_rule,
            )

        # 2. Resolve Active or Create New Payment Link
        active_link = db.scalar(
            select(RecoveryPaymentLink)
            .where(
                RecoveryPaymentLink.recovery_case_id == case.id,
                RecoveryPaymentLink.status.in_(["CREATED", "SENT"]),
            )
            .order_by(RecoveryPaymentLink.created_at.desc())
        )

        if not active_link:
            # Generate new payment link
            is_dry_run_link = (dry_run is True) or (settings.EXECUTION_MODE == "dry_run")
            if is_dry_run_link:
                sim_link_id = f"plink_sim_{uuid.uuid4().hex[:12]}"
                active_link = RecoveryPaymentLink(
                    recovery_case_id=case.id,
                    razorpay_payment_link_id=sim_link_id,
                    payment_url=f"https://rzp.io/i/{sim_link_id}",
                    amount=case.amount_at_risk or Decimal("0.00"),
                    currency=case.currency or "INR",
                    status="CREATED",
                )
                db.add(active_link)
                db.flush()
            else:
                client = RazorpayPaymentLinkClient()
                link_dto = client.create_payment_link(
                    amount=case.amount_at_risk or Decimal("0.00"),
                    currency=case.currency or "INR",
                    description=f"Payment recovery for case {case.id}",
                    customer_name=case.customer.name if case.customer else None,
                    customer_email=case.customer.email if case.customer else None,
                    customer_contact=case.customer.phone if case.customer else None,
                )
                active_link = RecoveryPaymentLink(
                    recovery_case_id=case.id,
                    razorpay_payment_link_id=link_dto.razorpay_payment_link_id,
                    payment_url=link_dto.url,
                    amount=Decimal(str(link_dto.amount)),
                    currency=link_dto.currency,
                    status="CREATED",
                )
                db.add(active_link)
                db.flush()

        # 3. Generate Draft Message
        draft = RecoveryMessageGenerator.generate(
            recovery_case=case,
            payment_link_url=active_link.payment_url,
            language=language,
        )

        cls._audit(
            db=db,
            case_id=case.id,
            action="WHATSAPP_MESSAGE_GENERATED",
            metadata={
                "template": draft.template_name,
                "version": draft.template_version,
                "language": draft.language,
                "recipient_masked": draft.recipient_masked,
            },
        )

        # 4. Check Idempotency Key
        attempt_num = policy_res.attempt_count + 1
        idempotency_key = f"comm_{case.id}_WHATSAPP_{attempt_num}"

        existing_comm = db.scalar(
            select(Communication).where(Communication.idempotency_key == idempotency_key)
        )
        if existing_comm:
            logger.info(f"[COMMUNICATION_IDEMPOTENT] Reusing existing communication record {existing_comm.id}.")
            return WhatsAppOutreachResult(
                case_id=str(case.id),
                communication_id=str(existing_comm.id),
                channel=existing_comm.channel,
                status=existing_comm.status,
                template_name=existing_comm.template_name,
                template_version=existing_comm.template_version,
                language=existing_comm.language,
                recipient_masked=existing_comm.recipient_masked,
                message_body=existing_comm.message_body,
                payment_link_url=active_link.payment_url,
                is_simulated=existing_comm.is_simulated,
                provider=existing_comm.provider,
                provider_message_id=existing_comm.provider_message_id,
                reason="Communication already created for this attempt.",
            )

        # 5. Create Initial Communication Record (QUEUED)
        provider_mode = "dry_run" if (dry_run is True or settings.COMMUNICATION_MODE == "dry_run") else settings.COMMUNICATION_MODE
        provider_name = "DEVELOPMENT" if provider_mode in ["dry_run", "development"] else "TWILIO"

        comm = Communication(
            recovery_case_id=case.id,
            customer_id=case.customer_id,
            channel="WHATSAPP",
            provider=provider_name,
            template_name=draft.template_name,
            template_version=draft.template_version,
            language=draft.language,
            recipient_reference=draft.recipient_raw,
            recipient_masked=draft.recipient_masked,
            message_body=draft.message_body,
            status="QUEUED",
            idempotency_key=idempotency_key,
            attempt_number=attempt_num,
            is_simulated=(provider_mode in ["dry_run", "development"]),
            communication_metadata={"payment_link_id": active_link.razorpay_payment_link_id},
        )
        db.add(comm)
        db.flush()

        cls._audit(
            db=db,
            case_id=case.id,
            communication_id=comm.id,
            action="WHATSAPP_QUEUED",
            metadata={"attempt_number": attempt_num, "template": draft.template_name},
        )

        # 6. Dispatch through WhatsApp Provider
        provider = provider_override or get_whatsapp_provider(mode_override=provider_mode)
        send_result: WhatsAppSendResult = provider.send_message(
            recipient=draft.recipient_raw,
            message=draft.message_body,
            template_name=draft.template_name,
            context={"case_id": str(case.id), "communication_id": str(comm.id)},
        )

        # 7. Update Communication State & Audit Trail
        if send_result.success:
            comm.status = "SENT"
            comm.sent_at = send_result.dispatched_at
            comm.provider_message_id = send_result.provider_message_id
            active_link.status = "SENT"
            case.status = "IN_PROGRESS"
            case.retry_count = (case.retry_count or 0) + 1
            db.commit()

            cls._audit(
                db=db,
                case_id=case.id,
                communication_id=comm.id,
                action="WHATSAPP_SENT",
                metadata={
                    "provider_message_id": send_result.provider_message_id,
                    "is_simulated": send_result.is_simulated,
                    "recipient_masked": draft.recipient_masked,
                },
            )

            return WhatsAppOutreachResult(
                case_id=str(case.id),
                communication_id=str(comm.id),
                channel="WHATSAPP",
                status="SENT",
                template_name=draft.template_name,
                template_version=draft.template_version,
                language=draft.language,
                recipient_masked=draft.recipient_masked,
                message_body=draft.message_body,
                payment_link_url=active_link.payment_url,
                is_simulated=send_result.is_simulated,
                provider=provider_name,
                provider_message_id=send_result.provider_message_id,
            )
        else:
            comm.status = "FAILED"
            comm.failed_at = now
            comm.failure_reason = send_result.error_message or "Provider dispatch error"
            db.commit()

            cls._audit(
                db=db,
                case_id=case.id,
                communication_id=comm.id,
                action="WHATSAPP_FAILED",
                metadata={
                    "error_code": send_result.error_code,
                    "error_message": send_result.error_message,
                },
            )

            return WhatsAppOutreachResult(
                case_id=str(case.id),
                communication_id=str(comm.id),
                channel="WHATSAPP",
                status="FAILED",
                template_name=draft.template_name,
                template_version=draft.template_version,
                language=draft.language,
                recipient_masked=draft.recipient_masked,
                message_body=draft.message_body,
                payment_link_url=active_link.payment_url,
                is_simulated=send_result.is_simulated,
                provider=provider_name,
                reason=send_result.error_message,
            )

    @classmethod
    def handle_status_webhook(
        cls,
        db: Session,
        provider_message_id: str,
        status: str,
        error_reason: Optional[str] = None,
        raw_payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Communication]:
        """Update Communication record based on provider status callback."""
        comm = db.scalar(
            select(Communication).where(Communication.provider_message_id == provider_message_id)
        )
        if not comm:
            logger.warning(f"[WHATSAPP_CALLBACK_UNKNOWN_MSG] Provider message ID {provider_message_id} not found.")
            return None

        now = datetime.now(timezone.utc)
        normalized_status = status.upper()

        if normalized_status in ["DELIVERED", "DELIVERY"]:
            comm.status = "DELIVERED"
            comm.delivered_at = now
            cls._audit(
                db=db,
                case_id=comm.recovery_case_id,
                communication_id=comm.id,
                action="WHATSAPP_DELIVERED",
                metadata={"provider_message_id": provider_message_id},
            )
        elif normalized_status in ["READ", "SEEN"]:
            comm.status = "READ"
            comm.read_at = now
        elif normalized_status in ["FAILED", "UNDELIVERED"]:
            comm.status = "FAILED"
            comm.failed_at = now
            comm.failure_reason = error_reason or "Provider delivery failed"
            cls._audit(
                db=db,
                case_id=comm.recovery_case_id,
                communication_id=comm.id,
                action="WHATSAPP_FAILED",
                metadata={"error_reason": comm.failure_reason},
            )

        db.commit()
        return comm

    @classmethod
    def stop_whatsapp_on_recovery(
        cls,
        db: Session,
        recovery_case: RecoveryCase,
    ) -> int:
        """Payment success stopping rule: Cancels all pending/queued WhatsApp outreach immediately."""
        now = datetime.now(timezone.utc)
        pending_comms = db.scalars(
            select(Communication).where(
                Communication.recovery_case_id == recovery_case.id,
                Communication.status.in_(["QUEUED", "GENERATED"]),
            )
        ).all()

        for comm in pending_comms:
            comm.status = "CANCELLED"
            comm.cancelled_at = now
            comm.failure_reason = "Payment captured; recovery outreach cancelled by stopping rule."

        if pending_comms:
            cls._audit(
                db=db,
                case_id=recovery_case.id,
                action="WHATSAPP_CANCELLED",
                metadata={"cancelled_count": len(pending_comms)},
            )

        cls._audit(
            db=db,
            case_id=recovery_case.id,
            action="WHATSAPP_STOPPED_AFTER_RECOVERY",
            metadata={"case_status": "RECOVERED"},
        )
        return len(pending_comms)

    @classmethod
    def _audit(
        cls,
        db: Session,
        case_id: uuid.UUID,
        action: str,
        metadata: Optional[Dict[str, Any]] = None,
        communication_id: Optional[uuid.UUID] = None,
    ) -> AuditLog:
        """Create structured audit log entry."""
        audit_entry = AuditLog(
            recovery_case_id=case_id,
            actor_type="SYSTEM",
            actor_id="communication_orchestrator_v1",
            action=action,
            entity_type="Communication",
            entity_id=str(communication_id or case_id),
            audit_metadata=metadata or {},
        )
        db.add(audit_entry)
        db.flush()
        return audit_entry
