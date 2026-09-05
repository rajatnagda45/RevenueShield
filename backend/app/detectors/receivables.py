"""Overdue receivables (B2B collections).

Sources: Razorpay Invoices webhooks (`invoice.expired`, `invoice.partially_paid`, `invoice.paid`)
and an import API for AR ledgers kept outside Razorpay. A RECEIVABLE_OVERDUE case opens when an
unpaid invoice passes its due date; ageing buckets (1-7, 8-30, 31-60, 60+) drive the dunning
ladder: reminder -> statement + payment link -> promise-to-pay call -> escalation to merchant
finance (human approval) -> legal notice draft (approval, never auto-sent).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.detectors.case_factory import CaseFactory
from app.domain.surfaces import LeakSurface, ageing_bucket
from app.ledger.service import LedgerService
from app.models.customer import Customer
from app.models.event import Event
from app.models.invoice import Invoice
from app.models.recovery_case import RecoveryCase
from app.outcomes.engine import OutcomeEngine
from app.schemas.event import NormalizedEvent

logger = logging.getLogger(__name__)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ReceivableImportRow(BaseModel):
    """One AR ledger line imported from the merchant's accounting system."""

    external_invoice_id: str
    amount: Decimal = Field(..., gt=0)
    currency: str = "INR"
    due_date: datetime
    issued_at: Optional[datetime] = None
    amount_paid: Decimal = Decimal("0.00")
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    external_customer_id: Optional[str] = None
    customer_reference: Optional[str] = None
    notes: Dict[str, Any] = Field(default_factory=dict)


class ReceivablesDetector:
    """Tracks invoices and opens overdue-receivable cases."""

    @classmethod
    def upsert_invoice(
        cls,
        db: Session,
        *,
        external_invoice_id: str,
        customer: Customer,
        amount: Decimal,
        currency: str,
        due_date: datetime,
        status: str = "ISSUED",
        amount_paid: Optional[Decimal] = None,
        issued_at: Optional[datetime] = None,
        source: str = "RAZORPAY",
        short_url: Optional[str] = None,
        customer_reference: Optional[str] = None,
        notes: Optional[Dict[str, Any]] = None,
    ) -> Invoice:
        inv = db.scalar(select(Invoice).where(Invoice.external_invoice_id == external_invoice_id))
        if inv is None:
            inv = Invoice(
                external_invoice_id=external_invoice_id, customer_id=customer.id, amount=Decimal(str(amount)),
                currency=(currency or "INR").upper(), status=status.upper(), due_date=due_date,
                amount_paid=Decimal(str(amount_paid or 0)), issued_at=issued_at, source=source,
                short_url=short_url, customer_reference=customer_reference, notes=notes or {},
            )
            db.add(inv)
        else:
            if inv.status != "PAID":
                inv.status = status.upper()
            inv.amount = Decimal(str(amount))
            inv.due_date = due_date
            if amount_paid is not None:
                inv.amount_paid = Decimal(str(amount_paid))
            if short_url:
                inv.short_url = short_url
            if notes:
                merged = dict(inv.notes or {})
                merged.update(notes)
                inv.notes = merged
        db.flush()
        return inv

    @classmethod
    def import_rows(cls, db: Session, rows: Iterable[ReceivableImportRow]) -> Dict[str, Any]:
        from app.services.customer_service import CustomerService  # lazy: avoids the services<->detectors cycle

        imported: List[str] = []
        for row in rows:
            probe = NormalizedEvent(
                event_id=f"import_{row.external_invoice_id}", event_type="invoice.imported", source="IMPORT",
                amount=row.amount, currency=row.currency, external_customer_id=row.external_customer_id,
                customer_email=row.customer_email, customer_phone=row.customer_phone, customer_name=row.customer_name,
                external_invoice_id=row.external_invoice_id,
            )
            customer = CustomerService.resolve_or_create_customer(db, probe)
            cls.upsert_invoice(
                db, external_invoice_id=row.external_invoice_id, customer=customer, amount=row.amount, currency=row.currency,
                due_date=_aware(row.due_date), status="PARTIALLY_PAID" if row.amount_paid > 0 else "ISSUED",
                amount_paid=row.amount_paid, issued_at=_aware(row.issued_at), source="IMPORT",
                customer_reference=row.customer_reference, notes=row.notes,
            )
            imported.append(row.external_invoice_id)
        db.flush()
        return {"imported": len(imported), "invoice_ids": imported}

    @classmethod
    def _open_case_for_invoice(cls, db: Session, invoice: Invoice, customer: Customer, db_event: Event, now: datetime, batch_id: Optional[str] = None) -> RecoveryCase:
        existing = db.scalar(
            select(RecoveryCase).where(
                RecoveryCase.invoice_id == invoice.id,
                RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PAUSED", "PTP"]),
            )
        )
        if existing:
            return existing

        due = _aware(invoice.due_date) or now
        days_overdue = max((now - due).total_seconds() / 86400.0, 0.0)
        outstanding = Decimal(str(invoice.amount)) - Decimal(str(invoice.amount_paid or 0))
        diag_event = NormalizedEvent(
            event_id=db_event.external_event_id, event_type="invoice.overdue", amount=outstanding, currency=invoice.currency,
            external_invoice_id=invoice.external_invoice_id, external_customer_id=customer.external_customer_id,
            customer_email=customer.email, customer_phone=customer.phone, failure_reason="receivable_overdue",
            failure_code="receivable_overdue", failure_description=f"Invoice {invoice.external_invoice_id} is {days_overdue:.0f} days past due",
            metadata={"invoice_id": invoice.external_invoice_id},
        )
        case = CaseFactory.open_case(
            db, customer=customer, db_event=db_event, surface=LeakSurface.RECEIVABLE_OVERDUE, amount=outstanding,
            currency=invoice.currency, diagnosis_event=diag_event, invoice=invoice,
            batch_id=batch_id or (invoice.notes or {}).get("batch_id"),
            metadata={
                "invoice_id": invoice.external_invoice_id, "due_date": due.isoformat(), "days_overdue": round(days_overdue, 1),
                "ageing_bucket": ageing_bucket(days_overdue), "invoice_amount": float(invoice.amount),
                "amount_paid": float(invoice.amount_paid or 0), "source": invoice.source, "customer_reference": invoice.customer_reference,
            },
            actor_id="receivables_detector_v1", ledger_reference=invoice.external_invoice_id,
        )
        if invoice.status not in ("PAID",):
            invoice.status = "OVERDUE"
        db.flush()
        return case

    @classmethod
    def on_invoice_expired(cls, db: Session, *, invoice: Invoice, customer: Customer, db_event: Event, now: Optional[datetime] = None, batch_id: Optional[str] = None) -> Optional[RecoveryCase]:
        if invoice.status == "PAID":
            return None
        return cls._open_case_for_invoice(db, invoice, customer, db_event, now or datetime.now(timezone.utc), batch_id=batch_id)

    @classmethod
    def on_invoice_partially_paid(cls, db: Session, *, invoice: Invoice, amount_paid_total: Decimal, payment_id: Optional[str], provider_event_id: str, occurred_at: Optional[datetime]) -> Optional[RecoveryCase]:
        previous = Decimal(str(invoice.amount_paid or 0))
        invoice.amount_paid = Decimal(str(amount_paid_total))
        invoice.status = "PARTIALLY_PAID"
        db.flush()
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.invoice_id == invoice.id, RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PAUSED", "PTP"])))
        increment = Decimal(str(amount_paid_total)) - previous
        if case and increment > 0:
            LedgerService.post_capture(db, case, increment, provider_reference=payment_id or provider_event_id, occurred_at=occurred_at, metadata={"partial": True, "invoice_id": invoice.external_invoice_id})
            meta = dict(case.case_metadata or {})
            meta["amount_paid"] = float(invoice.amount_paid)
            case.case_metadata = meta
            case.status = "IN_PROGRESS"
        return case

    @classmethod
    def on_invoice_paid(cls, db: Session, *, invoice: Invoice, amount_paid: Decimal, payment_id: Optional[str], provider_event_id: str, occurred_at: Optional[datetime]) -> Optional[RecoveryCase]:
        invoice.status = "PAID"
        invoice.amount_paid = Decimal(str(amount_paid))
        invoice.paid_at = occurred_at or datetime.now(timezone.utc)
        db.flush()
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.invoice_id == invoice.id, RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PAUSED", "PTP"])))
        if case:
            already = Decimal(str(LedgerService.case_balance(db, case.id)["recovered_gross"]))
            remaining = max(Decimal(str(amount_paid)) - already, Decimal("0.00"))
            OutcomeEngine.process_payment_capture(
                db=db, recovery_case=case, captured_amount=remaining if remaining > 0 else Decimal(str(amount_paid)),
                captured_at=invoice.paid_at, provider_event_id=provider_event_id, provider_payment_id=payment_id or invoice.external_invoice_id,
            )
        return case

    @classmethod
    def sweep(cls, db: Session, reference_time: Optional[datetime] = None, limit: int = 500) -> Dict[str, Any]:
        """Open RECEIVABLE_OVERDUE cases for unpaid invoices past their due date."""
        now = reference_time or datetime.now(timezone.utc)
        candidates: List[Invoice] = list(
            db.scalars(
                select(Invoice)
                .where(Invoice.status.in_(["ISSUED", "PARTIALLY_PAID", "EXPIRED"]), Invoice.due_date < now)
                .order_by(Invoice.due_date.asc()).limit(limit)
            ).all()
        )
        opened, already = [], 0
        for inv in candidates:
            customer = inv.customer or db.scalar(select(Customer).where(Customer.id == inv.customer_id))
            open_case = db.scalar(select(RecoveryCase).where(RecoveryCase.invoice_id == inv.id, RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PAUSED", "PTP"])))
            if open_case:
                already += 1
                inv.status = "OVERDUE" if inv.status != "PARTIALLY_PAID" else inv.status
                continue
            evt = CaseFactory.synthetic_event(
                db, customer=customer, event_type="invoice.overdue", entity_id=inv.external_invoice_id,
                payload={"invoice_id": inv.external_invoice_id, "amount": float(inv.amount), "due_date": _aware(inv.due_date).isoformat()},
                occurred_at=now, invoice=inv,
            )
            case = cls._open_case_for_invoice(db, inv, customer, evt, now)
            opened.append({"invoice_id": inv.external_invoice_id, "case_id": str(case.id), "ageing_bucket": (case.case_metadata or {}).get("ageing_bucket"), "amount": float(case.amount_at_risk)})
        db.flush()
        logger.info(f"[RECEIVABLES_SWEEP] candidates={len(candidates)} opened={len(opened)} already_open={already}")
        return {"reference_time": now.isoformat(), "candidates": len(candidates), "opened": opened, "already_open": already}

    @classmethod
    def ageing_report(cls, db: Session, reference_time: Optional[datetime] = None) -> Dict[str, Any]:
        now = reference_time or datetime.now(timezone.utc)
        rows = db.scalars(select(Invoice).where(Invoice.status != "PAID", Invoice.due_date < now)).all()
        buckets: Dict[str, Dict[str, float]] = {b: {"invoices": 0, "outstanding": 0.0} for b in ["1-7", "8-30", "31-60", "60+"]}
        for inv in rows:
            days = max((now - _aware(inv.due_date)).total_seconds() / 86400.0, 0.0)
            b = ageing_bucket(days)
            buckets[b]["invoices"] += 1
            buckets[b]["outstanding"] += float(Decimal(str(inv.amount)) - Decimal(str(inv.amount_paid or 0)))
        total = sum(v["outstanding"] for v in buckets.values())
        return {"reference_time": now.isoformat(), "buckets": buckets, "total_outstanding": round(total, 2), "overdue_invoices": len(rows)}
