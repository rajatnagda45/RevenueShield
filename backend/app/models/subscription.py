import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from sqlalchemy import String, Numeric, Integer, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.event import Event
    from app.models.recovery_case import RecoveryCase


class Subscription(Base):
    """Recurring subscription entity."""

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    external_subscription_id: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="USD"
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, default="ACTIVE"
    )
    current_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    current_period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    halted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # v2 mandate fields
    plan_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    charge_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    auth_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    remaining_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mandate_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # UPI_AUTOPAY | EMANDATE | CARD
    payment_method: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    short_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    notes: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    customer: Mapped["Customer"] = relationship(
        "Customer", back_populates="subscriptions"
    )
    events: Mapped[List["Event"]] = relationship(
        "Event", back_populates="subscription"
    )
    recovery_cases: Mapped[List["RecoveryCase"]] = relationship(
        "RecoveryCase", back_populates="subscription"
    )
