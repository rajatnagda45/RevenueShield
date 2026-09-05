"""Model Evaluation Model tracking out-of-time benchmark metrics and business impacts."""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional
from sqlalchemy import DateTime, Float, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, GUID


class ModelEvaluation(Base):
    """Stores periodic offline/out-of-time evaluation runs for candidate and active model versions."""

    __tablename__ = "model_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    model_version: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    evaluation_period_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    evaluation_period_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    roc_auc: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    log_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    brier_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    precision: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    recall: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    f1: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    recovery_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    amount_recovered: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    amount_at_risk: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
