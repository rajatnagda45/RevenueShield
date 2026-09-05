"""Multi-State Revenue Recovery Voice Conversation State Machine."""
from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.recovery_case import RecoveryCase
from app.models.voice_call import VoiceCall
from app.services.voice_conversation_state import (
    ConversationIntent,
    ConversationSafetyGuard,
    ConversationState,
)
from app.services.voice_intent_extractor import VoiceIntentExtractor

logger = logging.getLogger(__name__)


class VoiceConversationManager:
    """Manages multi-turn conversation state, intent transitions, and safe response generation."""

    @classmethod
    def generate_initial_twiml(
        cls,
        db: Session,
        voice_call: VoiceCall,
        gather_url: str,
    ) -> str:
        """Generate the standard compliant initial greeting TwiML."""
        case = voice_call.recovery_case or db.scalar(
            select(RecoveryCase).where(RecoveryCase.id == voice_call.recovery_case_id)
        )
        customer = voice_call.customer or (case.customer if case else None)

        vars_dict = voice_call.dynamic_variables or {}
        customer_name = vars_dict.get("customer_name") or (customer.name if customer else "Customer")
        amount_due = vars_dict.get("amount_due") or float(case.amount_at_risk if case else 0.0)
        currency = vars_dict.get("currency") or (case.currency if case else "INR")
        due_date = vars_dict.get("due_date") or (
            case.created_at.strftime("%B %d, %Y") if case and case.created_at else "recently"
        )

        currency_display = "Rupees" if currency.upper() in ["INR", "₹"] else currency
        amount_display = f"{amount_due:,.2f}"

        # Initialize conversation state
        metadata = dict(voice_call.call_metadata or {})
        metadata["conversation_state"] = ConversationState.PAYMENT_STATUS.value
        metadata["conversation_history"] = [
            {
                "role": "agent",
                "state": ConversationState.GREETING.value,
                "text": f"Hello {customer_name}. This is a payment recovery call regarding your outstanding {currency_display} {amount_display} payment, which was due on {due_date}. I'm calling to help you resolve it. Are you able to make the payment today?",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
        metadata["clarification_attempts"] = 0
        voice_call.call_metadata = metadata
        db.commit()

        initial_prompt = (
            f"Hello {customer_name}. This is a payment recovery call regarding your "
            f"outstanding {currency_display} {amount_display} payment, which was due on {due_date}. "
            f"I'm calling to help you resolve it. Are you able to make the payment today?"
        )
        initial_prompt = ConversationSafetyGuard.sanitize_speech_output(initial_prompt)

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="{gather_url}" method="POST" speechTimeout="auto" language="en-IN">
        <Say language="en-IN" voice="Polly.Aditi">{initial_prompt}</Say>
    </Gather>
    <Say language="en-IN" voice="Polly.Aditi">We did not receive your response. A direct payment link has been sent to your registered contact. Thank you, and have a wonderful day.</Say>
    <Hangup/>
</Response>"""
        return twiml.strip()

    @classmethod
    def handle_turn(
        cls,
        db: Session,
        voice_call: VoiceCall,
        payload: Dict[str, Any],
        gather_url: str,
    ) -> str:
        """Process spoken response, transition conversation state, and return corresponding TwiML."""
        case = voice_call.recovery_case or db.scalar(
            select(RecoveryCase).where(RecoveryCase.id == voice_call.recovery_case_id)
        )
        customer = voice_call.customer or (case.customer if case else None)
        customer_name = (customer.name if customer else None) or "Customer"

        metadata = dict(voice_call.call_metadata or {})
        current_state_str = metadata.get("conversation_state", ConversationState.PAYMENT_STATUS.value)
        try:
            current_state = ConversationState(current_state_str)
        except ValueError:
            current_state = ConversationState.PAYMENT_STATUS

        speech_result = str(payload.get("SpeechResult") or payload.get("speech_result") or "").strip()
        confidence_str = payload.get("Confidence") or payload.get("confidence") or "0.9"
        try:
            confidence = float(confidence_str)
        except (ValueError, TypeError):
            confidence = 0.9

        # 1. Classify Intent via Structured Classifier & Deterministic Fallback
        now = datetime.now(timezone.utc)
        customer_tz = getattr(customer, "timezone", None) or settings.DEFAULT_TIMEZONE or "Asia/Kolkata"

        structured_intent = VoiceIntentExtractor.classify_intent(
            speech_text=speech_result,
            reference_datetime=now,
            timezone_str=customer_tz,
            speech_confidence=confidence,
            current_state=current_state.value,
        )

        # Append turn to history
        history = list(metadata.get("conversation_history", []))
        history.append({
            "role": "customer",
            "state": current_state.value,
            "text": speech_result,
            "confidence": confidence,
            "detected_intent": structured_intent.intent.value,
            "timestamp": now.isoformat(),
        })

        amount_due = float(case.amount_at_risk if case else 4999.0)
        currency = case.currency if case else "INR"
        currency_display = "Rupees" if currency.upper() in ["INR", "₹"] else currency
        amount_display = f"{amount_due:,.2f}"

        # 2. State Machine Routing
        # ==============================================================================
        # A. State: PROMISE_CONFIRMATION
        # ==============================================================================
        if current_state == ConversationState.PROMISE_CONFIRMATION:
            if structured_intent.intent in [ConversationIntent.CONFIRMATION_YES, ConversationIntent.PAY_NOW]:
                # User confirmed the promised date -> create PromiseToPay
                pending_ptp = metadata.get("pending_ptp", {})
                promised_date_iso = pending_ptp.get("promised_date")
                promised_display = pending_ptp.get("promised_display") or "your promised date"

                if promised_date_iso and case:
                    try:
                        from app.services.promise_to_pay_service import PromiseToPayService
                        promised_dt = datetime.fromisoformat(promised_date_iso)
                        promise = PromiseToPayService.create_promise(
                            db=db,
                            recovery_case_id=case.id,
                            promised_amount=case.amount_at_risk,
                            promised_date=promised_dt,
                            source="VOICE_ASSISTANT",
                            notes=f"Voice commitment confirmed: {promised_display}",
                        )
                        cls._record_audit(
                            db=db,
                            case_id=case.id,
                            action="VOICE_PROMISE_TO_PAY_RECORDED",
                            entity_id=str(promise.id),
                            metadata={"promised_date": promised_date_iso, "display": promised_display},
                        )
                    except Exception as exc:
                        logger.error(f"[VOICE_PTP_CONFIRM_ERROR] {exc}")

                say_text = (
                    "Thank you. Your payment commitment has been recorded. "
                    "We will pause further reminders until then."
                )
                metadata["conversation_state"] = ConversationState.COMPLETED.value
                voice_call.call_metadata = metadata
                db.commit()
                return cls._build_hangup_twiml(say_text)

            elif structured_intent.intent == ConversationIntent.CONFIRMATION_NO:
                # User said no -> re-prompt for date
                metadata["conversation_state"] = ConversationState.PROMISE_TO_PAY.value
                voice_call.call_metadata = metadata
                db.commit()
                say_text = "Understood. When would you be able to complete the payment?"
                return cls._build_gather_twiml(say_text, gather_url)

        # ==============================================================================
        # B. Intent: PAY_NOW
        # ==============================================================================
        if structured_intent.intent == ConversationIntent.PAY_NOW:
            metadata["conversation_state"] = ConversationState.PAY_NOW.value
            # Trigger email recovery / payment link if case available
            if case:
                try:
                    from app.services.email_recovery_service import EmailRecoveryService
                    EmailRecoveryService.execute_recovery(db=db, case_id=str(case.id))
                except Exception as exc:
                    logger.warning(f"[PAY_NOW_LINK_ERROR] Could not dispatch email link: {exc}")

            cls._record_audit(
                db=db,
                case_id=case.id if case else None,
                action="VOICE_PAY_NOW_INTENT_CAPTURED",
                entity_id=str(voice_call.id),
                metadata={"speech_result": speech_result},
            )
            metadata["conversation_state"] = ConversationState.COMPLETED.value
            voice_call.call_metadata = metadata
            db.commit()

            say_text = (
                "I can help you complete the payment. I will send you a secure payment link "
                "to your registered email. Thank you, and have a wonderful day."
            )
            return cls._build_hangup_twiml(say_text)

        # ==============================================================================
        # C. Intent: ALREADY_PAID
        # ==============================================================================
        if structured_intent.intent == ConversationIntent.ALREADY_PAID:
            cls._record_audit(
                db=db,
                case_id=case.id if case else None,
                action="VOICE_CUSTOMER_CLAIMED_ALREADY_PAID",
                entity_id=str(voice_call.id),
                metadata={"speech_result": speech_result},
            )

            # Check authoritative database state (NEVER trust conversation alone)
            is_verified_recovered = (case is not None and case.status == "RECOVERED")

            metadata["conversation_state"] = ConversationState.COMPLETED.value
            voice_call.call_metadata = metadata
            db.commit()

            if is_verified_recovered:
                say_text = "Thank you. We have confirmed your payment. No further action is required."
            else:
                say_text = (
                    "I cannot confirm the payment yet. We will verify the transaction and "
                    "avoid asking you to make another payment right now."
                )
            return cls._build_hangup_twiml(say_text)

        # ==============================================================================
        # D. Intent: PROMISE_TO_PAY
        # ==============================================================================
        if structured_intent.intent == ConversationIntent.PROMISE_TO_PAY:
            if structured_intent.promised_date:
                # Valid date found -> transition to PROMISE_CONFIRMATION
                metadata["pending_ptp"] = {
                    "promised_date": structured_intent.promised_date.isoformat(),
                    "promised_display": structured_intent.promised_date_display,
                    "amount": amount_due,
                    "currency": currency,
                }
                metadata["conversation_state"] = ConversationState.PROMISE_CONFIRMATION.value
                voice_call.call_metadata = metadata
                db.commit()

                say_text = (
                    f"Just to confirm, you will make the {currency_display} {amount_display} "
                    f"payment on {structured_intent.promised_date_display}. Is that correct?"
                )
                return cls._build_gather_twiml(say_text, gather_url)
            else:
                # Ambiguous date or asked when -> ask clarification
                metadata["conversation_state"] = ConversationState.PROMISE_TO_PAY.value
                voice_call.call_metadata = metadata
                db.commit()

                say_text = "When would you be able to complete the payment?"
                return cls._build_gather_twiml(say_text, gather_url)

        # ==============================================================================
        # E. Intent: REFUSAL_TO_PAY
        # ==============================================================================
        if structured_intent.intent == ConversationIntent.REFUSAL_TO_PAY:
            cls._record_audit(
                db=db,
                case_id=case.id if case else None,
                action="VOICE_CUSTOMER_REFUSAL_TO_PAY",
                entity_id=str(voice_call.id),
                metadata={"speech_result": speech_result},
            )
            # "Stop calling me" spoken during a refusal withdraws consent for calls.
            if customer is not None:
                from app.compliance.consent import ConsentService
                ConsentService.process_inbound_text(db, customer=customer, text=speech_result, channel="VOICE", case=case)
            metadata["conversation_state"] = ConversationState.COMPLETED.value
            voice_call.call_metadata = metadata
            db.commit()

            say_text = "I understand. I won't pressure you. We can stop here."
            return cls._build_hangup_twiml(say_text)

        # ==============================================================================
        # F. Intent: DISPUTE
        # ==============================================================================
        if structured_intent.intent == ConversationIntent.DISPUTE:
            cls._record_audit(
                db=db,
                case_id=case.id if case else None,
                action="VOICE_CUSTOMER_DISPUTE_LOGGED",
                entity_id=str(voice_call.id),
                metadata={"speech_result": speech_result},
            )
            if case is not None:
                from app.compliance.handoff import HandoffService
                HandoffService.freeze(db, case=case, reason="Customer disputed the charge on a recovery call", source="VOICE_INTENT:DISPUTE", metadata={"voice_call_id": str(voice_call.id)})
            metadata["conversation_state"] = ConversationState.COMPLETED.value
            voice_call.call_metadata = metadata
            db.commit()

            say_text = "I understand. I'll record this as a payment dispute so it can be reviewed."
            return cls._build_hangup_twiml(say_text)

        # ==============================================================================
        # G. Intent: WRONG_NUMBER
        # ==============================================================================
        if structured_intent.intent == ConversationIntent.WRONG_NUMBER:
            cls._record_audit(
                db=db,
                case_id=case.id if case else None,
                action="VOICE_CUSTOMER_WRONG_NUMBER_REPORTED",
                entity_id=str(voice_call.id),
                metadata={"speech_result": speech_result},
            )
            # Never call this number again for this customer record, and let a human fix the identity.
            if customer is not None:
                from app.compliance.consent import ConsentService
                ConsentService.record(db, customer=customer, channel="VOICE", status="OPT_OUT", source="VOICE_INTENT", reason="Wrong number reported on recovery call", case=case, metadata={"voice_call_id": str(voice_call.id)})
            if case is not None:
                from app.compliance.handoff import HandoffService
                HandoffService.freeze(db, case=case, reason="Wrong number reported; customer identity must be verified", source="VOICE_INTENT:WRONG_NUMBER", metadata={"voice_call_id": str(voice_call.id)})
            metadata["conversation_state"] = ConversationState.COMPLETED.value
            voice_call.call_metadata = metadata
            db.commit()

            say_text = "Understood. I'll record that this number may not belong to the intended customer. Thank you."
            return cls._build_hangup_twiml(say_text)

        # ==============================================================================
        # H. Intent: HUMAN_REQUEST
        # ==============================================================================
        if structured_intent.intent == ConversationIntent.HUMAN_REQUEST:
            cls._record_audit(
                db=db,
                case_id=case.id if case else None,
                action="HUMAN_REQUESTED",
                entity_id=str(voice_call.id),
                metadata={"speech_result": speech_result},
            )
            if case is not None:
                from app.compliance.handoff import HandoffService
                HandoffService.freeze(db, case=case, reason="Customer asked to speak to a human", source="VOICE_INTENT:HUMAN_REQUEST", metadata={"voice_call_id": str(voice_call.id)})
            metadata["conversation_state"] = ConversationState.COMPLETED.value
            voice_call.call_metadata = metadata
            db.commit()

            say_text = "Understood. I'll record your request for human assistance."
            return cls._build_hangup_twiml(say_text)

        # ==============================================================================
        # I. Intent: UNKNOWN / Low Confidence (Loop Protection)
        # ==============================================================================
        attempts = int(metadata.get("clarification_attempts", 0)) + 1
        metadata["clarification_attempts"] = attempts

        if attempts <= 1:
            metadata["conversation_state"] = ConversationState.PAYMENT_STATUS.value
            voice_call.call_metadata = metadata
            db.commit()

            say_text = "I didn't quite catch that. Are you able to make the payment today, or would you like to schedule a later date?"
            return cls._build_gather_twiml(say_text, gather_url)
        else:
            metadata["conversation_state"] = ConversationState.COMPLETED.value
            voice_call.call_metadata = metadata
            db.commit()

            say_text = "I don't want to misunderstand you. We'll stop here and arrange follow-up through another channel."
            return cls._build_hangup_twiml(say_text)

    @classmethod
    def _build_gather_twiml(cls, prompt_text: str, gather_url: str) -> str:
        """Build interactive Gather TwiML with sanitized output."""
        safe_prompt = ConversationSafetyGuard.sanitize_speech_output(prompt_text)
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" action="{gather_url}" method="POST" speechTimeout="auto" language="en-IN">
        <Say language="en-IN" voice="Polly.Aditi">{safe_prompt}</Say>
    </Gather>
    <Say language="en-IN" voice="Polly.Aditi">We did not receive your response. A direct payment link has been sent to your registered contact. Thank you, and have a wonderful day.</Say>
    <Hangup/>
</Response>""".strip()

    @classmethod
    def _build_hangup_twiml(cls, prompt_text: str) -> str:
        """Build terminal Say + Hangup TwiML with sanitized output."""
        safe_prompt = ConversationSafetyGuard.sanitize_speech_output(prompt_text)
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="en-IN" voice="Polly.Aditi">{safe_prompt}</Say>
    <Hangup/>
</Response>""".strip()

    @classmethod
    def _record_audit(
        cls,
        db: Session,
        case_id: Optional[uuid.UUID],
        action: str,
        entity_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record structured audit log event."""
        if not case_id:
            return
        audit = AuditLog(
            id=uuid.uuid4(),
            recovery_case_id=case_id,
            actor_type="VOICE_AI",
            action=action,
            entity_type="VOICE_CALL",
            entity_id=entity_id or str(case_id),
            timestamp=datetime.now(timezone.utc),
            audit_metadata=metadata or {},
        )
        db.add(audit)
