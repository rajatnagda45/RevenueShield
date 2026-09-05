import uuid
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional
from sqlalchemy import String, Boolean, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID

if TYPE_CHECKING:
    from app.models.payment import Payment
    from app.models.subscription import Subscription
    from app.models.invoice import Invoice
    from app.models.event import Event
    from app.models.recovery_case import RecoveryCase
    from app.models.communication_log import CommunicationLog
    from app.models.communication import Communication
    from app.models.promise_to_pay import PromiseToPay
    from app.models.voice_call import VoiceCall


class Customer(Base):
    """Customer entity representing a subscriber or account holder."""

    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    external_customer_id: Mapped[Optional[str]] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    segment: Mapped[str] = mapped_column(
        String(50), nullable=False, default="STANDARD"
    )
    preferred_channel: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )
    dnd_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    whatsapp_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    marketing_opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    transactional_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    timezone: Mapped[str] = mapped_column(
        String(50), nullable=False, default="Asia/Kolkata"
    )
    preferred_language: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ENGLISH"
    )
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
    payments: Mapped[List["Payment"]] = relationship(
        "Payment", back_populates="customer"
    )
    subscriptions: Mapped[List["Subscription"]] = relationship(
        "Subscription", back_populates="customer"
    )
    invoices: Mapped[List["Invoice"]] = relationship(
        "Invoice", back_populates="customer"
    )
    events: Mapped[List["Event"]] = relationship(
        "Event", back_populates="customer"
    )
    recovery_cases: Mapped[List["RecoveryCase"]] = relationship(
        "RecoveryCase", back_populates="customer"
    )
    communication_logs: Mapped[List["CommunicationLog"]] = relationship(
        "CommunicationLog", back_populates="customer"
    )
    communications: Mapped[List["Communication"]] = relationship(
        "Communication", back_populates="customer"
    )
    promise_to_pays: Mapped[List["PromiseToPay"]] = relationship(
        "PromiseToPay", back_populates="customer"
    )
    voice_calls: Mapped[List["VoiceCall"]] = relationship(
        "VoiceCall", back_populates="customer"
    )
