import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from sqlalchemy import String, Text, Float, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase
    from app.models.action_outcome import ActionOutcome
    from app.models.execution import RecoveryExecution
    from app.models.outcome import RecoveryOutcome
    from app.models.learning import LearningExample


class RecoveryAction(Base):
    """Action planned, recommended, approved, blocked, or executed to recover revenue."""

    __tablename__ = "recovery_actions"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("recovery_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(
        String(50), nullable=False, default="SYSTEM"
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, default="RECOMMENDED"
    )
    reason: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    confidence: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    decision_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    decision_confidence: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    decision_engine_version: Mapped[str] = mapped_column(
        String(50), nullable=False, default="decision_engine_v1"
    )
    policy_engine_version: Mapped[str] = mapped_column(
        String(50), nullable=False, default="policy_engine_v1"
    )
    policy_result: Mapped[Dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )
    alternatives: Mapped[List[Dict[str, Any]]] = mapped_column(
        JSON_TYPE, nullable=False, default=list
    )
    supporting_factors: Mapped[List[str]] = mapped_column(
        JSON_TYPE, nullable=False, default=list
    )
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    executed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Relationships
    recovery_case: Mapped["RecoveryCase"] = relationship(
        "RecoveryCase", back_populates="recovery_actions"
    )
    outcomes: Mapped[List["ActionOutcome"]] = relationship(
        "ActionOutcome", back_populates="action", cascade="all, delete-orphan"
    )
    executions: Mapped[List["RecoveryExecution"]] = relationship(
        "RecoveryExecution", back_populates="recovery_action", cascade="all, delete-orphan"
    )
    recovery_outcomes: Mapped[List["RecoveryOutcome"]] = relationship(
        "RecoveryOutcome", back_populates="recovery_action"
    )
    learning_examples: Mapped[List["LearningExample"]] = relationship(
        "LearningExample", back_populates="recovery_action"
    )
