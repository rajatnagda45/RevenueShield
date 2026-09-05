"""Metrics Service computing business KPIs, action effectiveness, and model calibration."""
import logging
from typing import Any, Dict, List
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.learning import LearningExample
from app.models.prediction import Prediction
from app.models.recovery_case import RecoveryCase

logger = logging.getLogger(__name__)


class LearningMetricsService:
    """Computes comprehensive business, model performance, and operational KPIs."""

    @classmethod
    def compute_business_metrics(cls, db: Session) -> Dict[str, Any]:
        """Calculate business-level recovery metrics without confusing monetary vs case recovery rates."""
        cases = db.scalars(select(RecoveryCase)).all()
        if not cases:
            return {
                "total_amount_at_risk": 0.0,
                "total_amount_recovered": 0.0,
                "monetary_recovery_rate": 0.0,
                "case_recovery_rate": 0.0,
                "total_cases": 0,
                "recovered_cases": 0,
                "unrecovered_cases": 0,
            }

        total_risk = sum(float(c.amount_at_risk or 0.0) for c in cases)
        total_recovered = sum(float(c.recovered_amount or 0.0) for c in cases)
        recovered_cases = sum(1 for c in cases if c.status == "RECOVERED")
        total_cases = len(cases)

        monetary_rate = (total_recovered / total_risk) if total_risk > 0 else 0.0
        case_rate = (recovered_cases / total_cases) if total_cases > 0 else 0.0

        return {
            "total_amount_at_risk": round(total_risk, 2),
            "total_amount_recovered": round(total_recovered, 2),
            "monetary_recovery_rate": round(monetary_rate, 4),
            "case_recovery_rate": round(case_rate, 4),
            "total_cases": total_cases,
            "recovered_cases": recovered_cases,
            "unrecovered_cases": total_cases - recovered_cases,
        }

    @classmethod
    def compute_action_performance(cls, db: Session) -> List[Dict[str, Any]]:
        """Calculate recovery rate and expected vs actual recovery by candidate action type."""
        examples = db.scalars(
            select(LearningExample).where(LearningExample.is_finalized == True)
        ).all()

        action_groups: Dict[str, List[LearningExample]] = {}
        for ex in examples:
            act = ex.action_type or "UNKNOWN"
            action_groups.setdefault(act, []).append(ex)

        results = []
        for act, group in action_groups.items():
            total = len(group)
            recovered = sum(1 for e in group if e.label == 1 or e.outcome_type == "RECOVERED")
            amt_recovered = sum(float(e.amount_recovered or 0.0) for e in group)
            avg_pred = float(np.mean([float(e.recovery_probability or 0.5) for e in group]))

            rec_rate = (recovered / total) if total > 0 else 0.0
            results.append({
                "action": act,
                "cases": total,
                "recovered": recovered,
                "recovery_rate": round(rec_rate, 4),
                "amount_recovered": round(amt_recovered, 2),
                "avg_predicted_probability": round(avg_pred, 4),
            })

        results.sort(key=lambda x: x["cases"], reverse=True)
        return results

    @classmethod
    def detect_action_imbalance(cls, db: Session, recent_limit: int = 100) -> Dict[str, Any]:
        """Detect whether a single action is disproportionately favored by model policies (>90%)."""
        preds = db.scalars(
            select(Prediction).order_by(Prediction.created_at.desc()).limit(recent_limit)
        ).all()

        if len(preds) < 20:
            return {"status": "BALANCED", "dominant_action": None, "dominant_ratio": 0.0}

        counts: Dict[str, int] = {}
        for p in preds:
            act = p.action_type or "UNKNOWN"
            counts[act] = counts.get(act, 0) + 1

        total = len(preds)
        dominant_action = max(counts, key=counts.get)
        dominant_ratio = counts[dominant_action] / total

        if dominant_ratio >= 0.90:
            logger.warning(
                f"[ACTION_IMBALANCE] Dominant action '{dominant_action}' accounts for {dominant_ratio*100:.1f}% of decisions."
            )
            return {
                "status": "ACTION_SELECTION_IMBALANCE",
                "dominant_action": dominant_action,
                "dominant_ratio": round(dominant_ratio, 4),
                "message": f"Action '{dominant_action}' selected in {dominant_ratio*100:.1f}% of recent inferences.",
            }

        return {
            "status": "BALANCED",
            "dominant_action": dominant_action,
            "dominant_ratio": round(dominant_ratio, 4),
        }
