"""End-to-End integration tests for Bounded Recovery Execution, Money Tracking, and Stopping Rules."""
from decimal import Decimal
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.recovery_action import RecoveryAction
from app.models.execution import RecoveryExecution
from app.models.audit_log import AuditLog
from app.schemas.event import NormalizedEvent
from app.services.event_processor import EventProcessor


def test_full_execution_flow_and_money_tracking_invariants(client: TestClient, db_session: Session):
    """Verify that executing an action creates a RecoveryExecution record and preserves amount_at_risk until capture."""
    # 1. Ingest failed payment
    fail_event = NormalizedEvent(
        event_id=f"evt_exec_flow_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        amount=Decimal("1500.00"),
        currency="INR",
        customer_email="vikram.malhotra@example.com",
        customer_name="Vikram Malhotra",
        customer_phone="+919876543111",
        external_payment_id="pay_exec_flow_01",
        payment_method="CARD",
        failure_code="BAD_REQUEST_ERROR",
        failure_reason="incorrect_otp",
        failure_description="OTP expired during checkout.",
    )
    fail_res = EventProcessor.process_normalized_event(db_session, fail_event)
    case_id = fail_res.recovery_case_id

    # 2. Call POST /recovery-cases/{case_id}/execute
    response = client.post(f"/recovery-cases/{case_id}/execute")
    assert response.status_code == 200
    exec_data = response.json()

    assert exec_data["case_id"] == case_id
    assert exec_data["action_type"] == "SEND_PAYMENT_LINK"
    assert exec_data["status"] == "SUCCEEDED"
    assert exec_data["provider"] == "DRY_RUN"
    assert exec_data["provider_url"] is not None
    assert exec_data["amount"] == 1500.0
    assert exec_data["currency"] == "INR"

    # 3. CRITICAL MONEY TRACKING CHECK:
    # Verify RecoveryCase remains OPEN and amount_recovered is STILL None/0
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(case_id)))
    assert case.status == "OPEN"
    assert case.amount_at_risk == Decimal("1500.00")
    assert case.recovered_amount is None

    # 4. Verify RecoveryAction is marked EXECUTED
    action = db_session.scalar(select(RecoveryAction).where(RecoveryAction.recovery_case_id == case.id))
    assert action.status == "EXECUTED"
    assert action.executed_at is not None

    # 5. Verify AuditLogs recorded execution lifecycle
    audits = db_session.scalars(select(AuditLog).where(AuditLog.recovery_case_id == case.id)).all()
    actions = [a.action for a in audits]
    assert "EXECUTION_STARTED" in actions
    assert "EXECUTION_SUCCEEDED" in actions

    # 6. Test Idempotency: Re-executing returns the same execution without error
    res_idempotent = client.post(f"/recovery-cases/{case_id}/execute")
    assert res_idempotent.status_code == 200
    idempotent_data = res_idempotent.json()
    assert idempotent_data["execution_id"] == exec_data["execution_id"]
    assert idempotent_data["provider_reference"] == exec_data["provider_reference"]

    # 7. Ingest payment.captured: Closes the recovery loop (Stopping Rule)
    cap_event = NormalizedEvent(
        event_id=f"evt_exec_cap_{uuid.uuid4()}",
        event_type="payment.captured",
        source="RAZORPAY",
        amount=Decimal("1500.00"),
        currency="INR",
        customer_email="vikram.malhotra@example.com",
        external_payment_id="pay_exec_flow_01",
        payment_status="SUCCESS",
    )
    cap_res = EventProcessor.process_normalized_event(db_session, cap_event)
    assert cap_res.status == "processed"

    # Verify case is now RECOVERED with recovered_amount updated
    db_session.refresh(case)
    assert case.status == "RECOVERED"
    assert case.recovered_amount == Decimal("1500.00")


def test_cannot_execute_on_already_recovered_case(client: TestClient, db_session: Session):
    """Verify that execution guard blocks attempting to execute an action on a recovered case."""
    # 1. Ingest failed payment
    fail_event = NormalizedEvent(
        event_id=f"evt_block_test_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        amount=Decimal("800.00"),
        currency="INR",
        customer_email="block_user@example.com",
        external_payment_id="pay_block_01",
        failure_reason="gateway_timeout",
    )
    fail_res = EventProcessor.process_normalized_event(db_session, fail_event)
    case_id = fail_res.recovery_case_id

    # 2. Mark case recovered before execution
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.id == uuid.UUID(case_id)))
    case.status = "RECOVERED"
    case.recovered_amount = Decimal("800.00")
    db_session.flush()

    # 3. Call execute -> Guard should block
    response = client.post(f"/recovery-cases/{case_id}/execute")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "BLOCKED"
    assert data["error_code"] == "CASE_INACTIVE"
