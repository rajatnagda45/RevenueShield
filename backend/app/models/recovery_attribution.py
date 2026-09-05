"""Recovery Attribution Model tracking multi-touch and single-touch recovery credit."""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional
from sqlalchemy import DateTime, Float, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase


class RecoveryAttribution(Base):
    """Stores deterministic credit attribution for recovered payments across intervention touchpoints."""

    __tablename__ = "recovery_attributions"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recovery_step_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID, ForeignKey("recovery_plan_steps.id", ondelete="SET NULL"), nullable=True, index=True
    )
    learning_example_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID, ForeignKey("learning_examples.id", ondelete="SET NULL"), nullable=True, index=True
    )

    amount_recovered: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    attribution_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="PRIMARY", index=True
    )  # PRIMARY, SECONDARY, UNCERTAIN
    attribution_weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    # Relationships
    recovery_case: Mapped["RecoveryCase"] = relationship(
        "RecoveryCase", back_populates="recovery_attributions"
    )
