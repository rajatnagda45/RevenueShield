"""Closed-Loop Self-Learning Dataset Builder filtering strictly for training-eligible records."""
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.learning import LearningExample
from app.ml.features import validate_point_in_time_features

logger = logging.getLogger(__name__)


class SelfLearningDatasetBuilder:
    """Builds machine learning datasets strictly from validated, training-eligible learning examples."""

    @classmethod
    def build_dataset(
        cls,
        db: Session,
        environment_type: Optional[str] = None,
        min_samples: int = 50,
    ) -> pd.DataFrame:
        """Extract training-eligible learning examples, enforcing zero leakage and exclusion of human overrides."""
        query = select(LearningExample).where(
            LearningExample.training_eligible == True,
            LearningExample.is_finalized == True,
        )

        if environment_type:
            query = query.where(LearningExample.environment_type == environment_type)

        examples = db.scalars(query.order_by(LearningExample.created_at.asc())).all()

        rows: List[Dict[str, Any]] = []
        for ex in examples:
            amount = float(ex.amount_at_risk or 0.0)
            target = int(ex.label if ex.label is not None else (1 if ex.outcome_type == "RECOVERED" else 0))

            row = {
                "case_id": str(ex.recovery_case_id),
                "amount_at_risk": amount,
                "log_amount": float(np.log1p(max(0.0, amount))),
                "case_age_at_decision_hours": float(ex.case_age_at_decision_hours or 0.0),
                "diagnosis_category": str(ex.diagnosis_category or "UNKNOWN"),
                "diagnosis_confidence": float(ex.diagnosis_confidence or 0.50),
                "risk_score": float(ex.risk_score or 50.0),
                "heuristic_recovery_probability": float(ex.recovery_probability or 0.50),
                "customer_success_rate": float(ex.customer_success_rate_at_decision or 0.0),
                "customer_success_count": 1,
                "customer_failure_count": int(ex.customer_failure_count_at_decision or 0),
                "previous_recovery_attempts": int(ex.previous_recovery_attempts or 0),
                "payment_method": str(ex.payment_method or "CARD"),
                "bank": str(ex.bank or "UNKNOWN"),
                "action_type": str(ex.action_type or "EMAIL_PAYMENT_RECOVERY"),
                "decision_score": float(ex.decision_score or 0.50),
                "decision_confidence": float(ex.decision_confidence or 0.70),
                "created_at": ex.created_at.isoformat() if ex.created_at else datetime.now(timezone.utc).isoformat(),
                "recovered": target,
            }

            try:
                validate_point_in_time_features(row)
                rows.append(row)
            except ValueError as ve:
                logger.warning(f"[LEAKAGE_SKIPPED] Example {ex.id} rejected by anti-leakage filter: {ve}")
                continue

        df = pd.DataFrame(rows)
        logger.info(f"[DATASET_EXTRACTED] Gathered {len(df)} eligible learning rows (Env: {environment_type or 'ALL'}).")
        return df
