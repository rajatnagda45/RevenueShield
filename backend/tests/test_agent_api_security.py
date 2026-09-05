"""Tests for Internal Server-to-Server API Security and Authentication."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid
import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.core.config import settings
from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan


def _create_secure_test_case(db: Session) -> RecoveryCase:
    uid = uuid.uuid4().hex[:8]
    cust = Customer(
        external_customer_id=f"cust_sec_{uid}",
        email=f"user_{uid}@enterprise.in",
        name="Security Test User",
        phone="+919876543222",
        whatsapp_allowed=True,
        transactional_allowed=True,
    )
    db.add(cust)
    db.flush()

    evt = Event(
        external_event_id=f"evt_sec_{uid}",
        event_type="payment.failed",
        source="RAZORPAY",
        customer_id=cust.id,
        processing_status="PROCESSED",
    )
    db.add(evt)
    db.flush()

    case = RecoveryCase(
        customer_id=cust.id,
        event_id=evt.id,
        amount_at_risk=Decimal("15000.00"),
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status="OPEN",
    )
    db.add(case)
    db.flush()

    plan = RecoveryPlan(
        recovery_case_id=case.id,
        status="ACTIVE",
        current_step=1,
        max_steps=3,
    )
    db.add(plan)
    db.commit()
    db.refresh(case)
    return case


def test_agent_api_security_enforcement(client: TestClient, db_session: Session, monkeypatch):
    """Verify that when INTERNAL_API_SECRET is configured, requests require valid authentication."""
    test_secret = "test_internal_secret_xyz789"
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", test_secret)

    case = _create_secure_test_case(db_session)
    future_date = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    promise_payload = {
        "promised_amount": 15000.0,
        "promised_date": future_date,
        "promised_time": "17:00",
        "source": "AGENT",
    }

    # 1. Unauthenticated request -> 401 Unauthorized
    res_unauth = client.post(f"/recovery-cases/{case.id}/promise-to-pay", json=promise_payload)
    assert res_unauth.status_code == 401
    assert "Unauthorized" in res_unauth.json()["detail"]

    # 2. Invalid secret -> 401 Unauthorized
    res_bad = client.post(
        f"/recovery-cases/{case.id}/promise-to-pay",
        json=promise_payload,
        headers={"X-Internal-Secret": "wrong_secret_123"},
    )
    assert res_bad.status_code == 401

    # 3. Authenticated via X-Internal-Secret -> 201 Created
    res_internal = client.post(
        f"/recovery-cases/{case.id}/promise-to-pay",
        json=promise_payload,
        headers={"X-Internal-Secret": test_secret},
    )
    assert res_internal.status_code == 201
    assert res_internal.json()["status"] == "ACTIVE"

    # 4. Authenticated via X-API-Key -> 200 OK for GET
    res_api_key = client.get(
        f"/recovery-cases/{case.id}/promise-to-pay",
        headers={"X-API-Key": test_secret},
    )
    assert res_api_key.status_code == 200
    assert res_api_key.json()["id"] == res_internal.json()["id"]

    # 5. Authenticated via Authorization: Bearer <secret>
    res_bearer = client.get(
        f"/recovery-cases/{case.id}/outcome",
        headers={"Authorization": f"Bearer {test_secret}"},
    )
    # 404 because no outcome recorded yet, but passed 401 auth
    assert res_bearer.status_code == 404


def test_agent_api_dev_mode_allowed_when_secret_unset(client: TestClient, db_session: Session, monkeypatch):
    """Verify that when INTERNAL_API_SECRET is None, local development is permitted without breaking."""
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", None)
    case = _create_secure_test_case(db_session)

    res = client.get(f"/recovery-cases/{case.id}/promise-to-pay")
    assert res.status_code == 200
