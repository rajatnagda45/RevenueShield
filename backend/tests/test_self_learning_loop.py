"""Comprehensive unit and integration tests for Step 13: Self-Learning Feedback Loop."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.models.customer import Customer
from app.models.event import Event
from app.models.learning import LearningExample
from app.models.model_evaluation import ModelEvaluation
from app.models.model_version import ModelVersion
from app.models.prediction import Prediction
from app.models.recovery_attribution import RecoveryAttribution
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep
from app.ml.action_model_trainer import RecoveryActionModelTrainer
from app.ml.dataset_builder import RecoveryMLDatasetBuilder
from app.ml.model_rollback_service import ModelRollbackService
from app.ml.registry import ModelRegistryService
from app.ml.retraining_service import RetrainingService
from app.ml.self_learning_dataset_builder import SelfLearningDatasetBuilder
from app.services.learning_metrics_service import LearningMetricsService
from app.services.recovery_attribution_engine import RecoveryAttributionEngine
from app.services.recovery_outcome_resolver import RecoveryOutcomeResolver


def _create_test_case_with_plan(db: Session, amount: Decimal = Decimal("10000.00")) -> RecoveryCase:
    """Helper to initialize a RecoveryCase with an active RecoveryPlan and Step."""
    uid = uuid.uuid4().hex[:8]
    cust = Customer(
        external_customer_id=f"cust_learn_{uid}",
        email=f"user_{uid}@example.com",
        name="Self Learning User",
        phone="+919876543999",
        whatsapp_allowed=True,
        transactional_allowed=True,
    )
    db.add(cust)
    db.flush()

    evt = Event(
        external_event_id=f"evt_learn_{uid}",
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

    plan = RecoveryPlan(
        recovery_case_id=case.id,
        status="ACTIVE",
        current_step=1,
        max_steps=3,
    )
    db.add(plan)
    db.flush()

    step = RecoveryPlanStep(
        recovery_plan_id=plan.id,
        step_number=1,
        action_type="EMAIL_PAYMENT_RECOVERY",
        channel="EMAIL",
        status="COMPLETED",
        prediction_score=0.45,
        expected_recovery_value=Decimal("4500.00"),
        executed_at=datetime.now(timezone.utc) - timedelta(hours=2),
        completed_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db.add(step)
    db.commit()
    db.refresh(case)
    return case


def test_payment_captured_creates_learning_example_and_attribution(db_session: Session):
    """Verify payment.captured creates a labeled learning example and primary attribution."""
    case = _create_test_case_with_plan(db_session, amount=Decimal("10000.00"))

    learning_ex = RecoveryOutcomeResolver.resolve_outcome(
        db=db_session,
        case=case,
        outcome_status="RECOVERED",
        amount_recovered=Decimal("10000.00"),
    )

    assert learning_ex.outcome_type == "RECOVERED"
    assert learning_ex.label == 1
    assert learning_ex.amount_recovered == Decimal("10000.00")
    assert learning_ex.training_eligible is True
    assert learning_ex.attribution == "PRIMARY"
    assert learning_ex.prediction_error == pytest.approx(1.0 - 0.45, 0.01)

    # Verify RecoveryAttribution row
    attr = db_session.scalar(
        select(RecoveryAttribution).where(RecoveryAttribution.recovery_case_id == case.id)
    )
    assert attr is not None
    assert attr.attribution_type == "PRIMARY"
    assert attr.attribution_weight == 1.0


def test_failed_recovery_creates_negative_learning_example(db_session: Session):
    """Verify unrecovered/expired cases generate label=0 negative training examples."""
    case = _create_test_case_with_plan(db_session, amount=Decimal("5000.00"))

    learning_ex = RecoveryOutcomeResolver.resolve_outcome(
        db=db_session,
        case=case,
        outcome_status="NOT_RECOVERED",
    )

    assert learning_ex.outcome_type == "NOT_RECOVERED"
    assert learning_ex.label == 0
    assert learning_ex.training_eligible is True
    assert learning_ex.prediction_error == pytest.approx(0.0 - 0.45, 0.01)


def test_attribution_window_boundary_primary_vs_uncertain(db_session: Session):
    """Verify actions outside 24h window are marked UNCERTAIN rather than PRIMARY."""
    case = _create_test_case_with_plan(db_session)
    # Set step execution time to 48 hours ago
    step = case.recovery_plan.steps[0]
    step.executed_at = datetime.now(timezone.utc) - timedelta(hours=48)
    db_session.commit()

    attr = RecoveryAttributionEngine.attribute_recovery(
        db=db_session,
        recovery_case_id=case.id,
        amount_recovered=Decimal("10000.00"),
        attribution_window_hours=24,
    )

    assert attr.attribution_type == "UNCERTAIN"
    assert attr.attribution_weight == 0.5


def test_multiple_interventions_last_touch_primary_attribution(db_session: Session):
    """Verify last eligible outreach step prior to payment receives primary attribution."""
    case = _create_test_case_with_plan(db_session)
    plan = case.recovery_plan

    # Add Step 2 (Follow-up) executed 1 hour ago
    step2 = RecoveryPlanStep(
        recovery_plan_id=plan.id,
        step_number=2,
        action_type="EMAIL_FOLLOWUP",
        channel="EMAIL",
        status="COMPLETED",
        executed_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add(step2)
    db_session.commit()

    attr = RecoveryAttributionEngine.attribute_recovery(
        db=db_session,
        recovery_case_id=case.id,
        amount_recovered=Decimal("10000.00"),
    )

    assert attr.recovery_step_id == step2.id
    assert attr.attribution_type == "PRIMARY"


def test_manual_override_exclusion_from_training(db_session: Session):
    """Verify manual operator overrides are excluded from training eligibility."""
    case = _create_test_case_with_plan(db_session)

    learning_ex = RecoveryOutcomeResolver.resolve_outcome(
        db=db_session,
        case=case,
        outcome_status="RECOVERED",
        amount_recovered=Decimal("10000.00"),
        is_manual_override=True,
    )

    assert learning_ex.training_eligible is False
    assert learning_ex.training_exclusion_reason == "MANUAL_OPERATOR_OVERRIDE"


def test_retraining_threshold_and_dataset_builder(db_session: Session):
    """Verify RetrainingService dataset builder and threshold trigger."""
    # Ensure baseline examples exist
    dataset_df = RecoveryMLDatasetBuilder.generate_synthetic_training_dataset(sample_count=60)
    for _, row in dataset_df.iterrows():
        case = _create_test_case_with_plan(db_session, amount=Decimal(str(row["amount_at_risk"])))
        RecoveryOutcomeResolver.resolve_outcome(
            db=db_session,
            case=case,
            outcome_status="RECOVERED" if row["recovered"] == 1 else "NOT_RECOVERED",
            amount_recovered=Decimal(str(row["amount_at_risk"])) if row["recovered"] == 1 else Decimal("0.00"),
        )

    res = RetrainingService.execute_retraining(db=db_session, force=True)
    assert res["status"] == "COMPLETED"
    assert "metrics" in res
    assert res["sample_count"] >= 50


def test_model_rollback_service(db_session: Session):
    """Verify ModelRollbackService safely restores previous champion model."""
    v1 = ModelVersion(
        id=uuid.uuid4(),
        model_name="action_recovery_model",
        version=f"v1_{uuid.uuid4().hex[:6]}",
        algorithm="LOGISTIC_REGRESSION",
        model_type="LOGISTIC_REGRESSION",
        dataset_type="TEST",
        dataset_version="test_v1",
        feature_schema_version="v1",
        metrics={"roc_auc": 0.75, "log_loss": 0.52, "brier_score": 0.19},
        status="RETIRED",
    )
    v2 = ModelVersion(
        id=uuid.uuid4(),
        model_name="action_recovery_model",
        version=f"v2_{uuid.uuid4().hex[:6]}",
        algorithm="LOGISTIC_REGRESSION",
        model_type="LOGISTIC_REGRESSION",
        dataset_type="TEST",
        dataset_version="test_v2",
        feature_schema_version="v1",
        metrics={"roc_auc": 0.78, "log_loss": 0.49, "brier_score": 0.17},
        status="ACTIVE",
        deployed_at=datetime.now(timezone.utc),
    )
    db_session.add_all([v1, v2])
    db_session.commit()

    rollback_res = ModelRollbackService.rollback_to_previous_model(
        db=db_session,
        target_version=v1.version,
    )

    assert rollback_res["status"] == "ROLLBACK_SUCCESSFUL"
    assert rollback_res["active_version"] == v1.version
    assert v1.status == "ACTIVE"
    assert v2.status == "RETIRED"


def test_learning_metrics_and_imbalance_detector(db_session: Session):
    """Verify business KPIs and action selection imbalance checks."""
    biz = LearningMetricsService.compute_business_metrics(db_session)
    assert "total_amount_at_risk" in biz
    assert "monetary_recovery_rate" in biz
    assert "case_recovery_rate" in biz

    imbalance = LearningMetricsService.detect_action_imbalance(db_session)
    assert "status" in imbalance


def test_ml_learning_and_performance_api_endpoints(client: TestClient, db_session: Session):
    """Verify GET /ml/learning/status and GET /ml/performance REST API endpoints."""
    res_status = client.get("/ml/learning/status")
    assert res_status.status_code == 200
    data_status = res_status.json()
    assert "training_examples" in data_status
    assert "next_training_threshold" in data_status

    res_perf = client.get("/ml/performance")
    assert res_perf.status_code == 200
    data_perf = res_perf.json()
    assert "business_performance" in data_perf
    assert "action_performance" in data_perf
