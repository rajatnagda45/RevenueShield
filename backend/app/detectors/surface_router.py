"""Routes non-payment Razorpay events (orders, invoices, subscriptions) to the right detector."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.detectors.checkout_abandonment import CheckoutAbandonmentDetector
from app.detectors.mandates import MandateDetector
from app.detectors.receivables import ReceivablesDetector
from app.models.customer import Customer
from app.models.event import Event
from app.models.payment import Payment
from app.models.recovery_case import RecoveryCase
from app.schemas.event import NormalizedEvent

logger = logging.getLogger(__name__)


def _epoch(ts) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None
    except Exception:
        return None


class SurfaceEventRouter:
    """Dispatches order.*, invoice.* and subscription.* events."""

    HANDLED_PREFIXES = ("order.", "invoice.", "subscription.")

    @classmethod
    def handles(cls, event_type: str) -> bool:
        return any(event_type.startswith(p) for p in cls.HANDLED_PREFIXES)

    @classmethod
    def handle(
        cls,
        db: Session,
        *,
        event: NormalizedEvent,
        customer: Customer,
        db_event: Event,
        payment: Optional[Payment],
    ) -> Optional[RecoveryCase]:
        et = event.event_type

        # ---------------------------------------------------------------- orders
        if et.startswith("order."):
            if not event.external_order_id:
                return None
            ometa = (event.metadata or {}).get("order", {}) or {}
            order = CheckoutAbandonmentDetector.upsert_order(
                db, external_order_id=event.external_order_id,
                amount=Decimal(str(ometa.get("amount") or event.amount or 0)), currency=event.currency, customer=customer,
                created_at=_epoch(ometa.get("created_at")) or event.occurred_at,
                status=str(ometa.get("status") or "created"), receipt=ometa.get("receipt"),
                notes=(event.metadata or {}).get("notes") or {}, attempts=int(ometa.get("attempts") or 0),
                amount_paid=Decimal(str(ometa.get("amount_paid") or 0)),
            )
            if et == "order.paid":
                return CheckoutAbandonmentDetector.on_order_paid(
                    db, order=order, amount_paid=Decimal(str(ometa.get("amount_paid") or ometa.get("amount") or event.amount or order.amount)),
                    paid_at=event.occurred_at, provider_event_id=event.event_id, provider_payment_id=event.external_payment_id,
                )
            return None

        # -------------------------------------------------------------- invoices
        if et.startswith("invoice."):
            if not event.external_invoice_id:
                return None
            imeta = (event.metadata or {}).get("invoice", {}) or {}
            due = _epoch(imeta.get("due_by")) or event.occurred_at
            total = Decimal(str(imeta.get("amount") or event.amount or 0))
            paid = Decimal(str(imeta.get("amount_paid") or 0))
            status_map = {"invoice.paid": "PAID", "invoice.partially_paid": "PARTIALLY_PAID", "invoice.expired": "EXPIRED", "invoice.issued": "ISSUED"}
            invoice = ReceivablesDetector.upsert_invoice(
                db, external_invoice_id=event.external_invoice_id, customer=customer, amount=total or event.amount,
                currency=event.currency, due_date=due, status=status_map.get(et, str(imeta.get("status") or "ISSUED")),
                # payment handlers own amount_paid so they can compute the increment
                amount_paid=paid if et not in ("invoice.partially_paid", "invoice.paid") else None,
                issued_at=_epoch(imeta.get("issued_at")), source="RAZORPAY", short_url=imeta.get("short_url"),
                notes=(event.metadata or {}).get("notes") or {},
            )
            db_event.invoice_id = invoice.id
            if et == "invoice.expired":
                return ReceivablesDetector.on_invoice_expired(db, invoice=invoice, customer=customer, db_event=db_event, now=event.occurred_at, batch_id=event.batch_id)
            if et == "invoice.partially_paid":
                return ReceivablesDetector.on_invoice_partially_paid(db, invoice=invoice, amount_paid_total=paid, payment_id=event.external_payment_id, provider_event_id=event.event_id, occurred_at=event.occurred_at)
            if et == "invoice.paid":
                return ReceivablesDetector.on_invoice_paid(db, invoice=invoice, amount_paid=paid or total, payment_id=event.external_payment_id, provider_event_id=event.event_id, occurred_at=event.occurred_at)
            return None

        # ---------------------------------------------------------- subscriptions
        if et.startswith("subscription."):
            if not event.external_subscription_id:
                return None
            if et == "subscription.pending":
                case = MandateDetector.on_subscription_pending(db, event=event, customer=customer, db_event=db_event, payment=payment)
            elif et == "subscription.halted":
                case = MandateDetector.on_subscription_halted(db, event=event, customer=customer, db_event=db_event, payment=payment)
            elif et in ("subscription.charged", "subscription.activated", "subscription.resumed"):
                case = MandateDetector.on_subscription_recovered(db, event=event, customer=customer)
            else:
                MandateDetector.upsert_subscription(db, event=event, customer=customer)
                case = None
            sub = db.scalar(select(RecoveryCase.subscription_id).where(RecoveryCase.id == case.id)) if case else None
            if sub:
                db_event.subscription_id = sub
            return case

        return None
