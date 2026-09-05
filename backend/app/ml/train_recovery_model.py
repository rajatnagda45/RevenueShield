"""CLI entry point to build training datasets, train calibrated recovery models, and evaluate quality gates."""
import argparse
import logging
from pathlib import Path
import sys

# Ensure backend directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TrainRecoveryModel")

from app.db.session import SessionLocal
from app.ml.action_model_trainer import RecoveryActionModelTrainer
from app.ml.registry import ModelRegistryService


def main():
    parser = argparse.ArgumentParser(description="Train and register a data-driven recovery action model.")
    parser.add_argument("--min-samples", type=int, default=50, help="Minimum training samples threshold")
    parser.add_argument("--model-type", type=str, default="LOGISTIC_REGRESSION", choices=["LOGISTIC_REGRESSION", "RANDOM_FOREST"])
    parser.add_argument("--auto-promote", action="store_true", help="Automatically promote model if quality gates pass")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        logger.info("=================================================================")
        logger.info("  AI REVENUE RECOVERY: RECOVERY ACTION MODEL TRAINING")
        logger.info("=================================================================")
        logger.info(f"Model Type: {args.model_type} | Min Samples: {args.min_samples}")

        res = RecoveryActionModelTrainer.train_and_register(
            db=db,
            min_samples=args.min_samples,
            model_type=args.model_type,
        )

        logger.info("\n--- Training Result ---")
        logger.info(f"Status: {res['status']}")
        if res.get("metrics"):
            logger.info(f"Model Version: {res['model_version']}")
            logger.info(f"ROC-AUC: {res['metrics']['roc_auc']:.4f}")
            logger.info(f"Log Loss: {res['metrics']['log_loss']:.4f}")
            logger.info(f"Brier Score: {res['metrics']['brier_score']:.4f}")
            logger.info(f"Training Samples: {res['training_samples']}")

        if args.auto_promote and res.get("model_version"):
            # Find registered model
            active_candidate = ModelRegistryService.list_models(db, limit=1)
            if active_candidate:
                prom_res = ModelRegistryService.evaluate_and_promote(db, active_candidate[0].id)
                logger.info(f"\n--- Promotion Gate Evaluation ---")
                logger.info(f"Promoted: {prom_res['promoted']}")
                if not prom_res['promoted']:
                    logger.warning(f"Reason: {prom_res.get('reason')}")

        logger.info("\n✅ Recovery model training execution completed.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
