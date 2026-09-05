"""Approval queue: bounded autonomy with a human in the loop for the moves that deserve one."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.agent_run import AgentRun
from app.models.approval import Approval
from app.models.audit_log import AuditLog
from app.models.recovery_case import RecoveryCase

logger = logging.getLogger(__name__)


class ApprovalService:
    @classmethod
    def request(cls, db: Session, *, case: RecoveryCase, action: str, payload: Dict[str, Any], reason: str, agent_run: Optional[AgentRun] = None, now: Optional[datetime] = None) -> Approval:
        now = now or datetime.now(timezone.utc)
        pending = db.scalar(select(Approval).where(Approval.recovery_case_id == case.id, Approval.action == action, Approval.status == "PENDING"))
        if pending:
            return pending
        approval = Approval(
            recovery_case_id=case.id, agent_run_id=agent_run.id if agent_run else None, action=action, payload=payload, reason=reason,
            status="PENDING", requested_at=now, expires_at=now + timedelta(hours=settings.APPROVAL_SLA_HOURS),
        )
        db.add(approval)
        db.flush()
        db.add(AuditLog(recovery_case_id=case.id, actor_type="AGENT", actor_id="recovery_agent_v1", action="APPROVAL_REQUESTED", entity_type="Approval", entity_id=str(approval.id), audit_metadata={"action": action, "reason": reason, "expires_at": approval.expires_at.isoformat()}))
        db.flush()
        return approval

    @classmethod
    def approve(cls, db: Session, approval_id: uuid.UUID, *, operator: str, note: Optional[str] = None, dry_run: bool = True, now: Optional[datetime] = None) -> Approval:
        now = now or datetime.now(timezone.utc)
        approval = db.scalar(select(Approval).where(Approval.id == approval_id))
        if not approval:
            raise ValueError("Approval not found.")
        if approval.status != "PENDING":
            raise ValueError(f"Approval is already {approval.status}.")
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == approval.recovery_case_id))
        approval.status = "APPROVED"
        approval.decided_at = now
        approval.decided_by = operator
        approval.decision_note = note
        db.add(AuditLog(recovery_case_id=case.id, actor_type="OPERATOR", actor_id=operator, action="APPROVAL_GRANTED", entity_type="Approval", entity_id=str(approval.id), audit_metadata={"action": approval.action, "note": note}))
        db.flush()

        # Execute the deferred action through the same gated toolbox the agent uses.
        from app.agent.runner import PlanExecutor
        from app.agent.schemas import AgentPlan
        plan = AgentPlan.model_validate(approval.payload.get("plan") or {"action": approval.action, "rationale": approval.reason, "confidence": 1.0})
        result = PlanExecutor.execute(db, case, plan, dry_run=dry_run, now=now, approved=True)
        approval.execution_result = result
        run = db.scalar(select(AgentRun).where(AgentRun.id == approval.agent_run_id)) if approval.agent_run_id else None
        if run:
            run.execution_result = result
            run.status = "EXECUTED" if result.get("ok") else "BLOCKED"
            run.completed_at = now
        db.flush()
        return approval

    @classmethod
    def reject(cls, db: Session, approval_id: uuid.UUID, *, operator: str, note: Optional[str] = None, now: Optional[datetime] = None) -> Approval:
        now = now or datetime.now(timezone.utc)
        approval = db.scalar(select(Approval).where(Approval.id == approval_id))
        if not approval:
            raise ValueError("Approval not found.")
        if approval.status != "PENDING":
            raise ValueError(f"Approval is already {approval.status}.")
        approval.status = "REJECTED"
        approval.decided_at = now
        approval.decided_by = operator
        approval.decision_note = note
        db.add(AuditLog(recovery_case_id=approval.recovery_case_id, actor_type="OPERATOR", actor_id=operator, action="APPROVAL_REJECTED", entity_type="Approval", entity_id=str(approval.id), audit_metadata={"action": approval.action, "note": note}))
        run = db.scalar(select(AgentRun).where(AgentRun.id == approval.agent_run_id)) if approval.agent_run_id else None
        if run:
            run.status = "BLOCKED"
            run.execution_result = {"ok": False, "blocking_rule": "APPROVAL_REJECTED", "reason": note or "rejected by operator"}
            run.completed_at = now
        db.flush()
        return approval

    @classmethod
    def expire_due(cls, db: Session, now: Optional[datetime] = None) -> List[str]:
        now = now or datetime.now(timezone.utc)
        expired: List[str] = []
        for a in db.scalars(select(Approval).where(Approval.status == "PENDING", Approval.expires_at <= now)).all():
            a.status = "EXPIRED"
            a.decided_at = now
            a.decided_by = "system"
            a.decision_note = "SLA expired without a decision; the action was not taken."
            db.add(AuditLog(recovery_case_id=a.recovery_case_id, actor_type="SYSTEM", actor_id="approval_service_v1", action="APPROVAL_EXPIRED", entity_type="Approval", entity_id=str(a.id), audit_metadata={"action": a.action}))
            run = db.scalar(select(AgentRun).where(AgentRun.id == a.agent_run_id)) if a.agent_run_id else None
            if run and run.status == "AWAITING_APPROVAL":
                run.status = "BLOCKED"
                run.execution_result = {"ok": False, "blocking_rule": "APPROVAL_EXPIRED"}
                run.completed_at = now
            expired.append(str(a.id))
        db.flush()
        return expired

    @staticmethod
    def serialize(a: Approval) -> Dict[str, Any]:
        return {
            "id": str(a.id), "case_id": str(a.recovery_case_id), "agent_run_id": str(a.agent_run_id) if a.agent_run_id else None,
            "action": a.action, "reason": a.reason, "status": a.status, "payload": a.payload,
            "requested_at": a.requested_at.isoformat() if a.requested_at else None, "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            "decided_at": a.decided_at.isoformat() if a.decided_at else None, "decided_by": a.decided_by, "decision_note": a.decision_note,
            "execution_result": a.execution_result,
        }
