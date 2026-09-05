"""Model Registry Service for Versioning, Tracking, and Activation."""
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.model_version import ModelVersion
from app.ml.features import FeatureSchemaV1
from app.ml.pipeline import ModelPackage

logger = logging.getLogger(__name__)


class ModelRegistryService:
    """Manages model version lifecycles, validation gates, and activation transitions."""

    @classmethod
    def register_model(
        cls,
        db: Session,
        package: ModelPackage,
        artifact_path: str,
        training_started_at: datetime,
        training_completed_at: datetime,
        dataset_version: Optional[str] = None,
    ) -> ModelVersion:
        """Register a freshly trained model package into the Model Registry."""
        # Initial status: DEVELOPMENT_ONLY if synthetic, else VALIDATED
        initial_status = "DEVELOPMENT_ONLY" if package.dataset_type == "SYNTHETIC_DEMO" else "VALIDATED"

        model_record = ModelVersion(
            id=uuid.UUID(package.model_id),
            model_name=package.model_name,
            version=package.model_version,
            algorithm=package.model_type,
            model_type=package.model_type,
            dataset_type=package.dataset_type,
            dataset_version=dataset_version or f"{package.dataset_type.lower()}_v1",
            feature_schema_version=package.feature_schema_version,
            metrics=package.metrics,
            status=initial_status,
            artifact_path=artifact_path,
            training_started_at=training_started_at,
            training_completed_at=training_completed_at,
        )
        db.add(model_record)
        db.commit()
        db.refresh(model_record)

        logger.info(
            f"[MODEL_REGISTERED] Registered model {model_record.id} version={model_record.version} "
            f"status={model_record.status} auc={package.metrics.get('roc_auc')}"
        )
        return model_record

    @classmethod
    def activate_model(cls, db: Session, model_id: uuid.UUID) -> ModelVersion:
        """Promote a VALIDATED or DEVELOPMENT_ONLY model to ACTIVE status and retire previously active model."""
        model = db.scalar(select(ModelVersion).where(ModelVersion.id == model_id))
        if not model:
            raise ValueError(f"Model version {model_id} not found in registry.")

        # 1. Validation Pre-Flight Checks
        if model.status not in ("VALIDATED", "DEVELOPMENT_ONLY"):
            raise ValueError(
                f"Cannot activate model with status '{model.status}'. Must be in 'VALIDATED' or 'DEVELOPMENT_ONLY' status."
            )

        if not model.artifact_path or not Path(model.artifact_path).exists():
            raise FileNotFoundError(
                f"Cannot activate model {model_id}: Artifact file '{model.artifact_path}' does not exist."
            )

        if model.feature_schema_version != FeatureSchemaV1.SCHEMA_VERSION:
            raise ValueError(
                f"Feature schema mismatch: Model requires '{model.feature_schema_version}', system is '{FeatureSchemaV1.SCHEMA_VERSION}'"
            )

        if not model.metrics or "roc_auc" not in model.metrics:
            raise ValueError(f"Cannot activate model {model_id}: Missing required evaluation metrics.")

        # 2. Retire currently ACTIVE models for the same model_name
        db.execute(
            update(ModelVersion)
            .where(ModelVersion.model_name == model.model_name, ModelVersion.status == "ACTIVE")
            .values(status="RETIRED")
        )

        # 3. Promote selected model to ACTIVE
        model.status = "ACTIVE"
        model.deployed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(model)

        logger.info(f"[MODEL_ACTIVATED] Model {model.id} version={model.version} is now ACTIVE.")
        return model

    @classmethod
    def register_model_version(
        cls,
        db: Session,
        model_name: str,
        version: str,
        algorithm: str,
        feature_schema_version: str,
        metrics: Dict[str, Any],
        training_samples: int,
        artifact_path: str,
        status: str = "VALIDATED",
    ) -> ModelVersion:
        """Directly register a validated model version record."""
        now = datetime.now(timezone.utc)
        record = ModelVersion(
            id=uuid.uuid4(),
            model_name=model_name,
            version=version,
            algorithm=algorithm,
            model_type=algorithm,
            dataset_type="HISTORICAL_RECOVERY",
            dataset_version=f"{version}_dataset",
            feature_schema_version=feature_schema_version,
            metrics=metrics,
            status=status,
            artifact_path=artifact_path,
            training_started_at=now,
            training_completed_at=now,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record

    @classmethod
    def evaluate_and_promote(
        cls,
        db: Session,
        candidate_model_id: uuid.UUID,
        brier_threshold: float = 0.25,
    ) -> Dict[str, Any]:
        """Evaluate candidate model metrics against active model and promote only if quality criteria met."""
        candidate = db.scalar(select(ModelVersion).where(ModelVersion.id == candidate_model_id))
        if not candidate:
            raise ValueError(f"Candidate model '{candidate_model_id}' not found.")

        cand_metrics = candidate.metrics or {}
        cand_log_loss = float(cand_metrics.get("log_loss", 99.0))
        cand_brier = float(cand_metrics.get("brier_score", 99.0))

        # Check Brier score sanity threshold
        if cand_brier > brier_threshold:
            logger.warning(
                f"[PROMOTION_REJECTED] Candidate {candidate.id} Brier score {cand_brier} exceeds threshold {brier_threshold}."
            )
            return {
                "promoted": False,
                "reason": f"Brier score {cand_brier} exceeds quality threshold {brier_threshold}.",
                "candidate_metrics": cand_metrics,
            }

        # Compare against active model if present
        active = cls.get_active_model(db, model_name=candidate.model_name)
        if active and active.metrics:
            active_loss = float(active.metrics.get("log_loss", 99.0))
            if cand_log_loss > active_loss * 1.05:  # Cannot be > 5% worse in Log Loss
                logger.warning(
                    f"[PROMOTION_REJECTED] Candidate {candidate.id} Log Loss ({cand_log_loss}) is worse than active model ({active_loss})."
                )
                return {
                    "promoted": False,
                    "reason": f"Candidate Log Loss ({cand_log_loss}) is worse than active model ({active_loss}).",
                    "candidate_metrics": cand_metrics,
                    "active_metrics": active.metrics,
                }

        # Quality Gate passed -> Activate candidate model
        cls.activate_model(db, candidate.id)
        return {
            "promoted": True,
            "status": "ACTIVE",
            "model_version": candidate.version,
            "metrics": cand_metrics,
        }

    @classmethod
    def get_active_model(
        cls, db: Session, model_name: str = "action_recovery_model"
    ) -> Optional[ModelVersion]:
        """Fetch the currently active model record, if any."""
        # Check for action_recovery_model first, fallback to recovery_value_predictor
        model = db.scalar(
            select(ModelVersion)
            .where(ModelVersion.model_name == model_name, ModelVersion.status == "ACTIVE")
            .order_by(ModelVersion.deployed_at.desc())
            .limit(1)
        )
        if not model and model_name == "action_recovery_model":
            model = db.scalar(
                select(ModelVersion)
                .where(ModelVersion.model_name == "recovery_value_predictor", ModelVersion.status == "ACTIVE")
                .order_by(ModelVersion.deployed_at.desc())
                .limit(1)
            )
        return model

    @classmethod
    def list_models(cls, db: Session, limit: int = 50) -> List[ModelVersion]:
        """List model versions registered in the system."""
        return list(
            db.scalars(
                select(ModelVersion).order_by(ModelVersion.created_at.desc()).limit(limit)
            ).all()
        )
