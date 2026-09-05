"""Leak surfaces: adapter normalisation, checkout abandonment, receivables, mandates, surface policy."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.decision.base import ActionType, DecisionContext
from app.decision.policy import PolicyEngine
from app.detectors.checkout_abandonment import CheckoutAbandonmentDetector
from app.detectors.mandates import MAX_MANDATE_RETRIES, MandateRetrySequencer
from app.detectors.receivables import ReceivableImportRow, ReceivablesDetector
from app.domain.surfaces import LeakSurface, ageing_bucket, is_checkout_abandonment, profile_for
from app.integrations.razorpay.adapter import RazorpayAdapter
from app.integrations.razorpay.security import compute_razorpay_signature
from app.models.audit_log import AuditLog
from app.models.diagnosis import Diagnosis
from app.models.invoice import Invoice
from app.models.ledger_entry import LedgerEntry
from app.models.mandate_retry import MandateRetry
from app.models.order import Order
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan
from app.models.subscription import Subscription
from app.services.event_processor import EventProcessor
from tests.helpers_v2 import make_customer

SECRET = "surface_secret"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", None)


def _post(client: TestClient, payload: dict, event_id: str = None):
    raw = json.dumps(payload).encode()
    return client.post(
        "/webhooks/razorpay", content=raw,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": compute_razorpay_signature(raw, SECRET), "x-razorpay-event-id": event_id or f"evt_{uuid.uuid4().hex[:10]}"},
    )


# ------------------------------------------------------------------ domain helpers

def test_surface_profiles_and_helpers():
    assert profile_for("CHECKOUT_ABANDONMENT").voice_allowed is False
    assert profile_for("RECEIVABLE_OVERDUE").retry_allowed is False
    assert profile_for("garbage").surface == LeakSurface.PAYMENT_FAILURE
    assert ageing_bucket(3) == "1-7" and ageing_bucket(20) == "8-30" and ageing_bucket(45) == "31-60" and ageing_bucket(90) == "60+"
    assert is_checkout_abandonment("payment_cancelled", None, None, has_order=True)
    assert not is_checkout_abandonment("payment_cancelled", None, None, has_order=False)
    assert not is_checkout_abandonment("insufficient_funds", None, None, has_order=True)


# ------------------------------------------------------------------ adapter

def test_adapter_normalises_invoice_order_and_subscription_payloads():
    inv = RazorpayAdapter.normalize({
        "event": "invoice.expired", "created_at": 1716300500,
        "payload": {"invoice": {"entity": {"id": "inv_1", "amount": 500000, "amount_paid": 0, "amount_due": 500000, "currency": "INR", "status": "expired",
                                            "expire_by": 1716200000, "customer_details": {"name": "Acme Ltd", "email": "ap@acme.com", "contact": "+919000000001", "customer_id": "cust_acme"}}}},
    })
    assert inv.external_invoice_id == "inv_1" and inv.amount == Decimal("5000.00") and inv.customer_email == "ap@acme.com"
    assert inv.failure_reason == "invoice_expired" and inv.metadata["invoice"]["due_by"] == 1716200000

    order = RazorpayAdapter.normalize({
        "event": "order.paid", "payload": {"order": {"entity": {"id": "order_1", "amount": 120000, "amount_paid": 120000, "amount_due": 0, "currency": "INR", "status": "paid", "receipt": "r1", "notes": {"customer_email": "buyer@x.com"}}},
                                           "payment": {"entity": {"id": "pay_o1", "amount": 120000, "currency": "INR", "status": "captured", "method": "upi", "email": "buyer@x.com", "order_id": "order_1"}}},
    })
    assert order.external_order_id == "order_1" and order.external_payment_id == "pay_o1" and order.metadata["order"]["status"] == "paid"

    sub = RazorpayAdapter.normalize({
        "event": "subscription.pending", "payload": {"subscription": {"entity": {"id": "sub_1", "plan_id": "plan_1", "status": "pending", "charge_at": 1716400000, "auth_attempts": 1, "paid_count": 3, "remaining_count": 9, "payment_method": "upi", "customer_id": "cust_s1"}},
                                                     "payment": {"entity": {"id": "pay_s1", "amount": 99900, "currency": "INR", "status": "failed", "method": "upi", "email": "s@x.com", "contact": "+919000000002", "error_reason": "insufficient_funds", "error_description": "Mandate execution failed: insufficient balance"}}},
    })
    assert sub.external_subscription_id == "sub_1" and sub.amount == Decimal("999.00") and sub.failure_reason == "insufficient_funds"
    assert sub.metadata["subscription"]["plan_id"] == "plan_1"


# ------------------------------------------------------------------ checkout abandonment

def test_user_cancelled_payment_with_order_opens_checkout_case(client: TestClient, db_session: Session):
    res = _post(client, {
        "event": "payment.failed", "created_at": 1716300500,
        "payload": {"payment": {"entity": {"id": f"pay_{uuid.uuid4().hex[:8]}", "order_id": "order_ab_1", "amount": 89900, "currency": "INR", "status": "failed", "method": "upi",
                                           "email": "shopper@x.com", "contact": "+919000000003", "error_code": "BAD_REQUEST_ERROR", "error_reason": "payment_cancelled", "error_description": "Payment was cancelled by the user", "created_at": 1716300500}}},
    })
    assert res.status_code == 200, res.text
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(res.json()["recovery_case_id"])))
    assert case.leak_surface == "CHECKOUT_ABANDONMENT" and case.case_type == "CHECKOUT_ABANDONMENT"
    assert case.case_metadata["order_id"] == "order_ab_1"
    order = db_session.scalar(select(Order).where(Order.external_order_id == "order_ab_1"))
    assert order.status == "ABANDONED" and order.abandonment_case_id == case.id
    diag = db_session.scalar(select(Diagnosis).where(Diagnosis.recovery_case_id == case.id))
    assert diag.category == "USER_FRICTION"
    plan = db_session.scalar(select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case.id))
    assert plan.max_steps == 2
    assert db_session.scalar(select(LedgerEntry).where(LedgerEntry.recovery_case_id == case.id, LedgerEntry.entry_type == "AT_RISK")).amount == Decimal("899.00")

    # order.paid closes it through the outcome engine and posts the recovery
    res2 = _post(client, {
        "event": "order.paid", "created_at": 1716301000,
        "payload": {"order": {"entity": {"id": "order_ab_1", "amount": 89900, "amount_paid": 89900, "amount_due": 0, "currency": "INR", "status": "paid", "attempts": 2}},
                    "payment": {"entity": {"id": "pay_ab_ok", "order_id": "order_ab_1", "amount": 89900, "currency": "INR", "status": "captured", "method": "upi", "email": "shopper@x.com"}}},
    })
    assert res2.status_code == 200, res2.text
    db_session.refresh(case)
    assert case.status == "RECOVERED"
    assert db_session.scalar(select(LedgerEntry).where(LedgerEntry.recovery_case_id == case.id, LedgerEntry.entry_type == "RECOVERED_CAPTURE")).provider_reference == "pay_ab_ok"


def test_bank_failure_with_order_stays_payment_failure(client: TestClient, db_session: Session):
    res = _post(client, {
        "event": "payment.failed", "created_at": 1716300500,
        "payload": {"payment": {"entity": {"id": f"pay_{uuid.uuid4().hex[:8]}", "order_id": "order_bank_1", "amount": 50000, "currency": "INR", "status": "failed", "method": "card",
                                           "email": "b@x.com", "error_reason": "insufficient_funds", "created_at": 1716300500}}},
    })
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(res.json()["recovery_case_id"])))
    assert case.leak_surface == "PAYMENT_FAILURE" and case.case_metadata == {"order_id": "order_bank_1"}
    order = db_session.scalar(select(Order).where(Order.external_order_id == "order_bank_1"))
    assert order.status == "ATTEMPTED" and order.attempts == 1


def test_order_sweep_opens_cases_for_quiet_orders(client: TestClient, db_session: Session):
    now = datetime.now(timezone.utc)
    r = client.post("/surfaces/orders", json={"external_order_id": "order_quiet_1", "amount": 1500, "customer_email": "q1@x.com", "customer_phone": "+919000000004", "created_at": (now - timedelta(hours=2)).isoformat()})
    assert r.status_code == 201, r.text
    client.post("/surfaces/orders", json={"external_order_id": "order_fresh_1", "amount": 700, "customer_email": "q2@x.com", "created_at": now.isoformat()})
    res = client.post("/surfaces/orders/sweep", json={"reference_time": now.isoformat(), "abandon_after_minutes": 30})
    assert res.status_code == 200, res.text
    body = res.json()
    assert [o["order_id"] for o in body["opened"]] == ["order_quiet_1"]
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(body["opened"][0]["case_id"])))
    assert case.leak_surface == "CHECKOUT_ABANDONMENT"
    # idempotent
    res2 = client.post("/surfaces/orders/sweep", json={"reference_time": now.isoformat(), "abandon_after_minutes": 30})
    assert res2.json()["opened"] == []


# ------------------------------------------------------------------ receivables

def test_receivables_import_sweep_and_payment(client: TestClient, db_session: Session):
    now = datetime.now(timezone.utc)
    rows = [
        {"external_invoice_id": "INV-1001", "amount": 250000, "due_date": (now - timedelta(days=45)).isoformat(), "customer_name": "Acme Ltd", "customer_email": "ap@acme.com", "customer_phone": "+919000000005"},
        {"external_invoice_id": "INV-1002", "amount": 40000, "due_date": (now + timedelta(days=10)).isoformat(), "customer_email": "ap2@acme.com"},
        {"external_invoice_id": "INV-1003", "amount": 100000, "amount_paid": 30000, "due_date": (now - timedelta(days=3)).isoformat(), "customer_email": "ap3@acme.com"},
    ]
    res = client.post("/surfaces/receivables/import", json={"rows": rows})
    assert res.status_code == 200 and res.json()["imported"] == 3

    res = client.post("/surfaces/receivables/sweep", json={"reference_time": now.isoformat()})
    body = res.json()
    opened = {o["invoice_id"]: o for o in body["opened"]}
    assert set(opened) == {"INV-1001", "INV-1003"}
    assert opened["INV-1001"]["ageing_bucket"] == "31-60" and opened["INV-1001"]["amount"] == 250000.0
    assert opened["INV-1003"]["ageing_bucket"] == "1-7" and opened["INV-1003"]["amount"] == 70000.0  # outstanding only

    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(opened["INV-1001"]["case_id"])))
    assert case.leak_surface == "RECEIVABLE_OVERDUE"
    diag = db_session.scalar(select(Diagnosis).where(Diagnosis.recovery_case_id == case.id))
    assert diag.category == "RECEIVABLE_OVERDUE"

    ageing = client.get("/surfaces/receivables/ageing", params={"reference_time": now.isoformat()}).json()
    assert ageing["buckets"]["31-60"]["invoices"] == 1 and ageing["total_outstanding"] == 320000.0

    # Razorpay invoice.partially_paid then invoice.paid on INV-1001
    _post(client, {"event": "invoice.partially_paid", "created_at": int(now.timestamp()),
                   "payload": {"invoice": {"entity": {"id": "INV-1001", "amount": 25000000, "amount_paid": 10000000, "amount_due": 15000000, "currency": "INR", "status": "partially_paid", "customer_details": {"email": "ap@acme.com"}}},
                               "payment": {"entity": {"id": "pay_inv_p1", "invoice_id": "INV-1001", "amount": 10000000, "currency": "INR", "status": "captured", "method": "netbanking", "email": "ap@acme.com"}}}})
    db_session.refresh(case)
    assert case.status == "IN_PROGRESS"
    partial = db_session.scalar(select(LedgerEntry).where(LedgerEntry.recovery_case_id == case.id, LedgerEntry.entry_type == "RECOVERED_PARTIAL"))
    assert partial is not None and partial.amount == Decimal("100000.00")

    _post(client, {"event": "invoice.paid", "created_at": int(now.timestamp()) + 60,
                   "payload": {"invoice": {"entity": {"id": "INV-1001", "amount": 25000000, "amount_paid": 25000000, "amount_due": 0, "currency": "INR", "status": "paid", "customer_details": {"email": "ap@acme.com"}}},
                               "payment": {"entity": {"id": "pay_inv_p2", "invoice_id": "INV-1001", "amount": 15000000, "currency": "INR", "status": "captured", "method": "netbanking", "email": "ap@acme.com"}}}})
    db_session.refresh(case)
    assert case.status == "RECOVERED"
    inv = db_session.scalar(select(Invoice).where(Invoice.external_invoice_id == "INV-1001"))
    assert inv.status == "PAID"


def test_invoice_expired_webhook_opens_case(client: TestClient, db_session: Session):
    res = _post(client, {"event": "invoice.expired", "created_at": 1716300500,
                         "payload": {"invoice": {"entity": {"id": f"inv_{uuid.uuid4().hex[:6]}", "amount": 900000, "amount_paid": 0, "amount_due": 900000, "currency": "INR", "status": "expired", "expire_by": 1716200000,
                                                             "customer_details": {"name": "Beta Pvt", "email": "beta@x.com", "contact": "+919000000006"}}}}})
    assert res.status_code == 200 and res.json()["recovery_case_id"]
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(res.json()["recovery_case_id"])))
    assert case.leak_surface == "RECEIVABLE_OVERDUE" and case.amount_at_risk == Decimal("9000.00")


# ------------------------------------------------------------------ mandates

def test_mandate_retry_sequencer_respects_notice_and_salary_days():
    now = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)  # 11:30 IST on the 10th
    plan = MandateRetrySequencer.next_retry_at(now, timezone_name="Asia/Kolkata", pre_debit_notice_hours=24)
    assert plan["scheduled_at"] - now >= timedelta(hours=24)
    assert plan["notify_by"] == plan["scheduled_at"] - timedelta(hours=24)
    # earliest = 11:30 IST on the 11th; 10:00 on the 11th is too early, so 10:00 on the 12th; no salary day within 7 days
    local = plan["scheduled_at"].astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
    assert local.hour == 10 and local.day == 12
    plan2 = MandateRetrySequencer.next_retry_at(datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc))
    assert plan2["scheduled_at"].astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).day == 28
    held = MandateRetrySequencer.next_retry_at(now, held_until=now + timedelta(days=3))
    assert held["scheduled_at"] >= now + timedelta(days=3)


def test_subscription_pending_then_charged(client: TestClient, db_session: Session):
    sub_id = f"sub_{uuid.uuid4().hex[:6]}"
    res = _post(client, {"event": "subscription.pending", "created_at": 1716300500,
                         "payload": {"subscription": {"entity": {"id": sub_id, "plan_id": "plan_pro", "status": "pending", "charge_at": 1716400000, "auth_attempts": 1, "paid_count": 4, "payment_method": "upi", "customer_id": "cust_sub1"}},
                                     "payment": {"entity": {"id": f"pay_{uuid.uuid4().hex[:6]}", "amount": 149900, "currency": "INR", "status": "failed", "method": "upi", "email": "sub@x.com", "contact": "+919000000007",
                                                            "error_reason": "insufficient_funds", "error_description": "Mandate execution failed", "created_at": 1716300500}}}})
    assert res.status_code == 200, res.text
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(res.json()["recovery_case_id"])))
    assert case.leak_surface == "SUBSCRIPTION_MANDATE_FAILURE" and case.amount_at_risk == Decimal("1499.00")
    assert case.case_metadata["mandate_type"] == "UPI_AUTOPAY"
    sub = db_session.scalar(select(Subscription).where(Subscription.external_subscription_id == sub_id))
    assert sub.status == "PENDING" and case.subscription_id == sub.id
    retry = db_session.scalar(select(MandateRetry).where(MandateRetry.recovery_case_id == case.id))
    assert retry.status == "SCHEDULED" and retry.notify_by < retry.scheduled_at
    assert db_session.scalar(select(AuditLog).where(AuditLog.recovery_case_id == case.id, AuditLog.action == "MANDATE_RETRY_SCHEDULED")) is not None

    # a second pending webhook must not open a second case
    res_dup = _post(client, {"event": "subscription.pending", "created_at": 1716386900,
                             "payload": {"subscription": {"entity": {"id": sub_id, "status": "pending", "auth_attempts": 2}},
                                         "payment": {"entity": {"id": f"pay_{uuid.uuid4().hex[:6]}", "amount": 149900, "currency": "INR", "status": "failed", "method": "upi", "email": "sub@x.com", "error_reason": "insufficient_funds"}}}})
    assert res_dup.json()["recovery_case_id"] == str(case.id)

    # the sweep sends the pre-debit notice, then executes at the scheduled time
    sweep1 = MandateRetrySequencer.sweep(db_session, now=retry.notify_by + timedelta(minutes=1))
    assert str(retry.id) in sweep1["notified"]
    sweep2 = MandateRetrySequencer.sweep(db_session, now=retry.scheduled_at + timedelta(minutes=1))
    assert str(retry.id) in sweep2["executed"]

    res_ok = _post(client, {"event": "subscription.charged", "created_at": 1716500000,
                            "payload": {"subscription": {"entity": {"id": sub_id, "status": "active", "paid_count": 5}},
                                        "payment": {"entity": {"id": "pay_sub_ok", "amount": 149900, "currency": "INR", "status": "captured", "method": "upi", "email": "sub@x.com"}}}})
    assert res_ok.status_code == 200
    db_session.refresh(case)
    db_session.refresh(retry)
    assert case.status == "RECOVERED" and retry.status == "SUCCEEDED"
    assert db_session.scalar(select(LedgerEntry).where(LedgerEntry.recovery_case_id == case.id, LedgerEntry.entry_type == "RECOVERED_CAPTURE")).provider_reference == "pay_sub_ok"


def test_subscription_halted_switches_to_reauthorisation(client: TestClient, db_session: Session):
    sub_id = f"sub_{uuid.uuid4().hex[:6]}"
    _post(client, {"event": "subscription.pending", "created_at": 1716300500,
                   "payload": {"subscription": {"entity": {"id": sub_id, "status": "pending", "payment_method": "card"}},
                               "payment": {"entity": {"id": f"pay_{uuid.uuid4().hex[:6]}", "amount": 59900, "currency": "INR", "status": "failed", "method": "card", "email": "h@x.com", "error_reason": "card_expired"}}}})
    res = _post(client, {"event": "subscription.halted", "created_at": 1716700000,
                         "payload": {"subscription": {"entity": {"id": sub_id, "status": "halted", "auth_attempts": 4, "payment_method": "card"}},
                                     "payment": {"entity": {"id": f"pay_{uuid.uuid4().hex[:6]}", "amount": 59900, "currency": "INR", "status": "failed", "method": "card", "email": "h@x.com", "error_reason": "card_expired"}}}})
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(res.json()["recovery_case_id"])))
    assert case.case_metadata["halted"] is True and case.case_metadata["playbook"] == "REAUTHORISE_MANDATE"
    retries = db_session.scalars(select(MandateRetry).where(MandateRetry.recovery_case_id == case.id)).all()
    assert retries and all(r.status == "CANCELLED" for r in retries)
    assert db_session.scalar(select(AuditLog).where(AuditLog.recovery_case_id == case.id, AuditLog.action == "MANDATE_HALTED")) is not None


def test_mandate_retry_cap(db_session: Session):
    customer = make_customer(db_session)
    from tests.helpers_v2 import make_case
    case = make_case(db_session, customer, leak_surface="SUBSCRIPTION_MANDATE_FAILURE")
    now = datetime.now(timezone.utc)
    for i in range(MAX_MANDATE_RETRIES):
        r = MandateRetrySequencer.schedule_next(db_session, case=case, subscription=None, now=now + timedelta(days=i * 3))
        assert r is not None and r.attempt_number == i + 1
        r.status = "FAILED"
        db_session.flush()
    assert MandateRetrySequencer.schedule_next(db_session, case=case, subscription=None, now=now + timedelta(days=30)) is None
    assert db_session.scalar(select(AuditLog).where(AuditLog.recovery_case_id == case.id, AuditLog.action == "MANDATE_RETRY_CAP_REACHED")) is not None


# ------------------------------------------------------------------ surface policy

def _ctx(surface, previous=None, **kw):
    return DecisionContext(
        case_id="c", case_type=surface, amount_at_risk=Decimal("1000"), currency="INR", case_age_hours=5, retry_count=0,
        diagnosis_category="USER_FRICTION", diagnosis_confidence=0.9, risk_score=40, recovery_probability=0.6,
        customer_phone_available=True, customer_email_available=True, previous_action_types=previous or [],
        current_time=datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc), metadata={"leak_surface": surface, "timezone": "Asia/Kolkata", **kw},
    )


def test_policy_enforces_surface_profiles():
    ck = _ctx("CHECKOUT_ABANDONMENT")
    assert PolicyEngine.evaluate(ActionType.VOICE_OUTREACH, ck, "OPEN").blocking_rule == "SURFACE_VOICE_NOT_ALLOWED"
    assert PolicyEngine.evaluate(ActionType.RETRY_PAYMENT, ck, "OPEN").blocking_rule == "SURFACE_RETRY_NOT_APPLICABLE"
    assert PolicyEngine.evaluate(ActionType.ESCALATE, ck, "OPEN").blocking_rule == "SURFACE_ESCALATION_NOT_ALLOWED"
    assert PolicyEngine.evaluate(ActionType.SEND_PAYMENT_LINK, ck, "OPEN").allowed
    capped = _ctx("CHECKOUT_ABANDONMENT", previous=["SEND_PAYMENT_LINK", "SEND_WHATSAPP_REMINDER"])
    assert PolicyEngine.evaluate(ActionType.SEND_PAYMENT_LINK, capped, "OPEN").blocking_rule == "SURFACE_TOUCH_CAP_REACHED"

    ar = _ctx("RECEIVABLE_OVERDUE")
    assert PolicyEngine.evaluate(ActionType.RETRY_PAYMENT, ar, "OPEN").blocking_rule == "SURFACE_RETRY_NOT_APPLICABLE"
    assert PolicyEngine.evaluate(ActionType.VOICE_OUTREACH, ar, "OPEN").allowed
    assert PolicyEngine.evaluate(ActionType.ESCALATE, ar, "OPEN").allowed

    held = _ctx("PAYMENT_FAILURE", systemic_incident_active=True)
    assert PolicyEngine.evaluate(ActionType.RETRY_PAYMENT, held, "OPEN").blocking_rule == "ISSUER_DEGRADED_HOLD"
    assert PolicyEngine.evaluate(ActionType.WAIT, held, "OPEN").allowed


def test_surface_summary_and_profiles_api(client: TestClient):
    assert client.get("/surfaces/profiles").json()["CHECKOUT_ABANDONMENT"]["max_touches"] == 2
    assert client.get("/surfaces/summary").status_code == 200
