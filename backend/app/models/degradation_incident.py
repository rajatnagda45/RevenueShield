"""Portfolio-level payment degradation incident (issuer / PSP outage)."""
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, GUID, JSON_TYPE


class DegradationIncident(Base):
    """A detected spike in failure rate for one (bank, method) cell of the payment matrix."""

    __tablename__ = "degradation_incidents"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    bank: Mapped[str] = mapped_column(String(100), nullable=False, index=True)          # "UNKNOWN" when the gateway did not report one
    payment_method: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="SUSPECTED", index=True)
    # SUSPECTED -> CONFIRMED -> RECOVERING -> CLOSED
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    recovering_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_evaluated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    window_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    observed_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observed_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observed_failure_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    baseline_failure_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    z_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    consecutive_detections: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    consecutive_clean: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    affected_case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    incident_metadata: Mapped[Dict[str, Any]] = mapped_column("metadata", JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
