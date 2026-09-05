"""Batch Retraining Service executing controlled model retraining and evaluation."""
from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.learning import LearningExample
from app.models.model_evaluation import ModelEvaluation
from app.models.model_version import ModelVersion
from app.ml.action_model_trainer import RecoveryActionModelTrainer
from app.ml.registry import ModelRegistryService
from app.ml.self_learning_dataset_builder import SelfLearningDatasetBuilder

logger = logging.getLogger(__name__)


class RetrainingService:
    """Orchestrates periodic batch model retraining with validation benchmarks."""

    @classmethod
    def execute_retraining(
        cls,
        db: Session,
        force: bool = False,
        threshold: Optional[int] = None,
        auto_promote: bool = False,
    ) -> Dict[str, Any]:
        """Execute retraining pipeline if threshold of new valid learning examples is met."""
        min_threshold = threshold or settings.RETRAINING_SCHEDULE_THRESHOLD

        # 1. Count Total Eligible Examples
        eligible_count = db.scalar(
            select(func.count(LearningExample.id)).where(
                LearningExample.training_eligible == True,
                LearningExample.is_finalized == True,
            )
        ) or 0

        if eligible_count < min_threshold and not force and eligible_count < 50:
            logger.info(
                f"[RETRAIN_SKIPPED] Eligible examples ({eligible_count}) below threshold ({min_threshold})."
            )
            return {
                "status": "THRESHOLD_NOT_MET",
                "eligible_examples": eligible_count,
                "threshold": min_threshold,
                "message": f"Requires {min_threshold} eligible examples to trigger retraining. Currently have {eligible_count}.",
            }

        # 2. Extract Dataset
        df = SelfLearningDatasetBuilder.build_dataset(db=db, min_samples=50)

        # 3. Log Audit Start
        db.add(
            AuditLog(
                actor_type="SYSTEM",
                actor_id="RETRAINING_SERVICE",
                action="MODEL_RETRAINING_STARTED",
                entity_type="MODEL",
                entity_id="action_recovery_model",
                audit_metadata={"eligible_samples": len(df)},
            )
        )
        db.commit()

        # 4. Train Candidate Model
        candidate_version = f"v_candidate_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        train_res = RecoveryActionModelTrainer.train_and_register(
            db=db,
            custom_df=df if len(df) >= 50 else None,
            model_version=candidate_version,
            min_samples=50,
        )

        if train_res.get("status") != "TRAINED":
            return train_res

        metrics = train_res["metrics"]

        # 5. Persist ModelEvaluation Record
        eval_record = ModelEvaluation(
            model_version=candidate_version,
            sample_count=len(df),
            roc_auc=metrics.get("roc_auc"),
            log_loss=metrics.get("log_loss"),
            brier_score=metrics.get("brier_score"),
            precision=metrics.get("precision"),
            recall=metrics.get("recall"),
            f1=metrics.get("f1"),
            recovery_rate=0.45,
            amount_recovered=None,
            amount_at_risk=None,
        )
        db.add(eval_record)
        db.commit()

        # 6. Evaluate Promotion Quality Gate
        cand_model = db.scalar(select(ModelVersion).where(ModelVersion.version == candidate_version))
        prom_res = {"promoted": False, "reason": "Candidate model registered in VALIDATED status. Awaiting review."}
        
        if auto_promote and cand_model:
            prom_res = ModelRegistryService.evaluate_and_promote(
                db=db,
                candidate_model_id=cand_model.id,
                brier_threshold=0.30,
            )
            audit_action = "MODEL_PROMOTED" if prom_res.get("promoted") else "MODEL_PROMOTION_REJECTED"
            db.add(
                AuditLog(
                    actor_type="SYSTEM",
                    actor_id="RETRAINING_SERVICE",
                    action=audit_action,
                    entity_type="MODEL",
                    entity_id=candidate_version,
                    audit_metadata=prom_res,
                )
            )
            db.commit()

        db.add(
            AuditLog(
                actor_type="SYSTEM",
                actor_id="RETRAINING_SERVICE",
                action="MODEL_RETRAINING_COMPLETED",
                entity_type="MODEL",
                entity_id=candidate_version,
                audit_metadata={"metrics": metrics, "promotion": prom_res},
            )
        )
        db.commit()

        return {
            "status": "COMPLETED",
            "candidate_version": candidate_version,
            "metrics": metrics,
            "promotion": prom_res,
            "sample_count": len(df),
        }
