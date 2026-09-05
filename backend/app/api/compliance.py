"""Compliance and audit endpoints: consent ledger, inbound opt-out, contact policy preview, handoff, chain verification."""
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.chain import AuditChain
from app.compliance.consent import ConsentService
from app.compliance.contact_policy import ContactPolicy
from app.compliance.handoff import HandoffService
from app.core.config import settings
from app.db.session import get_db
from app.models.audit_log import AuditLog
from app.models.customer import Customer
from app.models.recovery_case import RecoveryCase

router = APIRouter(tags=["Compliance & Audit"])


def _require_internal_secret(x_internal_secret: Optional[str]) -> None:
    if settings.INTERNAL_API_SECRET and x_internal_secret != settings.INTERNAL_API_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid internal secret.")


class ConsentRequest(BaseModel):
    customer_id: uuid.UUID
    channel: str = Field(..., description="WHATSAPP | SMS | EMAIL | VOICE | ALL")
    status: str = Field(..., description="OPT_IN | OPT_OUT")
    reason: Optional[str] = None
    operator: str = "operator"


class InboundTextRequest(BaseModel):
    phone: Optional[str] = None
    customer_id: Optional[uuid.UUID] = None
    channel: str = "WHATSAPP"
    text: str


class HandoffRequest(BaseModel):
    reason: str
    operator: str = "operator"
    note: Optional[str] = None


@router.get("/compliance/consent/{customer_id}", summary="Effective consent per channel and full history")
def get_consent(customer_id: uuid.UUID, db: Session = Depends(get_db)) -> Dict[str, Any]:
    if not db.scalar(select(Customer).where(Customer.id == customer_id)):
        raise HTTPException(status_code=404, detail="Customer not found.")
    return ConsentService.summary(db, customer_id)


@router.post("/compliance/consent", summary="Operator records consent on behalf of a customer")
def post_consent(body: ConsentRequest, db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    customer = db.scalar(select(Customer).where(Customer.id == body.customer_id))
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")
    try:
        rec = ConsentService.record(db, customer=customer, channel=body.channel, status=body.status, source="OPERATOR", reason=body.reason, metadata={"operator": body.operator})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"id": str(rec.id), "channel": rec.channel, "status": rec.status}


@router.post("/compliance/inbound", summary="Process an inbound customer text for opt-out keywords (STOP, 'do not call', Hinglish variants)")
def inbound_text(body: InboundTextRequest, db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    customer = None
    if body.customer_id:
        customer = db.scalar(select(Customer).where(Customer.id == body.customer_id))
    elif body.phone:
        digits = "".join(ch for ch in body.phone if ch.isdigit())[-10:]
        customer = db.scalar(select(Customer).where(Customer.phone.contains(digits))) if digits else None
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.customer_id == customer.id, RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PAUSED", "PTP"])).order_by(RecoveryCase.created_at.desc()))
    rec = ConsentService.process_inbound_text(db, customer=customer, text=body.text, channel=body.channel, case=case)
    db.commit()
    return {"opt_out_detected": rec is not None, "channel": rec.channel if rec else None, "consent_record_id": str(rec.id) if rec else None, "effective": ConsentService.summary(db, customer.id)["effective"]}


@router.get("/compliance/contact-policy/{case_id}", summary="Dry-run the contact policy for a case and channel")
def preview_contact_policy(case_id: uuid.UUID, channel: str = Query("WHATSAPP"), reference_time: Optional[datetime] = Query(None), db: Session = Depends(get_db)) -> Dict[str, Any]:
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(status_code=404, detail="Recovery case not found.")
    decision = ContactPolicy.evaluate(db, case=case, customer=case.customer, channel=channel, now=reference_time, record=False)
    return decision.to_dict()


@router.get("/compliance/touches/{customer_id}", summary="Recent cross-channel touches and blocks for a customer")
def touches(customer_id: uuid.UUID, days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    return ContactPolicy.touches(db, customer_id, days=days)


@router.post("/compliance/handoff/{case_id}", summary="Freeze a case for human handling")
def handoff(case_id: uuid.UUID, body: HandoffRequest, db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(status_code=404, detail="Recovery case not found.")
    HandoffService.freeze(db, case=case, reason=body.reason, source=f"OPERATOR:{body.operator}")
    db.commit()
    return {"case_id": str(case.id), "human_handoff": True}


@router.post("/compliance/handoff/{case_id}/release", summary="Release a case back to automation")
def release(case_id: uuid.UUID, body: HandoffRequest, db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(status_code=404, detail="Recovery case not found.")
    HandoffService.release(db, case=case, operator=body.operator, note=body.note)
    db.commit()
    return {"case_id": str(case.id), "human_handoff": False}


@router.get("/audit/verify", summary="Recompute the audit hash chain and report the first broken link, if any")
def verify_chain(case_id: Optional[uuid.UUID] = Query(None), limit: Optional[int] = Query(None, ge=1), db: Session = Depends(get_db)) -> Dict[str, Any]:
    return AuditChain.verify(db, case_id=case_id, limit=limit)


@router.get("/audit/cases/{case_id}", summary="Chained audit trail for one case")
def case_audit(case_id: uuid.UUID, db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    rows = db.scalars(select(AuditLog).where(AuditLog.recovery_case_id == case_id).order_by(AuditLog.sequence.asc().nullsfirst(), AuditLog.timestamp.asc())).all()
    return [
        {"sequence": r.sequence, "action": r.action, "actor_type": r.actor_type, "actor_id": r.actor_id, "entity_type": r.entity_type, "entity_id": r.entity_id,
         "metadata": r.audit_metadata, "timestamp": r.timestamp.isoformat() if r.timestamp else None, "prev_hash": r.prev_hash, "row_hash": r.row_hash}
        for r in rows
    ]
