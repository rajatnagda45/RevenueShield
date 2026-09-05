"""Dashboard Command Center Analytics and Reporting Endpoints."""
from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.dashboard_service import DashboardService

router = APIRouter(prefix="/dashboard", tags=["Command Center Dashboard"])


@router.get(
    "/summary",
    status_code=status.HTTP_200_OK,
    summary="Get Top-Level KPI Summary",
    description="Retrieve authoritative financial KPIs: Revenue at Risk, Revenue Recovered, Recovery Rate, Active Cases, and Expected Recovery Value.",
)
def get_dashboard_summary(
    start_date: Optional[datetime] = Query(None, description="Filter from start timestamp (ISO)"),
    end_date: Optional[datetime] = Query(None, description="Filter to end timestamp (ISO)"),
    currency: Optional[str] = Query(None, description="Currency filter (e.g. INR, USD)"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Fetch aggregated top KPI metrics."""
    return DashboardService.get_summary_kpis(
        db=db,
        start_date=start_date,
        end_date=end_date,
        currency=currency,
    )


@router.get(
    "/recovery-performance",
    status_code=status.HTTP_200_OK,
    summary="Get Recovery Performance Breakdown",
    description="Retrieve overall recovery resolution counts, resolution percentages, and average time-to-recovery.",
)
def get_recovery_performance(
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Fetch case volume resolution and speed metrics."""
    return DashboardService.get_recovery_performance(db=db)


@router.get(
    "/intervention-performance",
    status_code=status.HTTP_200_OK,
    summary="Get Recovery by Intervention Channel",
    description="Retrieve performance metrics per intervention channel: EMAIL, VOICE, PAYMENT_RETRY, WHATSAPP.",
)
def get_intervention_performance(
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    """Fetch per-channel intervention attempts and recovery conversions."""
    return DashboardService.get_intervention_performance(db=db)


@router.get(
    "/recommendations",
    status_code=status.HTTP_200_OK,
    summary="Get Active Next-Best-Action Recommendations",
    description="Retrieve recent active recovery cases with current ML Next-Best-Action recommendations and PolicyEngine authorization.",
)
def get_recent_recommendations(
    limit: int = Query(20, ge=1, le=100, description="Max cases to return"),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    """Fetch active case recommendations."""
    return DashboardService.get_recent_recommendations(db=db, limit=limit)


@router.get(
    "/recovery-trend",
    status_code=status.HTTP_200_OK,
    summary="Get Money Recovered Over Time Trend",
    description="Retrieve daily and cumulative authoritative recovery money over time from verified outcomes.",
)
def get_recovery_trend(
    days: int = Query(30, ge=1, le=365, description="Number of historical days"),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Fetch daily and cumulative revenue recovery time-series."""
    return DashboardService.get_recovery_trend(db=db, days=days)


@router.get(
    "/model-status",
    status_code=status.HTTP_200_OK,
    summary="Get ML Recovery Model Status & Metadata",
    description="Retrieve active predictive model version, cold-start state, training timestamp, and evaluation metrics.",
)
def get_model_status() -> Dict[str, Any]:
    """Fetch ML predictive model metadata and evaluation statistics."""
    return DashboardService.get_model_status_info()


@router.get(
    "/promises-to-pay",
    status_code=status.HTTP_200_OK,
    summary="Get Active Promise-to-Pay Commitments",
    description="Retrieve all Promise-to-Pay agreements with amounts, scheduled dates, and overdue status.",
)
def get_promises_to_pay(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    """Fetch customer promise-to-pay commitments."""
    return DashboardService.get_promise_to_pay_list(db=db, limit=limit)
