"""Case list and dossier endpoints for the Command Center v2."""
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.dossier import DossierBuilder
from app.db.session import get_db
from app.ledger.service import LedgerService
from app.models.customer import Customer
from app.models.diagnosis import Diagnosis
from app.models.recovery_case import RecoveryCase
from app.services.notification_service import mask_contact

router = APIRouter(prefix="/v2/cases", tags=["Cases (v2)"])


def _serialize(case: RecoveryCase, diagnosis: Optional[Diagnosis], customer: Optional[Customer]) -> Dict[str, Any]:
    return {
        "id": str(case.id),
        "status": case.status,
        "surface": case.leak_surface or case.case_type,
        "experiment_arm": case.experiment_arm,
        "batch_id": case.batch_id,
        "amount_at_risk": float(case.amount_at_risk or 0),
        "recovered_amount": float(case.recovered_amount or 0),
        "currency": case.currency,
        "diagnosis": diagnosis.category if diagnosis else None,
        "recovery_probability": case.recovery_probability,
        "risk_score": case.risk_score,
        "customer": {"name": customer.name if customer else None, "segment": customer.segment if customer else None, "phone_masked": mask_contact(customer.phone) if customer and customer.phone else None},
        "metadata": case.case_metadata or {},
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "closed_at": case.closed_at.isoformat() if case.closed_at else None,
    }


@router.get("", summary="List recovery cases with filters")
def list_cases(
    status_filter: Optional[str] = Query(None, alias="status"),
    surface: Optional[str] = Query(None),
    arm: Optional[str] = Query(None),
    batch_id: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="customer name contains"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    stmt = select(RecoveryCase).order_by(RecoveryCase.created_at.desc())
    count_stmt = select(func.count(RecoveryCase.id))
    conds = []
    if status_filter:
        conds.append(RecoveryCase.status == status_filter.upper())
    if surface:
        conds.append(RecoveryCase.leak_surface == surface.upper())
    if arm:
        conds.append(RecoveryCase.experiment_arm == arm.upper())
    if batch_id:
        conds.append(RecoveryCase.batch_id == batch_id)
    if q:
        conds.append(RecoveryCase.customer.has(Customer.name.ilike(f"%{q}%")))
    for c in conds:
        stmt = stmt.where(c)
        count_stmt = count_stmt.where(c)
    total = int(db.scalar(count_stmt) or 0)
    cases = db.scalars(stmt.offset(offset).limit(limit)).all()
    ids = [c.id for c in cases]
    diags: Dict[uuid.UUID, Diagnosis] = {}
    if ids:
        for d in db.scalars(select(Diagnosis).where(Diagnosis.recovery_case_id.in_(ids)).order_by(Diagnosis.created_at.asc())).all():
            diags[d.recovery_case_id] = d
    return {"total": total, "items": [_serialize(c, diags.get(c.id), c.customer) for c in cases]}


@router.get("/{case_id}", summary="Case detail with dossier, ledger balance and metadata")
def get_case(case_id: uuid.UUID, db: Session = Depends(get_db)) -> Dict[str, Any]:
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(status_code=404, detail="Recovery case not found.")
    diag = db.scalar(select(Diagnosis).where(Diagnosis.recovery_case_id == case.id).order_by(Diagnosis.created_at.desc()))
    return {
        **_serialize(case, diag, case.customer),
        "dossier": DossierBuilder.build(db, case),
        "ledger": LedgerService.case_balance(db, case.id),
    }
