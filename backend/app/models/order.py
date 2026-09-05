"""Razorpay Order (checkout) entity used for abandonment detection."""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Optional

from sqlalchemy import DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.recovery_case import RecoveryCase


class Order(Base):
    """A checkout intent. Status mirrors Razorpay (created / attempted / paid) plus our ABANDONED marker."""

    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    external_order_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID, ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="CREATED", index=True)
    receipt: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    notes: Mapped[Dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    razorpay_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_payment_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    abandonment_case_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID, ForeignKey("recovery_cases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    customer: Mapped[Optional["Customer"]] = relationship("Customer")
    abandonment_case: Mapped[Optional["RecoveryCase"]] = relationship("RecoveryCase", foreign_keys=[abandonment_case_id])
