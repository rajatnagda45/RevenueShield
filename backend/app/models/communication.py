"""Customer communication tracking model across WhatsApp, SMS, and messaging channels."""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional
from sqlalchemy import String, Text, Boolean, Integer, DateTime, ForeignKey, func, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.recovery_case import RecoveryCase


class Communication(Base):
    """Auditable communication record for individual customer outreach attempts."""

    __tablename__ = "communications"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_communications_idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("recovery_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    channel: Mapped[str] = mapped_column(
        String(50), nullable=False, default="WHATSAPP", index=True
    )
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, default="DEVELOPMENT"
    )
    template_name: Mapped[str] = mapped_column(
        String(100), nullable=False, default="PAYMENT_RECOVERY_EN_V1"
    )
    template_version: Mapped[str] = mapped_column(
        String(20), nullable=False, default="v1.0"
    )
    language: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ENGLISH"
    )
    recipient_reference: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    recipient_masked: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    message_body: Mapped[str] = mapped_column(
        Text, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="GENERATED", index=True
    )
    provider_message_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    is_simulated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    failure_reason: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    communication_metadata: Mapped[Dict[str, Any]] = mapped_column(
        "metadata", JSON_TYPE, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    recovery_case: Mapped["RecoveryCase"] = relationship(
        "RecoveryCase", back_populates="communications"
    )
    customer: Mapped[Optional["Customer"]] = relationship(
        "Customer", back_populates="communications"
    )
