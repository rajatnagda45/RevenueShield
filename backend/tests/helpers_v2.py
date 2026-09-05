"""Shared fixtures/helpers for v2 (measurement, surfaces, agent) tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.event import Event
from app.models.recovery_case import RecoveryCase


def make_customer(db: Session, *, phone: str = "+919876543210", email: Optional[str] = None, name: str = "Test Customer") -> Customer:
    uid = uuid.uuid4().hex[:8]
    customer = Customer(
        id=uuid.uuid4(),
        external_customer_id=f"cust_{uid}",
        name=name,
        email=email or f"cust_{uid}@example.com",
        phone=phone,
        segment="STANDARD",
        preferred_channel="WHATSAPP",
        dnd_enabled=False,
        timezone="Asia/Kolkata",
    )
    db.add(customer)
    db.flush()
    return customer


def make_case(
    db: Session,
    customer: Customer,
    *,
    amount: float = 2500.0,
    status: str = "OPEN",
    case_type: str = "PAYMENT_FAILURE",
    leak_surface: str = "PAYMENT_FAILURE",
    batch_id: Optional[str] = None,
    experiment_arm: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> RecoveryCase:
    evt = Event(
        id=uuid.uuid4(),
        external_event_id=f"evt_{uuid.uuid4().hex[:12]}",
        source="RAZORPAY",
        event_type="payment.failed",
        customer_id=customer.id,
        payload={"amount": int(amount * 100), "currency": "INR"},
        occurred_at=created_at or datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )
    db.add(evt)
    db.flush()

    case = RecoveryCase(
        id=uuid.uuid4(),
        customer_id=customer.id,
        event_id=evt.id,
        case_type=case_type,
        leak_surface=leak_surface,
        batch_id=batch_id,
        experiment_arm=experiment_arm,
        amount_at_risk=Decimal(str(amount)),
        currency="INR",
        status=status,
        retry_count=0,
    )
    if created_at is not None:
        case.created_at = created_at
    db.add(case)
    db.flush()
    return case
