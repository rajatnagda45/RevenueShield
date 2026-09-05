"""Recovery ledger and settlement reconciliation endpoints."""
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.ledger.service import LedgerService
from app.ledger.settlement_reconciliation import SettlementReconRow, SettlementReconciliationService
from app.models.recovery_case import RecoveryCase

router = APIRouter(prefix="/ledger", tags=["Recovery Ledger"])


class ReconcileRequest(BaseModel):
    rows: List[SettlementReconRow] = Field(..., description="Settlement recon rows (payment id -> settlement id)")


def _require_internal_secret(x_internal_secret: Optional[str]) -> None:
    if settings.INTERNAL_API_SECRET and x_internal_secret != settings.INTERNAL_API_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid internal secret.")


@router.get("/cases/{case_id}", status_code=status.HTTP_200_OK, summary="Ledger entries and balance for a case")
def get_case_ledger(case_id: uuid.UUID, db: Session = Depends(get_db)) -> Dict[str, Any]:
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Recovery case '{case_id}' not found.")
    entries = LedgerService.entries_for_case(db, case_id)
    return {
        "case_id": str(case_id),
        "experiment_arm": case.experiment_arm,
        "batch_id": case.batch_id,
        "leak_surface": case.leak_surface,
        "balance": LedgerService.case_balance(db, case_id),
        "entries": [
            {
                "id": str(e.id),
                "entry_type": e.entry_type,
                "amount": float(e.amount),
                "currency": e.currency,
                "channel": e.channel,
                "provider_reference": e.provider_reference,
                "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
                "metadata": e.entry_metadata,
            }
            for e in entries
        ],
    }


@router.post("/reconcile", status_code=status.HTTP_200_OK, summary="Reconcile settlement rows against recovered payments")
def reconcile_settlements(
    body: ReconcileRequest,
    db: Session = Depends(get_db),
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    summary = SettlementReconciliationService.reconcile_rows(db, body.rows)
    db.commit()
    return summary


@router.post("/reconcile/razorpay", status_code=status.HTTP_200_OK, summary="Fetch Razorpay settlement recon for a day and reconcile")
def reconcile_from_razorpay(
    year: int = Query(..., ge=2020, le=2100),
    month: int = Query(..., ge=1, le=12),
    day: Optional[int] = Query(None, ge=1, le=31),
    db: Session = Depends(get_db),
    x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret"),
) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    try:
        rows = SettlementReconciliationService.fetch_razorpay_recon(year=year, month=month, day=day)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    summary = SettlementReconciliationService.reconcile_rows(db, rows)
    db.commit()
    summary["fetched_rows"] = len(rows)
    return summary
