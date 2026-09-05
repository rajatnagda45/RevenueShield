"""End-to-End integration tests for the Recovery Decision Engine, Policy Engine, and Stopping Rule."""
from decimal import Decimal
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.recovery_action import RecoveryAction
from app.models.diagnosis import Diagnosis
from app.models.audit_log import AuditLog
from app.schemas.event import NormalizedEvent
from app.services.event_processor import EventProcessor


def test_payment_failed_automatically_generates_approved_recovery_action(db_session: Session):
    """Test that ingesting payment.failed creates Diagnosis, RecoveryAction (APPROVED), and AuditLogs."""
    event = NormalizedEvent(
        event_id=f"evt_dec_test_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        amount=Decimal("1500.00"),
        currency="INR",
        customer_email="rahul.verma@example.com",
        customer_name="Rahul Verma",
        customer_phone="+919876543210",
        external_payment_id=f"pay_dec_{uuid.uuid4()}",
        payment_method="CARD",
        payment_status="FAILED",
        failure_code="BAD_REQUEST_ERROR",
        failure_reason="incorrect_otp",
        failure_description="Customer entered invalid OTP during 3DS check.",
    )

    result = EventProcessor.process_normalized_event(db_session, event)
    assert result.status == "processed"
    case_id = result.recovery_case_id
    assert case_id is not None

    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(case_id)))
    assert case is not None

    # 1. Verify RecoveryAction created
    action = db_session.scalar(
        select(RecoveryAction).where(RecoveryAction.recovery_case_id == case.id)
    )
    assert action is not None
    assert action.action_type == "SEND_PAYMENT_LINK"
    assert action.status == "APPROVED"
    assert action.decision_score is not None
    assert action.confidence is not None
    assert action.decision_engine_version == "decision_engine_v1"
    assert action.policy_engine_version == "policy_engine_v1"
    assert action.policy_result["allowed"] is True
    assert len(action.alternatives) > 0
    assert len(action.supporting_factors) > 0

    # 2. Verify AuditLogs include RECOVERY_ACTION_RECOMMENDED
    audits = db_session.scalars(
        select(AuditLog).where(AuditLog.recovery_case_id == case.id)
    ).all()
    actions = [a.action for a in audits]
    assert "RECOVERY_CASE_OPENED" in actions
    assert "DIAGNOSIS_CREATED" in actions
    assert "RECOVERY_ACTION_RECOMMENDED" in actions

    rec_audit = next(a for a in audits if a.action == "RECOVERY_ACTION_RECOMMENDED")
    assert rec_audit.actor_id == "decision_engine_v1"
    assert rec_audit.audit_metadata["recommended_action"] == "SEND_PAYMENT_LINK"
    assert rec_audit.audit_metadata["status"] == "APPROVED"


def test_payment_captured_cancels_pending_recovery_action(db_session: Session):
    """Verify Stopping Rule: payment.captured marks case RECOVERED and sets pending action to CANCELLED."""
    # 1. Ingest failed payment
    fail_event = NormalizedEvent(
        event_id=f"evt_stop_fail_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        amount=Decimal("3500.00"),
        currency="INR",
        customer_email="stop_rule@example.com",
        customer_phone="+919876543299",
        external_payment_id="pay_stop_rule_01",
        failure_reason="insufficient_funds",
        failure_description="Account balance low",
    )
    res_fail = EventProcessor.process_normalized_event(db_session, fail_event)
    case_id = res_fail.recovery_case_id

    # Verify action is initially APPROVED
    action_before = db_session.scalar(
        select(RecoveryAction).where(RecoveryAction.recovery_case_id == uuid.UUID(case_id))
    )
    assert action_before is not None
    assert action_before.status == "APPROVED"

    # 2. Ingest payment.captured for same payment
    cap_event = NormalizedEvent(
        event_id=f"evt_stop_cap_{uuid.uuid4()}",
        event_type="payment.captured",
        source="RAZORPAY",
        amount=Decimal("3500.00"),
        currency="INR",
        customer_email="stop_rule@example.com",
        external_payment_id="pay_stop_rule_01",
        payment_status="SUCCESS",
    )
    res_cap = EventProcessor.process_normalized_event(db_session, cap_event)
    assert res_cap.status == "processed"

    # 3. Verify RecoveryCase is RECOVERED and action is CANCELLED
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(case_id)))
    assert case.status == "RECOVERED"

    action_after = db_session.scalar(
        select(RecoveryAction).where(RecoveryAction.recovery_case_id == uuid.UUID(case_id))
    )
    assert action_after.status == "CANCELLED"
    assert "Payment captured" in action_after.reason

    # 4. Verify RECOVERY_ACTION_CANCELLED audit log
    audits = db_session.scalars(
        select(AuditLog).where(AuditLog.recovery_case_id == case.id)
    ).all()
    actions = [a.action for a in audits]
    assert "RECOVERY_ACTION_CANCELLED" in actions


def test_get_recommendation_api_endpoint(client: TestClient, db_session: Session):
    """Test GET /recovery-cases/{case_id}/recommendation HTTP endpoint."""
    event = NormalizedEvent(
        event_id=f"evt_api_rec_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        amount=Decimal("499.00"),
        currency="INR",
        customer_email="api_rec@example.com",
        external_payment_id="pay_api_rec_01",
        failure_code="GATEWAY_ERROR",
        failure_reason="gateway_timeout",
        failure_description="Bank switch down",
    )
    res = EventProcessor.process_normalized_event(db_session, event)
    case_id = res.recovery_case_id

    response = client.get(f"/recovery-cases/{case_id}/recommendation")
    assert response.status_code == 200
    data = response.json()

    assert data["case_id"] == case_id
    assert data["recommended_action"] == "RETRY_PAYMENT"
    assert data["channel"] == "GATEWAY"
    assert data["status"] == "APPROVED"
    assert data["decision_engine_version"] == "decision_engine_v1"
    assert data["policy_engine_version"] == "policy_engine_v1"
    assert data["policy"]["allowed"] is True
    assert len(data["alternatives"]) > 0
    assert len(data["supporting_factors"]) > 0


def test_get_recommendation_not_found(client: TestClient):
    """Test GET /recovery-cases/{case_id}/recommendation returns 404 for non-existent case."""
    random_id = str(uuid.uuid4())
    response = client.get(f"/recovery-cases/{random_id}/recommendation")
    assert response.status_code == 404
