"""Integration tests for Smart Payment Recovery Intervention (Step 9)."""
from datetime import datetime, timezone
from decimal import Decimal
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.payment import Payment
from app.models.recovery_case import RecoveryCase
from app.models.diagnosis import Diagnosis
from app.models.intervention import Intervention
from app.models.recovery_payment_link import RecoveryPaymentLink
from app.models.outcome import RecoveryOutcome
from app.models.learning import LearningExample
from app.models.audit_log import AuditLog
from app.services.intervention_service import InterventionService, InterventionResult
from app.services.event_processor import EventProcessor, WebhookProcessingResult
from app.integrations.razorpay.payment_link_client import RazorpayPaymentLinkClient, PaymentLinkResponse


def _setup_failed_case(db_session: Session, amount: Decimal = Decimal("5000.00"), retry_count: int = 0) -> RecoveryCase:
    """Helper to set up customer, payment, event, and open recovery case."""
    cust = Customer(
        external_customer_id=f"cust_int_{uuid.uuid4()}",
        email="intervention_user@example.com",
        name="Vikram Mehta",
        phone="+919876543210",
    )
    db_session.add(cust)
    db_session.flush()

    payment = Payment(
        external_payment_id=f"pay_int_{uuid.uuid4().hex[:12]}",
        customer_id=cust.id,
        amount=amount,
        currency="INR",
        status="FAILED",
        payment_method="UPI",
        failure_code="BAD_REQUEST_ERROR",
        failure_description="Payment authorization failed",
    )
    db_session.add(payment)
    db_session.flush()

    evt = Event(
        external_event_id=f"evt_int_{uuid.uuid4()}",
        event_type="payment.failed",
        source="RAZORPAY",
        customer_id=cust.id,
        payment_id=payment.id,
        processing_status="PROCESSED",
    )
    db_session.add(evt)
    db_session.flush()

    case = RecoveryCase(
        customer_id=cust.id,
        event_id=evt.id,
        payment_id=payment.id,
        amount_at_risk=amount,
        currency="INR",
        case_type="PAYMENT_FAILURE",
        status="OPEN",
        risk_score=30.0,
        recovery_probability=0.80,
        retry_count=retry_count,
    )
    db_session.add(case)
    db_session.flush()

    diag = Diagnosis(
        recovery_case_id=case.id,
        category="AUTHENTICATION_FAILED",
        explanation="Customer OTP entry failed",
        confidence=0.90,
        engine_version="diagnosis_engine_v1",
    )
    db_session.add(diag)
    db_session.commit()
    db_session.refresh(case)
    return case


def test_dry_run_intervention_lifecycle(db_session: Session):
    """Verify that dry-run intervention creates simulated link, logs audit trail, and sends notification."""
    case = _setup_failed_case(db_session, amount=Decimal("7500.00"))

    # 1. Preview
    preview = InterventionService.preview_intervention(db=db_session, recovery_case_id=case.id)
    assert preview["case_id"] == str(case.id)
    assert preview["policy_status"] in ("APPROVED", "RECOMMENDED")
    assert preview["amount_at_risk"] == 7500.0

    # 2. Execute Dry Run
    result: InterventionResult = InterventionService.execute_intervention(
        db=db_session,
        recovery_case_id=case.id,
        action_override="SEND_PAYMENT_LINK",
        dry_run=True,
    )

    assert result.status == "SENT"
    assert result.action == "SEND_PAYMENT_LINK"
    assert result.payment_link is not None
    assert "https://rzp.io/i/plink_sim_" in result.payment_link.url
    assert result.payment_link.amount == 7500.0

    # 3. Check DB Records
    intervention = db_session.scalar(
        select(Intervention).where(Intervention.recovery_case_id == case.id)
    )
    assert intervention is not None
    assert intervention.status == "SENT"

    plink = db_session.scalar(
        select(RecoveryPaymentLink).where(RecoveryPaymentLink.recovery_case_id == case.id)
    )
    assert plink is not None
    assert plink.status == "SENT"
    assert plink.amount == Decimal("7500.00")

    # 4. Check Audit Events
    audit_actions = db_session.scalars(
        select(AuditLog.action).where(AuditLog.recovery_case_id == case.id)
    ).all()

    assert "PREDICTION_CREATED" in audit_actions
    assert "POLICY_CHECKED" in audit_actions
    assert "INTERVENTION_CREATED" in audit_actions
    assert "PAYMENT_LINK_CREATED" in audit_actions
    assert "NOTIFICATION_GENERATED" in audit_actions


def test_intervention_idempotency_duplicate_prevention(db_session: Session):
    """Verify that calling execute_intervention twice reuses the active payment link and intervention."""
    case = _setup_failed_case(db_session, amount=Decimal("3000.00"))

    # First execution
    res1 = InterventionService.execute_intervention(
        db=db_session, recovery_case_id=case.id, dry_run=True
    )
    assert res1.status == "SENT"

    # Second execution
    res2 = InterventionService.execute_intervention(
        db=db_session, recovery_case_id=case.id, dry_run=True
    )
    assert res2.status == "SENT"
    assert res2.intervention_id == res1.intervention_id
    assert res2.payment_link.razorpay_payment_link_id == res1.payment_link.razorpay_payment_link_id

    # Verify only 1 Intervention and 1 Payment Link exist in DB
    interventions = db_session.scalars(
        select(Intervention).where(Intervention.recovery_case_id == case.id)
    ).all()
    assert len(interventions) == 1

    links = db_session.scalars(
        select(RecoveryPaymentLink).where(RecoveryPaymentLink.recovery_case_id == case.id)
    ).all()
    assert len(links) == 1


def test_policy_rejection_blocks_intervention_and_records_blocked_state(db_session: Session):
    """Verify that exceeding retry cap causes policy to block intervention without creating payment link."""
    # Setup case with 3 prior retries (policy cap = 3)
    case = _setup_failed_case(db_session, amount=Decimal("2000.00"), retry_count=3)

    result = InterventionService.execute_intervention(
        db=db_session, recovery_case_id=case.id, dry_run=True
    )

    assert result.status == "BLOCKED"
    assert result.payment_link is None

    # Check Intervention record
    intervention = db_session.scalar(
        select(Intervention).where(Intervention.recovery_case_id == case.id)
    )
    assert intervention is not None
    assert intervention.status == "BLOCKED"

    # Verify NO payment link was created
    links = db_session.scalars(
        select(RecoveryPaymentLink).where(RecoveryPaymentLink.recovery_case_id == case.id)
    ).all()
    assert len(links) == 0


def test_webhook_payment_captured_stopping_rule_and_reconciliation(db_session: Session):
    """Verify full end-to-end flow: Failed payment -> Intervention -> Customer pays -> Webhook -> RECOVERED."""
    case = _setup_failed_case(db_session, amount=Decimal("6000.00"))

    # 1. Execute Intervention
    exec_res = InterventionService.execute_intervention(
        db=db_session, recovery_case_id=case.id, dry_run=True
    )
    assert exec_res.status == "SENT"

    # 2. Simulate Customer paying the payment link -> Razorpay sends payment.captured webhook
    captured_event_id = f"evt_cap_{uuid.uuid4().hex[:12]}"
    payment_id = case.payment.external_payment_id

    from app.schemas.event import NormalizedEvent
    capture_payload = NormalizedEvent(
        event_id=captured_event_id,
        event_type="payment.captured",
        source="RAZORPAY",
        occurred_at=datetime.now(timezone.utc),
        external_customer_id=case.customer.external_customer_id,
        external_payment_id=payment_id,
        amount=Decimal("6000.00"),
        currency="INR",
        raw_payload={"id": payment_id, "amount": 600000, "status": "captured"},
    )

    proc_res: WebhookProcessingResult = EventProcessor.process_normalized_event(
        db=db_session, event=capture_payload
    )
    assert proc_res.status == "processed"

    db_session.refresh(case)
    # 3. Verify RecoveryCase is marked RECOVERED
    assert case.status == "RECOVERED"
    assert case.recovered_amount == Decimal("6000.00")

    # 4. Verify Intervention is marked SUCCEEDED
    intervention = db_session.scalar(
        select(Intervention).where(Intervention.recovery_case_id == case.id)
    )
    assert intervention.status == "SUCCEEDED"
    assert intervention.completed_at is not None

    # 5. Verify RecoveryPaymentLink is marked PAID
    plink = db_session.scalar(
        select(RecoveryPaymentLink).where(RecoveryPaymentLink.recovery_case_id == case.id)
    )
    assert plink.status == "PAID"
    assert plink.paid_at is not None

    # 6. Verify RecoveryOutcome is created
    outcome = db_session.scalar(
        select(RecoveryOutcome).where(RecoveryOutcome.recovery_case_id == case.id)
    )
    assert outcome is not None
    assert outcome.outcome_type == "RECOVERED"
    assert outcome.amount_recovered == Decimal("6000.00")

    # 7. Verify LearningExample is finalized with label = 1
    learning_ex = db_session.scalar(
        select(LearningExample).where(LearningExample.recovery_case_id == case.id)
    )
    assert learning_ex is not None
    assert learning_ex.label == 1
    assert learning_ex.is_finalized is True


def test_cannot_intervene_on_already_recovered_case(db_session: Session):
    """Verify that an already recovered case rejects any new intervention attempts."""
    case = _setup_failed_case(db_session, amount=Decimal("1500.00"))
    case.status = "RECOVERED"
    case.recovered_amount = Decimal("1500.00")
    db_session.commit()

    res = InterventionService.execute_intervention(
        db=db_session, recovery_case_id=case.id, dry_run=True
    )
    assert res.status == "ALREADY_RECOVERED"
    assert res.payment_link is None
