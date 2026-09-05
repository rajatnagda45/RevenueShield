"""Comprehensive unit and integration tests for Step 12: Data-Driven Next-Best-Action Model."""
from datetime import datetime, timezone
from decimal import Decimal
import uuid
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.models.customer import Customer
from app.models.event import Event
from app.models.learning import LearningExample
from app.models.model_version import ModelVersion
from app.models.prediction import Prediction
from app.models.recovery_case import RecoveryCase
from app.ml.action_model_trainer import RecoveryActionModelTrainer
from app.ml.action_prediction_service import RecoveryActionPredictionService
from app.ml.dataset_builder import RecoveryMLDatasetBuilder
from app.ml.drift_detector import ModelDriftDetector
from app.ml.features import (
    ActionPredictionFeatures,
    FeatureSchemaV1,
    build_feature_dict_for_inference,
    validate_point_in_time_features,
)
from app.ml.registry import ModelRegistryService
from app.services.action_policy import ActionPolicyContext, MLActionPolicy, NextBestAction, RuleBasedActionPolicy
from app.services.next_best_action_engine import NextBestActionEngine


def _create_test_case_helper(db: Session, amount: Decimal = Decimal("12000.00")) -> RecoveryCase:
    """Helper to create a fresh RecoveryCase with customer and event."""
    unique_id = uuid.uuid4().hex[:8]
    cust = Customer(
        external_customer_id=f"cust_ml_{unique_id}",
        email=f"user_{unique_id}@example.com",
        name="ML Test User",
        phone="+919876543888",
        whatsapp_allowed=True,
        transactional_allowed=True,
        marketing_opt_out=False,
    )
    db.add(cust)
    db.flush()

    evt = Event(
        external_event_id=f"evt_ml_{unique_id}",
        event_type="payment.failed",
        source="RAZORPAY",
        customer_id=cust.id,
        processing_status="PROCESSED",
    )
    db.add(evt)
    db.flush()

    case = RecoveryCase(
        customer_id=cust.id,
        event_id=evt.id,
        amount_at_risk=amount,
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status="OPEN",
    )
    db.add(case)
    db.flush()
    return case


def test_dataset_builder_generates_valid_features(db_session: Session):
    """Verify RecoveryMLDatasetBuilder extracts clean, point-in-time features with zero leakage."""
    df = RecoveryMLDatasetBuilder.generate_synthetic_training_dataset(sample_count=60)
    assert len(df) == 60
    assert "amount_at_risk" in df.columns
    assert "action_type" in df.columns
    assert "recovered" in df.columns

    # Verify each row passes anti-leakage validation
    for _, row in df.iterrows():
        validate_point_in_time_features(row.to_dict())


def test_anti_leakage_validator_rejects_forbidden_keys():
    """Verify that forbidden outcome keys raise ValueError."""
    bad_features = {
        "amount_at_risk": 5000.0,
        "action_type": "EMAIL_PAYMENT_RECOVERY",
        "amount_recovered": 5000.0,  # LEAKAGE!
    }
    with pytest.raises(ValueError, match="Point-in-time anti-leakage violation"):
        validate_point_in_time_features(bad_features)


def test_trainer_insufficient_samples_fallback():
    """Verify trainer returns INSUFFICIENT_DATA status when sample count is below minimum."""
    tiny_df = pd.DataFrame([
        {"amount_at_risk": 1000.0, "action_type": "EMAIL_PAYMENT_RECOVERY", "recovered": 1}
    ])
    res = RecoveryActionModelTrainer.train_and_register(custom_df=tiny_df, min_samples=50)
    assert res["status"] == "INSUFFICIENT_DATA"
    assert res["sample_count"] == 1


def test_trainer_trains_and_evaluates_calibrated_model(db_session: Session):
    """Verify training pipeline fits a calibrated classifier, computes metrics, and persists artifact."""
    synthetic_df = RecoveryMLDatasetBuilder.generate_synthetic_training_dataset(sample_count=80)
    version = f"test_v_{uuid.uuid4().hex[:6]}"

    res = RecoveryActionModelTrainer.train_and_register(
        db=db_session,
        custom_df=synthetic_df,
        model_version=version,
        min_samples=50,
    )

    assert res["status"] == "TRAINED"
    assert res["model_version"] == version
    assert "roc_auc" in res["metrics"]
    assert "log_loss" in res["metrics"]
    assert "brier_score" in res["metrics"]
    assert res["metrics"]["brier_score"] <= 0.35

    # Verify model record in DB
    model_record = db_session.scalar(select(ModelVersion).where(ModelVersion.version == version))
    assert model_record is not None
    assert model_record.status == "VALIDATED"


def test_model_registry_promotion_quality_gate(db_session: Session):
    """Verify ModelRegistryService promotes candidate model only when quality gate is satisfied."""
    synthetic_df = RecoveryMLDatasetBuilder.generate_synthetic_training_dataset(sample_count=80)
    version = f"promo_v_{uuid.uuid4().hex[:6]}"

    res = RecoveryActionModelTrainer.train_and_register(
        db=db_session,
        custom_df=synthetic_df,
        model_version=version,
        min_samples=50,
    )

    candidate = db_session.scalar(select(ModelVersion).where(ModelVersion.version == version))
    assert candidate is not None

    prom_res = ModelRegistryService.evaluate_and_promote(db_session, candidate.id, brier_threshold=0.35)
    assert prom_res["promoted"] is True
    assert prom_res["status"] == "ACTIVE"
    assert candidate.status == "ACTIVE"


def test_model_registry_rejects_inferior_model(db_session: Session):
    """Verify promotion gate rejects candidate model if Brier score exceeds threshold."""
    bad_model = ModelVersion(
        id=uuid.uuid4(),
        model_name="action_recovery_model",
        version=f"bad_v_{uuid.uuid4().hex[:6]}",
        algorithm="LOGISTIC_REGRESSION",
        model_type="LOGISTIC_REGRESSION",
        dataset_type="TEST",
        dataset_version="test_v1",
        feature_schema_version=FeatureSchemaV1.SCHEMA_VERSION,
        metrics={"roc_auc": 0.50, "log_loss": 1.20, "brier_score": 0.45},
        status="VALIDATED",
        artifact_path="/tmp/nonexistent.joblib",
    )
    db_session.add(bad_model)
    db_session.commit()

    prom_res = ModelRegistryService.evaluate_and_promote(db_session, bad_model.id, brier_threshold=0.25)
    assert prom_res["promoted"] is False
    assert "exceeds quality threshold" in prom_res["reason"]


def test_action_prediction_service_with_active_model(db_session: Session):
    """Verify live inference and Expected Recovery Value calculation using active ML model."""
    case = _create_test_case_helper(db_session, amount=Decimal("12000.00"))

    # Train and activate model
    synthetic_df = RecoveryMLDatasetBuilder.generate_synthetic_training_dataset(sample_count=80)
    version = f"act_v_{uuid.uuid4().hex[:6]}"
    RecoveryActionModelTrainer.train_and_register(db=db_session, custom_df=synthetic_df, model_version=version)
    cand = db_session.scalar(select(ModelVersion).where(ModelVersion.version == version))
    ModelRegistryService.evaluate_and_promote(db_session, cand.id, brier_threshold=0.35)

    # Score candidate action
    pred = RecoveryActionPredictionService.predict_action(
        db=db_session,
        case=case,
        action_type="EMAIL_FOLLOWUP",
        persist=True,
    )

    assert pred.action == "EMAIL_FOLLOWUP"
    assert 0.05 <= pred.probability <= 0.95
    assert pred.expected_recovered_value == round(pred.probability * 12000.0, 2)
    assert len(pred.contributing_factors) >= 1
    assert pred.model_status == "ACTIVE"

    # Verify prediction record persisted in database
    db_pred = db_session.scalar(select(Prediction).where(Prediction.id == uuid.UUID(pred.prediction_id)))
    assert db_pred is not None
    assert float(db_pred.predicted_probability) == pred.probability


def test_next_best_action_maximizes_expected_recovery_value(db_session: Session):
    """Verify NextBestActionEngine selects the candidate action with the highest Expected Recovery Value."""
    case = _create_test_case_helper(db_session, amount=Decimal("15000.00"))

    # Train and activate model
    synthetic_df = RecoveryMLDatasetBuilder.generate_synthetic_training_dataset(sample_count=80)
    version = f"ev_v_{uuid.uuid4().hex[:6]}"
    RecoveryActionModelTrainer.train_and_register(db=db_session, custom_df=synthetic_df, model_version=version)
    cand = db_session.scalar(select(ModelVersion).where(ModelVersion.version == version))
    ModelRegistryService.evaluate_and_promote(db_session, cand.id, brier_threshold=0.35)

    nba = NextBestActionEngine.compute_next_best_action(db=db_session, case=case)
    assert nba.action_type in ["EMAIL_PAYMENT_RECOVERY", "EMAIL_FOLLOWUP", "WHATSAPP_PAYMENT_RECOVERY", "SEND_PAYMENT_LINK"]
    assert nba.expected_recovery_value > Decimal("0.00")
    assert "Data-driven ML selection" in nba.reason


def test_ml_action_policy_fallback_to_rule_based():
    """Verify MLActionPolicy cleanly falls back to RuleBasedActionPolicy if predictions are empty."""
    ml_policy = MLActionPolicy(fallback_policy=RuleBasedActionPolicy())
    context = ActionPolicyContext(
        recovery_case_id="case-xyz",
        amount_at_risk=Decimal("10000.00"),
        attempt_number=1,
        ml_base_probability=0.45,
    )
    nba = ml_policy.select_action(context, candidate_predictions=None)
    assert nba.action_type == "EMAIL_PAYMENT_RECOVERY"
    assert nba.expected_recovery_value == Decimal("4500.00")


def test_model_drift_detector_assessment(db_session: Session):
    """Verify Population Stability Index (PSI) calculation and drift assessment."""
    # Test identical distributions -> PSI close to 0
    rng = np.random.RandomState(42)
    ref = rng.normal(0.5, 0.1, 500)
    curr = rng.normal(0.5, 0.1, 500)
    psi_stable = ModelDriftDetector.calculate_psi(ref, curr)
    assert psi_stable < 0.10

    # Test shifted distribution -> Higher PSI
    shifted = rng.normal(0.8, 0.1, 500)
    psi_drifted = ModelDriftDetector.calculate_psi(ref, shifted)
    assert psi_drifted > 0.10


def test_ml_model_status_api_endpoint(client: TestClient, db_session: Session):
    """Verify GET /ml/model/status API returns active model metrics and drift indicators."""
    res = client.get("/ml/model/status")
    assert res.status_code == 200
    data = res.json()
    assert "status" in data
    assert "drift_status" in data


def test_get_case_next_best_action_api_endpoint(client: TestClient, db_session: Session):
    """Verify GET /recovery-cases/{case_id}/next-best-action returns candidate actions breakdown."""
    case = _create_test_case_helper(db_session)
    res = client.get(f"/recovery-cases/{case.id}/next-best-action")
    assert res.status_code == 200
    data = res.json()
    assert data["case_id"] == str(case.id)
    assert len(data["candidate_actions"]) >= 2
    assert "selected_action" in data
    assert data["expected_recovery_value"] > 0
