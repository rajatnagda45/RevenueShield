"""Recovery execution ledger model."""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from sqlalchemy import String, Text, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase
    from app.models.recovery_action import RecoveryAction
    from app.models.outcome import RecoveryOutcome
    from app.models.learning import LearningExample


class RecoveryExecution(Base):
    """Immutable execution record representing an attempt to execute a recovery action."""

    __tablename__ = "recovery_executions"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("recovery_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recovery_action_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("recovery_actions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, default="PENDING"
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    provider_reference: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    provider_url: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    execution_metadata: Mapped[Dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Relationships
    recovery_case: Mapped["RecoveryCase"] = relationship(
        "RecoveryCase", back_populates="executions"
    )
    recovery_action: Mapped["RecoveryAction"] = relationship(
        "RecoveryAction", back_populates="executions"
    )
    recovery_outcomes: Mapped[List["RecoveryOutcome"]] = relationship(
        "RecoveryOutcome", back_populates="execution"
    )
    learning_examples: Mapped[List["LearningExample"]] = relationship(
        "LearningExample", back_populates="execution"
    )
