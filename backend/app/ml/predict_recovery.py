"""CLI entry point to score candidate recovery actions for an open RecoveryCase."""
import argparse
import logging
from pathlib import Path
import sys
import uuid

# Ensure backend directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("PredictRecovery")

from sqlalchemy import select
from app.db.session import SessionLocal
from app.models.recovery_case import RecoveryCase
from app.services.next_best_action_engine import NextBestActionEngine


def main():
    parser = argparse.ArgumentParser(description="Evaluate Next Best Action and candidate probabilities for a recovery case.")
    parser.add_argument("--case-id", type=str, help="UUID of RecoveryCase to score")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.case_id:
            case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(args.case_id)))
        else:
            case = db.scalar(select(RecoveryCase).order_by(RecoveryCase.created_at.desc()).limit(1))

        if not case:
            logger.error("No RecoveryCase found in database.")
            return

        logger.info("=================================================================")
        logger.info(f"  AI REVENUE RECOVERY: PREDICTION FOR CASE {case.id}")
        logger.info("=================================================================")
        logger.info(f"Amount at Risk: ₹{case.amount_at_risk} | Status: {case.status}")

        candidates = NextBestActionEngine.evaluate_candidate_actions(db=db, case=case)
        logger.info("\n--- Candidate Action Predictions ---")
        for c in candidates:
            logger.info(
                f"Action: {c['action']:<25} | P(Rec): {c['probability']*100:.1f}% | EV: ₹{c['expected_recovery_value']:,.2f} | Status: {c['model_status']}"
            )

        nba = NextBestActionEngine.compute_next_best_action(db=db, case=case)
        logger.info("\n--- Selected Next Best Action ---")
        logger.info(f"Recommended Action: {nba.action_type} ({nba.channel})")
        logger.info(f"Expected Recovery Value: ₹{nba.expected_recovery_value:,.2f} (Prob: {nba.expected_recovery_probability*100:.1f}%)")
        logger.info(f"Reason: {nba.reason}")

    finally:
        db.close()


if __name__ == "__main__":
    main()
