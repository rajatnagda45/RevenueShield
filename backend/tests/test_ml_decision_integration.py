"""Tests for Decision & Policy Integration with Predictive Recovery."""
from datetime import datetime, timezone
from decimal import Decimal
import uuid
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.diagnosis import Diagnosis
from app.models.recovery_action import RecoveryAction
from app.decision.service import DecisionService
from app.ml.pipeline import TrainingPipeline
from app.ml.registry import ModelRegistryService


def test_decision_engine_incorporates_ml_and_policy_guards(db_session: Session):
    """Verify that ML predictions flow into DecisionService, but PolicyEngine retains authority."""
    # 1. Train and activate model
    df = TrainingPipeline.load_dataset(dataset_type="SYNTHETIC_DEMO")
    package, _ = TrainingPipeline.train_and_evaluate(
        df=df,
        dataset_type="SYNTHETIC_DEMO",
        model_name="recovery_value_predictor",
        version="v1.0.0-integration",
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

    # 2. Setup recovery case
    cust = Customer(
        external_customer_id=f"cust_integ_{uuid.uuid4()}",
        email="integ@example.com",
        name="Integration User",
        phone="+919876543211",
    )
    db_session.add(cust)
    db_session.flush()

    evt = Event(
        external_event_id=f"evt_integ_{uuid.uuid4()}",
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
        amount_at_risk=Decimal("7500.00"),
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status="OPEN",
        risk_score=35.0,
        recovery_probability=0.70,
        retry_count=0,
    )
    db_session.add(case)
    db_session.flush()

    diag = Diagnosis(
        recovery_case_id=case.id,
        category="AUTHENTICATION_FAILED",
        root_cause="Customer OTP entry failed",
        confidence=0.90,
        engine_version="diagnosis_engine_v1",
        recommended_action="SEND_PAYMENT_LINK",
        recommended_channel="SMS",
        indicators={},
    )
    db_session.add(diag)
    db_session.commit()

    # 3. Generate recommendation
    action: RecoveryAction = DecisionService.generate_recommendation(
        db=db_session,
        recovery_case=case,
        diagnosis=diag,
    )

    assert action is not None
    assert action.status == "APPROVED"
    # Verify ML supporting factors are present
    factors = action.supporting_factors or []
    assert any("Predictive ML Model" in str(f) for f in factors)
