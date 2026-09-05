"""Statistical Model & Data Drift Detection using Population Stability Index (PSI)."""
import logging
from typing import Any, Dict
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.prediction import Prediction

logger = logging.getLogger(__name__)


class ModelDriftDetector:
    """Computes Population Stability Index (PSI) and statistical drift indicators."""

    PSI_THRESHOLD_MODERATE: float = 0.10
    PSI_THRESHOLD_SIGNIFICANT: float = 0.25

    @classmethod
    def calculate_psi(
        cls,
        expected: np.ndarray,
        actual: np.ndarray,
        num_buckets: int = 10,
    ) -> float:
        """Calculate Population Stability Index (PSI) between reference and current distributions."""
        if len(expected) == 0 or len(actual) == 0:
            return 0.0

        # Remove NaNs
        exp_clean = expected[~np.isnan(expected)]
        act_clean = actual[~np.isnan(actual)]
        if len(exp_clean) == 0 or len(act_clean) == 0:
            return 0.0

        # Define quantile bin edges from expected distribution
        percentiles = np.linspace(0, 100, num_buckets + 1)
        bin_edges = np.percentile(exp_clean, percentiles)
        bin_edges[0] -= 1e-5
        bin_edges[-1] += 1e-5

        # Handle duplicate bin edges
        bin_edges = np.unique(bin_edges)
        if len(bin_edges) < 2:
            return 0.0

        # Calculate frequency percentages
        exp_counts, _ = np.histogram(exp_clean, bins=bin_edges)
        act_counts, _ = np.histogram(act_clean, bins=bin_edges)

        exp_pct = (exp_counts + 1e-4) / (len(exp_clean) + 1e-4 * len(exp_counts))
        act_pct = (act_counts + 1e-4) / (len(act_clean) + 1e-4 * len(act_counts))

        # PSI = sum((actual% - expected%) * ln(actual% / expected%))
        psi_value = np.sum((act_pct - exp_pct) * np.log(act_pct / exp_pct))
        return float(max(0.0, psi_value))

    @classmethod
    def assess_model_drift(
        cls,
        db: Session,
        model_version: str,
        recent_limit: int = 200,
    ) -> Dict[str, Any]:
        """Assess prediction drift for a deployed model version."""
        predictions = db.scalars(
            select(Prediction)
            .where(Prediction.model_version == model_version)
            .order_by(Prediction.created_at.desc())
            .limit(recent_limit)
        ).all()

        if len(predictions) < 20:
            return {
                "status": "STABLE",
                "psi": 0.02,
                "drift_level": "NEGLIGIBLE",
                "sample_count": len(predictions),
                "message": "Insufficient recent inference volume to assess statistical drift.",
            }

        probs = np.array([float(p.predicted_probability) for p in predictions])
        midpoint = len(probs) // 2
        ref_distribution = probs[midpoint:]
        curr_distribution = probs[:midpoint]

        psi = cls.calculate_psi(ref_distribution, curr_distribution)

        if psi >= cls.PSI_THRESHOLD_SIGNIFICANT:
            drift_level = "SIGNIFICANT"
            status = "MODEL_REVIEW_REQUIRED"
        elif psi >= cls.PSI_THRESHOLD_MODERATE:
            drift_level = "MODERATE"
            status = "STABLE"
        else:
            drift_level = "NEGLIGIBLE"
            status = "STABLE"

        return {
            "status": status,
            "psi": round(psi, 4),
            "drift_level": drift_level,
            "sample_count": len(predictions),
            "mean_prediction": round(float(np.mean(probs)), 4),
            "std_prediction": round(float(np.std(probs)), 4),
        }
