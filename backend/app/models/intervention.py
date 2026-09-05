"""Intervention Entity."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional
import uuid
from sqlalchemy import DateTime, Float, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase
    from app.models.recovery_payment_link import RecoveryPaymentLink
    from app.models.prediction import Prediction
    from app.models.recovery_action import RecoveryAction


class Intervention(Base):
    """Auditable record of a recovery intervention attempt."""

    __tablename__ = "interventions"

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
        String(50), nullable=False, index=True, default="SEND_PAYMENT_LINK"
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, default="PENDING"
    )
    reason: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    prediction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("predictions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    policy_decision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("recovery_actions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    predicted_probability: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    expected_recovered_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    idempotency_key: Mapped[Optional[str]] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_reason: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    recovery_case: Mapped["RecoveryCase"] = relationship(
        "RecoveryCase", back_populates="interventions"
    )
    payment_links: Mapped[List["RecoveryPaymentLink"]] = relationship(
        "RecoveryPaymentLink", back_populates="intervention", cascade="all, delete-orphan"
    )
    prediction: Mapped[Optional["Prediction"]] = relationship("Prediction")
    recovery_action: Mapped[Optional["RecoveryAction"]] = relationship("RecoveryAction")
