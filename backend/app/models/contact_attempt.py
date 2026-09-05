"""Unified cross-channel contact registry used for frequency caps and compliance reporting."""
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, GUID, JSON_TYPE


class ContactAttempt(Base):
    """One customer touch (or a blocked attempt) across any channel."""

    __tablename__ = "contact_attempts"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    recovery_case_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID, ForeignKey("recovery_cases.id", ondelete="SET NULL"), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # SENT | BLOCKED
    blocking_rule: Mapped[Optional[str]] = mapped_column(String(60), nullable=True, index=True)
    reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)   # communication / call / link id
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    attempt_metadata: Mapped[Dict[str, Any]] = mapped_column("metadata", JSON_TYPE, nullable=False, default=dict)
