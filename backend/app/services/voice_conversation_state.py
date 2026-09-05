"""Voice Conversation State Machine, Intent Enums, and Safety Guards."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re
from typing import Any, Dict, Optional


class ConversationState(str, Enum):
    """Lifecycle states of a multi-turn voice recovery conversation."""
    GREETING = "GREETING"
    PAYMENT_STATUS = "PAYMENT_STATUS"
    PAY_NOW = "PAY_NOW"
    PROMISE_TO_PAY = "PROMISE_TO_PAY"
    PROMISE_CONFIRMATION = "PROMISE_CONFIRMATION"
    DISPUTE = "DISPUTE"
    REFUSAL = "REFUSAL"
    WRONG_NUMBER = "WRONG_NUMBER"
    HUMAN_REQUEST = "HUMAN_REQUEST"
    UNKNOWN = "UNKNOWN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ConversationIntent(str, Enum):
    """Classified customer intent from spoken response."""
    PAY_NOW = "PAY_NOW"
    PROMISE_TO_PAY = "PROMISE_TO_PAY"
    ALREADY_PAID = "ALREADY_PAID"
    REFUSAL_TO_PAY = "REFUSAL_TO_PAY"
    DISPUTE = "DISPUTE"
    WRONG_NUMBER = "WRONG_NUMBER"
    HUMAN_REQUEST = "HUMAN_REQUEST"
    CONFIRMATION_YES = "CONFIRMATION_YES"
    CONFIRMATION_NO = "CONFIRMATION_NO"
    UNKNOWN = "UNKNOWN"


@dataclass
class StructuredIntentResult:
    """Structured classification output with schema validation."""
    intent: ConversationIntent
    confidence: float = 1.0
    promised_date: Optional[datetime] = None
    promised_date_display: Optional[str] = None
    promised_amount: Optional[float] = None
    needs_clarification: bool = False
    reason: str = ""
    raw_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent.value,
            "confidence": round(self.confidence, 3),
            "promised_date": self.promised_date.isoformat() if self.promised_date else None,
            "promised_date_display": self.promised_date_display,
            "promised_amount": self.promised_amount,
            "needs_clarification": self.needs_clarification,
            "reason": self.reason,
            "raw_text": self.raw_text,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], raw_text: str = "") -> "StructuredIntentResult":
        """Parse from dictionary with robust validation."""
        raw_intent = data.get("intent", "UNKNOWN")
        try:
            intent = ConversationIntent(raw_intent)
        except ValueError:
            intent = ConversationIntent.UNKNOWN

        conf_raw = data.get("confidence", 0.0)
        try:
            conf = float(conf_raw) if conf_raw is not None else 0.0
        except (ValueError, TypeError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))

        p_date = None
        date_str = data.get("promised_date")
        if date_str:
            try:
                p_date = datetime.fromisoformat(date_str)
            except Exception:
                p_date = None

        return cls(
            intent=intent,
            confidence=conf,
            promised_date=p_date,
            promised_date_display=data.get("promised_date_display"),
            promised_amount=float(data["promised_amount"]) if data.get("promised_amount") is not None else None,
            needs_clarification=bool(data.get("needs_clarification", False)),
            reason=str(data.get("reason", "")),
            raw_text=raw_text or str(data.get("raw_text", "")),
        )


class ConversationSafetyGuard:
    """Enforces safety rules on voice interactions and outputs."""

    FORBIDDEN_PATTERNS = [
        r"\botp\b",
        r"\bcvv\b",
        r"\bcvc\b",
        r"\bpin\b",
        r"\bpassword\b",
        r"\bcard\s+number\b",
        r"\bcredit\s+card\b",
        r"\bdebit\s+card\b",
        r"\bbank\s+password\b",
        r"\bupi\s+pin\b",
    ]

    FORBIDDEN_CLAIMS = [
        r"\blegal\s+action\b",
        r"\bpolice\b",
        r"\bcourt\b",
        r"\barrest\b",
        r"\bjail\b",
        r"\bpenalty\b",
        r"\bdiscount\b",
        r"\bwaive\b",
    ]

    @classmethod
    def sanitize_speech_output(cls, text: str) -> str:
        """Ensure no sensitive credential requests or false claims appear in agent speech."""
        for pattern in cls.FORBIDDEN_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return "A secure payment link has been sent to your registered contact. Please use that link to complete your payment safely. Thank you."

        for pattern in cls.FORBIDDEN_CLAIMS:
            if re.search(pattern, text, re.IGNORECASE):
                # Replace with neutral professional statement
                text = re.sub(pattern, "standard balance review", text, flags=re.IGNORECASE)

        return text
