"""Enums, dataclasses, and configuration for Recovery Outcome and Attribution Engine."""
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional


class OutcomeType(str, Enum):
    """Controlled vocabulary for recovery case and action business outcomes."""

    RECOVERED = "RECOVERED"
    PARTIALLY_RECOVERED = "PARTIALLY_RECOVERED"
    NOT_RECOVERED = "NOT_RECOVERED"
    EXPIRED = "EXPIRED"
    CUSTOMER_DECLINED = "CUSTOMER_DECLINED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    ACTION_FAILED = "ACTION_FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class AttributionType(str, Enum):
    """Controlled vocabulary for causality attribution of recovered revenue."""

    DIRECT = "DIRECT"
    LIKELY = "LIKELY"
    UNCERTAIN = "UNCERTAIN"
    ORGANIC = "ORGANIC"
    UNKNOWN = "UNKNOWN"


class ObservationWindows:
    """Configurable duration windows (in seconds) during which an intervention can be attributed."""

    SEND_PAYMENT_LINK: int = 72 * 3600          # 72 hours
    SEND_WHATSAPP_REMINDER: int = 72 * 3600     # 72 hours
    VOICE_OUTREACH: int = 7 * 24 * 3600         # 7 days
    RETRY_PAYMENT: int = 24 * 3600              # 24 hours
    DEFAULT: int = 72 * 3600                    # 72 hours

    @classmethod
    def get_window_seconds(cls, action_type: Optional[str]) -> int:
        """Return the observation window in seconds for an action type."""
        if not action_type:
            return cls.DEFAULT
        return getattr(cls, action_type, cls.DEFAULT)


@dataclass
class OutcomeEvaluationResult:
    """Outcome computation payload returned by OutcomeEngine."""

    outcome_type: OutcomeType
    attribution: AttributionType
    amount_at_risk: Decimal
    amount_recovered: Decimal
    recovery_percentage: float
    time_to_recovery_seconds: Optional[float]
    customer_response: Optional[str] = None
    outcome_metadata: Dict[str, Any] = field(default_factory=dict)
