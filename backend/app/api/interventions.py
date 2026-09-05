"""Recovery Intervention API Endpoints."""
from datetime import datetime
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.recovery_case import RecoveryCase
from app.models.intervention import Intervention
from app.models.outcome import RecoveryOutcome
from app.models.prediction import Prediction
from app.services.intervention_service import InterventionService, InterventionResult

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Interventions"])


# --- Schemas ---

class CreateInterventionRequest(BaseModel):
    """Request payload to trigger a smart recovery intervention."""

    action: Optional[str] = Field(
        default="SEND_PAYMENT_LINK",
        description="Target recovery action to execute.",
    )
    dry_run: Optional[bool] = Field(
        default=None,
        description="Override dry_run flag. If omitted, server environment setting is used.",
    )
    reference_time: Optional[datetime] = Field(
        default=None,
        description="Evaluate time-based policy (contact window, frequency caps) at this instant. "
        "Omit in production to use the current time; used by replay and deterministic tests.",
    )


class PaymentLinkResponseSchema(BaseModel):
    """Payment Link summary payload."""

    id: str
    razorpay_payment_link_id: str
    url: str
    amount: float
    currency: str
    status: str


class InterventionResponseSchema(BaseModel):
    """Intervention execution response."""

    case_id: str
    intervention_id: Optional[str] = None
    action: str
    status: str
    reason: Optional[str] = None
    payment_link: Optional[PaymentLinkResponseSchema] = None
    predicted_probability: Optional[float] = None
    expected_recovered_value: Optional[float] = None
    notification: Optional[Dict[str, Any]] = None


class InterventionPreviewSchema(BaseModel):
    """Preview of recommended action and policy check without execution."""

    case_id: str
    amount_at_risk: float
    currency: str
    recommended_action: str
    probability: float
    expected_recovered_value: float
    policy_status: str
    policy_reasons: List[str]
    case_status: str


class DashboardMetricsSchema(BaseModel):
    """Aggregated revenue recovery dashboard analytics."""

    total_revenue_at_risk: float
    total_recovered: float
    recovery_rate: float
    active_cases: int
    successful_interventions: int
    failed_interventions: int
    average_time_to_recovery_seconds: Optional[float] = None
    predicted_recovery_value: float
    actual_recovered_value: float


from app.core.security import verify_internal_api_auth

# --- Endpoints ---

@router.post(
    "/recovery-cases/{case_id}/interventions",
    response_model=InterventionResponseSchema,
    status_code=status.HTTP_200_OK,
    summary="Execute a smart recovery intervention",
)
def execute_case_intervention(
    case_id: uuid.UUID,
    payload: Optional[CreateInterventionRequest] = None,
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_internal_api_auth),
) -> InterventionResponseSchema:
    """Trigger prediction, policy check, payment link creation, and customer notification."""
    action_override = payload.action if payload else None
    dry_run_override = payload.dry_run if payload else None
    reference_time = payload.reference_time if payload else None

    try:
        res: InterventionResult = InterventionService.execute_intervention(
            db=db,
            recovery_case_id=case_id,
            action_override=action_override,
            dry_run=dry_run_override,
            reference_time=reference_time,
        )

        plink_dto = None
        if res.payment_link:
            plink_dto = PaymentLinkResponseSchema(
                id=res.payment_link.id,
                razorpay_payment_link_id=res.payment_link.razorpay_payment_link_id,
                url=res.payment_link.url,
                amount=res.payment_link.amount,
                currency=res.payment_link.currency,
                status=res.payment_link.status,
            )

        return InterventionResponseSchema(
            case_id=res.case_id,
            intervention_id=res.intervention_id,
            action=res.action,
            status=res.status,
            reason=res.reason,
            payment_link=plink_dto,
            predicted_probability=res.predicted_probability,
            expected_recovered_value=res.expected_recovered_value,
            notification=res.notification,
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.exception(f"Unhandled error in execute_case_intervention: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Intervention execution failed: {str(e)}",
        )


@router.get(
    "/recovery-cases/{case_id}/intervention-preview",
    response_model=InterventionPreviewSchema,
    summary="Preview recommended action, predicted probability, and policy check without executing",
)
def get_case_intervention_preview(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> InterventionPreviewSchema:
    """Return recommendation, probability, expected value, and policy authorization without side effects."""
    try:
        preview_data = InterventionService.preview_intervention(db=db, recovery_case_id=case_id)
        return InterventionPreviewSchema(**preview_data)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.exception(f"Unhandled error in get_case_intervention_preview: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate intervention preview: {str(e)}",
        )


@router.get(
    "/admin/interventions/dashboard",
    response_model=DashboardMetricsSchema,
    summary="Aggregated metrics for recovery dashboard",
)
def get_interventions_dashboard(
    db: Session = Depends(get_db),
) -> DashboardMetricsSchema:
    """Return aggregated recovery metrics, interventions performance, and predicted vs actual value."""
    # 1. Total revenue at risk and recovered
    total_risk = db.scalar(select(func.sum(RecoveryCase.amount_at_risk))) or Decimal("0.00")
    total_rec = db.scalar(
        select(func.sum(RecoveryCase.recovered_amount)).where(RecoveryCase.status == "RECOVERED")
    ) or Decimal("0.00")

    # 2. Active cases count
    active_cases_count = db.scalar(
        select(func.count(RecoveryCase.id)).where(
            RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PTP"])
        )
    ) or 0

    # 3. Successful vs Failed Interventions
    succ_interventions = db.scalar(
        select(func.count(Intervention.id)).where(Intervention.status == "SUCCEEDED")
    ) or 0
    fail_interventions = db.scalar(
        select(func.count(Intervention.id)).where(Intervention.status == "FAILED")
    ) or 0

    # 4. Average Time to Recovery
    avg_ttr = db.scalar(
        select(func.avg(RecoveryOutcome.time_to_recovery_seconds)).where(
            RecoveryOutcome.time_to_recovery_seconds.is_not(None)
        )
    )

    # 5. Predicted vs Actual Recovered Value
    pred_val = db.scalar(select(func.sum(Prediction.expected_recovered_value))) or Decimal("0.00")

    risk_float = float(total_risk)
    rec_float = float(total_rec)
    rate = round((rec_float / risk_float * 100.0), 2) if risk_float > 0 else 0.0

    return DashboardMetricsSchema(
        total_revenue_at_risk=round(risk_float, 2),
        total_recovered=round(rec_float, 2),
        recovery_rate=rate,
        active_cases=active_cases_count,
        successful_interventions=succ_interventions,
        failed_interventions=fail_interventions,
        average_time_to_recovery_seconds=round(float(avg_ttr), 2) if avg_ttr is not None else None,
        predicted_recovery_value=round(float(pred_val), 2),
        actual_recovered_value=round(rec_float, 2),
    )
