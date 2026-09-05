"""Job queue endpoints: inspect, enqueue, run one worker cycle inline (demos / CI)."""
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal, get_db
from app.jobs.handlers import known_kinds
from app.jobs.queue import JobQueue
from app.jobs.worker import Worker
from app.models.job import Job

router = APIRouter(prefix="/jobs", tags=["Background Jobs"])


class EnqueueRequest(BaseModel):
    kind: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    run_at: Optional[datetime] = None
    idempotency_key: Optional[str] = None


class RunOnceRequest(BaseModel):
    reference_time: Optional[datetime] = None
    limit: Optional[int] = Field(None, ge=1, le=500)
    include_recurring: bool = True


def _require_internal_secret(x_internal_secret: Optional[str]) -> None:
    if settings.INTERNAL_API_SECRET and x_internal_secret != settings.INTERNAL_API_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid internal secret.")


@router.get("/stats", summary="Queue depth, dead jobs, last completion")
def stats(db: Session = Depends(get_db)) -> Dict[str, Any]:
    return {**JobQueue.stats(db), "kinds": known_kinds(), "enabled": settings.JOBS_ENABLED}


@router.get("", summary="List jobs")
def list_jobs(status_filter: Optional[str] = Query(None, alias="status"), kind: Optional[str] = Query(None), limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(Job.status == status_filter.upper())
    if kind:
        stmt = stmt.where(Job.kind == kind)
    return [JobQueue.serialize(j) for j in db.scalars(stmt).all()]


@router.post("/enqueue", status_code=status.HTTP_201_CREATED, summary="Enqueue a job")
def enqueue(body: EnqueueRequest, db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    if body.kind not in known_kinds():
        raise HTTPException(status_code=400, detail=f"Unknown job kind '{body.kind}'. Known: {known_kinds()}")
    job = JobQueue.enqueue(db, body.kind, body.payload, run_at=body.run_at, idempotency_key=body.idempotency_key)
    db.commit()
    if job is None:
        return {"enqueued": False, "reason": "JOBS_ENABLED is false"}
    return JobQueue.serialize(job)


@router.post("/run-once", summary="Run one worker cycle inline (for demos and CI)")
def run_once(body: RunOnceRequest = RunOnceRequest(), db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    worker = Worker(SessionLocal, worker_id="api-inline")
    if body.include_recurring:
        worker.enqueue_recurring(db, now=body.reference_time)
    summary = worker.run_once(db, now=body.reference_time, limit=body.limit)
    db.commit()
    return summary
