"""Recovery Payment Link Entity."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional
import uuid
from sqlalchemy import DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase
    from app.models.intervention import Intervention


class RecoveryPaymentLink(Base):
    """Tracks payment links created for revenue recovery cases."""

    __tablename__ = "recovery_payment_links"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("recovery_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    intervention_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("interventions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    razorpay_payment_link_id: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    payment_url: Mapped[str] = mapped_column(
        String(500), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    currency: Mapped[str] = mapped_column(
        String(10), nullable=False, default="INR"
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, default="CREATED"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paid_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(
        String(255), index=True, nullable=True
    )

    # Relationships
    recovery_case: Mapped["RecoveryCase"] = relationship(
        "RecoveryCase", back_populates="payment_links"
    )
    intervention: Mapped[Optional["Intervention"]] = relationship(
        "Intervention", back_populates="payment_links"
    )
