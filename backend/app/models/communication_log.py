import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from sqlalchemy import String, Text, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.recovery_case import RecoveryCase


class CommunicationLog(Base):
    """Customer interaction logs across email, SMS, WhatsApp, and voice channels."""

    __tablename__ = "communication_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("customers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    recovery_case_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("recovery_cases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    channel: Mapped[str] = mapped_column(
        String(50), nullable=False
    )
    direction: Mapped[str] = mapped_column(
        String(20), nullable=False, default="OUTBOUND"
    )
    provider_message_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    content: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="SENT"
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    customer: Mapped["Customer"] = relationship(
        "Customer", back_populates="communication_logs"
    )
    recovery_case: Mapped[Optional["RecoveryCase"]] = relationship(
        "RecoveryCase", back_populates="communication_logs"
    )
