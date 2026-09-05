"""Comprehensive unit and integration tests for Step 14: Promise-to-Pay + Intelligent Escalation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.models.customer import Customer
from app.models.event import Event
from app.models.promise_to_pay import PromiseToPay
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep
from app.decision.base import DecisionContext
from app.decision.policy import PolicyEngine
from app.outcomes.engine import OutcomeEngine
from app.services.escalation_policy import EscalationLevel, EscalationPolicy
from app.services.promise_eligibility_engine import PromiseEligibilityEngine
from app.services.promise_evaluation_service import PromiseEvaluationService
from app.services.promise_to_pay_service import PromiseToPayService


def _create_case_with_active_plan(db: Session, amount: Decimal = Decimal("25000.00")) -> RecoveryCase:
    """Helper to initialize a RecoveryCase with an active RecoveryPlan."""
    uid = uuid.uuid4().hex[:8]
    cust = Customer(
        external_customer_id=f"cust_ptp_{uid}",
        email=f"user_{uid}@enterprise.in",
        name="Promise Test User",
        phone="+919876543111",
        whatsapp_allowed=True,
        transactional_allowed=True,
    )
    db.add(cust)
    db.flush()

    evt = Event(
        external_event_id=f"evt_ptp_{uid}",
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
        expected_recovery_value=Decimal("11250.00"),
    )
    db.add(step)
    db.commit()
    db.refresh(case)
    return case


def test_valid_promise_creation_pauses_recovery_plan(db_session: Session):
    """Verify valid promise creation successfully creates record and pauses RecoveryPlan."""
    case = _create_case_with_active_plan(db_session, amount=Decimal("25000.00"))
    future_date = datetime.now(timezone.utc) + timedelta(days=3)

    promise = PromiseToPayService.create_promise(
        db=db_session,
        recovery_case_id=case.id,
        promised_amount=Decimal("25000.00"),
        promised_date=future_date,
        source="CUSTOMER",
        notes="Customer requested time until Friday.",
    )

    assert promise.status == "ACTIVE"
    assert promise.promised_amount == Decimal("25000.00")
    assert promise.amount_due == Decimal("25000.00")

    # Verify plan status paused
    db_session.refresh(case.recovery_plan)
    assert case.recovery_plan.status == "PAUSED"
    assert PromiseToPayService.has_active_promise(db_session, case.id) is True


def test_promise_validation_rejects_past_date(db_session: Session):
    """Verify validation rejects promise with past date."""
    case = _create_case_with_active_plan(db_session)
    past_date = datetime.now(timezone.utc) - timedelta(days=1)

    with pytest.raises(ValueError, match="strictly in the future"):
        PromiseToPayService.create_promise(
            db=db_session,
            recovery_case_id=case.id,
            promised_amount=Decimal("10000.00"),
            promised_date=past_date,
        )


def test_promise_validation_rejects_excessive_amount(db_session: Session):
    """Verify validation rejects promise exceeding amount due."""
    case = _create_case_with_active_plan(db_session, amount=Decimal("10000.00"))
    future_date = datetime.now(timezone.utc) + timedelta(days=2)

    with pytest.raises(ValueError, match="cannot exceed total amount due"):
        PromiseToPayService.create_promise(
            db=db_session,
            recovery_case_id=case.id,
            promised_amount=Decimal("15000.00"),
            promised_date=future_date,
        )


def test_active_promise_blocks_policy_engine_outreach(db_session: Session):
    """Verify PolicyEngine blocks outreach when promise is active."""
    case = _create_case_with_active_plan(db_session)
    future_date = datetime.now(timezone.utc) + timedelta(days=2)
    PromiseToPayService.create_promise(
        db=db_session,
        recovery_case_id=case.id,
        promised_amount=Decimal("25000.00"),
        promised_date=future_date,
    )

    # Evaluate Policy with promise_to_pay_active = True
    context = DecisionContext(
        case_id=str(case.id),
        case_type="PAYMENT_FAILURE",
        amount_at_risk=Decimal("25000.00"),
        currency="INR",
        case_age_hours=1.0,
        retry_count=1,
        diagnosis_category="BANK_DOWNTIME",
        diagnosis_confidence=0.85,
        risk_score=50.0,
        recovery_probability=0.45,
        previous_action_types=["EMAIL_PAYMENT_RECOVERY"],
        promise_to_pay_active=True,
    )

    res = PolicyEngine.evaluate(
        action_type="SEND_PAYMENT_LINK",
        context=context,
        case_status="OPEN",
    )
    assert res.allowed is False
    assert res.blocking_rule == "PROMISE_TO_PAY_ACTIVE"


def test_payment_captured_fulfills_promise_and_recovers_case(db_session: Session):
    """Verify payment.captured webhook automatically fulfills active promise and marks case RECOVERED."""
    case = _create_case_with_active_plan(db_session, amount=Decimal("20000.00"))
    future_date = datetime.now(timezone.utc) + timedelta(days=2)
    promise = PromiseToPayService.create_promise(
        db=db_session,
        recovery_case_id=case.id,
        promised_amount=Decimal("20000.00"),
        promised_date=future_date,
    )

    # Process payment capture
    OutcomeEngine.process_payment_capture(
        db=db_session,
        recovery_case=case,
        captured_amount=Decimal("20000.00"),
    )

    db_session.refresh(promise)
    assert promise.status == "FULFILLED"
    assert promise.fulfilled_at is not None
    assert case.status == "RECOVERED"


def test_promise_evaluation_missed_resumes_plan_and_triggers_nba(db_session: Session):
    """Verify evaluation of missed promise transitions status to MISSED and resumes plan."""
    case = _create_case_with_active_plan(db_session, amount=Decimal("25000.00"))
    due_date = datetime.now(timezone.utc) + timedelta(hours=1)
    promise = PromiseToPayService.create_promise(
        db=db_session,
        recovery_case_id=case.id,
        promised_amount=Decimal("25000.00"),
        promised_date=due_date,
    )

    # Evaluate at a time after due_date (simulate missed deadline)
    eval_time = due_date + timedelta(hours=2)
    eval_res = PromiseEvaluationService.evaluate_promise(
        db=db_session,
        promise_id=promise.id,
        reference_time=eval_time,
    )

    assert eval_res["status"] == "MISSED"
    db_session.refresh(promise)
    assert promise.status == "MISSED"

    # Verify plan was resumed
    db_session.refresh(case.recovery_plan)
    assert case.recovery_plan.status in ["ACTIVE", "WAITING", "COMPLETED"]


def test_promise_evaluation_partial_payment(db_session: Session):
    """Verify evaluation handles partial payments."""
    case = _create_case_with_active_plan(db_session, amount=Decimal("30000.00"))
    due_date = datetime.now(timezone.utc) + timedelta(hours=1)
    promise = PromiseToPayService.create_promise(
        db=db_session,
        recovery_case_id=case.id,
        promised_amount=Decimal("30000.00"),
        promised_date=due_date,
    )

    # Set partial recovery
    case.recovered_amount = Decimal("15000.00")
    db_session.commit()

    eval_time = due_date + timedelta(hours=2)
    eval_res = PromiseEvaluationService.evaluate_promise(
        db=db_session,
        promise_id=promise.id,
        reference_time=eval_time,
    )

    assert eval_res["status"] == "PARTIAL"
    assert eval_res["remaining_promised_amount"] == 15000.0


def test_promise_cancel_resumes_plan(db_session: Session):
    """Verify cancelling promise transitions status to CANCELLED and resumes recovery plan."""
    case = _create_case_with_active_plan(db_session)
    future_date = datetime.now(timezone.utc) + timedelta(days=2)
    promise = PromiseToPayService.create_promise(
        db=db_session,
        recovery_case_id=case.id,
        promised_amount=Decimal("25000.00"),
        promised_date=future_date,
    )

    cancelled = PromiseToPayService.cancel_promise(db_session, promise.id)
    assert cancelled.status == "CANCELLED"
    db_session.refresh(case.recovery_plan)
    assert case.recovery_plan.status in ["ACTIVE", "WAITING", "COMPLETED"]


def test_promise_eligibility_engine(db_session: Session):
    """Verify PromiseEligibilityEngine scores high-value cases accurately."""
    case = _create_case_with_active_plan(db_session, amount=Decimal("25000.00"))
    case.created_at = datetime.now(timezone.utc) - timedelta(hours=36)
    db_session.commit()

    eligibility = PromiseEligibilityEngine.evaluate_eligibility(case)
    assert eligibility["eligible"] is True
    assert eligibility["score"] >= 0.65


def test_escalation_policy_levels(db_session: Session):
    """Verify EscalationPolicy maps tiers correctly."""
    case_high = _create_case_with_active_plan(db_session, amount=Decimal("25000.00"))
    esc_high = EscalationPolicy.evaluate_level(case_high, hours_overdue=36.0, previous_attempts=2)
    assert esc_high["level"] == EscalationLevel.LEVEL_3.value

    case_low = _create_case_with_active_plan(db_session, amount=Decimal("500.00"))
    esc_low = EscalationPolicy.evaluate_level(case_low, hours_overdue=2.0, previous_attempts=0)
    assert esc_low["level"] == EscalationLevel.LEVEL_0.value


def test_promise_to_pay_api_endpoints(client: TestClient, db_session: Session):
    """Verify Promise-to-Pay REST API endpoints."""
    case = _create_case_with_active_plan(db_session, amount=Decimal("20000.00"))
    future_date = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()

    # 1. Create Promise
    res = client.post(
        f"/recovery-cases/{case.id}/promise-to-pay",
        json={
            "promised_amount": 20000.0,
            "promised_date": future_date,
            "promised_time": "17:00",
            "source": "CUSTOMER",
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "ACTIVE"
    promise_id = data["id"]

    # 2. Get Case Promise
    res_get = client.get(f"/recovery-cases/{case.id}/promise-to-pay")
    assert res_get.status_code == 200
    assert res_get.json()["id"] == promise_id

    # 3. List Promises
    res_list = client.get("/promise-to-pay?status=ACTIVE")
    assert res_list.status_code == 200
    assert len(res_list.json()) >= 1

    # 4. Dashboard Metrics
    res_metrics = client.get("/promise-to-pay/metrics/dashboard")
    assert res_metrics.status_code == 200
    assert "active_promises" in res_metrics.json()

    # 5. Cancel Promise
    res_cancel = client.post(f"/promise-to-pay/{promise_id}/cancel")
    assert res_cancel.status_code == 200
    assert res_cancel.json()["status"] == "CANCELLED"
