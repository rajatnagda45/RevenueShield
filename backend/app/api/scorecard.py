"""Batch scorecard and experiment endpoints."""
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.scorecard import ScorecardService
from app.core.config import settings
from app.db.session import get_db
from app.models.experiment_assignment import ExperimentAssignment

router = APIRouter(prefix="/scorecard", tags=["Batch Scorecard & Experiments"])


@router.get(
    "",
    status_code=status.HTTP_200_OK,
    summary="Batch scorecard: incremental money recovered, compliance, audit coverage",
    description=(
        "Computes recovered vs holdout rupees for a batch or time window, with lift, p-value, bootstrap CI, "
        "cost per recovered rupee, policy violations (expected 0) and audit coverage (expected 100%)."
    ),
)
def get_scorecard(
    batch_id: Optional[str] = Query(None, description="Batch identifier (from webhook notes.batch_id)"),
    start: Optional[datetime] = Query(None, description="Case created_at lower bound (ISO)"),
    end: Optional[datetime] = Query(None, description="Case created_at upper bound (ISO)"),
    surface: Optional[str] = Query(None, description="Leak surface filter, e.g. PAYMENT_FAILURE"),
    seed: int = Query(42, ge=0, description="Bootstrap seed for reproducible confidence intervals"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    return ScorecardService.compute(db=db, batch_id=batch_id, start=start, end=end, surface=surface, seed=seed)


@router.get(
    "/experiments",
    status_code=status.HTTP_200_OK,
    summary="Experiment configuration and arm distribution",
)
def get_experiments(db: Session = Depends(get_db)) -> Dict[str, Any]:
    rows = db.execute(
        select(ExperimentAssignment.experiment_key, ExperimentAssignment.arm, func.count(ExperimentAssignment.id))
        .group_by(ExperimentAssignment.experiment_key, ExperimentAssignment.arm)
    ).all()
    distribution: Dict[str, Dict[str, int]] = {}
    for key, arm, n in rows:
        distribution.setdefault(key, {})[arm] = int(n)
    return {
        "enabled": bool(settings.EXPERIMENTS_ENABLED),
        "experiment_key": settings.EXPERIMENT_KEY,
        "holdout_percent": float(settings.HOLDOUT_PERCENT),
        "holdout_percent_by_surface": settings.HOLDOUT_PERCENT_BY_SURFACE or None,
        "distribution": distribution,
    }


@router.get(
    "/batches",
    status_code=status.HTTP_200_OK,
    summary="List known batch ids with case counts",
)
def list_batches(db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    from app.models.recovery_case import RecoveryCase

    rows = db.execute(
        select(RecoveryCase.batch_id, func.count(RecoveryCase.id), func.min(RecoveryCase.created_at), func.max(RecoveryCase.created_at))
        .where(RecoveryCase.batch_id.isnot(None))
        .group_by(RecoveryCase.batch_id)
        .order_by(func.max(RecoveryCase.created_at).desc())
    ).all()
    return [
        {"batch_id": b, "cases": int(n), "first_case_at": first.isoformat() if first else None, "last_case_at": last.isoformat() if last else None}
        for b, n, first, last in rows
    ]
