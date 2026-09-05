"""Recovery outcome database model."""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Optional
from sqlalchemy import String, Numeric, Float, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase
    from app.models.recovery_action import RecoveryAction
    from app.models.execution import RecoveryExecution


class RecoveryOutcome(Base):
    """Immutable record of the business outcome resulting from a recovery case and intervention."""

    __tablename__ = "recovery_outcomes"

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
    outcome_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    attribution: Mapped[str] = mapped_column(
        String(50), nullable=False, default="UNKNOWN", index=True
    )
    amount_at_risk: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    amount_recovered: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    recovery_percentage: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    time_to_recovery_seconds: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    customer_response: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    provider_event_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    outcome_metadata: Mapped[Dict[str, Any]] = mapped_column(
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
        "RecoveryCase", back_populates="recovery_outcomes"
    )
    recovery_action: Mapped[Optional["RecoveryAction"]] = relationship(
        "RecoveryAction", back_populates="recovery_outcomes"
    )
    execution: Mapped[Optional["RecoveryExecution"]] = relationship(
        "RecoveryExecution", back_populates="recovery_outcomes"
    )
