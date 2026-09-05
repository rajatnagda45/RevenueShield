"""Learning dataset query endpoints for ML training and feature inspection."""
from typing import Any, Dict, Optional
import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.learning import LearningExample
from app.models.recovery_case import RecoveryCase

router = APIRouter()


class LearningExampleResponse(BaseModel):
    """Structured schema for a historical point-in-time learning example."""

    example_id: str = Field(..., description="UUID of the learning example")
    case_id: str = Field(..., description="UUID of the associated recovery case")
    diagnosis_category: str = Field(..., description="Root cause diagnosis category")
    action_type: str = Field(..., description="Recommended/executed recovery action type")
    decision_score: float = Field(..., description="Normalized score at decision time")
    decision_confidence: float = Field(..., description="Confidence at decision time")
    policy_allowed: bool = Field(..., description="Whether action was allowed by policy")
    amount_at_risk: float = Field(..., description="Pre-decision amount at risk")
    is_finalized: bool = Field(..., description="Whether the business outcome has been realized")
    label: Optional[int] = Field(None, description="Binary training target: 1 = Attributable Recovery, 0 = Non-recovery")
    outcome_type: Optional[str] = Field(None, description="Realized outcome type (e.g. RECOVERED, NOT_RECOVERED)")
    amount_recovered: Optional[float] = Field(None, description="Verified recovered amount")
    recovery_percentage: Optional[float] = Field(None, description="Percentage of amount at risk recovered")
    attribution: Optional[str] = Field(None, description="Causality attribution (DIRECT, LIKELY, UNCERTAIN, ORGANIC)")
    time_to_recovery_seconds: Optional[float] = Field(None, description="Time to recovery in seconds")
    feature_snapshot: Dict[str, Any] = Field(default_factory=dict, description="Immutable point-in-time pre-decision feature snapshot")
    created_at: str = Field(..., description="Timestamp when example was created")
    finalized_at: Optional[str] = Field(None, description="Timestamp when outcome was finalized")


@router.get(
    "/examples/{case_id}",
    response_model=LearningExampleResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Learning Example by Case ID",
    description="Retrieve the point-in-time feature snapshot, pre-decision variables, and finalized outcome label for a recovery case.",
)
def get_learning_example_by_case(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> LearningExampleResponse:
    """Fetch the latest learning example for a recovery case."""
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recovery case '{case_id}' not found.",
        )

    example = db.scalar(
        select(LearningExample)
        .where(LearningExample.recovery_case_id == case_id)
        .order_by(LearningExample.created_at.desc())
    )
    if not example:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No learning example found for recovery case '{case_id}'.",
        )

    return LearningExampleResponse(
        example_id=str(example.id),
        case_id=str(case.id),
        diagnosis_category=example.diagnosis_category,
        action_type=example.action_type,
        decision_score=example.decision_score,
        decision_confidence=example.decision_confidence,
        policy_allowed=example.policy_allowed,
        amount_at_risk=float(example.amount_at_risk),
        is_finalized=example.is_finalized,
        label=example.label,
        outcome_type=example.outcome_type,
        amount_recovered=float(example.amount_recovered) if example.amount_recovered is not None else None,
        recovery_percentage=example.recovery_percentage,
        attribution=example.attribution,
        time_to_recovery_seconds=example.time_to_recovery_seconds,
        feature_snapshot=example.feature_snapshot or {},
        created_at=example.created_at.isoformat(),
        finalized_at=example.finalized_at.isoformat() if example.finalized_at else None,
    )
