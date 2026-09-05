"""Experiment arm assignment (treatment vs holdout) per recovery case.

The holdout arm is the control group that makes "money recovered" an incremental,
measurable number rather than an attribution guess.
"""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase


class ExperimentAssignment(Base):
    """Deterministic arm assignment for one recovery case within one experiment."""

    __tablename__ = "experiment_assignments"
    __table_args__ = (
        UniqueConstraint("recovery_case_id", "experiment_key", name="uq_experiment_assignment_case_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    experiment_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    arm: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # TREATMENT | HOLDOUT
    bucket: Mapped[int] = mapped_column(Integer, nullable=False)  # 0..9999 hash bucket
    holdout_bps: Mapped[int] = mapped_column(Integer, nullable=False)  # basis points of holdout at assignment time
    salt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    leak_surface: Mapped[str] = mapped_column(String(50), nullable=False, default="PAYMENT_FAILURE")
    assignment_metadata: Mapped[Dict[str, Any]] = mapped_column("metadata", JSON_TYPE, nullable=False, default=dict)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    recovery_case: Mapped["RecoveryCase"] = relationship("RecoveryCase", back_populates="experiment_assignment")
