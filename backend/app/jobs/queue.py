"""Transactional outbox queue.

Jobs are rows. Enqueue happens inside the caller's transaction, so a job exists if and only if the
business write it belongs to was committed. Workers claim rows with `SELECT ... FOR UPDATE SKIP
LOCKED` on PostgreSQL (plain optimistic update on SQLite), retry with exponential backoff and give
up into DEAD after `max_attempts`, where the row keeps its last error for an operator.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.resilience import backoff_delay
from app.models.job import Job

logger = logging.getLogger(__name__)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class JobQueue:
    @classmethod
    def enqueue(
        cls,
        db: Session,
        kind: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        run_at: Optional[datetime] = None,
        idempotency_key: Optional[str] = None,
        max_attempts: Optional[int] = None,
    ) -> Optional[Job]:
        """Add a job. With an idempotency key, a duplicate is a no-op that returns the existing row."""
        if not settings.JOBS_ENABLED:
            return None
        if idempotency_key:
            existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
            if existing:
                return existing
        job = Job(
            kind=kind, payload=payload or {}, status="QUEUED", run_at=_aware(run_at) or datetime.now(timezone.utc),
            max_attempts=max_attempts or settings.JOB_MAX_ATTEMPTS, idempotency_key=idempotency_key,
        )
        try:
            with db.begin_nested():
                db.add(job)
                db.flush()
        except IntegrityError:
            return db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        return job

    @classmethod
    def claim(cls, db: Session, worker_id: str, limit: Optional[int] = None, now: Optional[datetime] = None) -> List[Job]:
        """Atomically claim due jobs for this worker."""
        now = now or datetime.now(timezone.utc)
        limit = limit or settings.WORKER_BATCH_SIZE
        stale = now - timedelta(minutes=settings.JOB_LOCK_TIMEOUT_MINUTES)
        stmt = select(Job).where(
            ((Job.status == "QUEUED") | ((Job.status == "RUNNING") & (Job.locked_at < stale))),
            Job.run_at <= now,
        ).order_by(Job.run_at.asc()).limit(limit)
        if db.get_bind().dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        jobs = list(db.scalars(stmt).all())
        for j in jobs:
            j.status = "RUNNING"
            j.locked_by = worker_id
            j.locked_at = now
            j.attempts += 1
        db.flush()
        return jobs

    @classmethod
    def complete(cls, db: Session, job: Job, result: Optional[Dict[str, Any]] = None, now: Optional[datetime] = None) -> None:
        job.status = "SUCCEEDED"
        job.result = result or {}
        job.completed_at = now or datetime.now(timezone.utc)
        job.locked_by = None
        db.flush()

    @classmethod
    def fail(cls, db: Session, job: Job, error: str, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        job.last_error = error[:4000]
        job.locked_by = None
        if job.attempts >= job.max_attempts:
            job.status = "DEAD"
            job.completed_at = now
            logger.error(f"[JOB_DEAD] {job.kind} {job.id} after {job.attempts} attempts: {error[:200]}")
        else:
            delay = backoff_delay(job.attempts, base_seconds=settings.JOB_BACKOFF_BASE_SECONDS, max_seconds=settings.JOB_BACKOFF_MAX_SECONDS)
            job.status = "QUEUED"
            job.run_at = now + timedelta(seconds=delay)
            logger.warning(f"[JOB_RETRY] {job.kind} {job.id} attempt {job.attempts}/{job.max_attempts} failed: {error[:200]}; retry in {delay:.1f}s")
        db.flush()

    @classmethod
    def stats(cls, db: Session) -> Dict[str, Any]:
        rows = db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
        by_status = {s: int(n) for s, n in rows}
        kinds = db.execute(select(Job.kind, Job.status, func.count(Job.id)).group_by(Job.kind, Job.status)).all()
        by_kind: Dict[str, Dict[str, int]] = {}
        for k, s, n in kinds:
            by_kind.setdefault(k, {})[s] = int(n)
        last_done = db.scalar(select(func.max(Job.completed_at)).where(Job.status == "SUCCEEDED"))
        oldest_queued = db.scalar(select(func.min(Job.run_at)).where(Job.status == "QUEUED"))
        return {
            "by_status": by_status, "by_kind": by_kind,
            "last_completed_at": _aware(last_done).isoformat() if last_done else None,
            "oldest_queued_run_at": _aware(oldest_queued).isoformat() if oldest_queued else None,
            "dead": by_status.get("DEAD", 0),
        }

    @staticmethod
    def serialize(j: Job) -> Dict[str, Any]:
        return {
            "id": str(j.id), "kind": j.kind, "status": j.status, "payload": j.payload, "attempts": j.attempts, "max_attempts": j.max_attempts,
            "run_at": _aware(j.run_at).isoformat() if j.run_at else None, "idempotency_key": j.idempotency_key, "locked_by": j.locked_by,
            "last_error": j.last_error, "result": j.result, "created_at": _aware(j.created_at).isoformat() if j.created_at else None,
            "completed_at": _aware(j.completed_at).isoformat() if j.completed_at else None,
        }
