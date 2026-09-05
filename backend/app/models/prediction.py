"""Prediction entity for action-level ML and heuristic inference records."""
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional
import uuid
from sqlalchemy import DateTime, Float, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, GUID, JSON_TYPE


class Prediction(Base):
    """Stores action-specific probability and expected recovered value predictions."""

    __tablename__ = "predictions"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("recovery_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("model_versions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    model_version: Mapped[str] = mapped_column(
        String(50), nullable=False, default="heuristic_v1"
    )
    feature_schema_version: Mapped[str] = mapped_column(
        String(50), nullable=False, default="v1"
    )
    action_type: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    predicted_probability: Mapped[float] = mapped_column(
        Float, nullable=False
    )
    expected_recovered_value: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    strategy: Mapped[str] = mapped_column(
        String(50), nullable=False, default="ML", index=True
    )
    contributing_factors: Mapped[Dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
