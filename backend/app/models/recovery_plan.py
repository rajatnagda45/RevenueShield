"""RecoveryPlan and RecoveryPlanStep database models for adaptive, bounded recovery orchestration."""
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional
import uuid
from sqlalchemy import String, Text, Integer, Float, Numeric, DateTime, ForeignKey, func, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase


class RecoveryPlan(Base):
    """Adaptive, bounded recovery plan for a single recovery case."""

    __tablename__ = "recovery_plans"
    __table_args__ = (
        UniqueConstraint("recovery_case_id", name="uq_recovery_plans_recovery_case_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("recovery_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="ACTIVE", index=True
    )  # ACTIVE, WAITING, EVALUATING, EXECUTING, RECOVERED, PAUSED, EXPIRED, COMPLETED, CANCELLED
    current_step: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    max_steps: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3
    )
    next_evaluation_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    completion_reason: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    recovery_case: Mapped["RecoveryCase"] = relationship(
        "RecoveryCase", back_populates="recovery_plan", foreign_keys=[recovery_case_id]
    )
    steps: Mapped[List["RecoveryPlanStep"]] = relationship(
        "RecoveryPlanStep",
        back_populates="recovery_plan",
        cascade="all, delete-orphan",
        order_by="RecoveryPlanStep.step_number",
    )


class RecoveryPlanStep(Base):
    """Individual execution step within a RecoveryPlan."""

    __tablename__ = "recovery_plan_steps"
    __table_args__ = (
        UniqueConstraint("recovery_plan_id", "step_number", name="uq_recovery_plan_steps_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    recovery_plan_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("recovery_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_number: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    action_type: Mapped[str] = mapped_column(
        String(100), nullable=False
    )  # EMAIL_PAYMENT_RECOVERY, EMAIL_FOLLOWUP, NO_ACTION, etc.
    channel: Mapped[str] = mapped_column(
        String(50), nullable=False, default="EMAIL"
    )  # EMAIL, WHATSAPP, VOICE, NONE
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="PENDING", index=True
    )  # PENDING, SCHEDULED, RUNNING, COMPLETED, SKIPPED, BLOCKED, CANCELLED
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    executed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    prediction_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    expected_recovery_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    step_metadata: Mapped[Dict[str, Any]] = mapped_column(
        "metadata", JSON_TYPE, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    recovery_plan: Mapped["RecoveryPlan"] = relationship(
        "RecoveryPlan", back_populates="steps"
    )
