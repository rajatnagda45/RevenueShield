"""Agent run: one planner evaluation of one case, with its full trace."""
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, GUID, JSON_TYPE


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    recovery_case_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(60), nullable=False, default="n/a")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="RUNNING", index=True)
    # RUNNING | EXECUTED | AWAITING_APPROVAL | BLOCKED | HANDOFF | NO_ACTION | FAILED
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    degraded_to_rules: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dossier_hash: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    dossier: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=True)
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trace: Mapped[List[Dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=False, default=list)
    final_plan: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=True)
    validation: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=True)
    execution_result: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=True)
    approval_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    recovery_case = relationship("RecoveryCase")
