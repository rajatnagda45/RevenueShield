"""End-to-End integration tests for OutcomeEngine, Learning Dataset, and API endpoints."""
from datetime import datetime, timezone
from decimal import Decimal
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.recovery_action import RecoveryAction
from app.models.outcome import RecoveryOutcome
from app.models.learning import LearningExample
from app.schemas.event import NormalizedEvent
from app.services.event_processor import EventProcessor


def test_full_outcome_and_learning_lifecycle(client: TestClient, db_session: Session):
    """Verify complete lifecycle: failed payment -> execution -> payment capture -> outcome & learning finalization."""
    # 1. Ingest payment.failed
    fail_event = NormalizedEvent(
        event_id=f"evt_flow_fail_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        amount=Decimal("3000.00"),
        currency="INR",
        customer_email="aditya.verma@example.com",
        customer_name="Aditya Verma",
        customer_phone="+919876500077",
        external_payment_id="pay_flow_test_01",
        payment_method="CARD",
        failure_code="BAD_REQUEST_ERROR",
        failure_reason="incorrect_otp",
        failure_description="OTP expired during checkout.",
    )
    fail_res = EventProcessor.process_normalized_event(db_session, fail_event)
    case_id = fail_res.recovery_case_id

    # Verify pending LearningExample was created
    case_uuid = uuid.UUID(case_id)
    initial_learn = db_session.scalar(
        select(LearningExample).where(LearningExample.recovery_case_id == case_uuid)
    )
    assert initial_learn is not None
    assert initial_learn.is_finalized is False
    assert initial_learn.amount_at_risk == Decimal("3000.00")
    assert initial_learn.action_type == "SEND_PAYMENT_LINK"

    # 2. Execute Action via POST /execute
    res_exec = client.post(f"/recovery-cases/{case_id}/execute")
    assert res_exec.status_code == 200
    exec_data = res_exec.json()
    assert exec_data["status"] == "SUCCEEDED"

    # Verify money is still at risk
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == case_uuid))
    assert case.status == "OPEN"
    assert case.recovered_amount is None

    # 3. Ingest payment.captured
    cap_event = NormalizedEvent(
        event_id=f"evt_flow_cap_{uuid.uuid4()}",
        event_type="payment.captured",
        source="RAZORPAY",
        amount=Decimal("3000.00"),
        currency="INR",
        customer_email="aditya.verma@example.com",
        external_payment_id="pay_flow_test_01",
        payment_status="SUCCESS",
    )
    cap_res = EventProcessor.process_normalized_event(db_session, cap_event)
    assert cap_res.status == "processed"

    # 4. Verify RecoveryCase is RECOVERED
    db_session.refresh(case)
    assert case.status == "RECOVERED"
    assert case.recovered_amount == Decimal("3000.00")

    # 5. Verify GET /recovery-cases/{case_id}/outcome
    res_outcome = client.get(f"/recovery-cases/{case_id}/outcome")
    assert res_outcome.status_code == 200
    outcome_data = res_outcome.json()
    assert outcome_data["case_id"] == case_id
    assert outcome_data["outcome_type"] == "RECOVERED"
    assert outcome_data["amount_recovered"] == 3000.0
    assert outcome_data["recovery_percentage"] == 100.0
    assert outcome_data["attribution"] == "DIRECT"
    assert outcome_data["time_to_recovery_seconds"] is not None

    # 6. Verify GET /learning/examples/{case_id}
    res_learn = client.get(f"/learning/examples/{case_id}")
    assert res_learn.status_code == 200
    learn_data = res_learn.json()
    assert learn_data["case_id"] == case_id
    assert learn_data["is_finalized"] is True
    assert learn_data["label"] == 1
    assert learn_data["outcome_type"] == "RECOVERED"
    assert learn_data["attribution"] in ["DIRECT", "PRIMARY", "UNCERTAIN"]
    assert learn_data.get("feature_snapshot") is not None


def test_partial_recovery_integration(client: TestClient, db_session: Session):
    """Verify that partial payment capture correctly updates recovery outcome percentage and label."""
    fail_event = NormalizedEvent(
        event_id=f"evt_partial_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        amount=Decimal("5000.00"),
        currency="INR",
        customer_email="partial_user@example.com",
        external_payment_id="pay_partial_01",
        failure_reason="incorrect_otp",
    )
    fail_res = EventProcessor.process_normalized_event(db_session, fail_event)
    case_id = fail_res.recovery_case_id

    # Execute action
    client.post(f"/recovery-cases/{case_id}/execute")

    # Capture partial amount: ₹3000 of ₹5000 (60%)
    cap_event = NormalizedEvent(
        event_id=f"evt_partial_cap_{uuid.uuid4()}",
        event_type="payment.captured",
        source="RAZORPAY",
        amount=Decimal("3000.00"),
        currency="INR",
        customer_email="partial_user@example.com",
        external_payment_id="pay_partial_01",
    )
    EventProcessor.process_normalized_event(db_session, cap_event)

    # Verify Outcome API
    res = client.get(f"/recovery-cases/{case_id}/outcome")
    assert res.status_code == 200
    data = res.json()
    assert data["outcome_type"] == "PARTIALLY_RECOVERED"
    assert data["amount_recovered"] == 3000.0
    assert data["recovery_percentage"] == 60.0
