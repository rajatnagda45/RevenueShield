"""End-to-End Demonstration Script for Step 12: Data-Driven Next-Best-Action Model."""
from datetime import datetime, timezone
from decimal import Decimal
import logging
from pathlib import Path
import sys
import uuid

# Ensure backend directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DemonstrateMLNextBestAction")

from sqlalchemy import select
from app.db.session import SessionLocal
from app.models.customer import Customer
from app.models.event import Event
from app.models.model_version import ModelVersion
from app.models.recovery_case import RecoveryCase
from app.ml.action_model_trainer import RecoveryActionModelTrainer
from app.ml.action_prediction_service import RecoveryActionPredictionService
from app.ml.dataset_builder import RecoveryMLDatasetBuilder
from app.ml.drift_detector import ModelDriftDetector
from app.ml.registry import ModelRegistryService
from app.services.next_best_action_engine import NextBestActionEngine


def run_demo():
    db = SessionLocal()
    try:
        print("\n" + "=" * 80)
        print("  AI REVENUE RECOVERY: STEP 12 DATA-DRIVEN NEXT-BEST-ACTION DEMO")
        print("=" * 80 + "\n")

        # ---------------------------------------------------------------------
        # 1. Dataset Extraction & Anti-Leakage Validation
        # ---------------------------------------------------------------------
        print("--- STAGE 1: Dataset Extraction & Anti-Leakage Validation ---")
        dataset_df = RecoveryMLDatasetBuilder.build_training_dataset(
            db=db,
            include_synthetic_if_insufficient=True,
            min_samples=100,
            output_csv_path="data/ml/recovery_training_dataset.csv",
        )
        print(f"[OK] Extracted {len(dataset_df)} training samples.")
        print(f"Features: {list(dataset_df.columns[:8])} ...")
        print(f"Class Balance (Recovered=1): {dataset_df['recovered'].mean()*100:.1f}%\n")

        # ---------------------------------------------------------------------
        # 2. Train & Calibrate Tabular Action Model
        # ---------------------------------------------------------------------
        print("--- STAGE 2: Calibrated Tabular ML Model Training ---")
        train_result = RecoveryActionModelTrainer.train_and_register(
            db=db,
            custom_df=dataset_df,
            min_samples=50,
            model_type="LOGISTIC_REGRESSION",
        )
        version = train_result["model_version"]
        metrics = train_result["metrics"]
        print(f"[OK] Trained Model Version: {version}")
        print(f"   ROC-AUC:     {metrics['roc_auc']:.4f}")
        print(f"   Log Loss:    {metrics['log_loss']:.4f} (Probability Quality)")
        print(f"   Brier Score: {metrics['brier_score']:.4f} (Mean Squared Probability Error)")
        print(f"   Accuracy:    {metrics['accuracy']*100:.1f}%\n")

        # ---------------------------------------------------------------------
        # 3. Model Registry Promotion Quality Gate
        # ---------------------------------------------------------------------
        print("--- STAGE 3: Model Registry Promotion Quality Gate ---")
        cand_model = db.scalar(select(ModelVersion).where(ModelVersion.version == version))
        promotion_result = ModelRegistryService.evaluate_and_promote(
            db=db,
            candidate_model_id=cand_model.id,
            brier_threshold=0.30,
        )
        print(f"Promotion Result: {'PROMOTED' if promotion_result['promoted'] else 'REJECTED'}")
        print(f"Active Model Status: {promotion_result.get('status')}\n")

        # ---------------------------------------------------------------------
        # 4. Create New Recovery Case
        # ---------------------------------------------------------------------
        print("--- STAGE 4: Open New Recovery Case for ML Scoring ---")
        uid = uuid.uuid4().hex[:6]
        cust = Customer(
            external_customer_id=f"demo_cust_{uid}",
            email=f"ananya.{uid}@techstart.in",
            name="Ananya Sharma",
            phone="+919876500123",
            whatsapp_allowed=True,
            transactional_allowed=True,
        )
        db.add(cust)
        db.flush()

        evt = Event(
            external_event_id=f"demo_evt_{uid}",
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
            amount_at_risk=Decimal("12000.00"),
            currency="INR",
            case_type="PAYMENT_FAILURE",
            status="OPEN",
        )
        db.add(case)
        db.commit()
        db.refresh(case)
        print(f"[OK] Case Created: {case.id} | Amount: Rs. {case.amount_at_risk:,.2f} | Customer: {cust.name}\n")

        # ---------------------------------------------------------------------
        # 5. Evaluate Candidate Actions & Expected Recovery Values
        # ---------------------------------------------------------------------
        print("--- STAGE 5: Candidate Action Prediction & Expected Recovery Values ---")
        candidate_evals = NextBestActionEngine.evaluate_candidate_actions(db=db, case=case)
        print(f"{'Candidate Action':<28} | {'P(Recovery)':<12} | {'Expected Value':<16} | {'Status'}")
        print("-" * 75)
        for c in candidate_evals:
            print(f"{c['action']:<28} | {c['probability']*100:>8.1f}%   | Rs. {c['expected_recovery_value']:>10,.2f} | {c['model_status']}")
        print()

        # ---------------------------------------------------------------------
        # 6. Select Next Best Action (Expected Value Maximization)
        # ---------------------------------------------------------------------
        print("--- STAGE 6: Next Best Action Selection & Explainability ---")
        nba = NextBestActionEngine.compute_next_best_action(db=db, case=case)
        print(f"[ACTION SELECTED]:             {nba.action_type}")
        print(f"   Delivery Channel:              {nba.channel}")
        print(f"   Estimated Recovery Probability: {nba.expected_recovery_probability*100:.1f}%")
        print(f"   Expected Recovery Value:       Rs. {nba.expected_recovery_value:,.2f}")
        print(f"   Reason & Explainability:       {nba.reason}\n")

        # ---------------------------------------------------------------------
        # 7. Model Drift Indicator Assessment
        # ---------------------------------------------------------------------
        print("--- STAGE 7: Model Monitoring & Statistical Drift Detection ---")
        drift_report = ModelDriftDetector.assess_model_drift(db=db, model_version=version)
        print(f"Drift Status: {drift_report['status']} (PSI: {drift_report['psi']}, Level: {drift_report['drift_level']})")
        print(f"Sample Inferences: {drift_report['sample_count']}\n")

        print("=" * 80)
        print("[SUCCESS] STEP 12 DATA-DRIVEN NEXT-BEST-ACTION DEMO COMPLETED")
        print("=" * 80 + "\n")

    finally:
        db.close()


if __name__ == "__main__":
    run_demo()
