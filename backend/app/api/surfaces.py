"""Leak-surface endpoints: register orders, import receivables, run detector sweeps, inspect mandates."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.detectors.checkout_abandonment import CheckoutAbandonmentDetector
from app.detectors.mandates import MandateRetrySequencer
from app.detectors.receivables import ReceivableImportRow, ReceivablesDetector
from app.domain.surfaces import SURFACE_PROFILES
from app.models.mandate_retry import MandateRetry
from app.models.order import Order
from app.models.recovery_case import RecoveryCase
from app.schemas.event import NormalizedEvent
from app.services.customer_service import CustomerService

router = APIRouter(prefix="/surfaces", tags=["Leak Surfaces"])


def _require_internal_secret(x_internal_secret: Optional[str]) -> None:
    if settings.INTERNAL_API_SECRET and x_internal_secret != settings.INTERNAL_API_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid internal secret.")


class RegisterOrderRequest(BaseModel):
    external_order_id: str
    amount: Decimal = Field(..., gt=0, description="Order amount in rupees")
    currency: str = "INR"
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_name: Optional[str] = None
    external_customer_id: Optional[str] = None
    receipt: Optional[str] = None
    notes: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None


class SweepRequest(BaseModel):
    reference_time: Optional[datetime] = None
    abandon_after_minutes: int = Field(30, ge=1, le=7 * 24 * 60)
    dry_run: bool = True


class ImportReceivablesRequest(BaseModel):
    rows: List[ReceivableImportRow]


@router.get("/profiles", summary="Bounded recovery profile per leak surface")
def get_profiles() -> Dict[str, Any]:
    return {
        s.value: {
            "max_touches": p.max_touches, "voice_allowed": p.voice_allowed, "retry_allowed": p.retry_allowed,
            "escalation_allowed": p.escalation_allowed, "reevaluation_hours": p.reevaluation_hours,
            "max_duration_hours": p.max_duration_hours, "pre_debit_notice_hours": p.pre_debit_notice_hours, "description": p.description,
        }
        for s, p in SURFACE_PROFILES.items()
    }


@router.get("/summary", summary="Open and recovered cases by leak surface")
def get_summary(db: Session = Depends(get_db)) -> Dict[str, Any]:
    rows = db.execute(
        select(RecoveryCase.leak_surface, RecoveryCase.status, func.count(RecoveryCase.id), func.coalesce(func.sum(RecoveryCase.amount_at_risk), 0))
        .group_by(RecoveryCase.leak_surface, RecoveryCase.status)
    ).all()
    out: Dict[str, Dict[str, Any]] = {}
    for surface, st, n, amt in rows:
        s = out.setdefault(surface or "PAYMENT_FAILURE", {"cases": 0, "amount_at_risk": 0.0, "by_status": {}})
        s["cases"] += int(n)
        s["amount_at_risk"] += float(amt or 0)
        s["by_status"][st] = int(n)
    return out


# ------------------------------------------------------------------ orders / checkout

@router.post("/orders", status_code=status.HTTP_201_CREATED, summary="Register a checkout (order created) for abandonment tracking")
def register_order(body: RegisterOrderRequest, db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    probe = NormalizedEvent(
        event_id=f"order_register_{body.external_order_id}", event_type="order.created", amount=body.amount, currency=body.currency,
        external_customer_id=body.external_customer_id, customer_email=body.customer_email, customer_phone=body.customer_phone,
        customer_name=body.customer_name, external_order_id=body.external_order_id,
    )
    customer = CustomerService.resolve_or_create_customer(db, probe)
    order = CheckoutAbandonmentDetector.upsert_order(
        db, external_order_id=body.external_order_id, amount=body.amount, currency=body.currency, customer=customer,
        created_at=body.created_at or datetime.now(timezone.utc), receipt=body.receipt, notes=body.notes,
    )
    db.commit()
    return {"order_id": order.external_order_id, "status": order.status, "customer_id": str(customer.id), "amount": float(order.amount)}


@router.get("/orders", summary="List tracked orders")
def list_orders(status_filter: Optional[str] = Query(None, alias="status"), limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    stmt = select(Order).order_by(Order.razorpay_created_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(Order.status == status_filter.upper())
    return [
        {"order_id": o.external_order_id, "status": o.status, "amount": float(o.amount), "amount_paid": float(o.amount_paid or 0), "attempts": o.attempts,
         "created_at": o.razorpay_created_at.isoformat() if o.razorpay_created_at else None, "abandonment_case_id": str(o.abandonment_case_id) if o.abandonment_case_id else None}
        for o in db.scalars(stmt).all()
    ]


@router.post("/orders/sweep", summary="Open CHECKOUT_ABANDONMENT cases for quiet orders")
def sweep_orders(body: SweepRequest = SweepRequest(), db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    res = CheckoutAbandonmentDetector.sweep(db, reference_time=body.reference_time, abandon_after_minutes=body.abandon_after_minutes)
    db.commit()
    return res


# ------------------------------------------------------------------ receivables

@router.post("/receivables/import", summary="Import an AR ledger (invoices) for overdue tracking")
def import_receivables(body: ImportReceivablesRequest, db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    res = ReceivablesDetector.import_rows(db, body.rows)
    db.commit()
    return res


@router.post("/receivables/sweep", summary="Open RECEIVABLE_OVERDUE cases for invoices past due")
def sweep_receivables(body: SweepRequest = SweepRequest(), db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    res = ReceivablesDetector.sweep(db, reference_time=body.reference_time)
    db.commit()
    return res


@router.get("/receivables/ageing", summary="AR ageing report (1-7 / 8-30 / 31-60 / 60+ days)")
def ageing(reference_time: Optional[datetime] = Query(None), db: Session = Depends(get_db)) -> Dict[str, Any]:
    return ReceivablesDetector.ageing_report(db, reference_time=reference_time)


# ------------------------------------------------------------------ mandates

@router.get("/mandates/retries", summary="Scheduled / executed mandate retries")
def list_mandate_retries(status_filter: Optional[str] = Query(None, alias="status"), limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    stmt = select(MandateRetry).order_by(MandateRetry.scheduled_at.asc()).limit(limit)
    if status_filter:
        stmt = stmt.where(MandateRetry.status == status_filter.upper())
    return [
        {"id": str(r.id), "case_id": str(r.recovery_case_id), "attempt_number": r.attempt_number, "status": r.status,
         "scheduled_at": r.scheduled_at.isoformat() if r.scheduled_at else None, "notify_by": r.notify_by.isoformat() if r.notify_by else None,
         "notified_at": r.notified_at.isoformat() if r.notified_at else None, "executed_at": r.executed_at.isoformat() if r.executed_at else None, "reason": r.reason}
        for r in db.scalars(stmt).all()
    ]


@router.post("/mandates/sweep", summary="Send due pre-debit notices and execute due mandate retries")
def sweep_mandates(body: SweepRequest = SweepRequest(), db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    res = MandateRetrySequencer.sweep(db, now=body.reference_time, dry_run=body.dry_run)
    db.commit()
    return res
