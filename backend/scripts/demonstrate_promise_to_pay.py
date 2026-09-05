"""End-to-End Demonstration of Step 14: Promise-to-Pay & Intelligent Escalation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from pathlib import Path
import sys
import uuid

# Ensure backend directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DemonstratePromiseToPay")

from app.db.session import SessionLocal
from app.decision.base import ActionType, DecisionContext
from app.decision.policy import PolicyEngine
from app.models.customer import Customer
from app.models.event import Event
from app.models.payment import Payment
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep
from app.outcomes.engine import OutcomeEngine
from app.services.escalation_policy import EscalationLevel, EscalationPolicy
from app.services.promise_eligibility_engine import PromiseEligibilityEngine
from app.services.promise_evaluation_service import PromiseEvaluationService
from app.services.promise_to_pay_service import PromiseToPayService


def run_demo():
    db = SessionLocal()
    try:
        print("\n" + "=" * 85)
        print("  AI REVENUE RECOVERY: STEP 14 PROMISE-TO-PAY + INTELLIGENT ESCALATION DEMO")
        print("=" * 85 + "\n")

        uid = uuid.uuid4().hex[:6]
        # 1. Setup Overdue High-Value Customer & Case
        cust = Customer(
            external_customer_id=f"ptp_cust_{uid}",
            email=f"aditya.sharma.{uid}@enterprise.in",
            name="Aditya Sharma",
            phone="+919876500123",
            whatsapp_allowed=True,
            transactional_allowed=True,
        )
        db.add(cust)
        db.flush()

        evt = Event(
            external_event_id=f"ptp_evt_{uid}",
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
            amount_at_risk=Decimal("25000.00"),
            currency="INR",
            case_type="PAYMENT_FAILURE",
            status="OPEN",
        )
        db.add(case)
        db.flush()

        payment = Payment(
            customer_id=cust.id,
            external_payment_id=f"pay_ptp_{uid}",
            amount=Decimal("25000.00"),
            currency="INR",
            status="FAILED",
        )
        db.add(payment)
        db.flush()
        case.payment_id = payment.id

        plan = RecoveryPlan(
            recovery_case_id=case.id,
            status="ACTIVE",
            current_step=1,
            max_steps=3,
        )
        db.add(plan)
        db.flush()

        step1 = RecoveryPlanStep(
            recovery_plan_id=plan.id,
            step_number=1,
            action_type="EMAIL_PAYMENT_RECOVERY",
            channel="EMAIL",
            status="COMPLETED",
            prediction_score=0.45,
            expected_recovery_value=Decimal("11250.00"),
            executed_at=datetime.now(timezone.utc) - timedelta(hours=30),
        )
        db.add(step1)
        db.commit()
        db.refresh(case)

        # ---------------------------------------------------------------------
        # Stage 1: Case Escalation & Eligibility Evaluation
        # ---------------------------------------------------------------------
        print("--- STAGE 1: Case Escalation Tier & Promise Eligibility ---")
        esc_report = EscalationPolicy.evaluate_level(case, hours_overdue=30.0, previous_attempts=1)
        print(f"Case Amount: Rs. {case.amount_at_risk:,.2f} | Status: {case.status}")
        print(f"Escalation Level: {esc_report['level']} ({esc_report['reason']})")

        elig_report = PromiseEligibilityEngine.evaluate_eligibility(case)
        print(f"Promise Eligibility: {elig_report['eligible']} (Score: {elig_report['score']})")
        print(f"Decision Reason: {elig_report['reason']}\n")

        # ---------------------------------------------------------------------
        # Stage 2: Customer Commits -> Promise-to-Pay Created
        # ---------------------------------------------------------------------
        print("--- STAGE 2: Customer Commitment -> Promise-to-Pay Created ---")
        friday_deadline = datetime.now(timezone.utc) + timedelta(days=2)
        promise = PromiseToPayService.create_promise(
            db=db,
            recovery_case_id=case.id,
            promised_amount=Decimal("25000.00"),
            promised_date=friday_deadline,
            promised_time="17:00",
            source="CUSTOMER",
            notes="Customer stated funds will clear on Friday afternoon.",
        )
        print(f"[OK] Promise Created: ID={promise.id} Status={promise.status}")
        print(f"     Promised Amount: Rs. {promise.promised_amount:,.2f}")
        print(f"     Promised Deadline: {promise.promised_date.strftime('%A, %d %b %Y %H:%M UTC')}")
        db.refresh(case.recovery_plan)
        print(f"     Recovery Plan Status: {case.recovery_plan.status} (Outreach Paused)\n")

        # ---------------------------------------------------------------------
        # Stage 3: Outreach Blocking Verification
        # ---------------------------------------------------------------------
        print("--- STAGE 3: Policy Engine Outreach Halting Check ---")
        context = DecisionContext(
            case_id=str(case.id),
            case_type="PAYMENT_FAILURE",
            amount_at_risk=Decimal("25000.00"),
            currency="INR",
            case_age_hours=30.0,
            retry_count=1,
            diagnosis_category="INSUFFICIENT_FUNDS",
            diagnosis_confidence=0.90,
            risk_score=60.0,
            recovery_probability=0.45,
            promise_to_pay_active=True,
        )
        pol_res = PolicyEngine.evaluate(
            action_type=ActionType.SEND_PAYMENT_LINK,
            context=context,
            case_status=case.status,
        )
        print(f"Proposed Action: SEND_PAYMENT_LINK")
        print(f"Policy Engine Allowed: {pol_res.allowed}")
        print(f"Blocking Rule: {pol_res.blocking_rule}")
        print(f"Policy Reason: {pol_res.reason}\n")

        # ---------------------------------------------------------------------
        # Stage 4: Deadline Passes Without Payment (MISSED)
        # ---------------------------------------------------------------------
        print("--- STAGE 4: Evaluation at Deadline (Unpaid -> MISSED) ---")
        eval_time = friday_deadline + timedelta(hours=2)
        eval_res = PromiseEvaluationService.evaluate_promise(
            db=db,
            promise_id=promise.id,
            reference_time=eval_time,
        )
        print(f"[OK] Promise Evaluation Status: {eval_res['status']}")
        db.refresh(case.recovery_plan)
        print(f"     Recovery Plan Resumed: Status={case.recovery_plan.status}")
        print(f"     Next Best Action Selected: {eval_res.get('next_best_action')}")
        print(f"     Expected Recovery Value: Rs. {eval_res.get('expected_recovery_value', 0.0):,.2f}\n")

        # ---------------------------------------------------------------------
        # Stage 5: Payment Captured -> FULFILLED & RECOVERED
        # ---------------------------------------------------------------------
        print("--- STAGE 5: Payment Captured Webhook Reconciliation ---")
        # Customer pays payment link
        outcome = OutcomeEngine.process_payment_capture(
            db=db,
            recovery_case=case,
            captured_amount=Decimal("25000.00"),
        )
        db.refresh(promise)
        db.refresh(case)
        db.refresh(case.recovery_plan)
        print(f"[OK] Razorpay Payment Captured: Rs. {outcome.amount_recovered:,.2f}")
        print(f"     Case Status: {case.status} (Recovered: Rs. {case.recovered_amount:,.2f})")
        print(f"     Promise Status: {promise.status} (Fulfilled: {promise.fulfilled_at.isoformat() if promise.fulfilled_at else 'N/A'})")
        print(f"     Plan Status: {case.recovery_plan.status} (Completed: {case.recovery_plan.completion_reason})\n")

        # ---------------------------------------------------------------------
        # Stage 6: Customer Promise Fulfillment History
        # ---------------------------------------------------------------------
        print("--- STAGE 6: Customer Commitment Reliability Score ---")
        history = PromiseToPayService.get_customer_promise_history(db, cust.id)
        print(f"Customer: {cust.name} ({cust.email})")
        print(f"Total Commitments: {history['total_promises']}")
        print(f"Fulfilled Commitments: {history['fulfilled']}")
        print(f"Fulfillment Rate: {history['fulfillment_rate']*100:.1f}%\n")

        print("=" * 85)
        print("[SUCCESS] STEP 14 PROMISE-TO-PAY & INTELLIGENT ESCALATION COMPLETED")
        print("=" * 85 + "\n")

    finally:
        db.close()


if __name__ == "__main__":
    run_demo()
