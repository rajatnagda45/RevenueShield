"""Transactional outbox, worker cycle, handlers, recurring schedules, resilience primitives."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.resilience import CircuitBreaker, backoff_delay, retry_with_backoff
from app.jobs.handlers import RECURRING, get_handler, handler, known_kinds, recurring_key
from app.jobs.queue import JobQueue
from app.jobs.worker import Worker
from app.models.job import Job
from app.models.recovery_plan import RecoveryPlanStep
from app.services.recovery_scheduler import RecoveryScheduler
from tests.helpers_v2 import make_case, make_customer

NOW = datetime(2026, 9, 7, 5, 30, tzinfo=timezone.utc)  # 11:00 IST


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", None)
    monkeypatch.setattr(settings, "JOBS_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_PROVIDER", "null")


def _worker(db_session):
    return Worker(lambda: db_session, worker_id="test-worker")


def test_enqueue_is_idempotent_and_claim_marks_running(db_session: Session):
    j1 = JobQueue.enqueue(db_session, "approvals.expire", {}, run_at=NOW, idempotency_key="k1")
    j2 = JobQueue.enqueue(db_session, "approvals.expire", {}, run_at=NOW, idempotency_key="k1")
    assert j1.id == j2.id
    future = JobQueue.enqueue(db_session, "approvals.expire", {}, run_at=NOW + timedelta(hours=1))
    claimed = JobQueue.claim(db_session, "w1", now=NOW)
    assert [j.id for j in claimed] == [j1.id]
    assert j1.status == "RUNNING" and j1.attempts == 1 and j1.locked_by == "w1"
    assert future.status == "QUEUED"


def test_failure_backoff_then_dead(db_session: Session, monkeypatch):
    monkeypatch.setattr(settings, "JOB_MAX_ATTEMPTS", 2)
    job = JobQueue.enqueue(db_session, "approvals.expire", {}, run_at=NOW)
    JobQueue.claim(db_session, "w1", now=NOW)
    JobQueue.fail(db_session, job, "boom", now=NOW)
    assert job.status == "QUEUED" and job.run_at > NOW and "boom" in job.last_error
    JobQueue.claim(db_session, "w1", now=job.run_at + timedelta(seconds=1))
    JobQueue.fail(db_session, job, "boom again", now=NOW)
    assert job.status == "DEAD" and job.attempts == 2
    stats = JobQueue.stats(db_session)
    assert stats["dead"] == 1


def test_worker_runs_handlers_and_records_results(db_session: Session):
    calls = []

    @handler("test.echo")
    def _echo(db, payload, now):
        calls.append(payload)
        return {"echo": payload.get("x")}

    @handler("test.fail")
    def _fail(db, payload, now):
        raise RuntimeError("handler exploded")

    # The test fixture shares one transaction, so a handler rollback also undoes earlier work in this test.
    # Run the failing job first, then the succeeding one (production sessions commit each job for real).
    bad = JobQueue.enqueue(db_session, "test.fail", {}, run_at=NOW)
    summary = _worker(db_session).run_once(db_session, now=NOW)
    assert summary["failed"] >= 1
    db_session.refresh(bad)
    assert bad.status == "QUEUED" and "handler exploded" in bad.last_error and bad.attempts == 1

    JobQueue.enqueue(db_session, "test.echo", {"x": 1}, run_at=NOW)
    summary = _worker(db_session).run_once(db_session, now=NOW)
    assert summary["succeeded"] >= 1
    assert calls == [{"x": 1}]
    done = db_session.scalar(select(Job).where(Job.kind == "test.echo"))
    assert done.status == "SUCCEEDED" and done.result == {"echo": 1}


def test_recurring_keys_are_bucketed_and_enqueued_once(db_session: Session):
    k1 = recurring_key("plans.process_due", 300, NOW)
    k2 = recurring_key("plans.process_due", 300, NOW + timedelta(seconds=299))
    k3 = recurring_key("plans.process_due", 300, NOW + timedelta(seconds=301))
    assert k1 == k2 != k3
    w = _worker(db_session)
    w.enqueue_recurring(db_session, now=NOW)
    w.enqueue_recurring(db_session, now=NOW + timedelta(seconds=10))
    keys = [recurring_key(k, i, NOW) for k, i in RECURRING]
    n = db_session.scalar(select(__import__("sqlalchemy").func.count(Job.id)).where(Job.idempotency_key.in_(keys)))
    assert n == len(RECURRING)
    assert set(known_kinds()) >= {k for k, _ in RECURRING} | {"plan.evaluate", "agent.run"}


def test_case_open_enqueues_first_plan_evaluation_and_handler_advances_plan(db_session: Session):
    from app.detectors.case_factory import CaseFactory
    from app.domain.surfaces import LeakSurface
    from app.schemas.event import NormalizedEvent
    from decimal import Decimal

    customer = make_customer(db_session, phone="+919000000061")
    evt = CaseFactory.synthetic_event(db_session, customer=customer, event_type="payment.failed", entity_id="x", payload={}, occurred_at=NOW)
    diag = NormalizedEvent(event_id=evt.external_event_id, event_type="payment.failed", amount=Decimal("1200"), currency="INR", failure_reason="insufficient_funds", customer_email=customer.email, customer_phone=customer.phone)
    case = CaseFactory.open_case(db_session, customer=customer, db_event=evt, surface=LeakSurface.PAYMENT_FAILURE, amount=Decimal("1200"), currency="INR", diagnosis_event=diag)
    job = db_session.scalar(select(Job).where(Job.kind == "plan.evaluate", Job.idempotency_key == f"plan.evaluate:first:{case.id}"))
    assert job is not None and job.payload["case_id"] == str(case.id)

    res = get_handler("plan.evaluate")(db_session, job.payload, NOW)
    assert res["status"] in ("WAITING", "BLOCKED", "COMPLETED", "HELD", "EXECUTED")
    steps = db_session.scalars(select(RecoveryPlanStep).join(RecoveryPlanStep.recovery_plan).where(RecoveryPlanStep.recovery_plan.has(recovery_case_id=case.id))).all()
    assert len(steps) >= 1


def test_recurring_handlers_run_clean_on_empty_db(db_session: Session):
    for kind, _ in RECURRING:
        res = get_handler(kind)(db_session, {}, NOW)
        assert isinstance(res, dict), kind


def test_jobs_api(client: TestClient, db_session: Session):
    res = client.post("/jobs/enqueue", json={"kind": "approvals.expire", "payload": {}, "idempotency_key": "api-k"})
    assert res.status_code == 201 and res.json()["status"] == "QUEUED"
    assert client.post("/jobs/enqueue", json={"kind": "nope"}).status_code == 400
    run = client.post("/jobs/run-once", json={"include_recurring": True})
    assert run.status_code == 200 and run.json()["claimed"] >= 1
    assert client.get("/jobs/stats").json()["by_status"].get("SUCCEEDED", 0) >= 1
    assert client.get("/jobs", params={"kind": "approvals.expire"}).status_code == 200


# ------------------------------------------------------------------ resilience primitives

def test_retry_with_backoff_and_breaker():
    attempts = {"n": 0}
    slept = []

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("transient")
        return "ok"

    assert retry_with_backoff(flaky, retries=3, base_seconds=0.1, max_seconds=1, sleep=slept.append, jitter=False) == "ok"
    assert attempts["n"] == 3 and slept == [0.1, 0.2]

    def auth_error():
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        retry_with_backoff(auth_error, retries=3, retry_on=(ConnectionError,), sleep=lambda s: None)

    assert backoff_delay(1, base_seconds=1, max_seconds=10, jitter=False) == 1 and backoff_delay(5, base_seconds=1, max_seconds=10, jitter=False) == 10

    clock = {"t": 0.0}
    cb = CircuitBreaker(cooldown_seconds=60, failure_threshold=2, clock=lambda: clock["t"])
    with pytest.raises(RuntimeError):
        cb.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert cb.state == "closed"
    with pytest.raises(RuntimeError):
        cb.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert cb.state == "open" and cb.call(lambda: "never", on_open=lambda: "fallback") == "fallback"
    clock["t"] = 61.0
    assert cb.state == "half_open" and cb.call(lambda: "ok") == "ok" and cb.state == "closed"
