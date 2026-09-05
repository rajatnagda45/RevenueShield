"""Tests for Feature Schema & Point-in-Time Anti-Leakage Validation."""
import pytest
from app.ml.features import (
    FeatureSchemaV1,
    validate_point_in_time_features,
    build_feature_dict_for_inference,
)


def test_validate_point_in_time_features_passes_for_clean_features():
    """Verify that legitimate pre-decision features pass validation without error."""
    clean_features = {
        "amount_at_risk": 5000.0,
        "case_age_at_decision_hours": 2.5,
        "diagnosis_category": "INSUFFICIENT_FUNDS",
        "diagnosis_confidence": 0.85,
        "risk_score": 45.0,
        "heuristic_recovery_probability": 0.65,
        "customer_success_rate": 0.80,
        "customer_success_count": 8,
        "customer_failure_count": 2,
        "previous_recovery_attempts": 1,
        "payment_method": "CARD",
        "bank": "HDFC",
        "error_code": "BAD_REQUEST_ERROR",
        "error_source": "ISSUER_BANK",
        "error_step": "PAYMENT_AUTHORIZATION",
        "error_reason": "insufficient_funds",
        "action_type": "SEND_PAYMENT_LINK",
        "decision_score": 0.75,
        "decision_confidence": 0.80,
    }
    # Should not raise
    validate_point_in_time_features(clean_features)


def test_validate_point_in_time_features_rejects_outcome_leakage():
    """Verify that any post-decision outcome attributes trigger strict ValueError."""
    forbidden_examples = [
        {"amount_recovered": 5000.0, "diagnosis_category": "CARD_DECLINED"},
        {"recovered_amount": 2500.0, "action_type": "RETRY_PAYMENT"},
        {"outcome_type": "RECOVERED", "amount_at_risk": 1000.0},
        {"payment_captured_at": "2026-08-22T00:00:00Z"},
        {"recovery_percentage": 100.0},
        {"time_to_recovery": 3600.0},
        {"final_payment_status": "CAPTURED"},
        {"attribution_type": "DIRECT"},
        {"label": 1},
    ]

    for bad_feat in forbidden_examples:
        with pytest.raises(ValueError, match="Point-in-time anti-leakage violation"):
            validate_point_in_time_features(bad_feat)


def test_build_feature_dict_for_inference():
    """Verify inference feature map builder creates complete, valid dictionary."""
    f = build_feature_dict_for_inference(
        amount_at_risk=2500.0,
        case_age_at_decision_hours=4.0,
        diagnosis_category="AUTHENTICATION_FAILED",
        diagnosis_confidence=0.90,
        risk_score=30.0,
        heuristic_recovery_probability=0.75,
        customer_success_rate=0.85,
        customer_success_count=5,
        customer_failure_count=1,
        previous_recovery_attempts=0,
        payment_method="UPI",
        bank="ICICI",
        error_code="BAD_REQUEST_ERROR",
        error_source="CUSTOMER",
        error_step="AUTHENTICATION",
        error_reason="incorrect_otp",
        action_type="SEND_PAYMENT_LINK",
    )
    assert f["amount_at_risk"] == 2500.0
    assert f["diagnosis_category"] == "AUTHENTICATION_FAILED"
    assert f["action_type"] == "SEND_PAYMENT_LINK"
    for k in FeatureSchemaV1.ALL_FEATURES:
        assert k in f
