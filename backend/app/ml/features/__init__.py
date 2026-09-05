"""Feature Schema & Point-in-Time Anti-Leakage Validation for Predictive Recovery."""
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional, Set
import uuid

from app.ml.features.recovery_features import (
    RecoveryFeatureVector,
    RecoveryFeatureSchema,
    validate_pre_intervention_features,
    CategoricalEncoder,
    InterventionTrainingRow,
)

logger = logging.getLogger(__name__)


@dataclass
class ActionPredictionFeatures:
    """Consolidated decision-time feature vector for action scoring."""

    amount_at_risk: float
    case_age_at_decision_hours: float
    diagnosis_category: str
    diagnosis_confidence: float
    risk_score: float
    heuristic_recovery_probability: float
    customer_success_rate: float
    customer_success_count: int
    customer_failure_count: int
    previous_recovery_attempts: int
    payment_method: str
    action_type: str
    bank: str = "UNKNOWN"
    error_code: str = "UNKNOWN"
    error_source: str = "UNKNOWN"
    error_step: str = "UNKNOWN"
    error_reason: str = "UNKNOWN"
    decision_score: float = 0.50
    decision_confidence: float = 0.70
    hour_of_day: int = 12
    day_of_week: int = 2
    is_business_hour: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class FeatureSchemaV1:
    """Canonical feature specification for v1 predictive models."""

    SCHEMA_VERSION = "v1"

    NUMERICAL_FEATURES: List[str] = [
        "amount_at_risk",
        "case_age_at_decision_hours",
        "diagnosis_confidence",
        "risk_score",
        "heuristic_recovery_probability",
        "customer_success_rate",
        "customer_success_count",
        "customer_failure_count",
        "previous_recovery_attempts",
        "decision_score",
        "decision_confidence",
    ]

    CATEGORICAL_FEATURES: List[str] = [
        "diagnosis_category",
        "payment_method",
        "bank",
        "error_code",
        "error_source",
        "error_step",
        "error_reason",
        "action_type",
    ]

    ALL_FEATURES: List[str] = NUMERICAL_FEATURES + CATEGORICAL_FEATURES

    # Forbidden outcome/post-decision keys to ensure zero future information leakage
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
        "label",
        "is_recovered",
    }


def validate_point_in_time_features(feature_dict: Dict[str, Any]) -> None:
    """Validate that a feature map contains strictly point-in-time attributes and zero outcome leakage."""
    if not isinstance(feature_dict, dict):
        raise ValueError(f"Feature snapshot must be a dict, got {type(feature_dict)}")

    leaked_keys = [k for k in feature_dict.keys() if k.lower() in FeatureSchemaV1.FORBIDDEN_OUTCOME_KEYS]
    if leaked_keys:
        raise ValueError(
            f"Point-in-time anti-leakage violation: Detected forbidden outcome attributes in feature snapshot: {leaked_keys}"
        )


def build_feature_dict_for_inference(
    amount_at_risk: float,
    case_age_at_decision_hours: float,
    diagnosis_category: str,
    diagnosis_confidence: float,
    risk_score: float,
    heuristic_recovery_probability: float,
    customer_success_rate: float,
    customer_success_count: int,
    customer_failure_count: int,
    previous_recovery_attempts: int,
    payment_method: str,
    bank: Optional[str],
    error_code: Optional[str],
    error_source: Optional[str],
    error_step: Optional[str],
    error_reason: Optional[str],
    action_type: str,
    decision_score: float = 0.50,
    decision_confidence: float = 0.70,
) -> Dict[str, Any]:
    """Construct a clean, validated feature dictionary for a single candidate action inference."""
    features = {
        "amount_at_risk": float(amount_at_risk),
        "case_age_at_decision_hours": float(max(0.0, case_age_at_decision_hours)),
        "diagnosis_category": str(diagnosis_category or "UNKNOWN"),
        "diagnosis_confidence": float(diagnosis_confidence or 0.50),
        "risk_score": float(risk_score or 50.0),
        "heuristic_recovery_probability": float(heuristic_recovery_probability or 0.50),
        "customer_success_rate": float(customer_success_rate or 0.0),
        "customer_success_count": int(customer_success_count or 0),
        "customer_failure_count": int(customer_failure_count or 0),
        "previous_recovery_attempts": int(previous_recovery_attempts or 0),
        "payment_method": str(payment_method or "CARD"),
        "bank": str(bank or "UNKNOWN"),
        "error_code": str(error_code or "UNKNOWN"),
        "error_source": str(error_source or "UNKNOWN"),
        "error_step": str(error_step or "UNKNOWN"),
        "error_reason": str(error_reason or "UNKNOWN"),
        "action_type": str(action_type),
        "decision_score": float(decision_score or 0.50),
        "decision_confidence": float(decision_confidence or 0.70),
    }

    validate_point_in_time_features(features)
    return features


__all__ = [
    "ActionPredictionFeatures",
    "FeatureSchemaV1",
    "validate_point_in_time_features",
    "build_feature_dict_for_inference",
    "RecoveryFeatureVector",
    "RecoveryFeatureSchema",
    "validate_pre_intervention_features",
    "CategoricalEncoder",
    "InterventionTrainingRow",
]
