import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional
from sqlalchemy import Float, String, Numeric, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase
    from app.models.customer import Customer


class PromiseToPay(Base):
    """Customer commitment to pay outstanding dues by a specific date."""

    __tablename__ = "promise_to_pays"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("recovery_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    amount_due: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    promised_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    promised_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    promised_time: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True, default="17:00"
    )
    currency: Mapped[str] = mapped_column(
        String(10), nullable=False, default="INR"
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, default="ACTIVE"
    )  # ACTIVE, FULFILLED, MISSED, CANCELLED, EXPIRED
    source: Mapped[str] = mapped_column(
        String(50), nullable=False, default="CUSTOMER"
    )  # CUSTOMER, AGENT, OPERATOR, SYSTEM
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0
    )
    notes: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    fulfilled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    recovery_case: Mapped["RecoveryCase"] = relationship(
        "RecoveryCase", back_populates="promise_to_pays"
    )
    customer: Mapped["Customer"] = relationship(
        "Customer", back_populates="promise_to_pays"
    )
