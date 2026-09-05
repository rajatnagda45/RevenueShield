"""Model Registry Entity."""
from datetime import datetime
from typing import Any, Dict, Optional
import uuid
from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, GUID, JSON_TYPE


class ModelVersion(Base):
    """Registry entity for machine learning models and versions."""

    __tablename__ = "model_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    model_name: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    version: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    algorithm: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    model_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="LOGISTIC_REGRESSION"
    )
    dataset_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="REAL"
    )
    dataset_version: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    feature_schema_version: Mapped[str] = mapped_column(
        String(50), nullable=False, default="v1"
    )
    metrics: Mapped[Dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, default="TRAINED"
    )
    artifact_path: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    training_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    training_completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deployed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __init__(self, **kwargs: Any) -> None:
        if "training_dataset_version" in kwargs and "dataset_version" not in kwargs:
            kwargs["dataset_version"] = kwargs.pop("training_dataset_version")
        super().__init__(**kwargs)
