"""Unit tests for LearningDataService, point-in-time snapshot purity, and data quality validation."""
from datetime import datetime, timezone
from decimal import Decimal
import uuid
import pytest
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.recovery_action import RecoveryAction
from app.models.diagnosis import Diagnosis
from app.models.outcome import RecoveryOutcome
from app.models.learning import LearningExample
from app.learning.service import LearningDataService, DataQualityError


@pytest.fixture
def sample_case_for_learning(db_session: Session):
    customer = Customer(
        name="Learning User",
        email="learn@example.com",
        phone="+919876500055",
    )
    db_session.add(customer)
    db_session.flush()

    event = Event(
        external_event_id=f"evt_learn_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        customer_id=customer.id,
        payload={},
        occurred_at=datetime.now(timezone.utc),
    )
    db_session.add(event)
    db_session.flush()

    case = RecoveryCase(
        customer_id=customer.id,
        event_id=event.id,
        amount_at_risk=Decimal("2500.00"),
        case_type="PAYMENT_FAILURE",
        status="OPEN",
        risk_score=45.0,
        recovery_probability=0.85,
    )
    db_session.add(case)
    db_session.flush()

    diagnosis = Diagnosis(
        recovery_case_id=case.id,
        category="BANK_TECHNICAL_FAILURE",
        confidence=0.9,
        risk_score=45.0,
        recovery_probability=0.85,
        engine_version="diagnosis_engine_v1",
        explanation="Temporary bank failure.",
        evidence={"payment_method": "UPI", "bank": "HDFC"},
    )
    db_session.add(diagnosis)
    db_session.flush()

    action = RecoveryAction(
        recovery_case_id=case.id,
        action_type="RETRY_PAYMENT",
        channel="GATEWAY",
        status="APPROVED",
        decision_score=0.85,
        decision_confidence=0.88,
    )
    db_session.add(action)
    db_session.flush()

    return case, action, diagnosis


def test_feature_snapshot_contains_strictly_pre_decision_features(sample_case_for_learning):
    """Verify that feature snapshot contains all pre-decision features and zero outcome leakage."""
    case, action, diagnosis = sample_case_for_learning

    snapshot = LearningDataService.create_feature_snapshot(
        recovery_case=case,
        action=action,
        diagnosis=diagnosis,
    )

    assert snapshot["amount_at_risk"] == 2500.0
    assert snapshot["diagnosis_category"] == "BANK_TECHNICAL_FAILURE"
    assert snapshot["payment_method"] == "UPI"
    assert snapshot["bank"] == "HDFC"
    assert snapshot["action_type"] == "RETRY_PAYMENT"

    # Anti-leakage checks
    assert "amount_recovered" not in snapshot
    assert "time_to_recovery" not in snapshot
    assert "captured_at" not in snapshot
    assert "outcome_type" not in snapshot
    assert "label" not in snapshot


def test_learning_example_lifecycle_and_binary_label_assignment(db_session: Session, sample_case_for_learning):
    """Verify complete learning example creation, pending state, and finalization with label=1."""
    case, action, diagnosis = sample_case_for_learning

    # 1. Create initial pending learning example
    example = LearningDataService.create_initial_example(
        db=db_session,
        recovery_case=case,
        action=action,
        diagnosis=diagnosis,
    )

    assert example.is_finalized is False
    assert example.label is None
    assert example.outcome_type is None
    assert example.amount_at_risk == Decimal("2500.00")

    # 2. Finalize with attributable recovery -> label = 1
    outcome = RecoveryOutcome(
        recovery_case_id=case.id,
        recovery_action_id=action.id,
        outcome_type="RECOVERED",
        attribution="DIRECT",
        amount_at_risk=Decimal("2500.00"),
        amount_recovered=Decimal("2500.00"),
        recovery_percentage=100.0,
        time_to_recovery_seconds=3600.0,
    )
    db_session.add(outcome)
    db_session.flush()

    finalized = LearningDataService.finalize_learning_example(
        db=db_session,
        recovery_case=case,
        outcome=outcome,
    )

    assert finalized is not None
    assert finalized.is_finalized is True
    assert finalized.label == 1
    assert finalized.outcome_type == "RECOVERED"
    assert finalized.attribution == "DIRECT"
    assert finalized.finalized_at is not None


def test_organic_recovery_assigns_label_zero(db_session: Session, sample_case_for_learning):
    """Verify that organic recovery assigns label=0 because it was not attributable to the intervention."""
    case, action, diagnosis = sample_case_for_learning

    example = LearningDataService.create_initial_example(
        db=db_session,
        recovery_case=case,
        action=action,
        diagnosis=diagnosis,
    )

    outcome = RecoveryOutcome(
        recovery_case_id=case.id,
        recovery_action_id=action.id,
        outcome_type="RECOVERED",
        attribution="ORGANIC",
        amount_at_risk=Decimal("2500.00"),
        amount_recovered=Decimal("2500.00"),
        recovery_percentage=100.0,
    )
    db_session.add(outcome)
    db_session.flush()

    finalized = LearningDataService.finalize_learning_example(
        db=db_session,
        recovery_case=case,
        outcome=outcome,
    )

    assert finalized.label == 0


def test_data_quality_validator_detects_anomalies():
    """Verify that data quality validator catches invalid percentages or negative amounts."""
    bad_example = LearningExample(
        diagnosis_category="INSUFFICIENT_FUNDS",
        diagnosis_confidence=0.9,
        risk_score=50.0,
        recovery_probability=0.8,
        amount_at_risk=Decimal("100.00"),
        action_type="SEND_PAYMENT_LINK",
        decision_score=0.8,
        decision_confidence=0.8,
        amount_recovered=Decimal("200.00"),  # Exceeds amount_at_risk
        recovery_percentage=200.0,            # Invalid percentage
        time_to_recovery_seconds=-50.0,       # Negative time
        feature_snapshot={"amount_recovered": 200.0},  # Leaked feature
    )

    issues = LearningDataService.validate_data_quality(bad_example)
    assert len(issues) >= 3
    assert any("exceeds amount_at_risk" in s for s in issues)
    assert any("Invalid recovery_percentage" in s for s in issues)
    assert any("negative time_to_recovery" in s for s in issues)
    assert any("Leakage" in s for s in issues)
