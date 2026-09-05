"""Model Rollback Service reverting active deployments to previous champion model versions."""
from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.model_version import ModelVersion

logger = logging.getLogger(__name__)


class ModelRollbackService:
    """Safely rolls back production models to previously validated champion checkpoints."""

    @classmethod
    def rollback_to_previous_model(
        cls,
        db: Session,
        model_name: str = "action_recovery_model",
        target_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Roll back active model to target version or the most recently retired champion."""
        # 1. Fetch Currently Active Model
        current_active = db.scalar(
            select(ModelVersion)
            .where(ModelVersion.model_name == model_name, ModelVersion.status == "ACTIVE")
            .order_by(desc(ModelVersion.deployed_at))
            .limit(1)
        )

        # 2. Find Target Rollback Candidate
        if target_version:
            target_model = db.scalar(
                select(ModelVersion).where(
                    ModelVersion.model_name == model_name,
                    ModelVersion.version == target_version,
                )
            )
        else:
            # Find most recent RETIRED or VALIDATED model
            target_model = db.scalar(
                select(ModelVersion)
                .where(
                    ModelVersion.model_name == model_name,
                    ModelVersion.status.in_(["RETIRED", "VALIDATED", "DEVELOPMENT_ONLY"]),
                )
                .order_by(desc(ModelVersion.created_at))
                .limit(1)
            )

        if not target_model:
            raise ValueError("No eligible prior model version found to perform rollback.")

        current_version_str = current_active.version if current_active else "NONE"

        # 3. Retire Current Active Model
        if current_active:
            current_active.status = "RETIRED"
            db.add(current_active)

        # 4. Activate Target Model
        target_model.status = "ACTIVE"
        target_model.deployed_at = datetime.now(timezone.utc)
        db.add(target_model)

        # 5. Emit Audit Log
        db.add(
            AuditLog(
                actor_type="SYSTEM",
                actor_id="MODEL_ROLLBACK_SERVICE",
                action="MODEL_ROLLBACK",
                entity_type="MODEL",
                entity_id=target_model.version,
                audit_metadata={
                    "previous_active_version": current_version_str,
                    "restored_version": target_model.version,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        )
        db.commit()
        db.refresh(target_model)

        logger.warning(
            f"[MODEL_ROLLBACK] Successfully rolled back from '{current_version_str}' to '{target_model.version}'."
        )

        return {
            "status": "ROLLBACK_SUCCESSFUL",
            "previous_version": current_version_str,
            "active_version": target_model.version,
            "metrics": target_model.metrics,
        }
