"""Tests for PredictionService, Expected Value Calculation, and Fallback."""
from datetime import datetime, timezone
from decimal import Decimal
import uuid
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.diagnosis import Diagnosis
from app.ml.pipeline import TrainingPipeline
from app.ml.registry import ModelRegistryService
from app.ml.prediction_service import PredictionService, CasePredictionResult


def _setup_test_case(db_session: Session) -> RecoveryCase:
    cust = Customer(
        external_customer_id=f"cust_pred_{uuid.uuid4()}",
        email="test_pred@example.com",
        name="Pred User",
        phone="+919876543210",
    )
    db_session.add(cust)
    db_session.flush()

    evt = Event(
        external_event_id=f"evt_pred_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        customer_id=cust.id,
        processing_status="PROCESSED",
    )
    db_session.add(evt)
    db_session.flush()

    case = RecoveryCase(
        customer_id=cust.id,
        event_id=evt.id,
        amount_at_risk=Decimal("5000.00"),
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status="OPEN",
        risk_score=40.0,
        recovery_probability=0.60,
    )
    db_session.add(case)
    db_session.flush()

    diag = Diagnosis(
        recovery_case_id=case.id,
        category="AUTHENTICATION_FAILED",
        root_cause="Customer OTP entry timed out",
        confidence=0.88,
        engine_version="diagnosis_engine_v1",
        recommended_action="SEND_PAYMENT_LINK",
        recommended_channel="SMS",
        indicators={},
    )
    db_session.add(diag)
    db_session.commit()
    db_session.refresh(case)
    return case


def test_heuristic_fallback_when_no_active_model(db_session: Session):
    """Verify that PredictionService provides heuristic predictions and Expected Values if no ML model is active."""
    case = _setup_test_case(db_session)

    res: CasePredictionResult = PredictionService.predict_for_case(db=db_session, recovery_case=case)
    assert res.strategy == "HEURISTIC"
    assert res.model_status in ("INSUFFICIENT_DATA", "FALLBACK")
    assert len(res.predictions) > 0

    for pred in res.predictions:
        assert 0.0 <= pred.probability <= 1.0
        # Expected value formula: P * amount_at_risk
        expected_ev = round(pred.probability * 5000.0, 2)
        assert abs(pred.expected_recovered_value - expected_ev) < 0.01
        assert len(pred.contributing_factors) > 0

    # Ensure ranking is sorted descending by expected value
    for i in range(len(res.predictions) - 1):
        assert res.predictions[i].expected_recovered_value >= res.predictions[i + 1].expected_recovered_value


def test_ml_predictions_with_active_model(db_session: Session):
    """Verify that PredictionService switches to ML strategy when a model is active."""
    # 1. Train and activate model
    df = TrainingPipeline.load_dataset(dataset_type="SYNTHETIC_DEMO")
    package, _ = TrainingPipeline.train_and_evaluate(
        df=df,
        dataset_type="SYNTHETIC_DEMO",
        model_name="recovery_value_predictor",
        version="v1.0.0-ml-test",
    )
    artifact_path = TrainingPipeline.save_artifact(package)
    now = datetime.now(timezone.utc)
    model_rec = ModelRegistryService.register_model(
        db=db_session,
        package=package,
        artifact_path=artifact_path,
        training_started_at=now,
        training_completed_at=now,
    )
    ModelRegistryService.activate_model(db_session, model_rec.id)

    # 2. Predict for case
    case = _setup_test_case(db_session)
    res: CasePredictionResult = PredictionService.predict_for_case(db=db_session, recovery_case=case)

    assert res.strategy == "ML"
    assert res.model_status == "ACTIVE"
    assert res.model_version == "v1.0.0-ml-test"
    assert len(res.predictions) > 0

    top_pred = res.predictions[0]
    assert 0.0 <= top_pred.probability <= 1.0
    assert top_pred.expected_recovered_value >= 0.0
    assert any("diagnosis pattern" in f for f in top_pred.contributing_factors)
