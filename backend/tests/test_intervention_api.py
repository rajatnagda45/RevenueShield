"""API tests for Interventions endpoints and dashboard analytics."""
from decimal import Decimal
import uuid
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.diagnosis import Diagnosis


def _create_api_case(db_session: Session, amount: Decimal = Decimal("8000.00")) -> RecoveryCase:
    cust = Customer(
        external_customer_id=f"cust_api_{uuid.uuid4()}",
        email="api_user@example.com",
        name="Anjali Rao",
        phone="+919876543219",
    )
    db_session.add(cust)
    db_session.flush()

    evt = Event(
        external_event_id=f"evt_api_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        customer_id=cust.id,
        processing_status="PROCESSED",
    )
    db_session.add(evt)
    db_session.flush()

    case = RecoveryCase(
        customer_id=cust.id,
        event_id=evt.id,
        amount_at_risk=amount,
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status="OPEN",
        risk_score=35.0,
        recovery_probability=0.75,
    )
    db_session.add(case)
    db_session.flush()

    diag = Diagnosis(
        recovery_case_id=case.id,
        category="AUTHENTICATION_FAILED",
        explanation="3DS verification timeout",
        confidence=0.85,
        engine_version="diagnosis_engine_v1",
    )
    db_session.add(diag)
    db_session.commit()
    db_session.refresh(case)
    return case


def test_intervention_preview_endpoint(client: TestClient, db_session: Session):
    """Verify GET /recovery-cases/{id}/intervention-preview endpoint."""
    case = _create_api_case(db_session, amount=Decimal("8000.00"))

    res = client.get(f"/recovery-cases/{case.id}/intervention-preview")
    assert res.status_code == 200
    data = res.json()
    assert data["case_id"] == str(case.id)
    assert data["amount_at_risk"] == 8000.0
    assert data["recommended_action"] is not None
    assert 0.0 <= data["probability"] <= 1.0
    assert data["expected_recovered_value"] > 0
    assert data["policy_status"] in ("APPROVED", "RECOMMENDED")


def test_execute_intervention_endpoint(client: TestClient, db_session: Session):
    """Verify POST /recovery-cases/{id}/interventions endpoint."""
    case = _create_api_case(db_session, amount=Decimal("11000.00"))

    res = client.post(
        f"/recovery-cases/{case.id}/interventions",
        json={"action": "SEND_PAYMENT_LINK", "dry_run": True},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["case_id"] == str(case.id)
    assert data["status"] == "SENT"
    assert data["action"] == "SEND_PAYMENT_LINK"
    assert data["payment_link"] is not None
    assert data["payment_link"]["amount"] == 11000.0
    assert data["payment_link"]["currency"] == "INR"
    assert "https://rzp.io/i/" in data["payment_link"]["url"]
    assert data["notification"] is not None
    assert data["notification"]["status"] == "SENT"


def test_interventions_dashboard_endpoint(client: TestClient, db_session: Session):
    """Verify GET /admin/interventions/dashboard endpoint metrics."""
    res = client.get("/admin/interventions/dashboard")
    assert res.status_code == 200
    data = res.json()
    assert "total_revenue_at_risk" in data
    assert "total_recovered" in data
    assert "recovery_rate" in data
    assert "active_cases" in data
    assert "successful_interventions" in data
    assert "failed_interventions" in data
    assert "predicted_recovery_value" in data
    assert "actual_recovered_value" in data
