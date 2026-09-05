"""API router for Promise-to-Pay creation, evaluation, cancellation, and metrics."""
from datetime import datetime
from decimal import Decimal
import logging
from typing import List, Optional
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.promise_to_pay import PromiseToPay
from app.services.promise_evaluation_service import PromiseEvaluationService
from app.services.promise_to_pay_service import PromiseToPayService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Promise to Pay & Escalation"])


class CreatePromiseRequest(BaseModel):
    """Payload for creating or updating a customer Promise-to-Pay commitment."""

    model_config = ConfigDict(populate_by_name=True)

    promised_amount: float = Field(..., gt=0, description="Amount customer has committed to pay")
    promised_date: datetime = Field(..., description="Future date and time when payment is promised")
    promised_time: Optional[str] = Field(default="17:00", description="Time string, e.g. '17:00'")
    source: str = Field(default="CUSTOMER", description="'CUSTOMER', 'AGENT', 'OPERATOR', or 'SYSTEM'")
    notes: Optional[str] = Field(default=None, description="Optional agent or customer notes")


class PromiseToPayResponse(BaseModel):
    """Promise-to-Pay entity response."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    recovery_case_id: str
    customer_id: str
    amount_due: float
    promised_amount: float
    promised_date: str
    promised_time: Optional[str]
    currency: str
    status: str
    source: str
    confidence: float
    notes: Optional[str] = None
    created_at: str
    fulfilled_at: Optional[str] = None
    expired_at: Optional[str] = None
    cancelled_at: Optional[str] = None


class PromiseMetricsResponse(BaseModel):
    """Aggregated Promise-to-Pay business KPIs."""

    model_config = ConfigDict(populate_by_name=True)

    total_promises: int
    active_promises: int
    fulfilled_promises: int
    missed_promises: int
    expired_promises: int
    fulfillment_rate: float
    total_amount_under_promise: float
    total_amount_recovered_through_promise: float


from app.core.security import verify_internal_api_auth

@router.post(
    "/recovery-cases/{case_id}/promise-to-pay",
    response_model=PromiseToPayResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record customer Promise-to-Pay and pause automated outreach",
)
def create_case_promise(
    case_id: uuid.UUID,
    payload: CreatePromiseRequest,
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_internal_api_auth),
):
    """Validate customer commitment, save Promise-to-Pay, and immediately pause active RecoveryPlans."""
    try:
        promise = PromiseToPayService.create_promise(
            db=db,
            recovery_case_id=case_id,
            promised_amount=Decimal(str(payload.promised_amount)),
            promised_date=payload.promised_date,
            promised_time=payload.promised_time,
            source=payload.source,
            notes=payload.notes,
        )
        return PromiseToPayResponse(
            id=str(promise.id),
            recovery_case_id=str(promise.recovery_case_id),
            customer_id=str(promise.customer_id),
            amount_due=float(promise.amount_due),
            promised_amount=float(promise.promised_amount),
            promised_date=promise.promised_date.isoformat(),
            promised_time=promise.promised_time,
            currency=promise.currency,
            status=promise.status,
            source=promise.source,
            confidence=promise.confidence,
            notes=promise.notes,
            created_at=promise.created_at.isoformat(),
            fulfilled_at=promise.fulfilled_at.isoformat() if promise.fulfilled_at else None,
            expired_at=promise.expired_at.isoformat() if promise.expired_at else None,
            cancelled_at=promise.cancelled_at.isoformat() if promise.cancelled_at else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get(
    "/recovery-cases/{case_id}/promise-to-pay",
    response_model=Optional[PromiseToPayResponse],
    status_code=status.HTTP_200_OK,
    summary="Get active or latest Promise-to-Pay for a recovery case",
)
def get_case_promise(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_internal_api_auth),
):
    """Retrieve active or most recent promise associated with a recovery case."""
    promise = db.scalar(
        select(PromiseToPay)
        .where(PromiseToPay.recovery_case_id == case_id)
        .order_by(desc(PromiseToPay.created_at))
        .limit(1)
    )
    if not promise:
        return None

    return PromiseToPayResponse(
        id=str(promise.id),
        recovery_case_id=str(promise.recovery_case_id),
        customer_id=str(promise.customer_id),
        amount_due=float(promise.amount_due),
        promised_amount=float(promise.promised_amount),
        promised_date=promise.promised_date.isoformat(),
        promised_time=promise.promised_time,
        currency=promise.currency,
        status=promise.status,
        source=promise.source,
        confidence=promise.confidence,
        notes=promise.notes,
        created_at=promise.created_at.isoformat(),
        fulfilled_at=promise.fulfilled_at.isoformat() if promise.fulfilled_at else None,
        expired_at=promise.expired_at.isoformat() if promise.expired_at else None,
        cancelled_at=promise.cancelled_at.isoformat() if promise.cancelled_at else None,
    )


@router.post(
    "/promise-to-pay/{promise_id}/cancel",
    response_model=PromiseToPayResponse,
    status_code=status.HTTP_200_OK,
    summary="Cancel active Promise-to-Pay and resume recovery plan",
)
def cancel_promise(
    promise_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    """Cancel promise and resume recovery plan sequencing."""
    try:
        promise = PromiseToPayService.cancel_promise(db=db, promise_id=promise_id)
        return PromiseToPayResponse(
            id=str(promise.id),
            recovery_case_id=str(promise.recovery_case_id),
            customer_id=str(promise.customer_id),
            amount_due=float(promise.amount_due),
            promised_amount=float(promise.promised_amount),
            promised_date=promise.promised_date.isoformat(),
            promised_time=promise.promised_time,
            currency=promise.currency,
            status=promise.status,
            source=promise.source,
            confidence=promise.confidence,
            notes=promise.notes,
            created_at=promise.created_at.isoformat(),
            fulfilled_at=promise.fulfilled_at.isoformat() if promise.fulfilled_at else None,
            expired_at=promise.expired_at.isoformat() if promise.expired_at else None,
            cancelled_at=promise.cancelled_at.isoformat() if promise.cancelled_at else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post(
    "/promise-to-pay/{promise_id}/evaluate",
    status_code=status.HTTP_200_OK,
    summary="Evaluate Promise-to-Pay payment status",
)
def evaluate_promise(
    promise_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    """Evaluate if promise is fulfilled, missed, partial, or expired."""
    try:
        res = PromiseEvaluationService.evaluate_promise(db=db, promise_id=promise_id)
        return res
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get(
    "/promise-to-pay",
    response_model=List[PromiseToPayResponse],
    status_code=status.HTTP_200_OK,
    summary="List Promise-to-Pay records with optional status filter",
)
def list_promises(
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status: ACTIVE, FULFILLED, MISSED, EXPIRED"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Retrieve Promise-to-Pay records with status filtering."""
    query = select(PromiseToPay)
    if status_filter:
        query = query.where(PromiseToPay.status == status_filter.upper())

    promises = db.scalars(query.order_by(desc(PromiseToPay.created_at)).limit(limit)).all()

    return [
        PromiseToPayResponse(
            id=str(p.id),
            recovery_case_id=str(p.recovery_case_id),
            customer_id=str(p.customer_id),
            amount_due=float(p.amount_due),
            promised_amount=float(p.promised_amount),
            promised_date=p.promised_date.isoformat(),
            promised_time=p.promised_time,
            currency=p.currency,
            status=p.status,
            source=p.source,
            confidence=p.confidence,
            notes=p.notes,
            created_at=p.created_at.isoformat(),
            fulfilled_at=p.fulfilled_at.isoformat() if p.fulfilled_at else None,
            expired_at=p.expired_at.isoformat() if p.expired_at else None,
            cancelled_at=p.cancelled_at.isoformat() if p.cancelled_at else None,
        )
        for p in promises
    ]


@router.get(
    "/promise-to-pay/metrics/dashboard",
    response_model=PromiseMetricsResponse,
    status_code=status.HTTP_200_OK,
    summary="Get aggregated Promise-to-Pay dashboard KPIs",
)
def get_promise_metrics(
    db: Session = Depends(get_db),
):
    """Compute fulfillment rate, active promises, and total committed funds."""
    all_promises = db.scalars(select(PromiseToPay)).all()
    total = len(all_promises)
    active = sum(1 for p in all_promises if p.status == "ACTIVE")
    fulfilled = sum(1 for p in all_promises if p.status == "FULFILLED")
    missed = sum(1 for p in all_promises if p.status == "MISSED")
    expired = sum(1 for p in all_promises if p.status == "EXPIRED")

    completed = fulfilled + missed + expired
    rate = (fulfilled / completed) if completed > 0 else 0.0

    under_promise = sum(float(p.promised_amount) for p in all_promises if p.status == "ACTIVE")
    recovered = sum(float(p.promised_amount) for p in all_promises if p.status == "FULFILLED")

    return PromiseMetricsResponse(
        total_promises=total,
        active_promises=active,
        fulfilled_promises=fulfilled,
        missed_promises=missed,
        expired_promises=expired,
        fulfillment_rate=round(rate, 4),
        total_amount_under_promise=round(under_promise, 2),
        total_amount_recovered_through_promise=round(recovered, 2),
    )
