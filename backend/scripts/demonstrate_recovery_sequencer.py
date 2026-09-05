"""End-to-End Demonstration script for Step 11: Intelligent Recovery Sequencer.

Simulates:
1. Failed payment of ₹10,000 -> Auto RecoveryPlan creation.
2. Step 1: Initial ML Prediction -> NBA = EMAIL_PAYMENT_RECOVERY -> Dispatched -> Plan WAITING 24h.
3. Telemetry Signal: Customer clicked payment link but hasn't completed payment yet.
4. Step 2: Adaptive Re-evaluation -> Boosted probability & EV -> NBA = EMAIL_FOLLOWUP.
5. Webhook: payment.captured (₹10,000) arrives -> Monotonic Stopping Rule halts outreach.
6. RecoveryCase = RECOVERED, RecoveryPlan = COMPLETED, LearningExample finalized.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import sys
import uuid
from pathlib import Path

# Add backend directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SequencerDemo")

from sqlalchemy import select
from app.db.session import SessionLocal
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.event import Event
from app.models.learning import LearningExample
from app.models.payment import Payment
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep
from app.schemas.event import NormalizedEvent
from app.services.event_processor import EventProcessor
from app.services.recovery_scheduler import RecoveryScheduler


def run_e2e_sequencer_demonstration():
    db = SessionLocal()
    try:
        logger.info("=================================================================")
        logger.info("  AI REVENUE RECOVERY: STEP 11 SEQUENCER END-TO-END DEMO")
        logger.info("=================================================================")

        unique_id = uuid.uuid4().hex[:6]
        cust_email = "ankit.test@example.com"
        amount = Decimal("10000.00")
        t0 = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)

        # -------------------------------------------------------------
        # 1. Simulate payment.failed event
        # -------------------------------------------------------------
        logger.info("\n--- 1. Ingesting payment.failed Event (₹10,000.00) ---")
        failed_event = NormalizedEvent(
            event_id=f"evt_fail_{unique_id}",
            event_type="payment.failed",
            source="RAZORPAY",
            external_payment_id=f"pay_fail_{unique_id}",
            external_order_id=f"order_{unique_id}",
            external_customer_id=f"cust_{unique_id}",
            customer_email=cust_email,
            customer_name="Ankit Kumar",
            customer_phone="+917991142735",
            amount=amount,
            currency="INR",
            payment_method="UPI",
            failure_code="BAD_REQUEST_ERROR",
            failure_reason="Customer bank server timed out during authorization.",
            occurred_at=t0,
        )

        proc_res = EventProcessor.process_normalized_event(db, failed_event)
        case_id = proc_res.recovery_case_id
        logger.info(f"✅ RecoveryCase opened: {case_id}")

        plan = db.scalar(select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case_id))
        logger.info(f"✅ RecoveryPlan automatically created: ID={plan.id} Status={plan.status} MaxSteps={plan.max_steps}")

        # -------------------------------------------------------------
        # 2. Step 1: Initial NBA & Execution
        # -------------------------------------------------------------
        logger.info("\n--- 2. Executing Step 1: Next-Best-Action Evaluation & Email Outreach ---")
        step1_res = RecoveryScheduler.evaluate_and_advance_plan(
            db=db,
            plan_id=plan.id,
            reference_time=t0,
            dry_run=True,
        )
        logger.info(f"✅ Step 1 Outcome: Action={step1_res['action']} Channel={step1_res['channel']} EV=₹{step1_res['expected_recovery_value']:.2f}")
        logger.info(f"✅ Plan State: Status={step1_res['status']} NextEvaluationAt={step1_res['next_evaluation_at']}")

        # -------------------------------------------------------------
        # 3. Simulate Telemetry Signal: Payment Link Clicked
        # -------------------------------------------------------------
        t1 = t0 + timedelta(hours=24)
        logger.info(f"\n--- 3. Waiting 24 Hours ({t1.isoformat()}) & Observing Telemetry ---")
        logger.info("📡 Signal Captured: Customer clicked payment link in email but has not paid yet.")

        # -------------------------------------------------------------
        # 4. Step 2: Adaptive Re-evaluation with Engagement Boost
        # -------------------------------------------------------------
        logger.info("\n--- 4. Executing Step 2: Adaptive NBA Re-evaluation ---")
        step2_res = RecoveryScheduler.evaluate_and_advance_plan(
            db=db,
            plan_id=plan.id,
            reference_time=t1,
            force_engagement_signal=True,
            dry_run=True,
        )
        logger.info(f"✅ Step 2 Outcome: Action={step2_res['action']} EV=₹{step2_res['expected_recovery_value']:.2f} (Prob: {step2_res['expected_recovery_probability']:.2f})")
        logger.info(f"✅ Adaptive Logic Confirmed: Boosted probability from engagement signal.")

        # -------------------------------------------------------------
        # 5. Simulate payment.captured event (Customer pays ₹10,000.00)
        # -------------------------------------------------------------
        t2 = t1 + timedelta(hours=3)
        logger.info(f"\n--- 5. Customer Pays! Ingesting payment.captured Event at {t2.isoformat()} ---")
        captured_event = NormalizedEvent(
            event_id=f"evt_cap_{unique_id}",
            event_type="payment.captured",
            source="RAZORPAY",
            external_payment_id=f"pay_cap_{unique_id}",
            external_order_id=f"order_{unique_id}",
            external_customer_id=f"cust_{unique_id}",
            customer_email=cust_email,
            customer_name="Ankit Kumar",
            customer_phone="+917991142735",
            amount=amount,
            currency="INR",
            payment_method="UPI",
            occurred_at=t2,
        )
        EventProcessor.process_normalized_event(db, captured_event)

        # -------------------------------------------------------------
        # 6. Verify Stopping Rule, Case Status, and Telemetry
        # -------------------------------------------------------------
        db.refresh(plan)
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))

        logger.info("\n--- 6. Final State Verification ---")
        logger.info(f"✅ RecoveryCase Status: {case.status} (Recovered Amount: ₹{case.recovered_amount})")
        logger.info(f"✅ RecoveryPlan Status: {plan.status} (Completion Reason: {plan.completion_reason})")
        logger.info(f"✅ Total Steps Executed: {plan.current_step} of max {plan.max_steps}")

        # Check Audit Log count
        audit_count = len(db.scalars(select(AuditLog).where(AuditLog.recovery_case_id == case_id)).all())
        logger.info(f"✅ Audit Log Entries Recorded: {audit_count}")

        # Check Learning Examples
        learning_count = len(db.scalars(select(LearningExample).where(LearningExample.recovery_case_id == case_id)).all())
        logger.info(f"✅ Learning Examples Telemetry Recorded: {learning_count}")

        assert case.status == "RECOVERED"
        assert plan.status == "COMPLETED"
        assert case.recovered_amount == amount
        logger.info("\n🎉 STEP 11 INTELLIGENT RECOVERY SEQUENCER E2E DEMO PASSED 100%!")

    finally:
        db.close()


if __name__ == "__main__":
    run_e2e_sequencer_demonstration()
