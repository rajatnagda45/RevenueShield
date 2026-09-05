import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional
from sqlalchemy import String, Text, Float, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase


class Diagnosis(Base):
    """Root cause diagnosis generated for a recovery case."""

    __tablename__ = "diagnoses"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("recovery_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, default="UNKNOWN"
    )
    failure_code: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    explanation: Mapped[str] = mapped_column(
        Text, nullable=False
    )
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    evidence: Mapped[Dict[str, Any]] = mapped_column(
        JSON_TYPE, nullable=False, default=dict
    )
    risk_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    recovery_probability: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    engine_version: Mapped[str] = mapped_column(
        String(50), nullable=False, default="diagnosis_engine_v1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    recovery_case: Mapped["RecoveryCase"] = relationship(
        "RecoveryCase", back_populates="diagnoses"
    )

    def __init__(self, **kwargs: Any) -> None:
        if "root_cause" in kwargs and "explanation" not in kwargs:
            kwargs["explanation"] = kwargs.pop("root_cause")
        if "indicators" in kwargs and "evidence" not in kwargs:
            kwargs["evidence"] = kwargs.pop("indicators")
        kwargs.pop("recommended_action", None)
        kwargs.pop("recommended_channel", None)
        super().__init__(**kwargs)
