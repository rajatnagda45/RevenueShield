"""End-to-End integration tests for Diagnosis Engine within the webhook & event pipeline."""
from decimal import Decimal
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.diagnosis import Diagnosis
from app.models.audit_log import AuditLog
from app.schemas.event import NormalizedEvent
from app.services.event_processor import EventProcessor


def test_payment_failed_automatically_executes_diagnosis_and_audit(db_session: Session):
    """Test that ingesting payment.failed creates RecoveryCase, Diagnosis, and audit records."""
    event = NormalizedEvent(
        event_id=f"evt_diag_test_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        amount=Decimal("899.00"),
        currency="INR",
        customer_email="sub_user@example.com",
        customer_name="Subscription User",
        external_payment_id=f"pay_diag_{uuid.uuid4()}",
        payment_method="CARD",
        payment_status="FAILED",
        failure_code="BAD_REQUEST_ERROR",
        failure_reason="insufficient_funds",
        failure_description="Transaction declined: low balance.",
    )

    result = EventProcessor.process_normalized_event(db_session, event)
    assert result.status == "processed"
    case_id = result.recovery_case_id
    assert case_id is not None

    # 1. Verify RecoveryCase has populated risk_score and recovery_probability
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(case_id)))
    assert case is not None
    assert case.status == "OPEN"
    assert case.risk_score is not None
    assert case.recovery_probability is not None
    assert 0.0 <= case.risk_score <= 100.0
    assert 0.0 <= case.recovery_probability <= 1.0

    # 2. Verify Diagnosis record in database
    diagnosis = db_session.scalar(select(Diagnosis).where(Diagnosis.recovery_case_id == case.id))
    assert diagnosis is not None
    assert diagnosis.category == "INSUFFICIENT_FUNDS"
    assert diagnosis.confidence >= 0.85
    assert diagnosis.engine_version == "diagnosis_engine_v1"
    assert diagnosis.evidence is not None
    assert diagnosis.evidence["error_reason"] == "insufficient_funds"
    assert diagnosis.risk_score == case.risk_score
    assert diagnosis.recovery_probability == case.recovery_probability

    # 3. Verify AuditLog for DIAGNOSIS_CREATED
    audits = db_session.scalars(
        select(AuditLog).where(AuditLog.recovery_case_id == case.id)
    ).all()
    actions = [a.action for a in audits]
    assert "RECOVERY_CASE_OPENED" in actions
    assert "DIAGNOSIS_CREATED" in actions

    diag_audit = next(a for a in audits if a.action == "DIAGNOSIS_CREATED")
    assert diag_audit.actor_type == "SYSTEM"
    assert diag_audit.actor_id == "diagnosis_engine_v1"
    assert diag_audit.audit_metadata["category"] == "INSUFFICIENT_FUNDS"


def test_payment_captured_does_not_trigger_diagnosis(db_session: Session):
    """Verify that a payment.captured event recovers the case without creating a second diagnosis."""
    # Step 1: Ingest failure
    fail_event = NormalizedEvent(
        event_id=f"evt_flow_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        amount=Decimal("1200.00"),
        currency="INR",
        customer_email="flow@example.com",
        external_payment_id="pay_flow_rec_01",
        failure_reason="gateway_timeout",
        failure_description="Bank switch down",
    )
    fail_res = EventProcessor.process_normalized_event(db_session, fail_event)
    case_id = fail_res.recovery_case_id

    # Should have exactly 1 diagnosis
    diag_count_before = len(
        db_session.scalars(select(Diagnosis).where(Diagnosis.recovery_case_id == uuid.UUID(case_id))).all()
    )
    assert diag_count_before == 1

    # Step 2: Ingest capture for same payment
    cap_event = NormalizedEvent(
        event_id=f"evt_flow_cap_{uuid.uuid4()}",
        event_type="payment.captured",
        source="RAZORPAY",
        amount=Decimal("1200.00"),
        currency="INR",
        customer_email="flow@example.com",
        external_payment_id="pay_flow_rec_01",
        payment_status="SUCCESS",
    )
    cap_res = EventProcessor.process_normalized_event(db_session, cap_event)
    assert cap_res.status == "processed"

    # Diagnosis count should still be 1 (no new diagnosis created on recovery)
    diag_count_after = len(
        db_session.scalars(select(Diagnosis).where(Diagnosis.recovery_case_id == uuid.UUID(case_id))).all()
    )
    assert diag_count_after == 1


def test_get_case_diagnosis_api_endpoint(client: TestClient, db_session: Session):
    """Test GET /recovery-cases/{case_id}/diagnosis HTTP endpoint."""
    # Ingest failure event to create case & diagnosis
    event = NormalizedEvent(
        event_id=f"evt_api_diag_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        amount=Decimal("2400.00"),
        currency="INR",
        customer_email="api_diag@example.com",
        external_payment_id="pay_api_diag_01",
        failure_code="INCORRECT_OTP",
        failure_reason="incorrect_otp",
        failure_description="OTP verification failed",
    )
    res = EventProcessor.process_normalized_event(db_session, event)
    case_id = res.recovery_case_id

    # Call API endpoint
    response = client.get(f"/recovery-cases/{case_id}/diagnosis")
    assert response.status_code == 200
    data = response.json()
    assert data["case_id"] == case_id
    assert data["category"] == "AUTHENTICATION_FAILURE"
    assert data["confidence"] >= 0.80
    assert data["engine_version"] == "diagnosis_engine_v1"
    assert data["risk_score"] is not None
    assert data["recovery_probability"] is not None
    assert "evidence" in data
    assert data["evidence"]["error_reason"] == "incorrect_otp"


def test_get_case_diagnosis_not_found(client: TestClient):
    """Test GET /recovery-cases/{case_id}/diagnosis returns 404 for non-existent case."""
    random_id = str(uuid.uuid4())
    response = client.get(f"/recovery-cases/{random_id}/diagnosis")
    assert response.status_code == 404
