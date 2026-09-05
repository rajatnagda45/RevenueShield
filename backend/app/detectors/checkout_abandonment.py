"""Checkout abandonment: orders that were created (or cancelled by the user) and never paid.

Detection has two triggers:
1. `payment.failed` with a user-cancellation reason and an order id (immediate).
2. A sweep over Orders still CREATED/ATTEMPTED after `abandon_after_minutes` (Razorpay emits no
   "order abandoned" webhook, so orders are registered via `order.*` webhooks or the merchant API).

Recovery is deliberately light: a payment link for the same order amount, at most two touches,
no voice calls (see SurfaceProfile). `order.paid` closes the case through the outcome engine.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.detectors.case_factory import CaseFactory
from app.domain.surfaces import LeakSurface
from app.models.customer import Customer
from app.models.event import Event
from app.models.order import Order
from app.models.recovery_case import RecoveryCase
from app.outcomes.engine import OutcomeEngine
from app.schemas.event import NormalizedEvent

logger = logging.getLogger(__name__)

DEFAULT_ABANDON_AFTER_MINUTES = 30


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class CheckoutAbandonmentDetector:
    """Registers orders and opens CHECKOUT_ABANDONMENT cases for the ones that go quiet."""

    @classmethod
    def upsert_order(
        cls,
        db: Session,
        *,
        external_order_id: str,
        amount: Decimal,
        currency: str = "INR",
        customer: Optional[Customer] = None,
        created_at: Optional[datetime] = None,
        status: str = "CREATED",
        receipt: Optional[str] = None,
        notes: Optional[Dict[str, Any]] = None,
        attempts: int = 0,
        amount_paid: Decimal = Decimal("0.00"),
    ) -> Order:
        order = db.scalar(select(Order).where(Order.external_order_id == external_order_id))
        if order is None:
            order = Order(
                external_order_id=external_order_id,
                customer_id=customer.id if customer else None,
                amount=Decimal(str(amount)),
                amount_paid=Decimal(str(amount_paid or 0)),
                currency=(currency or "INR").upper(),
                status=status.upper(),
                receipt=receipt,
                notes=notes or {},
                attempts=attempts or 0,
                razorpay_created_at=created_at or datetime.now(timezone.utc),
            )
            db.add(order)
        else:
            if customer and not order.customer_id:
                order.customer_id = customer.id
            if order.status not in ("PAID",):
                order.status = status.upper() if status else order.status
            order.attempts = max(order.attempts or 0, attempts or 0)
            if amount_paid:
                order.amount_paid = Decimal(str(amount_paid))
            if notes:
                merged = dict(order.notes or {})
                merged.update(notes)
                order.notes = merged
        db.flush()
        return order

    @classmethod
    def mark_attempt(cls, db: Session, order: Order, at: Optional[datetime] = None) -> None:
        order.attempts = (order.attempts or 0) + 1
        order.last_payment_attempt_at = at or datetime.now(timezone.utc)
        if order.status == "CREATED":
            order.status = "ATTEMPTED"
        db.flush()

    @classmethod
    def open_case_for_order(
        cls,
        db: Session,
        *,
        order: Order,
        customer: Customer,
        db_event: Event,
        reason: str,
        batch_id: Optional[str] = None,
        source_event: Optional[NormalizedEvent] = None,
    ) -> RecoveryCase:
        """Open (or return the existing) abandonment case for an order."""
        if order.abandonment_case_id:
            existing = db.scalar(select(RecoveryCase).where(RecoveryCase.id == order.abandonment_case_id))
            if existing and existing.status not in ("RECOVERED", "CLOSED"):
                return existing

        diag_event = NormalizedEvent(
            event_id=db_event.external_event_id,
            event_type="checkout.abandoned",
            amount=order.amount,
            currency=order.currency,
            external_order_id=order.external_order_id,
            external_customer_id=customer.external_customer_id,
            customer_email=customer.email,
            customer_phone=customer.phone,
            payment_method=(source_event.payment_method if source_event else None),
            failure_reason=(source_event.failure_reason if source_event and source_event.failure_reason else "checkout_abandoned"),
            failure_code=(source_event.failure_code if source_event else "checkout_abandoned"),
            failure_description=(source_event.failure_description if source_event else f"Order {order.external_order_id} {reason}"),
            occurred_at=(source_event.occurred_at if source_event else None) or datetime.now(timezone.utc),
            metadata={"order_id": order.external_order_id, "bank": (source_event.metadata.get("bank") if source_event else None), "replay": bool(source_event and (source_event.metadata or {}).get("replay"))},
        )
        created_at = _aware(order.razorpay_created_at) or datetime.now(timezone.utc)
        case = CaseFactory.open_case(
            db,
            customer=customer,
            db_event=db_event,
            surface=LeakSurface.CHECKOUT_ABANDONMENT,
            amount=order.amount - (order.amount_paid or Decimal("0.00")),
            currency=order.currency,
            diagnosis_event=diag_event,
            batch_id=batch_id or (order.notes or {}).get("batch_id"),
            metadata={
                "order_id": order.external_order_id,
                "order_created_at": created_at.isoformat(),
                "attempts": order.attempts or 0,
                "abandonment_reason": reason,
                "receipt": order.receipt,
            },
            actor_id="checkout_abandonment_detector_v1",
            ledger_reference=order.external_order_id,
        )
        order.status = "ABANDONED"
        order.abandonment_case_id = case.id
        db.flush()
        return case

    @classmethod
    def on_order_paid(
        cls,
        db: Session,
        *,
        order: Order,
        amount_paid: Decimal,
        paid_at: Optional[datetime],
        provider_event_id: str,
        provider_payment_id: Optional[str] = None,
    ) -> Optional[RecoveryCase]:
        """`order.paid`: close the abandonment case as recovered (stopping rules fire in OutcomeEngine)."""
        order.status = "PAID"
        order.amount_paid = Decimal(str(amount_paid))
        order.paid_at = paid_at or datetime.now(timezone.utc)
        db.flush()
        if not order.abandonment_case_id:
            return None
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == order.abandonment_case_id))
        if case and case.status not in ("RECOVERED", "CLOSED"):
            OutcomeEngine.process_payment_capture(
                db=db, recovery_case=case, captured_amount=Decimal(str(amount_paid)),
                captured_at=order.paid_at, provider_event_id=provider_event_id,
                provider_payment_id=provider_payment_id or order.external_order_id,
            )
            return case
        return case

    @classmethod
    def sweep(
        cls,
        db: Session,
        reference_time: Optional[datetime] = None,
        abandon_after_minutes: int = DEFAULT_ABANDON_AFTER_MINUTES,
        limit: int = 500,
    ) -> Dict[str, Any]:
        """Open cases for orders that have been quiet longer than the abandonment window."""
        now = reference_time or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=abandon_after_minutes)
        candidates: List[Order] = list(
            db.scalars(
                select(Order)
                .where(Order.status.in_(["CREATED", "ATTEMPTED"]), Order.abandonment_case_id.is_(None), Order.razorpay_created_at <= cutoff)
                .order_by(Order.razorpay_created_at.asc())
                .limit(limit)
            ).all()
        )
        opened, skipped = [], []
        for order in candidates:
            customer = order.customer or (db.scalar(select(Customer).where(Customer.id == order.customer_id)) if order.customer_id else None)
            if customer is None:
                skipped.append({"order_id": order.external_order_id, "reason": "no_customer_identity"})
                continue
            evt = CaseFactory.synthetic_event(
                db, customer=customer, event_type="checkout.abandoned", entity_id=order.external_order_id,
                payload={"order_id": order.external_order_id, "amount": float(order.amount), "abandon_after_minutes": abandon_after_minutes},
                occurred_at=now,
            )
            case = cls.open_case_for_order(
                db, order=order, customer=customer, db_event=evt,
                reason=f"no payment within {abandon_after_minutes} minutes of checkout",
            )
            opened.append({"order_id": order.external_order_id, "case_id": str(case.id), "amount": float(case.amount_at_risk)})
        db.flush()
        logger.info(f"[CHECKOUT_SWEEP] candidates={len(candidates)} opened={len(opened)} skipped={len(skipped)}")
        return {"reference_time": now.isoformat(), "candidates": len(candidates), "opened": opened, "skipped": skipped}
