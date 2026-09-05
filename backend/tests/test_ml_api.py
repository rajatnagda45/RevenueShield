"""Tests for ML and Prediction API Endpoints."""
from decimal import Decimal
import uuid
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase
from app.models.diagnosis import Diagnosis


def test_admin_train_synthetic_demo_endpoint(client: TestClient):
    """Verify that POST /admin/ml/train trains and registers a synthetic demo model."""
    res = client.post(
        "/admin/ml/train",
        json={"dataset_type": "SYNTHETIC_DEMO", "model_name": "test_api_model"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "DEVELOPMENT_ONLY"
    assert data["dataset_type"] == "SYNTHETIC_DEMO"
    assert data["model_id"] is not None
    assert "roc_auc" in data["metrics"]


def test_admin_train_real_dataset_blocks_when_insufficient(client: TestClient):
    """Verify that training on REAL dataset with insufficient records halts with INSUFFICIENT_DATA."""
    res = client.post(
        "/admin/ml/train",
        json={"dataset_type": "REAL", "model_name": "production_model"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "INSUFFICIENT_DATA"
    assert data["model_id"] is None
    assert data["sufficiency"]["is_sufficient"] is False


def test_admin_activate_model_endpoint(client: TestClient):
    """Verify that POST /admin/ml/models/{id}/activate promotes model to ACTIVE."""
    # 1. Train synthetic model
    train_res = client.post(
        "/admin/ml/train",
        json={"dataset_type": "SYNTHETIC_DEMO", "model_name": "activatable_model"},
    )
    model_id = train_res.json()["model_id"]

    # 2. Activate model
    act_res = client.post(f"/admin/ml/models/{model_id}/activate")
    assert act_res.status_code == 200
    data = act_res.json()
    assert data["status"] == "ACTIVE"
    assert data["id"] == model_id


def test_get_case_predictions_endpoint(client: TestClient, db_session: Session):
    """Verify that GET /recovery-cases/{id}/predictions returns action-level probabilities and expected values."""
    cust = Customer(
        external_customer_id=f"cust_api_{uuid.uuid4()}",
        email="api_pred@example.com",
        name="API User",
        phone="+919876543212",
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
        amount_at_risk=Decimal("12000.00"),
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status="OPEN",
        risk_score=50.0,
        recovery_probability=0.55,
    )
    db_session.add(case)
    db_session.flush()

    diag = Diagnosis(
        recovery_case_id=case.id,
        category="INSUFFICIENT_FUNDS",
        root_cause="Account balance inadequate",
        confidence=0.85,
        engine_version="diagnosis_engine_v1",
        recommended_action="SEND_WHATSAPP_REMINDER",
        recommended_channel="WHATSAPP",
        indicators={},
    )
    db_session.add(diag)
    db_session.commit()

    res = client.get(f"/recovery-cases/{case.id}/predictions")
    assert res.status_code == 200
    data = res.json()
    assert data["case_id"] == str(case.id)
    assert data["amount_at_risk"] == 12000.0
    assert len(data["predictions"]) > 0
    top = data["predictions"][0]
    assert "action" in top
    assert "probability" in top
    assert "expected_recovered_value" in top
    assert top["expected_recovered_value"] > 0
