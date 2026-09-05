"""Pre-Intervention Feature Engineering & Point-in-Time Schema for Next-Best-Action Engine."""
from dataclasses import dataclass, asdict
import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class RecoveryFeatureVector:
    """Consolidated pre-intervention feature vector.
    
    Contains ONLY features available before an intervention is selected.
    No future outcomes, labels, or post-intervention data allowed.
    """

    # Case Features
    amount_at_risk: float
    currency: str = "INR"
    days_overdue: float = 0.0
    failure_code: str = "UNKNOWN"
    failure_category: str = "UNKNOWN"
    payment_type: str = "card"
    is_subscription_or_invoice: int = 1

    # Customer Features (strictly historical up to prediction_timestamp)
    customer_age_days: float = 0.0
    previous_successful_payments: int = 0
    previous_failed_payments: int = 0
    previous_recoveries: int = 0
    previous_promises_to_pay: int = 0
    previous_ptp_fulfillment_rate: float = 0.0
    previous_voice_attempts: int = 0
    previous_email_attempts: int = 0
    previous_whatsapp_attempts: int = 0

    # Timing Features
    hour_of_day: int = 12
    day_of_week: int = 2
    days_since_failure: float = 0.0

    # History Features
    previous_intervention_outcome: str = "NONE"
    number_of_previous_recovery_attempts: int = 0
    previous_recovery_time_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert feature vector to dictionary."""
        return asdict(self)


class RecoveryFeatureSchema:
    """Canonical feature specification and anti-leakage guards for Next-Best-Action ML."""

    SCHEMA_VERSION = "v2_nba"

    NUMERICAL_FEATURES: List[str] = [
        "amount_at_risk",
        "days_overdue",
        "is_subscription_or_invoice",
        "customer_age_days",
        "previous_successful_payments",
        "previous_failed_payments",
        "previous_recoveries",
        "previous_promises_to_pay",
        "previous_ptp_fulfillment_rate",
        "previous_voice_attempts",
        "previous_email_attempts",
        "previous_whatsapp_attempts",
        "hour_of_day",
        "day_of_week",
        "days_since_failure",
        "number_of_previous_recovery_attempts",
        "previous_recovery_time_seconds",
    ]

    CATEGORICAL_FEATURES: List[str] = [
        "currency",
        "failure_code",
        "failure_category",
        "payment_type",
        "previous_intervention_outcome",
    ]

    ALL_FEATURES: List[str] = NUMERICAL_FEATURES + CATEGORICAL_FEATURES

    # Forbidden outcome/post-decision keys to ensure ZERO future information leakage
    FORBIDDEN_OUTCOME_KEYS: Set[str] = {
        "amount_recovered",
        "recovered_amount",
        "outcome_type",
        "payment_captured_at",
        "recovery_percentage",
        "time_to_recovery",
        "time_to_recovery_seconds",
        "final_payment_status",
        "attribution_type",
        "attribution",
        "label",
        "is_recovered",
        "recovered",
        "future_intervention",
        "future_promise_to_pay",
    }

    # Standard Categorical Vocabularies for Encoding
    CATEGORICAL_VOCABULARIES: Dict[str, List[str]] = {
        "currency": ["INR", "USD", "EUR", "GBP", "OTHER"],
        "failure_category": [
            "INSUFFICIENT_FUNDS",
            "AUTHENTICATION_FAILURE",
            "TECHNICAL_ERROR",
            "NETWORK_ERROR",
            "FRAUD_SUSPECTED",
            "EXPIRED_CARD",
            "USER_ABORTED",
            "UNKNOWN",
        ],
        "payment_type": ["card", "upi", "netbanking", "wallet", "emi", "unknown"],
        "previous_intervention_outcome": [
            "NONE",
            "SUCCESS",
            "FAILED",
            "PENDING",
            "NO_RESPONSE",
            "UNKNOWN",
        ],
        "intervention_type": [
            "PAYMENT_RETRY",
            "EMAIL",
            "VOICE",
            "WHATSAPP",
            "NO_ACTION",
            "UNKNOWN",
        ],
    }


def validate_pre_intervention_features(feature_dict: Dict[str, Any]) -> None:
    """Validate that a feature map contains strictly pre-intervention attributes and zero outcome leakage.

    Raises:
        ValueError: If any forbidden outcome attributes or invalid keys are detected.
    """
    if not isinstance(feature_dict, dict):
        raise ValueError(f"Feature snapshot must be a dict, got {type(feature_dict)}")

    leaked_keys = [
        k for k in feature_dict.keys()
        if k.lower() in RecoveryFeatureSchema.FORBIDDEN_OUTCOME_KEYS
    ]
    if leaked_keys:
        raise ValueError(
            f"Pre-intervention anti-leakage violation: Detected forbidden outcome attributes: {leaked_keys}"
        )

    # Basic range sanity checks
    if feature_dict.get("amount_at_risk", 0.0) < 0:
        raise ValueError("amount_at_risk cannot be negative")

    if feature_dict.get("hour_of_day") is not None and not (0 <= feature_dict["hour_of_day"] <= 23):
        raise ValueError(f"hour_of_day out of range [0, 23]: {feature_dict['hour_of_day']}")

    if feature_dict.get("day_of_week") is not None and not (0 <= feature_dict["day_of_week"] <= 6):
        raise ValueError(f"day_of_week out of range [0, 6]: {feature_dict['day_of_week']}")


class CategoricalEncoder:
    """Standardized deterministic encoder for categorical variables in ML features."""

    @classmethod
    def get_vocabulary(cls, feature_name: str) -> List[str]:
        return RecoveryFeatureSchema.CATEGORICAL_VOCABULARIES.get(feature_name, ["UNKNOWN"])

    @classmethod
    def encode_one_hot(cls, feature_name: str, value: Any) -> Dict[str, int]:
        """Encode a categorical feature as a dictionary of binary indicator variables."""
        vocab = cls.get_vocabulary(feature_name)
        val_str = str(value or "UNKNOWN").upper()
        encoded = {}
        matched = False
        for category in vocab:
            col_name = f"{feature_name}_{category.lower()}"
            if val_str == category.upper():
                encoded[col_name] = 1
                matched = True
            else:
                encoded[col_name] = 0

        # Default fallback to UNKNOWN / OTHER if not matched
        if not matched:
            fallback_col = f"{feature_name}_other" if f"{feature_name}_other" in encoded else f"{feature_name}_unknown"
            if fallback_col in encoded:
                encoded[fallback_col] = 1

        return encoded

    @classmethod
    def encode_features_to_numerical(cls, features: Dict[str, Any]) -> Dict[str, float]:
        """Convert all features (numerical + one-hot categorical) into a pure numerical dictionary."""
        result: Dict[str, float] = {}

        # Copy and cast numerical features
        for num_col in RecoveryFeatureSchema.NUMERICAL_FEATURES:
            raw_val = features.get(num_col, 0.0)
            try:
                result[num_col] = float(raw_val) if raw_val is not None else 0.0
            except (ValueError, TypeError):
                result[num_col] = 0.0

        # One-hot encode categorical features
        for cat_col in RecoveryFeatureSchema.CATEGORICAL_FEATURES:
            raw_val = features.get(cat_col, "UNKNOWN")
            one_hot = cls.encode_one_hot(cat_col, raw_val)
            for k, v in one_hot.items():
                result[k] = float(v)

        return result


@dataclass
class InterventionTrainingRow:
    """A single intervention-level training sample for the Next-Best-Action model."""

    case_id: str
    intervention_id: str
    intervention_type: str  # PAYMENT_RETRY, EMAIL, VOICE, WHATSAPP, NO_ACTION
    prediction_timestamp: str  # ISO-8601 string
    features: Dict[str, Any]
    recovered: int  # 0 or 1
    amount_at_risk: float
    amount_recovered: float
    time_to_recovery_seconds: Optional[float] = None

    def to_dict(self, flatten_features: bool = True) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        data = {
            "case_id": self.case_id,
            "intervention_id": self.intervention_id,
            "intervention_type": self.intervention_type,
            "prediction_timestamp": self.prediction_timestamp,
            "recovered": self.recovered,
            "amount_at_risk": self.amount_at_risk,
            "amount_recovered": self.amount_recovered,
            "time_to_recovery_seconds": self.time_to_recovery_seconds,
        }
        if flatten_features:
            for k, v in self.features.items():
                data[k] = v
        else:
            data["features"] = self.features
        return data
