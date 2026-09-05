"""Approval queue endpoints for operators."""
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.approvals import ApprovalService
from app.core.config import settings
from app.db.session import get_db
from app.models.approval import Approval

router = APIRouter(prefix="/approvals", tags=["Approval Queue"])


class DecisionRequest(BaseModel):
    operator: str
    note: Optional[str] = None
    dry_run: bool = True
    reference_time: Optional[datetime] = None


def _require_internal_secret(x_internal_secret: Optional[str]) -> None:
    if settings.INTERNAL_API_SECRET and x_internal_secret != settings.INTERNAL_API_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid internal secret.")


@router.get("", summary="List approvals (default: pending)")
def list_approvals(status_filter: Optional[str] = Query("PENDING", alias="status"), limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    stmt = select(Approval).order_by(Approval.requested_at.desc()).limit(limit)
    if status_filter and status_filter.upper() != "ALL":
        stmt = stmt.where(Approval.status == status_filter.upper())
    return [ApprovalService.serialize(a) for a in db.scalars(stmt).all()]


@router.post("/{approval_id}/approve", summary="Approve and execute the deferred action")
def approve(approval_id: uuid.UUID, body: DecisionRequest, db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    try:
        a = ApprovalService.approve(db, approval_id, operator=body.operator, note=body.note, dry_run=body.dry_run, now=body.reference_time)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    db.commit()
    return ApprovalService.serialize(a)


@router.post("/{approval_id}/reject", summary="Reject the deferred action")
def reject(approval_id: uuid.UUID, body: DecisionRequest, db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    try:
        a = ApprovalService.reject(db, approval_id, operator=body.operator, note=body.note, now=body.reference_time)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    db.commit()
    return ApprovalService.serialize(a)


@router.post("/expire", summary="Expire approvals past their SLA")
def expire(body: Optional[DecisionRequest] = None, db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    expired = ApprovalService.expire_due(db, now=body.reference_time if body else None)
    db.commit()
    return {"expired": expired}
