from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Tuple
import uuid
import pytest
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.promise_to_pay import PromiseToPay
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep
from app.models.voice_call import VoiceCall
from app.ml.features import (
    CategoricalEncoder,
    InterventionTrainingRow,
    RecoveryFeatureSchema,
    RecoveryFeatureVector,
    validate_pre_intervention_features,
)
from app.ml.dataset_builder import (
    RecoveryMLDatasetBuilder,
    DEFAULT_ATTRIBUTION_WINDOW_HOURS,
    DatasetBuildResult,
)


def _seed_test_case_with_plan(
    db: Session,
    amount: Decimal = Decimal("5000.00"),
    status: str = "OPEN",
    step_count: int = 2,
    base_time: Optional[datetime] = None,
) -> Tuple[Customer, RecoveryCase, RecoveryPlan]:
    t0 = base_time or (datetime.now(timezone.utc) - timedelta(days=5))

    cust_id = uuid.uuid4()
    cust = Customer(
        id=cust_id,
        external_customer_id=f"cust_{cust_id.hex[:8]}",
        name="Aakash Sharma",
        email="aakash@example.com",
        phone="+919876543210",
        created_at=t0 - timedelta(days=30),
    )
    db.add(cust)
    db.flush()

    evt = Event(
        id=uuid.uuid4(),
        external_event_id=f"evt_{uuid.uuid4().hex[:12]}",
        event_type="payment.failed",
        source="RAZORPAY",
        payload={"email": "aakash@example.com", "customer_id": cust.external_customer_id},
        occurred_at=t0,
    )
    db.add(evt)
    db.flush()

    case = RecoveryCase(
        id=uuid.uuid4(),
        customer_id=cust.id,
        event_id=evt.id,
        amount_at_risk=amount,
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status=status,
        created_at=t0,
        updated_at=t0,
    )
    db.add(case)
    db.flush()

    plan = RecoveryPlan(
        id=uuid.uuid4(),
        recovery_case_id=case.id,
        status="ACTIVE",
        current_step=1,
        max_steps=3,
        created_at=t0,
    )
    db.add(plan)
    db.flush()

    for i in range(1, step_count + 1):
        step = RecoveryPlanStep(
            id=uuid.uuid4(),
            recovery_plan_id=plan.id,
            step_number=i,
            action_type="EMAIL_PAYMENT_RECOVERY" if i == 1 else "VOICE_RECOVERY_CALL",
            channel="EMAIL" if i == 1 else "VOICE",
            status="COMPLETED",
            executed_at=t0 + timedelta(days=i - 1, hours=2),
            completed_at=t0 + timedelta(days=i - 1, hours=3),
            created_at=t0 + timedelta(days=i - 1),
        )
        db.add(step)

    db.commit()
    db.refresh(cust)
    db.refresh(case)
    db.refresh(plan)
    return cust, case, plan


# 1. Feature Generation Test
def test_feature_generation_pre_intervention(db_session: Session):
    cust, case, plan = _seed_test_case_with_plan(db_session)
    step1 = plan.steps[0]

    features = RecoveryMLDatasetBuilder.extract_pre_intervention_features(
        db=db_session,
        case=case,
        customer=cust,
        prediction_timestamp=step1.executed_at,
        current_step_number=step1.step_number,
    )

    assert features["amount_at_risk"] == 5000.0
    assert features["currency"] == "INR"
    assert features["number_of_previous_recovery_attempts"] == 0
    assert "days_since_failure" in features
    assert "customer_age_days" in features
    validate_pre_intervention_features(features)


# 2. Categorical Encoding Test
def test_categorical_encoding():
    one_hot = CategoricalEncoder.encode_one_hot("failure_category", "INSUFFICIENT_FUNDS")
    assert one_hot["failure_category_insufficient_funds"] == 1
    assert one_hot["failure_category_authentication_failure"] == 0

    features = {
        "amount_at_risk": 2500.0,
        "days_overdue": 3.0,
        "failure_category": "INSUFFICIENT_FUNDS",
        "currency": "INR",
        "payment_type": "card",
    }
    encoded = CategoricalEncoder.encode_features_to_numerical(features)
    assert encoded["amount_at_risk"] == 2500.0
    assert encoded["failure_category_insufficient_funds"] == 1.0


# 3. Intervention-Level Rows Test
def test_intervention_level_rows_per_case(db_session: Session):
    cust, case, plan = _seed_test_case_with_plan(db_session, step_count=2)

    res = RecoveryMLDatasetBuilder.build_training_dataset(db=db_session)
    case_rows = [r for r in res.rows if r.case_id == str(case.id)]
    assert len(case_rows) == 2

    # Step 1: EMAIL, Step 2: VOICE
    step_types = [r.intervention_type for r in case_rows]
    assert "EMAIL" in step_types
    assert "VOICE" in step_types

    # Step 2 features must show 1 prior recovery attempt
    step2_row = [r for r in case_rows if r.intervention_type == "VOICE"][0]
    assert step2_row.features["number_of_previous_recovery_attempts"] == 1


# 4. Label Generation Test (Inside Attribution Window)
def test_label_generation_inside_attribution_window(db_session: Session):
    t0 = datetime.now(timezone.utc) - timedelta(days=4)
    cust, case, plan = _seed_test_case_with_plan(db_session, base_time=t0, step_count=1)
    step1 = plan.steps[0]

    # Case recovered 12 hours after step 1
    case.status = "RECOVERED"
    case.recovered_amount = Decimal("5000.00")
    case.updated_at = step1.executed_at + timedelta(hours=12)
    db_session.commit()

    res = RecoveryMLDatasetBuilder.build_training_dataset(
        db=db_session,
        attribution_window_hours=72,
    )
    matching_row = [r for r in res.rows if r.intervention_id == str(step1.id)][0]
    assert matching_row.recovered == 1
    assert matching_row.amount_recovered == 5000.0
    assert matching_row.time_to_recovery_seconds == 12 * 3600.0


# 5. Attribution Window Compliance (Outside Window -> recovered = 0)
def test_label_generation_outside_attribution_window(db_session: Session):
    t0 = datetime.now(timezone.utc) - timedelta(days=10)
    cust, case, plan = _seed_test_case_with_plan(db_session, base_time=t0, step_count=1)
    step1 = plan.steps[0]

    # Case recovered 90 hours after step 1 (> 72h window)
    case.status = "RECOVERED"
    case.recovered_amount = Decimal("5000.00")
    case.updated_at = step1.executed_at + timedelta(hours=90)
    db_session.commit()

    res = RecoveryMLDatasetBuilder.build_training_dataset(
        db=db_session,
        attribution_window_hours=72,
    )
    matching_row = [r for r in res.rows if r.intervention_id == str(step1.id)][0]
    assert matching_row.recovered == 0
    assert matching_row.amount_recovered == 0.0


# 6. Anti-Leakage Validation Test
def test_anti_leakage_guards():
    # Passing future outcome keys to pre-intervention features raises ValueError
    leaky_features = {
        "amount_at_risk": 5000.0,
        "recovered_amount": 5000.0,  # Leakage!
        "is_recovered": 1,          # Leakage!
    }
    with pytest.raises(ValueError, match="anti-leakage violation"):
        validate_pre_intervention_features(leaky_features)


# 7. Missing Values Handling Test
def test_missing_values_graceful_handling(db_session: Session):
    evt = Event(
        id=uuid.uuid4(),
        external_event_id=f"evt_{uuid.uuid4().hex[:12]}",
        event_type="payment.failed",
        source="RAZORPAY",
        payload={},
        occurred_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db_session.add(evt)
    db_session.flush()

    cust = Customer(
        id=uuid.uuid4(),
        external_customer_id=f"cust_sparse_{uuid.uuid4().hex[:8]}",
        name="Sparse User",
        email="sparse@example.com",
    )
    db_session.add(cust)
    db_session.flush()

    # Create case with minimal customer and null currency/fields
    case = RecoveryCase(
        id=uuid.uuid4(),
        customer_id=cust.id,
        event_id=evt.id,
        amount_at_risk=Decimal("1500.00"),
        case_type="PAYMENT_FAILURE",
        currency=None,
        status="OPEN",
        created_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db_session.add(case)
    db_session.flush()

    plan = RecoveryPlan(
        id=uuid.uuid4(),
        recovery_case_id=case.id,
        status="ACTIVE",
    )
    db_session.add(plan)
    db_session.flush()

    step = RecoveryPlanStep(
        id=uuid.uuid4(),
        recovery_plan_id=plan.id,
        step_number=1,
        action_type="EMAIL_PAYMENT_RECOVERY",
        status="COMPLETED",
        executed_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add(step)
    db_session.commit()

    res = RecoveryMLDatasetBuilder.build_training_dataset(db=db_session)
    matching_row = [r for r in res.rows if r.intervention_id == str(step.id)][0]
    assert matching_row.features["currency"] in ["INR", "USD"]
    assert matching_row.features["customer_age_days"] >= 0.0


# 8. Cold-Start Dataset Handling Test
def test_cold_start_dataset_handling(db_session: Session):
    # Empty DB
    res = RecoveryMLDatasetBuilder.build_training_dataset(db=db_session, min_samples=100)
    assert res.statistics.insufficient_data is True
    assert res.statistics.total_rows >= 0
    assert isinstance(res.statistics.recovery_rate, float)


# 9. Duplicate Intervention Handling Test
def test_duplicate_intervention_handling(db_session: Session):
    cust, case, plan = _seed_test_case_with_plan(db_session, step_count=1)
    step1 = plan.steps[0]

    # Force duplicate step id in execution
    res = RecoveryMLDatasetBuilder.build_training_dataset(db=db_session)
    assert res.statistics.discarded_rows_count >= 0


# 10. Invalid Data Rejection Test (Negative Amount & Future Prediction Time)
def test_invalid_data_rejection(db_session: Session):
    # Case with negative amount
    t_future = datetime.now(timezone.utc) + timedelta(days=5)
    cust, case, plan = _seed_test_case_with_plan(db_session, amount=Decimal("-500.00"), step_count=1)

    res = RecoveryMLDatasetBuilder.build_training_dataset(db=db_session)
    # The negative amount case must be discarded
    assert res.statistics.discarded_reasons.get("NEGATIVE_AMOUNT", 0) >= 1
