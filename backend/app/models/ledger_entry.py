"""Append-only recovery ledger.

Every rupee the system tracks moves through here: amount put at risk when a case opens,
captured recoveries, refunds, settlements (money actually in the merchant's bank) and the
cost of each outreach. The scorecard is computed from this table, not from mutable case fields.
"""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, Optional

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase


class LedgerEntry(Base):
    """Immutable money movement attached to a recovery case."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint(
            "recovery_case_id", "entry_type", "provider_reference",
            name="uq_ledger_case_type_reference",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entry_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    channel: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, index=True)
    provider_reference: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    entry_metadata: Mapped[Dict[str, Any]] = mapped_column("metadata", JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    recovery_case: Mapped["RecoveryCase"] = relationship("RecoveryCase", back_populates="ledger_entries")
