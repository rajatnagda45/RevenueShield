import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from sqlalchemy import Boolean, String, Numeric, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.event import Event
    from app.models.recovery_case import RecoveryCase


class Payment(Base):
    """Normalized payment transaction record."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    external_payment_id: Mapped[Optional[str]] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(
        String(255), index=True, nullable=True
    )
    razorpay_order_id: Mapped[Optional[str]] = mapped_column(
        String(255), index=True, nullable=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="INR"
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    payment_method: Mapped[str] = mapped_column(
        String(50), nullable=False, default="CARD"
    )
    bank: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    wallet: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    vpa: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    international: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    captured: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    amount_refunded: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00"), nullable=False
    )
    refund_status: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )
    description: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    failure_code: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    failure_description: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    error_source: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    error_step: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    error_reason: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    paid_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    razorpay_created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw_payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    customer: Mapped["Customer"] = relationship(
        "Customer", back_populates="payments"
    )
    events: Mapped[List["Event"]] = relationship(
        "Event", back_populates="payment"
    )
    recovery_cases: Mapped[List["RecoveryCase"]] = relationship(
        "RecoveryCase", back_populates="payment"
    )
