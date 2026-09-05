"""WhatsApp Recovery Service orchestrating real Twilio Sandbox outreach, Razorpay payment link generation, safety policies, race condition guards, and stopping rules."""
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
from app.integrations.whatsapp import get_whatsapp_provider, WhatsAppSendResult
from app.integrations.razorpay.payment_link_client import RazorpayPaymentLinkClient
from app.integrations.twilio.client import normalize_whatsapp_address
from app.services.communication_scheduler import CommunicationScheduler, PolicyCheckResult
from app.services.recovery_message_generator import RecoveryMessageGenerator
from app.services.notification_service import mask_contact

logger = logging.getLogger(__name__)


@dataclass
class WhatsAppRecoveryResponse:
    """Standardized response from WhatsApp recovery execution."""

    case_id: str
    action: str
    status: str  # "SENT", "DELIVERED", "BLOCKED", "FAILED", "ALREADY_RECOVERED"
    payment_link: Dict[str, Any]
    communication: Dict[str, Any]
    reason: Optional[str] = None
    policy_blocking_rule: Optional[str] = None


class WhatsAppRecoveryService:
    """Core domain service for Real Twilio WhatsApp Payment Recovery."""

    @classmethod
    def preview_recovery(
        cls,
        db: Session,
        recovery_case_id: uuid.UUID,
        language: str = "ENGLISH",
        reference_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Generate side-effect-free preview of proposed WhatsApp recovery message and policy checks."""
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == recovery_case_id))
        if not case:
            raise ValueError(f"RecoveryCase '{recovery_case_id}' not found.")

        now = reference_time or datetime.now(timezone.utc)

        # 1. Policy Evaluation
        policy_res = cls._evaluate_policy(db=db, case=case, reference_time=now)

        # 2. Get active or placeholder payment link
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
            "message": draft.message_body,
            "payment_link": payment_url,
            "policy_status": "APPROVED" if policy_res.allowed else "BLOCKED",
            "policy_reasons": [policy_res.reason],
            "policy_blocking_rule": policy_res.blocking_rule,
            "attempt_number": policy_res.attempt_count + 1,
            "max_attempts": policy_res.max_attempts,
            "next_eligible_at": policy_res.next_eligible_at.isoformat() if policy_res.next_eligible_at else None,
        }

    @classmethod
    def execute_recovery(
        cls,
        db: Session,
        recovery_case_id: uuid.UUID,
        language: str = "ENGLISH",
        dry_run: Optional[bool] = None,
        reference_time: Optional[datetime] = None,
        message_override: Optional[str] = None,
    ) -> WhatsAppRecoveryResponse:
        """Execute bounded WhatsApp recovery outreach with policy verification, payment link creation, and race condition guards.

        `message_override` is an agent-personalised body that has already passed MessageValidator; `{payment_link}` is
        substituted with the real link here so the model never sees or invents a URL.
        """
        now = reference_time or datetime.now(timezone.utc)
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == recovery_case_id))
        if not case:
            raise ValueError(f"RecoveryCase '{recovery_case_id}' not found.")

        # 1. Policy Evaluation
        policy_res = cls._evaluate_policy(db=db, case=case, reference_time=now, dry_run=dry_run)

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
            return WhatsAppRecoveryResponse(
                case_id=str(case.id),
                action="WHATSAPP_PAYMENT_RECOVERY",
                status="BLOCKED",
                payment_link={},
                communication={
                    "provider": "TWILIO",
                    "status": "BLOCKED",
                    "reason": policy_res.reason,
                },
                reason=policy_res.reason,
                policy_blocking_rule=policy_res.blocking_rule,
            )

        cls._audit(
            db=db,
            case_id=case.id,
            action="WHATSAPP_APPROVED",
            metadata={"attempt_number": policy_res.attempt_count + 1},
        )

        # 2. Resolve or Create Razorpay Payment Link
        active_link, newly_created = cls._get_or_create_payment_link(db=db, case=case, dry_run=dry_run)
        if newly_created:
            cls._audit(
                db=db,
                case_id=case.id,
                action="PAYMENT_LINK_CREATED",
                metadata={
                    "payment_link_id": active_link.razorpay_payment_link_id,
                    "payment_url": active_link.payment_url,
                    "amount": float(active_link.amount),
                },
            )

        # 3. Generate Personalized Draft Message
        draft = RecoveryMessageGenerator.generate(
            recovery_case=case,
            payment_link_url=active_link.payment_url,
            language=language,
        )

        if message_override and message_override.strip():
            draft.message_body = message_override.replace("{payment_link}", active_link.payment_url)
            draft.template_name = "AGENT_PERSONALISED_V1"

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
            logger.info(f"[WHATSAPP_IDEMPOTENT] Reusing existing communication record {existing_comm.id}.")
            return WhatsAppRecoveryResponse(
                case_id=str(case.id),
                action="WHATSAPP_PAYMENT_RECOVERY",
                status=existing_comm.status,
                payment_link={
                    "url": active_link.payment_url,
                    "amount": float(active_link.amount),
                    "currency": active_link.currency,
                },
                communication={
                    "id": str(existing_comm.id),
                    "provider": existing_comm.provider,
                    "status": existing_comm.status,
                    "provider_message_id": existing_comm.provider_message_id,
                    "is_simulated": existing_comm.is_simulated,
                },
                reason="Communication already dispatched for this attempt.",
            )

        # 5. Immediate Pre-Flight Race Condition Guard
        # Re-query case and payment status directly from DB immediately before external network call
        db.refresh(case)
        if case.status in ["RECOVERED", "CLOSED"]:
            cls._audit(
                db=db,
                case_id=case.id,
                action="WHATSAPP_BLOCKED",
                metadata={"reason": "Race condition detected: case recovered immediately before send."},
            )
            return WhatsAppRecoveryResponse(
                case_id=str(case.id),
                action="WHATSAPP_PAYMENT_RECOVERY",
                status="BLOCKED",
                payment_link={},
                communication={"provider": "TWILIO", "status": "BLOCKED"},
                reason="Payment already captured; case recovered immediately before dispatch.",
                policy_blocking_rule="CASE_ALREADY_RECOVERED_OR_CLOSED",
            )

        # 6. Create Initial Communication Record (QUEUED)
        is_dry_run = (dry_run is True) or (settings.WHATSAPP_MODE == "DRY_RUN")
        provider_name = "DEVELOPMENT" if is_dry_run else "TWILIO"

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
            is_simulated=is_dry_run,
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

        # 7. Dispatch via Provider
        provider = get_whatsapp_provider(mode_override="dry_run" if is_dry_run else "twilio")
        send_result: WhatsAppSendResult = provider.send_message(
            recipient=draft.recipient_raw,
            message=draft.message_body,
            template_name=draft.template_name,
            context={"case_id": str(case.id), "communication_id": str(comm.id)},
        )

        # 8. Update State and Commit
        if send_result.success:
            comm.status = "SENT"
            comm.sent_at = now
            comm.created_at = now
            comm.provider_message_id = send_result.provider_message_id
            active_link.status = "SENT"
            case.status = "IN_PROGRESS"
            case.retry_count = (case.retry_count or 0) + 1
            from app.ledger.service import LedgerService
            from app.compliance.contact_policy import ContactPolicy
            LedgerService.post_cost(db, case, channel="WHATSAPP", provider_reference=str(comm.id))
            ContactPolicy.record_sent(db, case=case, channel="WHATSAPP", reference=str(comm.id), now=now)
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
                    "provider": provider_name,
                },
            )

            return WhatsAppRecoveryResponse(
                case_id=str(case.id),
                action="WHATSAPP_PAYMENT_RECOVERY",
                status="SENT",
                payment_link={
                    "url": active_link.payment_url,
                    "amount": float(active_link.amount),
                    "currency": active_link.currency,
                },
                communication={
                    "id": str(comm.id),
                    "provider": provider_name,
                    "status": "SENT",
                    "provider_message_id": send_result.provider_message_id,
                    "is_simulated": send_result.is_simulated,
                },
            )
        else:
            comm.status = "FAILED"
            comm.failed_at = now
            comm.failure_reason = send_result.error_message or "Twilio dispatch failure"
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

            return WhatsAppRecoveryResponse(
                case_id=str(case.id),
                action="WHATSAPP_PAYMENT_RECOVERY",
                status="FAILED",
                payment_link={
                    "url": active_link.payment_url,
                    "amount": float(active_link.amount),
                    "currency": active_link.currency,
                },
                communication={
                    "id": str(comm.id),
                    "provider": provider_name,
                    "status": "FAILED",
                    "error_code": send_result.error_code,
                    "error_message": send_result.error_message,
                },
                reason=send_result.error_message,
            )

    @classmethod
    def _evaluate_policy(
        cls,
        db: Session,
        case: RecoveryCase,
        reference_time: datetime,
        dry_run: Optional[bool] = None,
    ) -> PolicyCheckResult:
        """Run complete policy evaluation including DND, cooldown, attempts, consent, and Sandbox recipient restrictions."""
        # 0. Measurement: HOLDOUT arm never receives outreach
        from app.experiments.assigner import ExperimentAssigner
        holdout = ExperimentAssigner.holdout_block(case)
        if holdout:
            return PolicyCheckResult(
                allowed=False,
                reason=holdout["reason"],
                blocking_rule=holdout["blocking_rule"],
                attempt_count=0,
                max_attempts=settings.MAX_WHATSAPP_ATTEMPTS,
                evaluated_at=reference_time,
            )

        # 1. Base Scheduler Policy (Case open, Quiet hours 20:00-08:00, Cooldown 1440m, Max attempts 3, PromiseToPay)
        policy_res = CommunicationScheduler.evaluate_outreach_policy(
            db=db,
            recovery_case=case,
            reference_time=reference_time,
        )
        if not policy_res.allowed:
            return policy_res

        # 1b. Cross-channel compliance (consent ledger, DND registry, contact window, frequency caps, handoff)
        from app.compliance.contact_policy import ContactPolicy
        compliance = ContactPolicy.evaluate(db, case=case, customer=case.customer, channel="WHATSAPP", now=reference_time)
        if not compliance.allowed:
            return PolicyCheckResult(
                allowed=False,
                reason=compliance.reason,
                blocking_rule=compliance.rule,
                next_eligible_at=compliance.next_eligible_at,
                attempt_count=policy_res.attempt_count,
                max_attempts=policy_res.max_attempts,
                evaluated_at=reference_time,
            )

        # 2. Twilio Sandbox Recipient Restriction (enforced when in Sandbox and real execution)
        is_dry_run = (dry_run is True) or (settings.WHATSAPP_MODE == "DRY_RUN")
        if settings.TWILIO_WHATSAPP_MODE == "SANDBOX" and not is_dry_run:
            customer_phone = case.customer.phone if case.customer else None
            if not customer_phone:
                return PolicyCheckResult(
                    allowed=False,
                    reason="Customer phone number missing for WhatsApp outreach.",
                    blocking_rule="CUSTOMER_PHONE_MISSING",
                    attempt_count=policy_res.attempt_count,
                    max_attempts=policy_res.max_attempts,
                )

            norm_cust = normalize_whatsapp_address(customer_phone)
            norm_sandbox_to = normalize_whatsapp_address(settings.TWILIO_WHATSAPP_TO or "")

            if norm_sandbox_to and norm_cust != norm_sandbox_to:
                return PolicyCheckResult(
                    allowed=False,
                    reason=f"SANDBOX mode: Outgoing messages restricted to configured TWILIO_WHATSAPP_TO ({mask_contact(norm_sandbox_to.replace('whatsapp:', ''))}).",
                    blocking_rule="SANDBOX_RECIPIENT_RESTRICTION",
                    attempt_count=policy_res.attempt_count,
                    max_attempts=policy_res.max_attempts,
                )

        return policy_res

    @classmethod
    def _get_or_create_payment_link(
        cls,
        db: Session,
        case: RecoveryCase,
        dry_run: Optional[bool] = None,
    ) -> tuple[RecoveryPaymentLink, bool]:
        """Check for existing active payment link or create a new Razorpay Test Mode link."""
        active_link = db.scalar(
            select(RecoveryPaymentLink)
            .where(
                RecoveryPaymentLink.recovery_case_id == case.id,
                RecoveryPaymentLink.status.in_(["CREATED", "SENT"]),
            )
            .order_by(RecoveryPaymentLink.created_at.desc())
        )
        if active_link:
            logger.info(f"[PAYMENT_LINK_REUSED] Reusing active payment link {active_link.razorpay_payment_link_id}")
            return active_link, False

        # Create new link
        is_dry_run = (dry_run is True) or (settings.EXECUTION_MODE == "dry_run")
        if is_dry_run:
            sim_link_id = f"plink_sim_{uuid.uuid4().hex[:12]}"
            link = RecoveryPaymentLink(
                recovery_case_id=case.id,
                razorpay_payment_link_id=sim_link_id,
                payment_url=f"https://rzp.io/i/{sim_link_id}",
                amount=case.amount_at_risk or Decimal("0.00"),
                currency=case.currency or "INR",
                status="CREATED",
            )
            db.add(link)
            db.flush()
            return link, True
        else:
            client = RazorpayPaymentLinkClient()
            amount_paise = int(Decimal(str(case.amount_at_risk or "0.00")) * 100)
            link_dto = client.create_payment_link(
                amount_paise=amount_paise,
                currency=case.currency or "INR",
                description=f"Payment recovery for case {case.id}",
                customer_name=case.customer.name if case.customer else None,
                customer_email=case.customer.email if case.customer else None,
                customer_phone=case.customer.phone if case.customer else None,
            )
            existing_db_link = db.scalar(
                select(RecoveryPaymentLink)
                .where(RecoveryPaymentLink.razorpay_payment_link_id == link_dto.payment_link_id)
                .limit(1)
            )
            if existing_db_link:
                existing_db_link.recovery_case_id = case.id
                existing_db_link.amount = Decimal(str(link_dto.amount))
                existing_db_link.currency = link_dto.currency
                existing_db_link.payment_url = link_dto.short_url
                existing_db_link.status = "CREATED"
                db.flush()
                return existing_db_link, False

            link = RecoveryPaymentLink(
                recovery_case_id=case.id,
                razorpay_payment_link_id=link_dto.payment_link_id,
                payment_url=link_dto.short_url,
                amount=Decimal(str(link_dto.amount)),
                currency=link_dto.currency,
                status="CREATED",
            )
            db.add(link)
            db.flush()
            return link, True

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
            actor_id="whatsapp_recovery_service_v1",
            action=action,
            entity_type="Communication",
            entity_id=str(communication_id or case_id),
            audit_metadata=metadata or {},
        )
        db.add(audit_entry)
        db.flush()
        return audit_entry
