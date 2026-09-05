"""Scheduled mandate retry / pre-debit notification for subscription recovery."""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase
    from app.models.subscription import Subscription


class MandateRetry(Base):
    """One planned re-attempt of a recurring debit, with its RBI pre-debit notification deadline."""

    __tablename__ = "mandate_retries"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subscription_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID, ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    notify_by: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="SCHEDULED", index=True)
    # SCHEDULED -> NOTIFIED -> EXECUTED -> SUCCEEDED | FAILED ; or HELD (issuer degraded) / CANCELLED
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_metadata: Mapped[Dict[str, Any]] = mapped_column("metadata", JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    recovery_case: Mapped["RecoveryCase"] = relationship("RecoveryCase")
    subscription: Mapped[Optional["Subscription"]] = relationship("Subscription")
