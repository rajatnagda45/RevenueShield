"""Voice Intent & Promise-to-Pay Date Extractor from Speech Recognition."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import re
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.services.voice_conversation_state import ConversationIntent, StructuredIntentResult


class VoiceCustomerIntent(str, Enum):
    PROMISE_TO_PAY = "PROMISE_TO_PAY"
    PAY_NOW = "PAY_NOW"
    ALREADY_PAID = "ALREADY_PAID"
    REFUSAL_TO_PAY = "REFUSAL_TO_PAY"
    WRONG_NUMBER = "WRONG_NUMBER"
    DISPUTE = "DISPUTE"
    HUMAN_REQUEST = "HUMAN_REQUEST"
    CONFIRMATION_YES = "CONFIRMATION_YES"
    CONFIRMATION_NO = "CONFIRMATION_NO"
    UNKNOWN = "UNKNOWN"


@dataclass
class VoiceIntentResult:
    intent: VoiceCustomerIntent
    promised_date: Optional[datetime] = None
    promised_date_display: Optional[str] = None
    confidence: float = 1.0
    raw_text: str = ""
    notes: Optional[str] = None


class VoiceIntentExtractor:
    """Extracts customer intent and promised payment dates from spoken speech transcripts."""

    WEEKDAY_MAP = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }

    MONTH_MAP = {
        "january": 1, "jan": 1,
        "february": 2, "feb": 2,
        "march": 3, "mar": 3,
        "april": 4, "apr": 4,
        "may": 5,
        "june": 6, "jun": 6,
        "july": 7, "jul": 7,
        "august": 8, "aug": 8,
        "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10,
        "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }

    WORD_TO_NUMBER = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "a": 1, "an": 1,
    }

    @classmethod
    def classify_intent(
        cls,
        speech_text: Optional[str],
        reference_datetime: Optional[datetime] = None,
        timezone_str: Optional[str] = None,
        speech_confidence: float = 0.9,
        current_state: Optional[str] = None,
    ) -> StructuredIntentResult:
        """Classify speech transcript into StructuredIntentResult for state machine routing."""
        legacy_res = cls.extract_promise_intent(
            speech_text=speech_text,
            reference_datetime=reference_datetime,
            timezone_str=timezone_str,
            speech_confidence=speech_confidence,
            current_state=current_state,
        )

        try:
            mapped_intent = ConversationIntent(legacy_res.intent.value)
        except ValueError:
            mapped_intent = ConversationIntent.UNKNOWN

        return StructuredIntentResult(
            intent=mapped_intent,
            confidence=legacy_res.confidence,
            promised_date=legacy_res.promised_date,
            promised_date_display=legacy_res.promised_date_display,
            promised_amount=None,
            needs_clarification=(mapped_intent == ConversationIntent.UNKNOWN and legacy_res.confidence < 0.6),
            reason=legacy_res.notes or "",
            raw_text=legacy_res.raw_text,
        )

    @classmethod
    def extract_promise_intent(
        cls,
        speech_text: Optional[str],
        reference_datetime: Optional[datetime] = None,
        timezone_str: Optional[str] = None,
        speech_confidence: float = 0.9,
        current_state: Optional[str] = None,
    ) -> VoiceIntentResult:
        """Parse spoken text to identify intent and extract target payment date."""
        if not speech_text or not speech_text.strip():
            return VoiceIntentResult(
                intent=VoiceCustomerIntent.UNKNOWN,
                confidence=0.0,
                raw_text="",
                notes="Empty speech transcript received.",
            )

        text = speech_text.strip().lower()
        tz_name = timezone_str or settings.DEFAULT_TIMEZONE or "Asia/Kolkata"
        try:
            target_tz = ZoneInfo(tz_name)
        except Exception:
            target_tz = ZoneInfo("Asia/Kolkata")

        ref_dt = reference_datetime or datetime.now(timezone.utc)
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)
        local_ref = ref_dt.astimezone(target_tz)

        # 1. State-specific Confirmation Handling
        if current_state == "PROMISE_CONFIRMATION":
            if any(w in text for w in ["yes", "yeah", "yep", "sure", "correct", "confirmed", "right", "yes please", "that's right", "i confirm", "ok", "okay"]):
                return VoiceIntentResult(
                    intent=VoiceCustomerIntent.CONFIRMATION_YES,
                    confidence=speech_confidence,
                    raw_text=speech_text,
                    notes="Customer confirmed the promised payment date.",
                )
            if any(w in text for w in ["no", "nope", "not that date", "wrong date", "change date", "incorrect", "not today"]):
                return VoiceIntentResult(
                    intent=VoiceCustomerIntent.CONFIRMATION_NO,
                    confidence=speech_confidence,
                    raw_text=speech_text,
                    notes="Customer rejected the promised payment date confirmation.",
                )

        # 2. Check for Human Request
        if any(phrase in text for phrase in ["human", "agent", "person", "representative", "customer care", "speak to someone", "operator"]):
            return VoiceIntentResult(
                intent=VoiceCustomerIntent.HUMAN_REQUEST,
                confidence=speech_confidence,
                raw_text=speech_text,
                notes="Customer requested human assistance.",
            )

        # 3. Check for Already Paid
        if any(phrase in text for phrase in [
            "already paid", "i have paid", "done payment", "payment done",
            "paid yesterday", "i paid", "already made", "made the payment",
            "payment was made", "payment made", "completed the payment", "already completed"
        ]):
            return VoiceIntentResult(
                intent=VoiceCustomerIntent.ALREADY_PAID,
                confidence=speech_confidence,
                raw_text=speech_text,
                notes="Customer claims payment was already completed.",
            )

        # 4. Check for Wrong Number
        if any(phrase in text for phrase in ["wrong number", "not me", "wrong person", "who is this", "wrong contact"]):
            return VoiceIntentResult(
                intent=VoiceCustomerIntent.WRONG_NUMBER,
                confidence=speech_confidence,
                raw_text=speech_text,
                notes="Customer reported wrong number.",
            )

        # 5. Check for Dispute
        if any(phrase in text for phrase in ["dispute", "fraud", "incorrect charge", "did not order", "cancel subscription", "don't owe this", "not my bill", "not owing"]):
            return VoiceIntentResult(
                intent=VoiceCustomerIntent.DISPUTE,
                confidence=speech_confidence,
                raw_text=speech_text,
                notes="Customer initiated dispute or reported fraudulent charge.",
            )

        # 6. Check for Refusal
        if any(phrase in text for phrase in ["cannot pay", "will not pay", "won't pay", "refuse to pay", "never pay", "don't have money", "no money", "can't pay"]):
            return VoiceIntentResult(
                intent=VoiceCustomerIntent.REFUSAL_TO_PAY,
                confidence=speech_confidence,
                raw_text=speech_text,
                notes="Customer expressed inability or refusal to pay.",
            )

        # 7. Check for PAY NOW intent
        if any(phrase in text for phrase in ["pay now", "pay today", "pay right now", "i can pay now", "send link", "send payment link", "send me the link", "yes i can pay", "i can pay today", "ready to pay"]):
            return VoiceIntentResult(
                intent=VoiceCustomerIntent.PAY_NOW,
                confidence=speech_confidence,
                raw_text=speech_text,
                notes="Customer agreed to pay immediately today.",
            )

        # 8. Check for Ambiguous or General Promise to Pay (e.g. "pay later", "pay next month")
        if any(phrase in text for phrase in ["pay later", "pay next month", "pay sometime", "not today but later", "schedule later", "next month", "after some days", "later date"]):
            return VoiceIntentResult(
                intent=VoiceCustomerIntent.PROMISE_TO_PAY,
                promised_date=None,
                confidence=speech_confidence,
                raw_text=speech_text,
                notes="Customer indicated intent to pay at a later date.",
            )

        # 8. Extract Promised Date for Promise-to-Pay
        parsed_dt = cls._extract_date_from_text(text, local_ref)

        if parsed_dt:
            # Normalize to 17:00 local time
            target_local = parsed_dt.replace(hour=17, minute=0, second=0, microsecond=0)
            target_utc = target_local.astimezone(timezone.utc)

            # Ensure promised date is in future
            if target_utc <= ref_dt:
                target_utc = target_utc + timedelta(days=1)
                target_local = target_utc.astimezone(target_tz)

            formatted_display = target_local.strftime("%A, %B %d, %Y")

            return VoiceIntentResult(
                intent=VoiceCustomerIntent.PROMISE_TO_PAY,
                promised_date=target_utc,
                promised_date_display=formatted_display,
                confidence=speech_confidence,
                raw_text=speech_text,
                notes=f"Extracted promise for {formatted_display} from '{speech_text}'",
            )

        # 9. Simple yes in PAYMENT_STATUS state implies PAY_NOW if no other intent
        if current_state in ["PAYMENT_STATUS", "GREETING"] and any(w in text.split() for w in ["yes", "yeah", "yep", "sure", "ok", "okay"]):
            return VoiceIntentResult(
                intent=VoiceCustomerIntent.PAY_NOW,
                confidence=speech_confidence,
                raw_text=speech_text,
                notes="Customer answered yes to paying today.",
            )

        # 10. Default fallback if intent unclear
        return VoiceIntentResult(
            intent=VoiceCustomerIntent.UNKNOWN,
            confidence=0.5,
            raw_text=speech_text,
            notes="Could not extract a specific payment date or known intent from speech.",
        )

    @classmethod
    def _extract_date_from_text(cls, text: str, ref_dt: datetime) -> Optional[datetime]:
        """Heuristic and regex date extraction from normalized text."""
        # A. Relative Days: "tomorrow", "day after tomorrow", "today"
        if "day after tomorrow" in text or "day after" in text:
            return ref_dt + timedelta(days=2)
        if "tomorrow" in text:
            return ref_dt + timedelta(days=1)
        if "today" in text or "tonight" in text or "evening" in text:
            return ref_dt + timedelta(days=1)

        # B. "in X days" / "in X weeks"
        days_match = re.search(r"in\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|a)\s+days?", text)
        if days_match:
            num_str = days_match.group(1)
            num_days = int(num_str) if num_str.isdigit() else cls.WORD_TO_NUMBER.get(num_str, 1)
            return ref_dt + timedelta(days=num_days)

        weeks_match = re.search(r"in\s+(\d+|one|two|three|four|a)\s+weeks?", text)
        if weeks_match:
            num_str = weeks_match.group(1)
            num_weeks = int(num_str) if num_str.isdigit() else cls.WORD_TO_NUMBER.get(num_str, 1)
            return ref_dt + timedelta(days=num_weeks * 7)

        # C. "next week"
        if "next week" in text:
            days_ahead = 7 - ref_dt.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            return ref_dt + timedelta(days=days_ahead)

        # D. Weekday expressions: "next monday", "this friday", "by wednesday", "on tuesday", "monday"
        for weekday_name, target_idx in cls.WEEKDAY_MAP.items():
            if weekday_name in text:
                current_idx = ref_dt.weekday()
                offset = (target_idx - current_idx) % 7
                if offset == 0:
                    offset = 7
                return ref_dt + timedelta(days=offset)

        # E. Explicit Month + Day: "28th August", "August 28", "28 Aug", "28th of August"
        for month_name, month_num in cls.MONTH_MAP.items():
            if month_name in text:
                day_match = re.search(r"(\d{1,2})(?:st|nd|rd|th)?", text)
                if day_match:
                    try:
                        day_val = int(day_match.group(1))
                        if 1 <= day_val <= 31:
                            target_year = ref_dt.year
                            target_date = datetime(target_year, month_num, day_val, tzinfo=ref_dt.tzinfo)
                            if target_date < ref_dt:
                                target_date = datetime(target_year + 1, month_num, day_val, tzinfo=ref_dt.tzinfo)
                            return target_date
                    except ValueError:
                        pass

        # F. "end of the month" / "month end" / "by 30th" / "by 31st"
        if "end of the month" in text or "month end" in text:
            next_month = ref_dt.replace(day=28) + timedelta(days=4)
            last_day = next_month - timedelta(days=next_month.day)
            return last_day

        return None
