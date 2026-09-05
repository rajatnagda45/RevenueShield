"""The worker loop: enqueue recurring ticks, claim due jobs, run handlers, retry or bury."""
from __future__ import annotations

import logging
import socket
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.jobs.handlers import RECURRING, get_handler, recurring_key
from app.jobs.queue import JobQueue
from app.models.job import Job

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, session_factory: Callable[[], Session], worker_id: Optional[str] = None):
        self.session_factory = session_factory
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.processed = 0
        self.failed = 0

    def enqueue_recurring(self, db: Session, now: Optional[datetime] = None) -> int:
        """Enqueue this interval bucket's tick for every recurring kind (idempotent across workers)."""
        now = now or datetime.now(timezone.utc)
        created = 0
        for kind, interval in RECURRING:
            key = recurring_key(kind, interval, now)
            job = JobQueue.enqueue(db, kind, {"interval_seconds": interval}, run_at=now, idempotency_key=key)
            if job is not None and job.status == "QUEUED" and job.attempts == 0:
                created += 1
        db.commit()
        return created

    def run_once(self, db: Session, now: Optional[datetime] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        """One cycle: claim due jobs and execute them. Each job commits or rolls back on its own."""
        now = now or datetime.now(timezone.utc)
        jobs = JobQueue.claim(db, self.worker_id, limit=limit, now=now)
        db.commit()
        summary = {"claimed": len(jobs), "succeeded": 0, "failed": 0, "dead": 0}
        from app.observability.metrics import metrics

        for job in jobs:
            job_id, job_kind, attempts_at_claim = job.id, job.kind, int(job.attempts or 0)
            fn = get_handler(job_kind)
            started = time.monotonic()
            try:
                if fn is None:
                    raise RuntimeError(f"no handler registered for job kind '{job_kind}'")
                result = fn(db, dict(job.payload or {}), now)
                JobQueue.complete(db, job, result, now=now)
                db.commit()
                summary["succeeded"] += 1
                self.processed += 1
                metrics.job_processed(job_kind, "succeeded", time.monotonic() - started)
            except Exception as exc:
                db.rollback()
                err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
                # The rollback may also have undone the claim (shared transaction): re-fetch the row and
                # re-apply the claim state so the attempt is counted and backoff / DEAD logic stays correct.
                fresh = db.get(Job, job_id)
                if fresh is not None:
                    fresh.attempts = max(int(fresh.attempts or 0), attempts_at_claim)
                    fresh.status = "RUNNING"
                    fresh.locked_by = self.worker_id
                    fresh.locked_at = now
                    JobQueue.fail(db, fresh, err, now=now)
                    db.commit()
                    if fresh.status == "DEAD":
                        summary["dead"] += 1
                summary["failed"] += 1
                self.failed += 1
                metrics.job_processed(job_kind, "failed", time.monotonic() - started)
        return summary

    def run_forever(self, poll_seconds: Optional[float] = None) -> None:
        poll = poll_seconds or settings.WORKER_POLL_SECONDS
        logger.info(f"[WORKER] {self.worker_id} started; poll every {poll}s; recurring: {[k for k, _ in RECURRING]}")
        while True:
            db = self.session_factory()
            try:
                self.enqueue_recurring(db)
                summary = self.run_once(db)
                if summary["claimed"]:
                    logger.info(f"[WORKER] {summary}")
            except Exception as exc:  # never let the loop die
                logger.exception(f"[WORKER] cycle failed: {exc}")
                try:
                    db.rollback()
                except Exception:
                    pass
            finally:
                db.close()
            time.sleep(poll)
