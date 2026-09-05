"""Unit tests for RuleBasedRecoveryDecisionEngine scoring, explanations, and alternatives."""
from datetime import datetime, timezone
from decimal import Decimal
import pytest

from app.decision.base import ActionType, DecisionContext
from app.decision.engine import RuleBasedRecoveryDecisionEngine
from app.services.customer_intelligence import CustomerHistoricalFeatures


@pytest.fixture
def engine():
    return RuleBasedRecoveryDecisionEngine()


def test_case_a_transient_bank_failure_recommends_retry(engine: RuleBasedRecoveryDecisionEngine):
    """CASE A: Transient technical failure with high recovery prob recommends RETRY_PAYMENT."""
    ctx = DecisionContext(
        case_id="case_a",
        case_type="PAYMENT_FAILURE",
        amount_at_risk=Decimal("500.00"),
        currency="INR",
        case_age_hours=0.5,
        retry_count=0,
        diagnosis_category="BANK_TECHNICAL_FAILURE",
        diagnosis_confidence=0.90,
        risk_score=25.0,
        recovery_probability=0.85,
        customer_features=CustomerHistoricalFeatures(
            customer_id="cust_a",
            total_attempts=6,
            successful_count=5,
            failed_count=1,
            success_rate=0.83,
        ),
        customer_phone_available=True,
        customer_email_available=True,
    )
    result = engine.recommend(ctx)
    assert result.recommended_action == ActionType.RETRY_PAYMENT
    assert result.score >= 0.80
    assert result.confidence >= 0.80
    assert "transient bank" in result.reason.lower()
    assert result.decision_engine_version == "decision_engine_v1"
    assert len(result.alternatives) > 0


def test_case_b_customer_actionable_failure_recommends_payment_link(engine: RuleBasedRecoveryDecisionEngine):
    """CASE B: Customer actionable failure (e.g. OTP failure) recommends SEND_PAYMENT_LINK."""
    ctx = DecisionContext(
        case_id="case_b",
        case_type="PAYMENT_FAILURE",
        amount_at_risk=Decimal("2000.00"),
        currency="INR",
        case_age_hours=1.0,
        retry_count=0,
        diagnosis_category="AUTHENTICATION_FAILURE",
        diagnosis_confidence=0.88,
        risk_score=35.0,
        recovery_probability=0.75,
        customer_phone_available=True,
        customer_email_available=True,
    )
    result = engine.recommend(ctx)
    assert result.recommended_action == ActionType.SEND_PAYMENT_LINK
    assert result.score >= 0.80
    assert "hosted payment link" in result.reason.lower()
    assert any("has_email=True" in f for f in result.supporting_factors)


def test_case_c_high_value_case_recommends_voice_outreach(engine: RuleBasedRecoveryDecisionEngine):
    """CASE C: High-value revenue case (e.g. ₹250,000) recommends VOICE_OUTREACH."""
    ctx = DecisionContext(
        case_id="case_c",
        case_type="PAYMENT_FAILURE",
        amount_at_risk=Decimal("250000.00"),
        currency="INR",
        case_age_hours=4.0,
        retry_count=1,
        diagnosis_category="BANK_DECLINE",
        diagnosis_confidence=0.75,
        risk_score=75.0,
        recovery_probability=0.60,
        customer_phone_available=True,
        customer_email_available=True,
    )
    result = engine.recommend(ctx)
    assert result.recommended_action == ActionType.VOICE_OUTREACH
    assert "high-value" in result.reason.lower()
    assert any("high_ticket_account=true" in f for f in result.supporting_factors)


def test_case_d_fraud_or_merchant_config_recommends_escalate(engine: RuleBasedRecoveryDecisionEngine):
    """CASE D: Severe risk/fraud or merchant config failure recommends ESCALATE."""
    ctx = DecisionContext(
        case_id="case_d",
        case_type="PAYMENT_FAILURE",
        amount_at_risk=Decimal("15000.00"),
        currency="INR",
        case_age_hours=0.5,
        retry_count=0,
        diagnosis_category="POSSIBLE_FRAUD_OR_SECURITY",
        diagnosis_confidence=0.90,
        risk_score=85.0,
        recovery_probability=0.15,
        customer_phone_available=True,
        customer_email_available=True,
    )
    result = engine.recommend(ctx)
    assert result.recommended_action == ActionType.ESCALATE
    assert "specialist review" in result.reason.lower()


def test_case_e_low_confidence_diagnosis_recommends_wait(engine: RuleBasedRecoveryDecisionEngine):
    """CASE E: Low-confidence diagnosis recommends WAIT for observation."""
    ctx = DecisionContext(
        case_id="case_e",
        case_type="PAYMENT_FAILURE",
        amount_at_risk=Decimal("800.00"),
        currency="INR",
        case_age_hours=0.1,
        retry_count=0,
        diagnosis_category="UNKNOWN",
        diagnosis_confidence=0.20,
        risk_score=40.0,
        recovery_probability=0.50,
    )
    result = engine.recommend(ctx)
    assert result.recommended_action == ActionType.WAIT
    assert "waiting" in result.reason.lower()


def test_deterministic_explanations_and_alternatives_ranking(engine: RuleBasedRecoveryDecisionEngine):
    """Verify that multiple evaluations of identical context yield strictly deterministic outputs."""
    ctx = DecisionContext(
        case_id="case_det",
        case_type="PAYMENT_FAILURE",
        amount_at_risk=Decimal("1200.00"),
        currency="USD",
        case_age_hours=2.0,
        retry_count=0,
        diagnosis_category="PAYMENT_METHOD_FAILURE",
        diagnosis_confidence=0.85,
        risk_score=45.0,
        recovery_probability=0.40,
        customer_email_available=True,
    )
    res1 = engine.recommend(ctx)
    res2 = engine.recommend(ctx)

    assert res1.recommended_action == res2.recommended_action
    assert res1.score == res2.score
    assert res1.reason == res2.reason
    assert res1.supporting_factors == res2.supporting_factors
    assert len(res1.alternatives) == 3
    # Top alternative must have lower score than winner
    assert res1.alternatives[0]["score"] <= res1.score
