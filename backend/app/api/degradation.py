"""Payment degradation endpoints: health matrix, incidents, monitoring tick."""
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.degradation.monitor import DegradationMonitor
from app.models.degradation_incident import DegradationIncident

router = APIRouter(prefix="/degradation", tags=["Payment Degradation Monitor"])


class EvaluateRequest(BaseModel):
    reference_time: Optional[datetime] = None


def _require_internal_secret(x_internal_secret: Optional[str]) -> None:
    if settings.INTERNAL_API_SECRET and x_internal_secret != settings.INTERNAL_API_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid internal secret.")


def _serialize(i: DegradationIncident) -> Dict[str, Any]:
    return {
        "id": str(i.id), "bank": i.bank, "payment_method": i.payment_method, "status": i.status,
        "opened_at": i.opened_at.isoformat() if i.opened_at else None,
        "confirmed_at": i.confirmed_at.isoformat() if i.confirmed_at else None,
        "recovering_at": i.recovering_at.isoformat() if i.recovering_at else None,
        "closed_at": i.closed_at.isoformat() if i.closed_at else None,
        "window_minutes": i.window_minutes, "observed_total": i.observed_total, "observed_failures": i.observed_failures,
        "observed_failure_rate": i.observed_failure_rate, "baseline_failure_rate": i.baseline_failure_rate, "z_score": i.z_score,
        "affected_case_count": i.affected_case_count, "history": (i.incident_metadata or {}).get("history", []),
    }


@router.get("/health", summary="Failure-rate matrix (bank x method) for the current window vs baseline")
def health(reference_time: Optional[datetime] = Query(None), window_minutes: Optional[int] = Query(None, ge=1, le=1440), db: Session = Depends(get_db)) -> Dict[str, Any]:
    return DegradationMonitor.health_matrix(db, now=reference_time, window_minutes=window_minutes)


@router.get("/incidents", summary="Degradation incidents")
def incidents(status_filter: Optional[str] = Query(None, alias="status"), limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    stmt = select(DegradationIncident).order_by(DegradationIncident.opened_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(DegradationIncident.status == status_filter.upper())
    return [_serialize(i) for i in db.scalars(stmt).all()]


@router.post("/evaluate", summary="Run one monitoring tick (opens/confirms/closes incidents, holds/releases cases)")
def evaluate(body: EvaluateRequest = EvaluateRequest(), db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    res = DegradationMonitor.evaluate(db, now=body.reference_time)
    db.commit()
    return res
