"""Voice Recovery Service for initiating outbound Twilio voice recovery calls."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import logging
import re
from typing import Any, Dict, Optional
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session
from twilio.request_validator import RequestValidator

from app.core.config import settings
from app.integrations.voice.twilio_client import TwilioVoiceClient
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.promise_to_pay import PromiseToPay
from app.models.recovery_case import RecoveryCase
from app.models.voice_call import VoiceCall

logger = logging.getLogger(__name__)

# E.164 standard phone format: + followed by 7 to 15 digits
E164_PHONE_REGEX = re.compile(r"^\+[1-9]\d{6,14}$")


def validate_e164_phone(phone: Optional[str]) -> str:
    """Validate that phone number complies with E.164 international format."""
    if not phone or not isinstance(phone, str):
        raise ValueError("Invalid phone number: Phone number string is required.")

    cleaned = phone.strip()
    if not E164_PHONE_REGEX.match(cleaned):
        raise ValueError(
            f"Invalid phone number '{phone}': Must strictly follow E.164 international format (e.g. +919876543210)."
        )
    return cleaned


class VoiceRecoveryService:
    """Service to orchestrate outbound Twilio recovery phone calls."""

    @classmethod
    def start_test_call(
        cls,
        phone_number: str,
        dry_run: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Validate destination phone number and dispatch outbound test call via Twilio.

        Args:
            phone_number: Destination phone number in E.164 format.
            dry_run: Optional override for simulated test dispatch.

        Returns:
            Dict containing call_sid and status.
        """
        validated_phone = validate_e164_phone(phone_number)
        is_dry_run = dry_run if dry_run is not None else (settings.EXECUTION_MODE == "dry_run")

        if is_dry_run:
            simulated_sid = f"CA_sim_{uuid.uuid4().hex[:28]}"
            logger.info(f"[TWILIO_VOICE_DRY_RUN] Simulated call to {validated_phone}: {simulated_sid}")
            return {
                "call_sid": simulated_sid,
                "status": "queued",
            }

        client = TwilioVoiceClient()
        base_url = settings.TWILIO_WEBHOOK_BASE_URL
        test_url = f"{base_url}/webhooks/twilio/test-voice" if base_url else None
        call_res = client.create_outbound_call(to_number=validated_phone, url=test_url)

        return {
            "call_sid": call_res["call_sid"],
            "status": call_res.get("status", "queued"),
        }

    @classmethod
    def start_recovery_call(
        cls,
        db: Session,
        case_id: uuid.UUID,
        dry_run: Optional[bool] = None,
        webhook_base_url: Optional[str] = None,
        reference_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Initiate an outbound personalized voice recovery call for a RecoveryCase.

        `reference_time` lets the agent, scheduler and replay harness evaluate eligibility (RBI window, caps) on
        their own clock instead of the wall clock.

        Args:
            db: Database session.
            case_id: UUID of RecoveryCase.
            dry_run: Optional override for simulated call execution.
            webhook_base_url: Optional public base URL for webhook resolution.

        Returns:
            Dict containing case_id, call_sid, status, provider, voice_call_id.
        """
        # 1. Load RecoveryCase
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
        if not case:
            raise ValueError(f"Recovery case '{case_id}' not found.")

        customer = case.customer
        if not customer:
            customer = db.scalar(select(Customer).where(Customer.id == case.customer_id))

        now = reference_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        # 2. Check Stopping Rules & Eligibility
        blocked_reason, blocking_rule = cls._evaluate_voice_eligibility(db=db, case=case, customer=customer, now=now)
        if blocked_reason:
            cls._record_audit_log(
                db=db,
                case_id=case.id,
                action="VOICE_CALL_BLOCKED",
                metadata={"reason": blocked_reason, "blocking_rule": blocking_rule},
            )
            db.commit()
            raise ValueError(f"Voice recovery blocked: {blocked_reason}")

        # 3. Compute Attempt Number
        existing_calls_count = db.scalar(
            select(VoiceCall)
            .where(VoiceCall.recovery_case_id == case.id)
            .with_only_columns(VoiceCall.id)
        )
        all_calls = db.scalars(
            select(VoiceCall)
            .where(VoiceCall.recovery_case_id == case.id)
            .order_by(VoiceCall.created_at.desc())
        ).all()
        attempt_number = len(all_calls) + 1

        to_phone = validate_e164_phone(customer.phone)
        from_phone = settings.TWILIO_PHONE_NUMBER or "+17372212163"

        # Format human-readable due date
        due_date_str = case.created_at.strftime("%B %d, %Y") if case.created_at else "recent date"
        dynamic_vars = {
            "customer_name": customer.name or "Valued Customer",
            "amount_due": float(case.amount_at_risk or Decimal("0.00")),
            "currency": case.currency or "INR",
            "due_date": due_date_str,
        }

        # 4. Create VoiceCall Record in Database
        voice_call = VoiceCall(
            id=uuid.uuid4(),
            recovery_case_id=case.id,
            customer_id=customer.id if customer else None,
            provider="TWILIO",
            provider_call_id=None,
            from_number=from_phone,
            to_number=to_phone,
            status="QUEUED",
            attempt_number=attempt_number,
            dynamic_variables=dynamic_vars,
            call_metadata={"execution_mode": "dry_run" if dry_run else settings.EXECUTION_MODE},
        )
        db.add(voice_call)
        db.flush()

        cls._record_audit_log(
            db=db,
            case_id=case.id,
            action="VOICE_CALL_REQUESTED",
            entity_id=str(voice_call.id),
            metadata={"attempt_number": attempt_number, "to_number": to_phone},
        )

        # 5. Resolve Webhook URLs for Dynamic TwiML & Status Callback
        base_url = (webhook_base_url or settings.TWILIO_WEBHOOK_BASE_URL or "http://127.0.0.1:8000").rstrip("/")
        twiml_url = f"{base_url}/webhooks/twilio/voice/{voice_call.id}"
        status_callback_url = f"{base_url}/webhooks/twilio/status"

        is_dry_run = dry_run if dry_run is not None else (settings.EXECUTION_MODE == "dry_run")

        from app.ledger.service import LedgerService
        from app.compliance.contact_policy import ContactPolicy
        LedgerService.post_cost(db, case, channel="VOICE", provider_reference=str(voice_call.id))
        ContactPolicy.record_sent(db, case=case, channel="VOICE", reference=str(voice_call.id), now=now)

        if is_dry_run:
            simulated_sid = f"CA_sim_{uuid.uuid4().hex[:28]}"
            voice_call.provider_call_id = simulated_sid
            voice_call.status = "QUEUED"
            cls._record_audit_log(
                db=db,
                case_id=case.id,
                action="VOICE_CALL_QUEUED",
                entity_id=str(voice_call.id),
                metadata={"call_sid": simulated_sid, "simulated": True},
            )
            db.commit()
            return {
                "case_id": str(case.id),
                "voice_call_id": str(voice_call.id),
                "call_sid": simulated_sid,
                "status": "QUEUED",
                "provider": "TWILIO",
            }

        # 6. Dispatch Real Twilio Outbound Call
        try:
            client = TwilioVoiceClient()
            call_res = client.create_outbound_call(
                to_number=to_phone,
                from_number=from_phone,
                url=twiml_url,
                status_callback=status_callback_url,
                status_callback_event=["initiated", "ringing", "answered", "completed"],
            )

            call_sid = call_res["call_sid"]
            voice_call.provider_call_id = call_sid
            voice_call.status = (call_res.get("status") or "QUEUED").upper()

            cls._record_audit_log(
                db=db,
                case_id=case.id,
                action="VOICE_CALL_QUEUED",
                entity_id=str(voice_call.id),
                metadata={"call_sid": call_sid, "status": voice_call.status},
            )
            db.commit()

            return {
                "case_id": str(case.id),
                "voice_call_id": str(voice_call.id),
                "call_sid": call_sid,
                "status": voice_call.status,
                "provider": "TWILIO",
            }

        except Exception as exc:
            voice_call.status = "FAILED"
            voice_call.call_metadata["error"] = str(exc)
            cls._record_audit_log(
                db=db,
                case_id=case.id,
                action="VOICE_CALL_FAILED",
                entity_id=str(voice_call.id),
                metadata={"error": str(exc)},
            )
            db.commit()
            raise

    @classmethod
    def generate_recovery_twiml(cls, db: Session, call_id: uuid.UUID) -> str:
        """Generate personalized multi-state compliant English TwiML response for answered Twilio recovery call."""
        voice_call = db.scalar(select(VoiceCall).where(VoiceCall.id == call_id))
        if not voice_call:
            raise ValueError(f"VoiceCall '{call_id}' not found.")

        base_url = settings.TWILIO_WEBHOOK_BASE_URL or ""
        gather_url = f"{base_url}/webhooks/twilio/voice/{call_id}/gather" if base_url else f"/webhooks/twilio/voice/{call_id}/gather"

        from app.services.voice_conversation_manager import VoiceConversationManager
        return VoiceConversationManager.generate_initial_twiml(
            db=db,
            voice_call=voice_call,
            gather_url=gather_url,
        )

    @classmethod
    def handle_voice_gather_response(
        cls,
        db: Session,
        call_id: uuid.UUID,
        payload: Dict[str, Any],
    ) -> str:
        """Process speech recognition result via VoiceConversationManager and return TwiML."""
        voice_call = db.scalar(select(VoiceCall).where(VoiceCall.id == call_id))
        if not voice_call:
            raise ValueError(f"VoiceCall '{call_id}' not found.")

        base_url = settings.TWILIO_WEBHOOK_BASE_URL or ""
        gather_url = f"{base_url}/webhooks/twilio/voice/{call_id}/gather" if base_url else f"/webhooks/twilio/voice/{call_id}/gather"

        from app.services.voice_conversation_manager import VoiceConversationManager
        return VoiceConversationManager.handle_turn(
            db=db,
            voice_call=voice_call,
            payload=payload,
            gather_url=gather_url,
        )

    @classmethod
    def handle_test_voice_gather_response(cls, db: Session, payload: Dict[str, Any]) -> str:
        """Process speech recognition result for test calls and return confirmation TwiML."""
        from app.models.customer import Customer
        from app.services.voice_intent_extractor import VoiceCustomerIntent, VoiceIntentExtractor

        cust = db.scalar(select(Customer).where(Customer.phone == "+917991142735"))
        customer_name = cust.name if cust and cust.name else "Ankit Kumar"

        speech_result = payload.get("SpeechResult") or payload.get("speech_result") or ""
        confidence_str = payload.get("Confidence") or payload.get("confidence") or "0.9"
        try:
            confidence = float(confidence_str)
        except (ValueError, TypeError):
            confidence = 0.9

        now = datetime.now(timezone.utc)
        intent_result = VoiceIntentExtractor.extract_promise_intent(
            speech_text=speech_result,
            reference_datetime=now,
            timezone_str="Asia/Kolkata",
            speech_confidence=confidence,
        )

        if intent_result.intent == VoiceCustomerIntent.PROMISE_TO_PAY and intent_result.promised_date:
            spoken_date = intent_result.promised_date_display
            return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="en-IN" voice="Polly.Aditi">Thank you {customer_name}. We have recorded your promise to pay on {spoken_date}. We have paused further payment reminders until then. A confirmation and direct payment link has been sent to your registered WhatsApp and email. Have a wonderful day.</Say>
    <Hangup/>
</Response>""".strip()

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="en-IN" voice="Polly.Aditi">Thank you {customer_name}. We have noted your response. A direct payment link has been sent to your registered WhatsApp and email. Have a wonderful day.</Say>
    <Hangup/>
</Response>""".strip()

    @classmethod
    def handle_status_callback(cls, db: Session, payload: Dict[str, Any]) -> Optional[VoiceCall]:
        """Update VoiceCall record based on Twilio status callback event."""
        call_sid = payload.get("CallSid") or payload.get("call_sid") or payload.get("callSid")
        call_status = (payload.get("CallStatus") or payload.get("call_status") or payload.get("status") or "").upper()

        if not call_sid:
            logger.warning("[TWILIO_STATUS_CALLBACK_WARNING] Missing CallSid in payload.")
            return None

        voice_call = db.scalar(select(VoiceCall).where(VoiceCall.provider_call_id == call_sid))
        if not voice_call:
            logger.info(f"[TWILIO_STATUS_CALLBACK] No VoiceCall matching CallSid '{call_sid}'.")
            return None

        # Map Twilio call statuses
        prev_status = voice_call.status
        voice_call.status = call_status

        now = datetime.now(timezone.utc)
        if call_status in ["IN-PROGRESS", "IN_PROGRESS", "ANSWERED"]:
            if not voice_call.started_at:
                voice_call.started_at = now
        elif call_status in ["COMPLETED", "FAILED", "BUSY", "NO-ANSWER", "NO_ANSWER", "CANCELED"]:
            voice_call.ended_at = now
            duration_str = payload.get("CallDuration") or payload.get("Duration") or payload.get("duration")
            if duration_str:
                try:
                    voice_call.duration_seconds = int(duration_str)
                except (ValueError, TypeError):
                    pass

        # Update metadata
        voice_call.call_metadata = {
            **(voice_call.call_metadata or {}),
            "last_twilio_status": call_status,
            "callback_payload": payload,
        }

        # Audit lifecycle update
        cls._record_audit_log(
            db=db,
            case_id=voice_call.recovery_case_id,
            action=f"VOICE_CALL_{call_status.replace('-', '_')}",
            entity_id=str(voice_call.id),
            metadata={"call_sid": call_sid, "previous_status": prev_status, "new_status": call_status},
        )
        db.commit()
        return voice_call

    @classmethod
    def validate_twilio_webhook_signature(
        cls,
        signature: Optional[str],
        url: str,
        params: Dict[str, Any],
    ) -> bool:
        """Validate Twilio X-Twilio-Signature against server Auth Token."""
        auth_token = settings.TWILIO_AUTH_TOKEN
        if not auth_token:
            logger.warning("[TWILIO_AUTH_WARNING] TWILIO_AUTH_TOKEN not configured; skipping signature validation.")
            return True

        if not signature:
            logger.warning("[TWILIO_AUTH_REJECT] Missing X-Twilio-Signature header.")
            return False

        try:
            validator = RequestValidator(auth_token)
            return validator.validate(url, params, signature)
        except Exception as exc:
            logger.error(f"[TWILIO_VALIDATOR_ERROR] Error validating Twilio signature: {exc}")
            return False

    @classmethod
    def _evaluate_voice_eligibility(
        cls,
        db: Session,
        case: RecoveryCase,
        customer: Optional[Customer],
        now: datetime,
    ) -> tuple[Optional[str], Optional[str]]:
        """Evaluate all business stopping rules before initiating a voice recovery call."""
        # 1. Check Case Status
        if case.status in ["RECOVERED", "CLOSED", "RESOLVED"]:
            return f"Recovery case is already {case.status}.", "CASE_ALREADY_RECOVERED"

        # 1b. Measurement: HOLDOUT arm never receives calls
        from app.experiments.assigner import ExperimentAssigner
        holdout = ExperimentAssigner.holdout_block(case)
        if holdout:
            return holdout["reason"], holdout["blocking_rule"]

        # 2. Check Customer Phone
        if not customer or not customer.phone:
            return "Customer phone number is missing.", "CUSTOMER_PHONE_MISSING"

        try:
            validate_e164_phone(customer.phone)
        except ValueError as exc:
            return str(exc), "INVALID_PHONE_FORMAT"

        # 3. Check Active Promise-to-Pay (Pauses outreach immediately)
        active_ptp = db.scalar(
            select(PromiseToPay).where(
                PromiseToPay.recovery_case_id == case.id,
                PromiseToPay.status == "ACTIVE",
            )
        )
        if active_ptp:
            return "Active Promise-to-Pay exists for this recovery case. Outreach is paused.", "PROMISE_TO_PAY_ACTIVE"

        # 4. Check Customer Explicit DND Flag
        if getattr(customer, "dnd_enabled", False):
            return "Customer has explicit DND flag enabled.", "CUSTOMER_DND_ENABLED"

        # 5. Check DND Quiet Hours (20:00 - 08:00)
        from zoneinfo import ZoneInfo
        tz_name = getattr(customer, "timezone", None) or settings.DEFAULT_TIMEZONE or "Asia/Kolkata"
        try:
            target_tz = ZoneInfo(tz_name)
        except Exception:
            target_tz = ZoneInfo("Asia/Kolkata")

        local_now = now.astimezone(target_tz)
        local_hour = local_now.hour

        dnd_start = settings.WHATSAPP_DND_START_HOUR or 20  # 20:00 (8 PM)
        dnd_end = settings.WHATSAPP_DND_END_HOUR or 8      # 08:00 (8 AM)

        if local_hour >= dnd_start or local_hour < dnd_end:
            return f"Customer is currently in DND quiet hours ({dnd_start:02d}:00 - {dnd_end:02d}:00 in {tz_name}).", "DND_QUIET_HOURS"

        # 5. Check Concurrent Active Calls
        active_call = db.scalar(
            select(VoiceCall).where(
                VoiceCall.recovery_case_id == case.id,
                VoiceCall.status.in_(["QUEUED", "RINGING", "IN-PROGRESS", "IN_PROGRESS"]),
            )
        )
        if active_call:
            return f"Another voice call is currently active ({active_call.status}).", "CONCURRENT_CALL_ACTIVE"

        # 6. Check Maximum Attempts Cap
        all_attempts = db.scalars(
            select(VoiceCall)
            .where(VoiceCall.recovery_case_id == case.id)
            .order_by(VoiceCall.created_at.desc())
        ).all()

        max_attempts = settings.MAX_VOICE_ATTEMPTS or 3
        if len(all_attempts) >= max_attempts:
            return f"Maximum voice recovery attempts ({max_attempts}) reached.", "MAX_ATTEMPTS_EXCEEDED"

        # 7. Check Cooldown Period
        cooldown_mins = settings.VOICE_COOLDOWN_MINUTES or 60
        if all_attempts:
            last_call = all_attempts[0]
            if last_call.created_at:
                elapsed = now - (last_call.created_at if last_call.created_at.tzinfo else last_call.created_at.replace(tzinfo=timezone.utc))
                if elapsed < timedelta(minutes=cooldown_mins):
                    mins_left = int((timedelta(minutes=cooldown_mins) - elapsed).total_seconds() / 60)
                    return f"Voice recovery cooldown active. Please wait {mins_left} more minutes.", "COOLDOWN_ACTIVE"

        # 8. Cross-channel compliance: consent ledger, TRAI DND, RBI call window (08:00-19:00), holidays, frequency caps, handoff
        from app.compliance.contact_policy import ContactPolicy
        compliance = ContactPolicy.evaluate(db, case=case, customer=customer, channel="VOICE", now=now)
        if not compliance.allowed:
            return compliance.reason, compliance.rule

        return None, None

    @classmethod
    def _record_audit_log(
        cls,
        db: Session,
        case_id: uuid.UUID,
        action: str,
        entity_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AuditLog:
        """Create structured audit log entry for voice recovery operations."""
        entry = AuditLog(
            recovery_case_id=case_id,
            actor_type="SYSTEM",
            actor_id="voice_recovery_service_v1",
            action=action,
            entity_type="VoiceCall",
            entity_id=entity_id or str(case_id),
            audit_metadata=metadata or {},
        )
        db.add(entry)
        db.flush()
        return entry
