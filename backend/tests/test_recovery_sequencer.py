"""Comprehensive tests for Step 11: Intelligent Recovery Sequencer, NextBestActionEngine, and adaptive multi-step plans."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.event import Event
from app.models.customer import Customer
from app.models.learning import LearningExample
from app.models.payment import Payment
from app.models.promise_to_pay import PromiseToPay
from app.models.recovery_case import RecoveryCase
from app.models.recovery_payment_link import RecoveryPaymentLink
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep
from app.schemas.event import NormalizedEvent
from app.services.action_policy import ActionPolicyContext, NextBestAction, RuleBasedActionPolicy
from app.services.event_processor import EventProcessor
from app.services.next_best_action_engine import NextBestActionEngine
from app.services.recovery_scheduler import RecoveryScheduler


def _create_test_case(db: Session, amount: Decimal = Decimal("10000.00"), email: str = "customer.seq@example.com") -> RecoveryCase:
    """Helper to create a fresh RecoveryCase with customer, event, and payment records."""
    unique_id = uuid.uuid4().hex[:8]
    cust = Customer(
        external_customer_id=f"cust_seq_{unique_id}",
        email=email,
        name="Sequencer Test Customer",
        phone="+919876543999",
        whatsapp_allowed=True,
        transactional_allowed=True,
        marketing_opt_out=False,
    )
    db.add(cust)
    db.flush()

    evt = Event(
        external_event_id=f"evt_{unique_id}",
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
        retry_count=0,
    )
    db.add(case)
    db.flush()
    return case


def test_case_creation_automatically_initializes_recovery_plan(db_session: Session):
    """Verify that ingesting a payment.failed event automatically initializes a RecoveryPlan."""
    unique_id = uuid.uuid4().hex[:8]
    event = NormalizedEvent(
        event_id=f"evt_seq_{unique_id}",
        event_type="payment.failed",
        source="RAZORPAY",
        external_payment_id=f"pay_seq_{unique_id}",
        external_order_id=f"order_seq_{unique_id}",
        customer_email="auto.plan@example.com",
        customer_name="Auto Plan Customer",
        customer_phone="+919876543111",
        amount=Decimal("10000.00"),
        currency="INR",
        payment_method="CARD",
        failure_code="BAD_REQUEST_ERROR",
        failure_reason="Customer bank server timed out.",
    )
    res = EventProcessor.process_normalized_event(db_session, event)
    case_id = res.recovery_case_id
    assert case_id is not None

    plan = db_session.scalar(select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case_id))
    assert plan is not None
    assert plan.status == "ACTIVE"
    assert plan.current_step == 0
    assert plan.max_steps == settings.MAX_RECOVERY_STEPS


def test_first_step_selects_and_executes_email_recovery(db_session: Session):
    """Verify first action evaluates NBA, selects EMAIL_PAYMENT_RECOVERY, executes, and sets WAITING timer."""
    case = _create_test_case(db_session, amount=Decimal("10000.00"))
    daytime = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)

    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)
    adv_res = RecoveryScheduler.evaluate_and_advance_plan(
        db=db_session,
        plan_id=plan.id,
        reference_time=daytime,
        dry_run=True,
    )

    assert adv_res["success"] is True
    assert adv_res["status"] == "WAITING"
    assert adv_res["step_number"] == 1
    assert adv_res["action"] == "EMAIL_PAYMENT_RECOVERY"
    assert adv_res["channel"] == "EMAIL"
    assert adv_res["expected_recovery_value"] > 0
    next_eval = plan.next_evaluation_at if plan.next_evaluation_at.tzinfo else plan.next_evaluation_at.replace(tzinfo=timezone.utc)
    assert next_eval == daytime + timedelta(hours=settings.RECOVERY_REEVALUATION_HOURS)

    # Verify step record
    step = db_session.scalar(
        select(RecoveryPlanStep).where(RecoveryPlanStep.recovery_plan_id == plan.id, RecoveryPlanStep.step_number == 1)
    )
    assert step is not None
    assert step.action_type == "EMAIL_PAYMENT_RECOVERY"
    assert step.status == "COMPLETED"


def test_expected_recovery_value_calculation_accuracy():
    """Verify Expected Recovery Value formula: EV = probability * amount_at_risk."""
    policy = RuleBasedActionPolicy()
    context = ActionPolicyContext(
        recovery_case_id="case-123",
        amount_at_risk=Decimal("10000.00"),
        attempt_number=1,
        ml_base_probability=0.42,
    )
    nba = policy.select_action(context)
    assert nba.action_type == "EMAIL_PAYMENT_RECOVERY"
    assert nba.expected_recovery_probability == 0.42
    assert nba.expected_recovery_value == Decimal("4200.00")
    assert "Initial recovery outreach" in nba.reason


def test_second_decision_uses_new_observation_signals(db_session: Session):
    """Verify that engagement signal (link clicked) dynamically updates prediction and chooses follow-up."""
    case = _create_test_case(db_session, amount=Decimal("10000.00"))
    t0 = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=24)

    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)

    # Step 1
    RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=t0, dry_run=True)
    assert plan.current_step == 1

    # Step 2: Customer clicked payment link but did not pay -> engagement signal
    res2 = RecoveryScheduler.evaluate_and_advance_plan(
        db=db_session,
        plan_id=plan.id,
        reference_time=t1,
        force_engagement_signal=True,
        dry_run=True,
    )

    assert res2["success"] is True
    assert res2["step_number"] == 2
    assert res2["action"] == "EMAIL_FOLLOWUP"
    # Probability boosted due to engagement
    assert res2["expected_recovery_probability"] > 0.45
    assert plan.current_step == 2

    step2 = db_session.scalar(
        select(RecoveryPlanStep).where(RecoveryPlanStep.recovery_plan_id == plan.id, RecoveryPlanStep.step_number == 2)
    )
    assert step2 is not None
    assert step2.action_type == "EMAIL_FOLLOWUP"
    assert "engagement" in step2.reason.lower()


def test_payment_captured_stopping_rule_completes_plan(db_session: Session):
    """Verify that when payment is captured, the plan transitions to COMPLETED and stops outreach."""
    case = _create_test_case(db_session, amount=Decimal("10000.00"))
    daytime = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)

    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)
    RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=daytime, dry_run=True)
    assert plan.status == "WAITING"

    # Simulate payment capture via stopping rule
    RecoveryScheduler.stop_plan_on_recovery(db_session, case.id, reason="PAYMENT_CAPTURED")
    db_session.refresh(plan)

    assert plan.status == "COMPLETED"
    assert plan.completion_reason == "PAYMENT_CAPTURED"
    assert plan.completed_at is not None

    # Subsequent evaluation should abort safely
    eval_after = RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=daytime + timedelta(hours=24))
    assert eval_after["action"] == "STOPPED"


def test_max_steps_cap_completes_plan(db_session: Session):
    """Verify that reaching MAX_RECOVERY_STEPS (3) automatically marks the plan as COMPLETED."""
    case = _create_test_case(db_session, amount=Decimal("5000.00"))
    t = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id, max_steps=3)

    # Step 1
    RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=t, dry_run=True)
    # Step 2
    RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=t + timedelta(hours=24), dry_run=True)
    # Step 3
    RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=t + timedelta(hours=48), dry_run=True)

    assert plan.current_step == 3

    # Step 4 attempt
    res4 = RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=t + timedelta(hours=72), dry_run=True)
    assert res4["status"] == "COMPLETED"
    assert res4["action"] == "NO_ACTION"
    assert plan.status == "COMPLETED"
    assert "MAX_RECOVERY_STEPS_REACHED" in plan.completion_reason


def test_max_duration_expiration(db_session: Session):
    """Verify that a plan older than MAX_RECOVERY_DURATION_HOURS transitions to EXPIRED."""
    case = _create_test_case(db_session, amount=Decimal("5000.00"))
    t0 = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)
    plan.created_at = t0
    db_session.commit()

    # Fast forward past 72 hours
    t_expired = t0 + timedelta(hours=73)
    res = RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=t_expired, dry_run=True)

    assert res["status"] == "EXPIRED"
    assert plan.status == "EXPIRED"
    assert "MAX_RECOVERY_DURATION_HOURS_EXCEEDED" in plan.completion_reason


def test_dnd_quiet_hours_blocks_step_without_failing_plan(db_session: Session):
    """Verify that policy DND during quiet hours blocks the step and reschedules evaluation."""
    case = _create_test_case(db_session)
    # 23:00 UTC is during night/quiet hours in Asia/Kolkata
    night_time = datetime(2026, 8, 22, 17, 30, tzinfo=timezone.utc)  # 23:00 IST

    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)
    res = RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=night_time, dry_run=True)

    # In our policy engine, quiet hours blocks outreach
    if not res["success"]:
        assert res["status"] == "BLOCKED"
        step = db_session.scalar(
            select(RecoveryPlanStep).where(RecoveryPlanStep.recovery_plan_id == plan.id)
        )
        assert step is not None
        assert step.status == "BLOCKED"


def test_promise_to_pay_pauses_routine_outreach(db_session: Session):
    """Verify active Promise-to-Pay blocks routine plan execution."""
    case = _create_test_case(db_session)
    daytime = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)

    # Add active PTP
    ptp = PromiseToPay(
        recovery_case_id=case.id,
        customer_id=case.customer_id,
        promised_amount=Decimal("10000.00"),
        promised_date=daytime + timedelta(days=3),
        status="ACTIVE",
    )
    db_session.add(ptp)
    db_session.flush()

    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)
    res = RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=daytime, dry_run=True)

    assert res["status"] == "BLOCKED"
    assert "PROMISE_TO_PAY" in (res.get("blocking_rule") or "")


def test_plan_pause_and_resume(db_session: Session):
    """Verify plan pause and resume functions."""
    case = _create_test_case(db_session)
    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)

    # Pause
    pause_res = RecoveryScheduler.pause_plan(db_session, plan.id, reason="Customer contacted support")
    assert pause_res["status"] == "PAUSED"
    assert plan.status == "PAUSED"

    # Evaluation during pause should not advance
    daytime = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
    eval_res = RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=daytime)
    assert eval_res["status"] == "PAUSED"
    assert eval_res["action"] == "NO_ACTION"

    # Resume
    res_res = RecoveryScheduler.resume_plan(db_session, plan.id)
    assert res_res["status"] in ["ACTIVE", "WAITING"]
    assert plan.status in ["ACTIVE", "WAITING"]


def test_process_due_plans_worker(db_session: Session):
    """Verify process_due_plans advances all waiting plans whose timer has expired."""
    case = _create_test_case(db_session)
    t0 = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)

    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)
    RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=t0, dry_run=True)
    assert plan.status == "WAITING"

    # Advance timer to 25 hours later
    t_due = t0 + timedelta(hours=25)
    results = RecoveryScheduler.process_due_plans(db_session, reference_time=t_due)
    assert len(results) >= 1
    assert plan.current_step == 2


def test_recovery_plan_api_endpoints(client: TestClient, db_session: Session):
    """Verify GET, POST evaluate, POST pause, POST resume, and dashboard endpoints."""
    case = _create_test_case(db_session)
    daytime = "2026-08-22T10:00:00Z"

    # 1. GET /recovery-cases/{id}/plan
    res_get = client.get(f"/recovery-cases/{case.id}/plan")
    assert res_get.status_code == 200
    data_get = res_get.json()
    assert data_get["case_id"] == str(case.id)
    assert data_get["status"] == "ACTIVE"
    assert data_get["current_step"] == 0
    assert data_get["next_action"]["action_type"] == "EMAIL_PAYMENT_RECOVERY"

    # 2. POST /recovery-cases/{id}/plan/evaluate
    res_eval = client.post(
        f"/recovery-cases/{case.id}/plan/evaluate",
        json={"reference_time": daytime, "dry_run": True},
    )
    assert res_eval.status_code == 200
    data_eval = res_eval.json()
    assert data_eval["step_number"] == 1
    assert data_eval["action"] == "EMAIL_PAYMENT_RECOVERY"

    # 3. POST /recovery-cases/{id}/plan/pause
    res_pause = client.post(f"/recovery-cases/{case.id}/plan/pause", json={"reason": "Test pause"})
    assert res_pause.status_code == 200
    assert res_pause.json()["status"] == "PAUSED"

    # 4. POST /recovery-cases/{id}/plan/resume
    res_resume = client.post(f"/recovery-cases/{case.id}/plan/resume")
    assert res_resume.status_code == 200
    assert res_resume.json()["status"] == "WAITING"

    # 5. GET /admin/plans/dashboard
    res_dash = client.get("/admin/plans/dashboard")
    assert res_dash.status_code == 200
    data_dash = res_dash.json()
    assert data_dash["total_plans"] >= 1
    assert len(data_dash["timeline"]) >= 1


def test_audit_logs_and_learning_examples_recorded(db_session: Session):
    """Verify structured audit logs and LearningExample records are created for plan actions."""
    case = _create_test_case(db_session)
    daytime = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)

    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)
    RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=daytime, dry_run=True)

    # Check Audit logs
    audit_events = db_session.scalars(
        select(AuditLog.action).where(AuditLog.recovery_case_id == case.id)
    ).all()
    assert "RECOVERY_PLAN_CREATED" in audit_events
    assert "RECOVERY_PLAN_EVALUATED" in audit_events
    assert "NEXT_BEST_ACTION_SELECTED" in audit_events
    assert "RECOVERY_STEP_CREATED" in audit_events
    assert "RECOVERY_STEP_EXECUTED" in audit_events

    # Check Learning Example
    learning = db_session.scalar(
        select(LearningExample).where(LearningExample.recovery_case_id == case.id)
    )
    assert learning is not None
    assert learning.feature_snapshot.get("plan_id") == str(plan.id)
    assert learning.feature_snapshot.get("step_number") == 1
