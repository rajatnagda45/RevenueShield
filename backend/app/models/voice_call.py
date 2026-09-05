"""Voice call tracking and lifecycle model for automated voice recovery."""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional
from sqlalchemy import String, Integer, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.recovery_case import RecoveryCase


class VoiceCall(Base):
    """Auditable voice call record for automated recovery calls."""

    __tablename__ = "voice_calls"

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
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, default="TWILIO"
    )
    provider_call_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    from_number: Mapped[str] = mapped_column(
        String(50), nullable=False
    )
    to_number: Mapped[str] = mapped_column(
        String(50), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="QUEUED", index=True
    )  # QUEUED, RINGING, IN_PROGRESS, COMPLETED, FAILED, NO_ANSWER
    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    outcome: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    dynamic_variables: Mapped[Dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )
    call_metadata: Mapped[Dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    recovery_case: Mapped["RecoveryCase"] = relationship(
        "RecoveryCase", back_populates="voice_calls"
    )
    customer: Mapped[Optional["Customer"]] = relationship(
        "Customer", back_populates="voice_calls"
    )
