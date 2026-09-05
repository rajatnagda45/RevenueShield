import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Optional
from sqlalchemy import String, Numeric, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.recovery_action import RecoveryAction
    from app.models.recovery_case import RecoveryCase


class ActionOutcome(Base):
    """Outcome and feedback recorded from a recovery action."""

    __tablename__ = "action_outcomes"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    action_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("recovery_actions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("recovery_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    outcome_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    recovered_amount: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    response_data: Mapped[Dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    action: Mapped["RecoveryAction"] = relationship(
        "RecoveryAction", back_populates="outcomes"
    )
    recovery_case: Mapped["RecoveryCase"] = relationship(
        "RecoveryCase", back_populates="action_outcomes"
    )
