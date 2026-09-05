"""Learning dataset model for future predictive modeling."""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Optional
from sqlalchemy import Boolean, Integer, String, Numeric, Float, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase
    from app.models.recovery_action import RecoveryAction
    from app.models.execution import RecoveryExecution


class LearningExample(Base):
    """Historical observation containing point-in-time pre-decision features and outcome targets."""

    __tablename__ = "learning_examples"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("recovery_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recovery_action_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("recovery_actions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    execution_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("recovery_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    recovery_plan_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("recovery_plans.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    recovery_step_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("recovery_plan_steps.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    channel: Mapped[str] = mapped_column(
        String(50), nullable=False, default="EMAIL", index=True
    )
    model_version: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, index=True
    )
    feature_version: Mapped[str] = mapped_column(
        String(50), nullable=False, default="v1"
    )
    expected_recovery_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    prediction_error: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    training_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    training_exclusion_reason: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    environment_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="TEST", index=True
    )

    # ---------------- 1. Point-in-Time Pre-Decision Features ----------------
    diagnosis_category: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    diagnosis_confidence: Mapped[float] = mapped_column(
        Float, nullable=False
    )
    risk_score: Mapped[float] = mapped_column(
        Float, nullable=False
    )
    recovery_probability: Mapped[float] = mapped_column(
        Float, nullable=False
    )
    amount_at_risk: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    case_age_at_decision_hours: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    customer_success_rate_at_decision: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    customer_failure_count_at_decision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    previous_recovery_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    payment_method: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )
    bank: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )
    action_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    decision_score: Mapped[float] = mapped_column(
        Float, nullable=False
    )
    decision_confidence: Mapped[float] = mapped_column(
        Float, nullable=False
    )
    policy_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    feature_snapshot: Mapped[Dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )

    # ---------------- 2. Outcome Realization Targets (Filled on Finalization) ----------------
    outcome_type: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, index=True
    )
    amount_recovered: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    recovery_percentage: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    time_to_recovery_seconds: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    attribution: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, index=True
    )
    label: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True
    )  # 1 = Attributable Recovery, 0 = Non-recovery / Organic
    is_finalized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    finalized_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    recovery_case: Mapped["RecoveryCase"] = relationship(
        "RecoveryCase", back_populates="learning_examples"
    )
    recovery_action: Mapped[Optional["RecoveryAction"]] = relationship(
        "RecoveryAction", back_populates="learning_examples"
    )
    execution: Mapped[Optional["RecoveryExecution"]] = relationship(
        "RecoveryExecution", back_populates="learning_examples"
    )
