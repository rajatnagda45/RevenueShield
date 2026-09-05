import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Customer,
    Payment,
    Subscription,
    Invoice,
    Event,
    RecoveryCase,
    Diagnosis,
    RecoveryAction,
    ActionOutcome,
    PromiseToPay,
    CommunicationLog,
    AuditLog,
    ModelVersion,
)


def test_customer_creation(db_session: Session) -> None:
    """Verify customer entity persistence and default values."""
    customer = Customer(
        external_customer_id="cust_ext_101",
        name="Acme Corp",
        email="billing@acme.com",
        phone="+1234567890",
        segment="ENTERPRISE",
        preferred_channel="WHATSAPP",
        dnd_enabled=False,
    )
    db_session.add(customer)
    db_session.commit()
    db_session.refresh(customer)

    assert customer.id is not None
    assert customer.name == "Acme Corp"
    assert customer.email == "billing@acme.com"
    assert customer.segment == "ENTERPRISE"
    assert customer.dnd_enabled is False
    assert customer.created_at is not None


def test_payment_references_customer(db_session: Session) -> None:
    """Verify payment entity creation referencing a customer foreign key."""
    customer = Customer(
        external_customer_id="cust_ext_102",
        name="Tech Solutions",
        email="tech@solutions.io",
    )
    db_session.add(customer)
    db_session.commit()

    payment = Payment(
        external_payment_id="pay_ext_501",
        customer_id=customer.id,
        amount=Decimal("150.00"),
        currency="USD",
        status="FAILED",
        payment_method="CARD",
        failure_code="insufficient_funds",
        failure_description="Card issuer declined due to insufficient balance",
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)

    assert payment.id is not None
    assert payment.customer_id == customer.id
    assert payment.customer.name == "Tech Solutions"
    assert payment.amount == Decimal("150.00")
    assert payment.status == "FAILED"


def test_event_persistence_with_json_payload(db_session: Session) -> None:
    """Verify event creation with arbitrary JSONB/JSON payload."""
    now = datetime.now(timezone.utc)
    event_payload = {
        "gateway": "stripe",
        "gateway_event_id": "evt_stripe_999",
        "error": {
            "code": "card_declined",
            "decline_code": "insufficient_funds",
            "message": "Your card has insufficient funds.",
        },
        "attempt_count": 2,
    }

    event = Event(
        external_event_id="evt_idempotency_101",
        event_type="payment.failed",
        source="STRIPE",
        payload=event_payload,
        occurred_at=now,
        processing_status="RECEIVED",
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    assert event.id is not None
    assert event.external_event_id == "evt_idempotency_101"
    assert event.payload["gateway"] == "stripe"
    assert event.payload["error"]["decline_code"] == "insufficient_funds"


def test_event_idempotency_prevents_duplicate_external_event_id(db_session: Session) -> None:
    """Verify unique constraint on external_event_id prevents duplicate ingestion."""
    now = datetime.now(timezone.utc)
    event1 = Event(
        external_event_id="evt_unique_key_001",
        event_type="payment.failed",
        source="STRIPE",
        payload={"transaction": 1},
        occurred_at=now,
    )
    db_session.add(event1)
    db_session.commit()

    event2 = Event(
        external_event_id="evt_unique_key_001",
        event_type="payment.failed",
        source="STRIPE",
        payload={"transaction": 2},
        occurred_at=now,
    )
    db_session.add(event2)

    with pytest.raises(IntegrityError):
        db_session.commit()

    db_session.rollback()


def test_recovery_case_referencing_customer_event_payment(db_session: Session) -> None:
    """Verify central RecoveryCase references Customer, Event, and Payment entities."""
    now = datetime.now(timezone.utc)
    customer = Customer(
        external_customer_id="cust_ext_103",
        name="Global Logistics",
        email="billing@globallogistics.com",
    )
    db_session.add(customer)
    db_session.commit()

    payment = Payment(
        external_payment_id="pay_ext_502",
        customer_id=customer.id,
        amount=Decimal("499.00"),
        currency="USD",
        status="FAILED",
        payment_method="CARD",
        failure_code="expired_card",
    )
    event = Event(
        external_event_id="evt_idempotency_102",
        event_type="payment.failed",
        source="GATEWAY",
        customer_id=customer.id,
        payload={"error": "card_expired"},
        occurred_at=now,
    )
    db_session.add_all([payment, event])
    db_session.commit()

    recovery_case = RecoveryCase(
        customer_id=customer.id,
        event_id=event.id,
        payment_id=payment.id,
        amount_at_risk=Decimal("499.00"),
        currency="USD",
        case_type="PAYMENT_FAILURE",
        status="OPEN",
        risk_score=0.35,
        recovery_probability=0.82,
        recommended_channel="WHATSAPP",
        recommended_action="SEND_PAYMENT_LINK",
        retry_count=0,
    )
    db_session.add(recovery_case)
    db_session.commit()
    db_session.refresh(recovery_case)

    assert recovery_case.id is not None
    assert recovery_case.customer_id == customer.id
    assert recovery_case.originating_event.id == event.id
    assert recovery_case.payment.id == payment.id
    assert recovery_case.amount_at_risk == Decimal("499.00")
    assert recovery_case.status == "OPEN"
    assert recovery_case.recovery_probability == 0.82


def test_recovery_lifecycle_and_relationship_chain(db_session: Session) -> None:
    """Verify complete relationship flow: Customer -> Event -> RecoveryCase -> Diagnosis -> Action -> Outcome."""
    now = datetime.now(timezone.utc)
    customer = Customer(
        external_customer_id="cust_ext_104",
        name="Retail Hub",
        email="finance@retailhub.com",
    )
    db_session.add(customer)
    db_session.commit()

    event = Event(
        external_event_id="evt_idempotency_103",
        event_type="invoice.overdue",
        source="BILLING_CRON",
        customer_id=customer.id,
        payload={"overdue_days": 15},
        occurred_at=now,
    )
    db_session.add(event)
    db_session.commit()

    case = RecoveryCase(
        customer_id=customer.id,
        event_id=event.id,
        amount_at_risk=Decimal("1200.00"),
        currency="USD",
        case_type="INVOICE_OVERDUE",
        status="IN_PROGRESS",
    )
    db_session.add(case)
    db_session.commit()

    # 1. Diagnosis
    diagnosis = Diagnosis(
        recovery_case_id=case.id,
        category="RECEIVABLE_DELAY",
        failure_code="OVERDUE_NET30",
        explanation="Customer AP department delayed payment processing cycle.",
        confidence=0.88,
    )
    # 2. Recovery Action
    action = RecoveryAction(
        recovery_case_id=case.id,
        action_type="SEND_WHATSAPP",
        channel="WHATSAPP",
        status="EXECUTED",
        reason="High engagement channel for SMB billing contact.",
        confidence=0.91,
        executed_at=now,
    )
    db_session.add_all([diagnosis, action])
    db_session.commit()

    # 3. Action Outcome
    outcome = ActionOutcome(
        action_id=action.id,
        recovery_case_id=case.id,
        outcome_type="PROMISE_TO_PAY",
        recovered_amount=Decimal("0.00"),
        response_data={"message": "We will process the invoice next Tuesday"},
        occurred_at=now,
    )
    # 4. Promise To Pay
    ptp = PromiseToPay(
        recovery_case_id=case.id,
        customer_id=customer.id,
        promised_amount=Decimal("1200.00"),
        promised_date=now + timedelta(days=5),
        status="ACTIVE",
    )
    # 5. Communication Log
    comm_log = CommunicationLog(
        customer_id=customer.id,
        recovery_case_id=case.id,
        channel="WHATSAPP",
        direction="OUTBOUND",
        provider_message_id="wamid_ABC123XYZ",
        content="Invoice #INV-2026-001 is overdue. Click here to view and pay.",
        status="DELIVERED",
    )
    # 6. Audit Log
    audit = AuditLog(
        recovery_case_id=case.id,
        actor_type="AI",
        actor_id="recovery_agent_v1",
        action="ACTION_TRIGGERED",
        entity_type="RecoveryAction",
        entity_id=str(action.id),
        audit_metadata={"rule_applied": "high_propensity_whatsapp_outreach"},
    )
    db_session.add_all([outcome, ptp, comm_log, audit])
    db_session.commit()
    db_session.refresh(case)

    # Validate full relationship traversal from RecoveryCase
    assert len(case.diagnoses) == 1
    assert case.diagnoses[0].category == "RECEIVABLE_DELAY"
    assert len(case.recovery_actions) == 1
    assert case.recovery_actions[0].action_type == "SEND_WHATSAPP"
    assert len(case.recovery_actions[0].outcomes) == 1
    assert case.recovery_actions[0].outcomes[0].outcome_type == "PROMISE_TO_PAY"
    assert len(case.promise_to_pays) == 1
    assert case.promise_to_pays[0].promised_amount == Decimal("1200.00")
    assert len(case.communication_logs) == 1
    assert case.communication_logs[0].provider_message_id == "wamid_ABC123XYZ"
    assert len(case.audit_logs) == 1
    assert case.audit_logs[0].audit_metadata["rule_applied"] == "high_propensity_whatsapp_outreach"


def test_model_version_registry(db_session: Session) -> None:
    """Verify ML ModelVersion registry entity persistence."""
    model = ModelVersion(
        model_name="churn_propensity_classifier",
        version="v1.2.0",
        algorithm="LightGBM",
        metrics={"auc_roc": 0.892, "f1_score": 0.841, "precision": 0.865},
        training_dataset_version="ds_2026_q1_v3",
        status="ACTIVE",
    )
    db_session.add(model)
    db_session.commit()
    db_session.refresh(model)

    assert model.id is not None
    assert model.model_name == "churn_propensity_classifier"
    assert model.metrics["auc_roc"] == 0.892
    assert model.status == "ACTIVE"
