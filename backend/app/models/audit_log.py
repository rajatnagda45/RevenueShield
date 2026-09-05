import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional
from sqlalchemy import BigInteger, String, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base, GUID, JSON_TYPE

if TYPE_CHECKING:
    from app.models.recovery_case import RecoveryCase


class AuditLog(Base):
    """Immutable audit trail for all system, AI, and human actions."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID, primary_key=True, default=uuid.uuid4
    )
    recovery_case_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID,
        ForeignKey("recovery_cases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    actor_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    action: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    # Use column name 'metadata' with attribute name 'audit_metadata' to avoid conflict with Base.metadata
    audit_metadata: Mapped[Dict[str, Any]] = mapped_column(
        "metadata", JSON_TYPE, nullable=False, default=dict
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    # Tamper-evident hash chain (v2). NULL on rows written before the chain existed.
    sequence: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, unique=True)
    prev_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    row_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Relationships
    recovery_case: Mapped[Optional["RecoveryCase"]] = relationship(
        "RecoveryCase", back_populates="audit_logs"
    )
