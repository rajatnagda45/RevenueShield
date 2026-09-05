"""Payment degradation monitor: detect an issuer outage, hold affected cases, release on recovery."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.degradation.monitor import DegradationMonitor
from app.integrations.razorpay.security import compute_razorpay_signature
from app.models.audit_log import AuditLog
from app.models.degradation_incident import DegradationIncident
from app.models.diagnosis import Diagnosis
from app.models.payment import Payment
from app.models.recovery_action import RecoveryAction
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan
from app.services.recovery_scheduler import RecoveryScheduler
from tests.helpers_v2 import make_case, make_customer

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
SECRET = "deg_secret"


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", None)


def _seed_payments(db: Session, customer, *, bank, method, n, failed, start, spread_minutes):
    for i in range(n):
        is_failed = i < failed
        ts = start + timedelta(seconds=(spread_minutes * 60 * i) // max(n, 1))
        db.add(Payment(
            external_payment_id=f"pay_{uuid.uuid4().hex[:10]}", customer_id=customer.id, amount=Decimal("500.00"), currency="INR",
            status="FAILED" if is_failed else "CAPTURED", payment_method=method, bank=bank, captured=not is_failed,
            error_reason="bank_timeout" if is_failed else None, razorpay_created_at=ts,
        ))
    db.flush()


def _seed_portfolio(db: Session, customer, spike_failed=40, spike_total=60):
    # 7-day baseline: HDFC UPI healthy (~5%), ICICI CARD healthy
    _seed_payments(db, customer, bank="HDFC", method="UPI", n=200, failed=10, start=NOW - timedelta(days=6), spread_minutes=5 * 24 * 60)
    _seed_payments(db, customer, bank="ICICI", method="CARD", n=120, failed=8, start=NOW - timedelta(days=6), spread_minutes=5 * 24 * 60)
    # current 15-minute window: HDFC UPI spikes, ICICI CARD normal
    _seed_payments(db, customer, bank="HDFC", method="UPI", n=spike_total, failed=spike_failed, start=NOW - timedelta(minutes=14), spread_minutes=13)
    _seed_payments(db, customer, bank="ICICI", method="CARD", n=30, failed=2, start=NOW - timedelta(minutes=14), spread_minutes=13)


def test_health_matrix_flags_only_the_degraded_cell(db_session: Session):
    customer = make_customer(db_session)
    _seed_portfolio(db_session, customer)
    matrix = DegradationMonitor.health_matrix(db_session, now=NOW)
    cells = {(c["bank"], c["payment_method"]): c for c in matrix["cells"]}
    hdfc, icici = cells[("HDFC", "UPI")], cells[("ICICI", "CARD")]
    assert hdfc["degraded"] is True and hdfc["baseline_source"] == "cell" and hdfc["z_score"] > 3
    assert abs(hdfc["failure_rate"] - 40 / 60) < 1e-3 and abs(hdfc["baseline_failure_rate"] - 0.05) < 1e-3
    assert icici["degraded"] is False


def test_incident_lifecycle_holds_and_releases_cases(db_session: Session):
    customer = make_customer(db_session)
    _seed_portfolio(db_session, customer)

    # An open case on an HDFC UPI payment (opened before the spike was recognised)
    failed_payment = db_session.scalar(select(Payment).where(Payment.bank == "HDFC", Payment.status == "FAILED").order_by(Payment.razorpay_created_at.desc()))
    case = make_case(db_session, customer, amount=500)
    case.payment_id = failed_payment.id
    db_session.flush()
    plan = RecoveryScheduler.create_or_get_plan(db_session, case.id)

    # Tick 1: SUSPECTED, case tagged
    t1 = DegradationMonitor.evaluate(db_session, now=NOW)
    assert len(t1["opened"]) == 1
    incident = db_session.scalar(select(DegradationIncident).where(DegradationIncident.id == uuid.UUID(t1["opened"][0])))
    assert incident.status == "SUSPECTED" and incident.bank == "HDFC" and incident.payment_method == "UPI"
    db_session.refresh(case)
    assert case.case_metadata["systemic_hold"] is True and case.case_metadata["systemic_incident_id"] == str(incident.id)
    assert DegradationMonitor.case_is_held(db_session, case)

    # Scheduler holds without consuming a plan step
    res = RecoveryScheduler.evaluate_and_advance_plan(db_session, plan.id, reference_time=NOW + timedelta(minutes=1), dry_run=True)
    assert res["status"] == "HELD" and res["blocking_rule"] == "ISSUER_DEGRADED_HOLD"
    db_session.refresh(plan)
    assert plan.current_step == 0 and plan.status == "WAITING"
    assert db_session.scalar(select(AuditLog).where(AuditLog.recovery_case_id == case.id, AuditLog.action == "RECOVERY_STEP_HELD_SYSTEMIC_INCIDENT")) is not None

    # Tick 2 (spike persists): CONFIRMED
    _seed_payments(db_session, customer, bank="HDFC", method="UPI", n=40, failed=30, start=NOW + timedelta(minutes=1), spread_minutes=13)
    t2 = DegradationMonitor.evaluate(db_session, now=NOW + timedelta(minutes=15))
    db_session.refresh(incident)
    assert incident.status == "CONFIRMED" and str(incident.id) in t2["confirmed"]

    # Tick 3 & 4 (healthy traffic): RECOVERING then CLOSED, case released, plan re-evaluation pulled forward
    _seed_payments(db_session, customer, bank="HDFC", method="UPI", n=40, failed=2, start=NOW + timedelta(minutes=31), spread_minutes=13)
    t3 = DegradationMonitor.evaluate(db_session, now=NOW + timedelta(minutes=45))
    db_session.refresh(incident)
    assert incident.status == "RECOVERING" and str(incident.id) in t3["recovering"]
    _seed_payments(db_session, customer, bank="HDFC", method="UPI", n=40, failed=1, start=NOW + timedelta(minutes=46), spread_minutes=13)
    t4 = DegradationMonitor.evaluate(db_session, now=NOW + timedelta(minutes=60))
    db_session.refresh(incident)
    db_session.refresh(case)
    db_session.refresh(plan)
    assert incident.status == "CLOSED" and str(incident.id) in t4["closed"]
    assert case.case_metadata["systemic_hold"] is False
    assert not DegradationMonitor.case_is_held(db_session, case)
    assert plan.next_evaluation_at is not None
    nea = plan.next_evaluation_at if plan.next_evaluation_at.tzinfo else plan.next_evaluation_at.replace(tzinfo=timezone.utc)
    assert nea <= NOW + timedelta(minutes=60) + timedelta(seconds=settings.DEGRADATION_RELEASE_JITTER_SECONDS)
    assert db_session.scalar(select(AuditLog).where(AuditLog.recovery_case_id == case.id, AuditLog.action == "CASE_RELEASED_SYSTEMIC_INCIDENT")) is not None


def test_new_failure_during_incident_is_diagnosed_systemic(client: TestClient, db_session: Session):
    customer = make_customer(db_session)
    _seed_portfolio(db_session, customer)
    DegradationMonitor.evaluate(db_session, now=NOW)

    payload = {
        "event": "payment.failed", "created_at": int(NOW.timestamp()),
        "payload": {"payment": {"entity": {"id": f"pay_{uuid.uuid4().hex[:8]}", "amount": 75000, "currency": "INR", "status": "failed", "method": "upi", "bank": "HDFC",
                                           "email": "victim@x.com", "contact": "+919000000010", "error_source": "bank", "error_reason": "bank_timeout", "error_description": "Issuing bank timed out", "created_at": int(NOW.timestamp())}}},
    }
    raw = json.dumps(payload).encode()
    res = client.post("/webhooks/razorpay", content=raw, headers={"Content-Type": "application/json", "X-Razorpay-Signature": compute_razorpay_signature(raw, SECRET), "x-razorpay-event-id": f"evt_{uuid.uuid4().hex[:8]}"})
    assert res.status_code == 200, res.text
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(res.json()["recovery_case_id"])))
    assert case.case_metadata["systemic_hold"] is True
    diag = db_session.scalar(select(Diagnosis).where(Diagnosis.recovery_case_id == case.id))
    assert diag.category == "SYSTEMIC_ISSUER_DEGRADATION"
    action = db_session.scalar(select(RecoveryAction).where(RecoveryAction.recovery_case_id == case.id))
    # The engine prefers a deferred retry, and policy holds it: no customer contact happens during the outage.
    assert action.action_type in ("RETRY_PAYMENT", "WAIT")
    assert action.status == "BLOCKED" and action.policy_result["blocking_rule"] == "ISSUER_DEGRADED_HOLD"


def test_no_incident_for_small_or_normal_cells(db_session: Session):
    customer = make_customer(db_session)
    _seed_payments(db_session, customer, bank="SBI", method="UPI", n=10, failed=9, start=NOW - timedelta(minutes=10), spread_minutes=5)  # too few samples
    _seed_payments(db_session, customer, bank="AXIS", method="CARD", n=100, failed=6, start=NOW - timedelta(minutes=10), spread_minutes=5)  # normal
    res = DegradationMonitor.evaluate(db_session, now=NOW)
    assert res["opened"] == []


def test_degradation_api(client: TestClient, db_session: Session):
    customer = make_customer(db_session)
    _seed_portfolio(db_session, customer)
    res = client.post("/degradation/evaluate", json={"reference_time": NOW.isoformat()})
    assert res.status_code == 200 and len(res.json()["opened"]) == 1
    inc = client.get("/degradation/incidents").json()
    assert inc[0]["bank"] == "HDFC" and inc[0]["status"] == "SUSPECTED" and inc[0]["history"]
    health = client.get("/degradation/health", params={"reference_time": NOW.isoformat()}).json()
    assert any(c["degraded"] for c in health["cells"])
