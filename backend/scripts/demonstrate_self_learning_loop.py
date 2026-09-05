"""End-to-End Demonstration of Step 13: Closed-Loop Self-Learning Feedback."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from pathlib import Path
import sys
import uuid

# Ensure backend directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DemonstrateSelfLearningLoop")

from sqlalchemy import select
from app.db.session import SessionLocal
from app.models.customer import Customer
from app.models.event import Event
from app.models.learning import LearningExample
from app.models.model_version import ModelVersion
from app.models.recovery_attribution import RecoveryAttribution
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep
from app.ml.dataset_builder import RecoveryMLDatasetBuilder
from app.ml.model_rollback_service import ModelRollbackService
from app.ml.retraining_service import RetrainingService
from app.services.learning_metrics_service import LearningMetricsService
from app.services.recovery_attribution_engine import RecoveryAttributionEngine
from app.services.recovery_outcome_resolver import RecoveryOutcomeResolver


def _create_demo_case(db, name, amount_val):
    uid = uuid.uuid4().hex[:6]
    cust = Customer(
        external_customer_id=f"sl_cust_{uid}",
        email=f"{name.lower().replace(' ', '')}.{uid}@enterprise.in",
        name=name,
        phone="+919876500456",
        whatsapp_allowed=True,
        transactional_allowed=True,
    )
    db.add(cust)
    db.flush()

    evt = Event(
        external_event_id=f"sl_evt_{uid}",
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
        amount_at_risk=Decimal(str(amount_val)),
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
    return case, plan


def run_demo():
    db = SessionLocal()
    try:
        print("\n" + "=" * 80)
        print("  AI REVENUE RECOVERY: STEP 13 CLOSED-LOOP SELF-LEARNING DEMO")
        print("=" * 80 + "\n")

        # ---------------------------------------------------------------------
        # Scenario 1: Immediate Recovery Touchpoint
        # ---------------------------------------------------------------------
        print("--- SCENARIO 1: Single Outreach -> Payment Captured (RECOVERED) ---")
        case1, plan1 = _create_demo_case(db, "Rohan Verma", 8500.00)
        step1 = RecoveryPlanStep(
            recovery_plan_id=plan1.id,
            step_number=1,
            action_type="EMAIL_PAYMENT_RECOVERY",
            channel="EMAIL",
            status="COMPLETED",
            prediction_score=0.42,
            expected_recovery_value=Decimal("3570.00"),
            executed_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        db.add(step1)
        db.commit()

        # Resolve Outcome on payment.captured
        ex1 = RecoveryOutcomeResolver.resolve_outcome(
            db=db,
            case=case1,
            outcome_status="RECOVERED",
            amount_recovered=Decimal("8500.00"),
        )
        print(f"[OK] Case 1 Outcome: {ex1.outcome_type} (Label={ex1.label})")
        print(f"     Attribution: {ex1.attribution} | Prediction Error: {ex1.prediction_error:+.4f}\n")

        # ---------------------------------------------------------------------
        # Scenario 2: Unsuccessful Recovery
        # ---------------------------------------------------------------------
        print("--- SCENARIO 2: Outreach Expiration -> No Payment (NOT_RECOVERED) ---")
        case2, plan2 = _create_demo_case(db, "Pooja Hegde", 15000.00)
        step2 = RecoveryPlanStep(
            recovery_plan_id=plan2.id,
            step_number=1,
            action_type="EMAIL_PAYMENT_RECOVERY",
            channel="EMAIL",
            status="COMPLETED",
            prediction_score=0.60,
            expected_recovery_value=Decimal("9000.00"),
            executed_at=datetime.now(timezone.utc) - timedelta(hours=72),
        )
        db.add(step2)
        db.commit()

        ex2 = RecoveryOutcomeResolver.resolve_outcome(
            db=db,
            case=case2,
            outcome_status="NOT_RECOVERED",
        )
        print(f"[OK] Case 2 Outcome: {ex2.outcome_type} (Label={ex2.label})")
        print(f"     Prediction Error: {ex2.prediction_error:+.4f}\n")

        # ---------------------------------------------------------------------
        # Scenario 3: Multi-Touch Sequence Attribution
        # ---------------------------------------------------------------------
        print("--- SCENARIO 3: Multi-Touch Sequence (Email -> Follow-up -> Paid) ---")
        case3, plan3 = _create_demo_case(db, "Vikram Malhotra", 22000.00)
        st1 = RecoveryPlanStep(
            recovery_plan_id=plan3.id,
            step_number=1,
            action_type="EMAIL_PAYMENT_RECOVERY",
            channel="EMAIL",
            status="COMPLETED",
            executed_at=datetime.now(timezone.utc) - timedelta(hours=28),
        )
        st2 = RecoveryPlanStep(
            recovery_plan_id=plan3.id,
            step_number=2,
            action_type="EMAIL_FOLLOWUP",
            channel="EMAIL",
            status="COMPLETED",
            prediction_score=0.68,
            expected_recovery_value=Decimal("14960.00"),
            executed_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        db.add_all([st1, st2])
        db.commit()

        ex3 = RecoveryOutcomeResolver.resolve_outcome(
            db=db,
            case=case3,
            outcome_status="RECOVERED",
            amount_recovered=Decimal("22000.00"),
        )
        print(f"[OK] Case 3 Outcome: {ex3.outcome_type} (Label={ex3.label})")
        print(f"     Attributed Step: Step {st2.step_number} ({st2.action_type}) | Type: {ex3.attribution}\n")

        # ---------------------------------------------------------------------
        # 4. Batch Retraining Trigger & Model Evaluation
        # ---------------------------------------------------------------------
        print("--- STAGE 4: Batch Retraining Trigger & Evaluation Benchmark ---")
        retrain_result = RetrainingService.execute_retraining(
            db=db,
            force=True,
            auto_promote=True,
        )
        print(f"[OK] Retraining Status:    {retrain_result['status']}")
        print(f"     Candidate Version:    {retrain_result.get('candidate_version')}")
        if retrain_result.get("metrics"):
            m = retrain_result["metrics"]
            print(f"     Candidate ROC-AUC:    {m['roc_auc']:.4f}")
            print(f"     Candidate Log Loss:   {m['log_loss']:.4f}")
            print(f"     Candidate Brier:      {m['brier_score']:.4f}")
        print(f"     Promotion Evaluation: {retrain_result.get('promotion')}\n")

        # ---------------------------------------------------------------------
        # 5. Business & Action Performance Metrics
        # ---------------------------------------------------------------------
        print("--- STAGE 5: Business Performance & Action Effectiveness Matrix ---")
        biz = LearningMetricsService.compute_business_metrics(db)
        print(f"Total Amount at Risk:      Rs. {biz['total_amount_at_risk']:>12,.2f}")
        print(f"Total Amount Recovered:   Rs. {biz['total_amount_recovered']:>12,.2f}")
        print(f"Monetary Recovery Rate:        {biz['monetary_recovery_rate']*100:>8.1f}%")
        print(f"Case-Level Recovery Rate:      {biz['case_recovery_rate']*100:>8.1f}%\n")

        acts = LearningMetricsService.compute_action_performance(db)
        print(f"{'Action':<25} | {'Cases':<6} | {'Recovered':<10} | {'Rate':<8} | {'Avg P(Rec)'}")
        print("-" * 65)
        for a in acts:
            print(f"{a['action']:<25} | {a['cases']:<6} | Rs. {a['amount_recovered']:>8,.0f} | {a['recovery_rate']*100:>6.1f}% | {a['avg_predicted_probability']*100:>7.1f}%")
        print()

        # ---------------------------------------------------------------------
        # 6. Model Rollback Safeguard
        # ---------------------------------------------------------------------
        print("--- STAGE 6: Model Rollback Safeguard Demonstration ---")
        # Ensure we have 2 versions to demonstrate rollback
        v_active = retrain_result.get("candidate_version")
        rollback_res = ModelRollbackService.rollback_to_previous_model(db=db)
        print(f"[OK] Rollback Status:    {rollback_res['status']}")
        print(f"     Previous Active:    {rollback_res['previous_version']}")
        print(f"     Restored Champion:  {rollback_res['active_version']}\n")

        print("=" * 80)
        print("[SUCCESS] STEP 13 CLOSED-LOOP SELF-LEARNING DEMO COMPLETED")
        print("=" * 80 + "\n")

    finally:
        db.close()


if __name__ == "__main__":
    run_demo()
