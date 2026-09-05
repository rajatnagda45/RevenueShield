"""Consent ledger: every opt-in / opt-out a customer expresses, per channel, with provenance."""
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, GUID, JSON_TYPE


class ConsentRecord(Base):
    """Append-only consent event. The latest record per (customer, channel) is authoritative; ALL covers every channel."""

    __tablename__ = "consent_records"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # WHATSAPP | SMS | EMAIL | VOICE | ALL
    status: Mapped[str] = mapped_column(String(10), nullable=False, index=True)   # OPT_IN | OPT_OUT
    source: Mapped[str] = mapped_column(String(40), nullable=False)               # CUSTOMER_KEYWORD | VOICE_INTENT | OPERATOR | IMPORT | DND_REGISTRY | SYSTEM
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recovery_case_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID, ForeignKey("recovery_cases.id", ondelete="SET NULL"), nullable=True, index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    consent_metadata: Mapped[Dict[str, Any]] = mapped_column("metadata", JSON_TYPE, nullable=False, default=dict)

    customer = relationship("Customer")
