"""Comprehensive Unit & Integration Test Suite for Next-Best-Action (NBA) Engine and ERV Scoring."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.db.session import get_db
from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep
from app.models.diagnosis import Diagnosis
from app.models.promise_to_pay import PromiseToPay
from app.models.audit_log import AuditLog
from app.decision.base import ActionType
from app.decision.policy import PolicyEngine, PolicyEvaluationResult
from app.ml.expected_recovery import calculate_expected_recovered_value
from app.services.action_candidate_service import ActionCandidateService
from app.services.next_best_action import NextBestActionService
from app.ml.recovery_probability_model import RecoveryProbabilityModelService

client = TestClient(app)


@pytest.fixture
def test_customer(db_session: Session) -> Customer:
    """Fixture creating a test customer."""
    cust = Customer(
        id=uuid.uuid4(),
        name="Arjun Sharma",
        email="arjun.sharma@example.com",
        phone="+919876543210",
        segment="ENTERPRISE",
    )
    db_session.add(cust)
    db_session.commit()
    db_session.refresh(cust)
    return cust


@pytest.fixture
def test_recovery_case(db_session: Session, test_customer: Customer) -> RecoveryCase:
    """Fixture creating an open recovery case with diagnosis and plan."""
    evt = Event(
        id=uuid.uuid4(),
        external_event_id=f"evt_test_{uuid.uuid4().hex[:8]}",
        event_type="payment.failed",
        source="razorpay",
        payload={"amount": 2500000, "currency": "INR"},
        received_at=datetime.now(timezone.utc),
    )
    db_session.add(evt)
    db_session.flush()

    case = RecoveryCase(
        id=uuid.uuid4(),
        customer_id=test_customer.id,
        event_id=evt.id,
        case_type="INVOICE",
        amount_at_risk=Decimal("25000.00"),
        currency="INR",
        status="OPEN",
        retry_count=0,
        created_at=datetime.now(timezone.utc) - timedelta(hours=4),
    )
    db_session.add(case)
    db_session.flush()

    diag = Diagnosis(
        id=uuid.uuid4(),
        recovery_case_id=case.id,
        category="AUTHENTICATION_FAILURE",
        failure_code="BAD_REQUEST_ERROR",
        explanation="Customer OTP expired during checkout",
        confidence=0.92,
        evidence={"attempt": 1},
    )
    db_session.add(diag)

    plan = RecoveryPlan(
        id=uuid.uuid4(),
        recovery_case_id=case.id,
        status="ACTIVE",
        current_step=0,
        max_steps=3,
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(case)
    return case


# 1. Expected Recovered Value (ERV) Unit Tests
def test_calculate_expected_recovered_value_valid():
    res = calculate_expected_recovered_value(0.61, 25000.0)
    assert res["probability"] == 0.61
    assert res["amount_at_risk"] == 25000.0
    assert res["expected_recovered_value"] == 15250.0


def test_calculate_expected_recovered_value_boundary_bounds():
    assert calculate_expected_recovered_value(0.0, 1000.0)["expected_recovered_value"] == 0.0
    assert calculate_expected_recovered_value(1.0, 1000.0)["expected_recovered_value"] == 1000.0


def test_calculate_expected_recovered_value_negative_amount_rejection():
    with pytest.raises(ValueError, match="Invalid amount at risk"):
        calculate_expected_recovered_value(0.5, -500.0)


def test_calculate_expected_recovered_value_probability_bounds_rejection():
    with pytest.raises(ValueError, match="Invalid recovery probability"):
        calculate_expected_recovered_value(1.5, 1000.0)

    with pytest.raises(ValueError, match="Invalid recovery probability"):
        calculate_expected_recovered_value(-0.1, 1000.0)


# 2. Candidate Action Generator Tests
def test_action_candidate_generation_with_full_channels(test_recovery_case: RecoveryCase, db_session: Session):
    candidates = ActionCandidateService.get_candidate_actions(test_recovery_case, db=db_session)
    assert "PAYMENT_RETRY" in candidates
    assert "EMAIL" in candidates
    assert "VOICE" in candidates
    assert "WHATSAPP" in candidates
    assert "NO_ACTION" in candidates


def test_action_candidate_generation_without_phone(test_recovery_case: RecoveryCase, db_session: Session):
    test_recovery_case.customer.phone = None
    db_session.commit()

    candidates = ActionCandidateService.get_candidate_actions(test_recovery_case, db=db_session)
    assert "VOICE" not in candidates
    assert "WHATSAPP" not in candidates
    assert "EMAIL" in candidates
    assert "NO_ACTION" in candidates


def test_action_candidate_generation_already_recovered(test_recovery_case: RecoveryCase, db_session: Session):
    test_recovery_case.status = "RECOVERED"
    db_session.commit()

    candidates = ActionCandidateService.get_candidate_actions(test_recovery_case, db=db_session)
    assert candidates == ["NO_ACTION"]


def test_action_candidate_generation_active_ptp(test_recovery_case: RecoveryCase, db_session: Session):
    ptp = PromiseToPay(
        id=uuid.uuid4(),
        recovery_case_id=test_recovery_case.id,
        customer_id=test_recovery_case.customer_id,
        amount_due=Decimal("25000.00"),
        promised_amount=Decimal("25000.00"),
        promised_date=datetime.now(timezone.utc) + timedelta(days=3),
        status="ACTIVE",
    )
    db_session.add(ptp)
    db_session.commit()

    candidates = ActionCandidateService.get_candidate_actions(test_recovery_case, db=db_session)
    assert candidates == ["NO_ACTION"]


# 3. Next-Best-Action Selection & Highest-Value Action
def test_nba_recommends_highest_expected_value(
    test_recovery_case: RecoveryCase,
    db_session: Session,
    monkeypatch,
):
    # Mock probability model predictions
    mock_probabilities = {
        "EMAIL": 0.32,
        "VOICE": 0.61,
        "PAYMENT_RETRY": 0.47,
        "WHATSAPP": 0.40,
        "NO_ACTION": 0.0,
    }

    def mock_predict(features, intervention_type, model_version=None):
        return {
            "probability": mock_probabilities.get(intervention_type, 0.50),
            "model_version": "test_v1",
            "is_model_prediction": True,
            "status": "PREDICTED",
        }

    monkeypatch.setattr(RecoveryProbabilityModelService, "predict_probability", mock_predict)
    monkeypatch.setattr(RecoveryProbabilityModelService, "load_model", lambda ver=None: "mock_pipeline")
    monkeypatch.setattr(PolicyEngine, "evaluate", lambda *args, **kwargs: PolicyEvaluationResult(allowed=True, reason="Permitted for testing"))

    res = NextBestActionService.recommend_next_best_action(
        case_id=test_recovery_case.id,
        db=db_session,
    )

    assert res["decision_mode"] == "ML_NBA"
    assert res["recommended_action"] == "VOICE"
    assert res["amount_at_risk"] == 25000.0
    assert res["predicted_probability"] == 0.61
    assert res["expected_recovered_value"] == 15250.0


# 4. PolicyEngine Blocks Highest-Value Action -> Fallback to Next Best Permitted Action
def test_nba_falls_back_when_policy_blocks_highest_value(
    test_recovery_case: RecoveryCase,
    db_session: Session,
    monkeypatch,
):
    mock_probabilities = {
        "VOICE": 0.61,       # ERV = 15250 (BLOCKED BY POLICY)
        "PAYMENT_RETRY": 0.47, # ERV = 11750 (ALLOWED)
        "EMAIL": 0.32,       # ERV = 8000 (ALLOWED)
        "WHATSAPP": 0.20,
    }

    def mock_predict(features, intervention_type, model_version=None):
        return {
            "probability": mock_probabilities.get(intervention_type, 0.50),
            "model_version": "test_v1",
            "is_model_prediction": True,
        }

    # Policy blocks VOICE
    def mock_evaluate(action_type, context, case_status, active_interventions_count):
        if action_type == ActionType.VOICE_OUTREACH:
            return PolicyEvaluationResult(allowed=False, reason="Quiet hours active", blocking_rule="DND")
        return PolicyEvaluationResult(allowed=True, reason="Permitted")

    monkeypatch.setattr(RecoveryProbabilityModelService, "predict_probability", mock_predict)
    monkeypatch.setattr(RecoveryProbabilityModelService, "load_model", lambda ver=None: "mock_pipeline")
    monkeypatch.setattr(PolicyEngine, "evaluate", mock_evaluate)

    res = NextBestActionService.recommend_next_best_action(
        case_id=test_recovery_case.id,
        db=db_session,
    )

    assert res["recommended_action"] == "PAYMENT_RETRY"
    assert res["predicted_probability"] == 0.47
    assert res["expected_recovered_value"] == 11750.0

    # Verify ranking structure
    voice_item = next(x for x in res["ranking"] if x["action"] == "VOICE")
    retry_item = next(x for x in res["ranking"] if x["action"] == "PAYMENT_RETRY")
    assert voice_item["policy_allowed"] is False
    assert retry_item["policy_allowed"] is True


# 5. Cold-Start Fallback Behavior
def test_nba_cold_start_fallback_when_model_absent(
    test_recovery_case: RecoveryCase,
    db_session: Session,
    monkeypatch,
):
    monkeypatch.setattr(RecoveryProbabilityModelService, "load_model", lambda ver=None: None)

    res = NextBestActionService.recommend_next_best_action(
        case_id=test_recovery_case.id,
        db=db_session,
    )

    assert res["decision_mode"] == "RULE_BASED_COLD_START"
    assert "ranking" in res
    assert len(res["ranking"]) > 0


# 6. Audit Logging Verification
def test_nba_records_audit_event(
    test_recovery_case: RecoveryCase,
    db_session: Session,
):
    res = NextBestActionService.recommend_next_best_action(
        case_id=test_recovery_case.id,
        db=db_session,
    )

    audit = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.recovery_case_id == test_recovery_case.id,
            AuditLog.action == "NEXT_BEST_ACTION_RECOMMENDED",
        )
        .order_by(AuditLog.timestamp.desc())
        .first()
    )

    assert audit is not None
    assert audit.audit_metadata["recommended_action"] == res["recommended_action"]
    assert audit.audit_metadata["decision_mode"] == res["decision_mode"]


# 7. Non-Execution / Purity Check (No State Modifications on Case/Customer)
def test_nba_does_not_execute_or_mutate_case_state(
    test_recovery_case: RecoveryCase,
    db_session: Session,
):
    initial_status = test_recovery_case.status
    initial_retry_count = test_recovery_case.retry_count

    NextBestActionService.recommend_next_best_action(
        case_id=test_recovery_case.id,
        db=db_session,
    )

    db_session.refresh(test_recovery_case)
    assert test_recovery_case.status == initial_status
    assert test_recovery_case.retry_count == initial_retry_count


# 8. API Endpoint GET /recovery-cases/{case_id}/next-best-action
def test_api_get_next_best_action_endpoint(test_recovery_case: RecoveryCase, db_session: Session):
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        resp = client.get(f"/recovery-cases/{test_recovery_case.id}/next-best-action")
        assert resp.status_code == 200
        data = resp.json()

        assert data["case_id"] == str(test_recovery_case.id)
        assert data["decision_mode"] in ["ML_NBA", "RULE_BASED_COLD_START"]
        assert data["recommended_action"] in ["VOICE", "EMAIL", "PAYMENT_RETRY", "WHATSAPP", "NO_ACTION"]
        assert "ranking" in data
        assert len(data["ranking"]) >= 1
        assert data["amount_at_risk"] == 25000.0
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_api_get_next_best_action_404_not_found(db_session: Session):
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        fake_id = uuid.uuid4()
        resp = client.get(f"/recovery-cases/{fake_id}/next-best-action")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)


# 9. Feature Invariance Across Candidate Actions Test
def test_feature_invariance_across_candidate_actions(
    test_recovery_case: RecoveryCase,
    db_session: Session,
    monkeypatch,
):
    captured_features = []

    def mock_predict(features, intervention_type, model_version=None):
        # Record features passed for each action
        captured_features.append((intervention_type, dict(features)))
        return {
            "probability": 0.50,
            "model_version": "test_v1",
            "is_model_prediction": True,
        }

    monkeypatch.setattr(RecoveryProbabilityModelService, "predict_probability", mock_predict)
    monkeypatch.setattr(RecoveryProbabilityModelService, "load_model", lambda ver=None: "mock_pipeline")

    NextBestActionService.recommend_next_best_action(
        case_id=test_recovery_case.id,
        db=db_session,
    )

    assert len(captured_features) >= 2
    # Verify that all non-intervention_type features are 100% identical
    first_itype, first_feat = captured_features[0]
    for other_itype, other_feat in captured_features[1:]:
        for k, v in first_feat.items():
            if k != "intervention_type":
                assert other_feat[k] == v, f"Feature mismatch for key {k}: {other_feat[k]} vs {v}"


# 10. Model Version Inclusion in Response Test
def test_model_version_inclusion_in_response(
    test_recovery_case: RecoveryCase,
    db_session: Session,
    monkeypatch,
):
    monkeypatch.setattr(
        RecoveryProbabilityModelService,
        "predict_probability",
        lambda features, intervention_type, model_version=None: {
            "probability": 0.55,
            "model_version": "recovery_probability_v1",
        },
    )
    monkeypatch.setattr(RecoveryProbabilityModelService, "load_model", lambda ver=None: "mock_pipeline")

    res = NextBestActionService.recommend_next_best_action(
        case_id=test_recovery_case.id,
        db=db_session,
        model_version="recovery_probability_v1",
    )

    assert "model_version" in res
    assert res["model_version"] == "recovery_probability_v1"
