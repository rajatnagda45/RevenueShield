"""Unit tests for the Rule-Based Diagnosis Engine and scoring modules."""
from decimal import Decimal
import pytest

from app.diagnosis.base import DiagnosisInput
from app.diagnosis.rules import RootCauseCategory, RuleEngine
from app.diagnosis.engine import RuleBasedDiagnosisEngine
from app.diagnosis.risk_scorer import NormalizedRiskScorer
from app.diagnosis.recovery_predictor import HeuristicRecoveryPredictor
from app.services.customer_intelligence import CustomerHistoricalFeatures


@pytest.fixture
def engine():
    return RuleBasedDiagnosisEngine()


def test_insufficient_funds_diagnosis(engine: RuleBasedDiagnosisEngine):
    """Test classification of insufficient balance failures."""
    inp = DiagnosisInput(
        event_type="payment.failed",
        amount=Decimal("500.00"),
        currency="INR",
        payment_method="CARD",
        error_source="customer",
        error_step="payment_authorization",
        error_reason="insufficient_funds",
        failure_code="BAD_REQUEST_ERROR",
        failure_description="Payment failed due to low account balance.",
    )
    res = engine.diagnose(inp)
    assert res.category == RootCauseCategory.INSUFFICIENT_FUNDS
    assert res.confidence >= 0.85
    assert "insufficient" in res.explanation.lower()
    assert res.engine_version == "diagnosis_engine_v1"
    assert res.evidence["matched_rule"] == "rule_insufficient_funds"


def test_bank_technical_failure_diagnosis(engine: RuleBasedDiagnosisEngine):
    """Test classification of bank outage / gateway timeout errors."""
    inp = DiagnosisInput(
        event_type="payment.failed",
        amount=Decimal("1500.00"),
        currency="INR",
        payment_method="NETBANKING",
        error_source="bank",
        error_step="payment_authorization",
        error_reason="gateway_timeout",
        failure_code="GATEWAY_ERROR",
        failure_description="Issuing bank switch downtime.",
    )
    res = engine.diagnose(inp)
    assert res.category == RootCauseCategory.BANK_TECHNICAL_FAILURE
    assert res.confidence >= 0.85
    assert "bank" in res.explanation.lower()


def test_authentication_failure_diagnosis(engine: RuleBasedDiagnosisEngine):
    """Test classification of OTP and 3DS authentication errors."""
    inp = DiagnosisInput(
        event_type="payment.failed",
        amount=Decimal("750.00"),
        currency="INR",
        payment_method="CARD",
        error_source="customer",
        error_step="payment_authentication",
        error_reason="incorrect_otp",
        failure_code="BAD_REQUEST_ERROR",
        failure_description="Invalid OTP entered by customer.",
    )
    res = engine.diagnose(inp)
    assert res.category == RootCauseCategory.AUTHENTICATION_FAILURE
    assert res.confidence >= 0.80
    assert "authentication" in res.explanation.lower()


def test_payment_method_failure_diagnosis(engine: RuleBasedDiagnosisEngine):
    """Test classification of expired card or invalid VPA errors."""
    inp = DiagnosisInput(
        event_type="payment.failed",
        amount=Decimal("1200.00"),
        currency="USD",
        payment_method="CARD",
        error_source="customer",
        error_reason="expired_card",
        failure_code="CARD_EXPIRED",
        failure_description="Card validity expired.",
    )
    res = engine.diagnose(inp)
    assert res.category == RootCauseCategory.PAYMENT_METHOD_FAILURE
    assert "expired" in res.explanation.lower()


def test_fraud_or_security_diagnosis(engine: RuleBasedDiagnosisEngine):
    """Test classification of risk / security block failures."""
    inp = DiagnosisInput(
        event_type="payment.failed",
        amount=Decimal("50000.00"),
        currency="INR",
        error_source="bank",
        error_reason="stolen_card",
        failure_description="Transaction restricted due to security risk.",
    )
    res = engine.diagnose(inp)
    assert res.category == RootCauseCategory.POSSIBLE_FRAUD_OR_SECURITY
    assert res.risk_score >= 50.0  # high severity


def test_user_friction_diagnosis(engine: RuleBasedDiagnosisEngine):
    """Test classification of user drop-off or cancellation."""
    inp = DiagnosisInput(
        event_type="payment.failed",
        amount=Decimal("300.00"),
        currency="INR",
        error_reason="user_cancelled",
        failure_description="Customer cancelled checkout session.",
    )
    res = engine.diagnose(inp)
    assert res.category == RootCauseCategory.USER_FRICTION


def test_merchant_configuration_diagnosis(engine: RuleBasedDiagnosisEngine):
    """Test classification of merchant/gateway configuration issues."""
    inp = DiagnosisInput(
        event_type="payment.failed",
        amount=Decimal("800.00"),
        currency="EUR",
        error_source="business",
        failure_code="currency_not_supported",
        failure_description="Merchant account cannot process EUR currency.",
    )
    res = engine.diagnose(inp)
    assert res.category == RootCauseCategory.MERCHANT_CONFIGURATION


def test_generic_bank_decline_diagnosis(engine: RuleBasedDiagnosisEngine):
    """Test classification of generic bank decline."""
    inp = DiagnosisInput(
        event_type="payment.failed",
        amount=Decimal("1000.00"),
        currency="INR",
        error_source="bank",
        failure_code="do_not_honor",
        failure_description="Declined by issuing bank.",
    )
    res = engine.diagnose(inp)
    assert res.category == RootCauseCategory.BANK_DECLINE


def test_unknown_failure_and_missing_data_graceful_handling(engine: RuleBasedDiagnosisEngine):
    """Test that empty or missing failure details gracefully map to UNKNOWN with lower confidence."""
    inp = DiagnosisInput(
        event_type="payment.failed",
        amount=Decimal("200.00"),
        currency="INR",
        error_source=None,
        error_step=None,
        error_reason=None,
        failure_code=None,
        failure_description=None,
    )
    res = engine.diagnose(inp)
    assert res.category == RootCauseCategory.UNKNOWN
    assert res.confidence == 0.20  # low confidence due to missing signals
    assert "No diagnostic error codes" in res.explanation


def test_risk_score_calculation():
    """Verify that NormalizedRiskScorer produces values in 0-100 and scales with amount and severity."""
    scorer = NormalizedRiskScorer()

    features_good = CustomerHistoricalFeatures(
        customer_id="cust_1",
        total_attempts=10,
        successful_count=9,
        success_rate=0.90,
        consecutive_failures=0,
    )
    features_risky = CustomerHistoricalFeatures(
        customer_id="cust_2",
        total_attempts=5,
        successful_count=1,
        success_rate=0.20,
        consecutive_failures=4,
    )

    score_low = scorer.calculate_risk_score(
        amount=Decimal("100.00"),
        category=RootCauseCategory.BANK_TECHNICAL_FAILURE,
        confidence=0.90,
        customer_features=features_good,
    )

    score_high = scorer.calculate_risk_score(
        amount=Decimal("50000.00"),
        category=RootCauseCategory.POSSIBLE_FRAUD_OR_SECURITY,
        confidence=0.88,
        customer_features=features_risky,
    )

    assert 0.0 <= score_low <= 100.0
    assert 0.0 <= score_high <= 100.0
    assert score_high > score_low


def test_recovery_probability_calculation():
    """Verify that HeuristicRecoveryPredictor generates higher probability for transient errors and loyal customers."""
    predictor = HeuristicRecoveryPredictor()

    features_loyal = CustomerHistoricalFeatures(
        customer_id="cust_1",
        total_attempts=20,
        successful_count=19,
        success_rate=0.95,
        recent_success_count=3,
        previous_recovered_amount=1000.0,
    )
    features_poor = CustomerHistoricalFeatures(
        customer_id="cust_2",
        total_attempts=4,
        successful_count=1,
        success_rate=0.25,
        consecutive_failures=3,
    )

    prob_technical_loyal = predictor.predict_probability(
        category=RootCauseCategory.BANK_TECHNICAL_FAILURE,
        amount=Decimal("500.00"),
        customer_features=features_loyal,
        evidence={},
    )

    prob_fraud_poor = predictor.predict_probability(
        category=RootCauseCategory.POSSIBLE_FRAUD_OR_SECURITY,
        amount=Decimal("500.00"),
        customer_features=features_poor,
        evidence={},
    )

    assert 0.05 <= prob_technical_loyal <= 0.98
    assert 0.05 <= prob_fraud_poor <= 0.98
    assert prob_technical_loyal > prob_fraud_poor
    assert prob_technical_loyal >= 0.85
