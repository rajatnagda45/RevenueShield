"""Synchronization Checkpoint Model.

Tracks batch and historical API synchronization jobs, execution metrics,
date windows, and status.
"""
from datetime import datetime
from typing import Any, Dict, Optional
import uuid
from sqlalchemy import String, Integer, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, GUID


class SyncCheckpoint(Base):
    """Execution ledger and checkpoint for external payment synchronization."""

    __tablename__ = "sync_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    source: Mapped[str] = mapped_column(
        String(50), nullable=False, default="RAZORPAY_API"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    from_timestamp: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    to_timestamp: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    records_fetched: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    records_created: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    records_updated: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="RUNNING", index=True
    )  # RUNNING, SUCCEEDED, PARTIAL, FAILED
    error_message: Mapped[Optional[str]] = mapped_column(
        String(1000), nullable=True
    )
    sync_metadata: Mapped[Optional[Dict[str, Any]]] = mapped_column(
        JSONB().with_variant(JSON, "sqlite"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
