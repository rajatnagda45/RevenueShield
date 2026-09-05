"""Recovery case query, diagnosis, decision recommendation, execution, and outcome endpoints."""
import uuid
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.recovery_case import RecoveryCase
from app.models.diagnosis import Diagnosis
from app.models.recovery_action import RecoveryAction
from app.models.outcome import RecoveryOutcome
from app.execution.service import ExecutionService

router = APIRouter()


class DiagnosisResponse(BaseModel):
    """Structured response schema for recovery case diagnosis."""

    case_id: str = Field(..., description="UUID of the parent recovery case")
    category: str = Field(..., description="Root cause category")
    failure_code: Optional[str] = Field(None, description="Diagnostic failure code")
    explanation: str = Field(..., description="Human-readable root cause explanation")
    confidence: float = Field(..., description="Heuristic confidence score (0.0 to 1.0)")
    risk_score: Optional[float] = Field(None, description="Revenue risk score (0 to 100)")
    recovery_probability: Optional[float] = Field(None, description="Predicted recovery probability (0.0 to 1.0)")
    engine_version: str = Field(..., description="Identifier of the diagnosis engine version that produced this decision")
    evidence: Dict[str, Any] = Field(default_factory=dict, description="Structured failure evidence and signals")
    created_at: str = Field(..., description="Timestamp when diagnosis was generated")


class PolicyResponse(BaseModel):
    """Structured policy compliance evaluation result."""

    allowed: bool = Field(..., description="Whether action is permitted by policy")
    reason: str = Field(..., description="Policy justification or compliance explanation")
    blocking_rule: Optional[str] = Field(None, description="Identifier of blocking rule if prohibited")
    evaluated_at: str = Field(..., description="Timestamp of policy evaluation")


class RecommendationResponse(BaseModel):
    """Structured response schema for Next Best Action recommendation."""

    case_id: str = Field(..., description="UUID of the recovery case")
    action_id: str = Field(..., description="UUID of the recommended RecoveryAction record")
    recommended_action: str = Field(..., description="Controlled action recommendation")
    channel: str = Field(..., description="Recommended communication/intervention channel")
    status: str = Field(..., description="Action status (e.g. APPROVED, BLOCKED, RECOMMENDED, CANCELLED)")
    confidence: float = Field(..., description="Confidence score (0.0 to 1.0)")
    decision_score: Optional[float] = Field(None, description="Normalized candidate score (0.0 to 1.0)")
    reason: str = Field(..., description="Deterministic decision explanation")
    supporting_factors: List[str] = Field(default_factory=list, description="Explainable supporting factors")
    alternatives: List[Dict[str, Any]] = Field(default_factory=list, description="Top ranked alternative candidate actions")
    policy: PolicyResponse = Field(..., description="Policy and compliance evaluation result")
    decision_engine_version: str = Field(..., description="Version of decision engine")
    policy_engine_version: str = Field(..., description="Version of policy engine")
    created_at: str = Field(..., description="Timestamp when recommendation was generated")


class ExecutionResponse(BaseModel):
    """Normalized response schema for recovery action execution."""

    execution_id: str = Field(..., description="UUID of the execution record")
    case_id: str = Field(..., description="UUID of the recovery case")
    action_id: str = Field(..., description="UUID of the recovery action")
    action_type: str = Field(..., description="Executed action type")
    status: str = Field(..., description="Execution status (SUCCEEDED, FAILED, BLOCKED, CANCELLED)")
    provider: str = Field(..., description="Provider that executed the action (e.g. RAZORPAY, DRY_RUN)")
    provider_reference: Optional[str] = Field(None, description="Provider reference ID (e.g. plink_xxx)")
    provider_url: Optional[str] = Field(None, description="Provider URL if applicable (e.g. payment link URL)")
    amount: float = Field(..., description="Amount at risk for the case")
    currency: str = Field(..., description="Currency code (e.g. INR)")
    idempotency_key: str = Field(..., description="Deterministic idempotency key for this execution")
    error_code: Optional[str] = Field(None, description="Error code if execution failed or was blocked")
    error_message: Optional[str] = Field(None, description="Error explanation if execution failed or was blocked")
    created_at: str = Field(..., description="Timestamp of execution record creation")


class OutcomeResponse(BaseModel):
    """Normalized response schema for recovery case outcome."""

    case_id: str = Field(..., description="UUID of the recovery case")
    outcome_type: str = Field(..., description="Controlled business outcome (e.g. RECOVERED, PARTIALLY_RECOVERED, NOT_RECOVERED)")
    amount_at_risk: float = Field(..., description="Total amount at risk for this case")
    amount_recovered: float = Field(..., description="Verified recovered amount")
    recovery_percentage: float = Field(..., description="Percentage of amount at risk successfully recovered (0 to 100)")
    attribution: str = Field(..., description="Causal attribution category (DIRECT, LIKELY, UNCERTAIN, ORGANIC)")
    time_to_recovery_seconds: Optional[float] = Field(None, description="Elapsed seconds from intervention execution to payment capture")
    occurred_at: str = Field(..., description="Timestamp when outcome event occurred")


@router.get(
    "/{case_id}/diagnosis",
    response_model=DiagnosisResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Recovery Case Diagnosis",
    description="Retrieve the root cause diagnosis, evidence, risk score, and recovery probability for a recovery case.",
)
def get_case_diagnosis(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> DiagnosisResponse:
    """Fetch the latest diagnosis record for a recovery case."""
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recovery case '{case_id}' not found.",
        )

    diagnosis = db.scalar(
        select(Diagnosis)
        .where(Diagnosis.recovery_case_id == case_id)
        .order_by(Diagnosis.created_at.desc())
    )
    if not diagnosis:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No diagnosis generated for recovery case '{case_id}'.",
        )

    return DiagnosisResponse(
        case_id=str(case.id),
        category=diagnosis.category,
        failure_code=diagnosis.failure_code,
        explanation=diagnosis.explanation,
        confidence=diagnosis.confidence,
        risk_score=diagnosis.risk_score,
        recovery_probability=diagnosis.recovery_probability,
        engine_version=diagnosis.engine_version,
        evidence=diagnosis.evidence or {},
        created_at=diagnosis.created_at.isoformat(),
    )


@router.get(
    "/{case_id}/recommendation",
    response_model=RecommendationResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Next Best Action Recommendation",
    description="Retrieve the latest next-best-action recommendation, alternatives, supporting factors, and policy compliance result for a recovery case.",
)
def get_case_recommendation(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RecommendationResponse:
    """Fetch the latest action recommendation record for a recovery case."""
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recovery case '{case_id}' not found.",
        )

    action = db.scalar(
        select(RecoveryAction)
        .where(RecoveryAction.recovery_case_id == case_id)
        .order_by(RecoveryAction.created_at.desc())
    )
    if not action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No recommendation generated for recovery case '{case_id}'.",
        )

    policy_raw = action.policy_result or {}
    policy_obj = PolicyResponse(
        allowed=policy_raw.get("allowed", True),
        reason=policy_raw.get("reason", "No policy constraints triggered."),
        blocking_rule=policy_raw.get("blocking_rule"),
        evaluated_at=policy_raw.get("evaluated_at", action.created_at.isoformat()),
    )

    return RecommendationResponse(
        case_id=str(case.id),
        action_id=str(action.id),
        recommended_action=action.action_type,
        channel=action.channel,
        status=action.status,
        confidence=action.decision_confidence or action.confidence or 0.0,
        decision_score=action.decision_score,
        reason=action.reason or "",
        supporting_factors=action.supporting_factors or [],
        alternatives=action.alternatives or [],
        policy=policy_obj,
        decision_engine_version=action.decision_engine_version,
        policy_engine_version=action.policy_engine_version,
        created_at=action.created_at.isoformat(),
    )


@router.post(
    "/{case_id}/execute",
    response_model=ExecutionResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute Approved Recovery Action",
    description="Execute the latest approved recovery action through the Execution Guard and appropriate provider executor.",
)
def execute_case_action(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> ExecutionResponse:
    """Execute the stored approved recommendation for a recovery case."""
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recovery case '{case_id}' not found.",
        )

    try:
        execution = ExecutionService.execute_action(db=db, recovery_case=case)
        db.commit()
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    return ExecutionResponse(
        execution_id=str(execution.id),
        case_id=str(case.id),
        action_id=str(execution.recovery_action_id),
        action_type=execution.action_type,
        status=execution.status,
        provider=execution.provider,
        provider_reference=execution.provider_reference,
        provider_url=execution.provider_url,
        amount=float(case.amount_at_risk),
        currency=case.currency,
        idempotency_key=execution.idempotency_key,
        error_code=execution.error_code,
        error_message=execution.error_message,
        created_at=execution.created_at.isoformat(),
    )


from app.core.security import verify_internal_api_auth

@router.get(
    "/{case_id}/outcome",
    response_model=OutcomeResponse,
    status_code=status.HTTP_200_OK,
    summary="Get Recovery Case Outcome",
    description="Retrieve the verified business outcome, recovery percentage, attribution, and time to recovery for a recovery case.",
)
def get_case_outcome(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
    _auth: bool = Depends(verify_internal_api_auth),
) -> OutcomeResponse:
    """Fetch the latest outcome record for a recovery case."""
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recovery case '{case_id}' not found.",
        )

    outcome = db.scalar(
        select(RecoveryOutcome)
        .where(RecoveryOutcome.recovery_case_id == case_id)
        .order_by(RecoveryOutcome.created_at.desc())
    )
    if not outcome:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No outcome recorded yet for recovery case '{case_id}'.",
        )

    return OutcomeResponse(
        case_id=str(case.id),
        outcome_type=outcome.outcome_type,
        amount_at_risk=float(outcome.amount_at_risk),
        amount_recovered=float(outcome.amount_recovered),
        recovery_percentage=outcome.recovery_percentage,
        attribution=outcome.attribution,
        time_to_recovery_seconds=outcome.time_to_recovery_seconds,
        occurred_at=outcome.occurred_at.isoformat(),
    )


class RecoveryCasePortalSummary(BaseModel):
    """Customer-facing recovery case portal summary."""
    case_id: str
    customer_name: str
    customer_email: Optional[str] = None
    amount_due: float
    currency: str
    due_date: str
    status: str
    created_at: str


@router.get(
    "/{case_id}",
    response_model=RecoveryCasePortalSummary,
    status_code=status.HTTP_200_OK,
    summary="Get Recovery Case Portal Summary",
    description="Retrieve recovery case summary for frontend customer recovery portal.",
)
def get_case_portal_summary(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> RecoveryCasePortalSummary:
    """Fetch public recovery portal case information."""
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recovery case '{case_id}' not found.",
        )

    customer = case.customer
    customer_name = customer.name if customer and customer.name else "Valued Customer"
    customer_email = customer.email if customer else None

    from datetime import timedelta
    if case.invoice and getattr(case.invoice, "due_date", None) and case.invoice.due_date:
        due_date_str = case.invoice.due_date.strftime("%Y-%m-%d")
    else:
        due_date_str = (case.created_at + timedelta(days=3)).strftime("%Y-%m-%d")

    return RecoveryCasePortalSummary(
        case_id=str(case.id),
        customer_name=customer_name,
        customer_email=customer_email,
        amount_due=float(case.amount_at_risk or 0.0),
        currency=case.currency or "INR",
        due_date=due_date_str,
        status=case.status,
        created_at=case.created_at.isoformat(),
    )


class ActionRankingItem(BaseModel):
    action: str
    predicted_probability: float
    probability: Optional[float] = None
    amount_at_risk: float
    expected_recovered_value: float
    policy_allowed: bool
    policy_reason: Optional[str] = None
    contributing_factors: List[str] = Field(default_factory=list)


class NextBestActionDecisionResponse(BaseModel):
    case_id: str
    decision_mode: str
    recommended_action: str
    selected_action: Optional[str] = None
    amount_at_risk: float
    model_version: str
    predicted_probability: float
    expected_recovery_probability: Optional[float] = None
    expected_recovered_value: float
    expected_recovery_value: Optional[float] = None
    ranking: List[ActionRankingItem]
    candidate_actions: List[ActionRankingItem] = Field(default_factory=list)
    reason: str


@router.get(
    "/{case_id}/next-best-action",
    response_model=NextBestActionDecisionResponse,
    status_code=status.HTTP_200_OK,
    summary="Recommend Next Best Action for Recovery Case",
    description="Evaluate candidate actions with ML recovery probabilities, ERV scoring, PolicyEngine authorization, and audit logging. Only recommends; does NOT execute.",
)
def get_case_next_best_action_recommendation(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> NextBestActionDecisionResponse:
    """Recommend Next-Best-Action for recovery case based on ML and PolicyEngine."""
    from app.services.next_best_action import NextBestActionService

    try:
        recommendation = NextBestActionService.recommend_next_best_action(case_id=case_id, db=db)
        
        # Populate backward-compatible aliases
        ranking_items = [
            ActionRankingItem(
                action=r["action"],
                predicted_probability=r["predicted_probability"],
                probability=r["predicted_probability"],
                amount_at_risk=r["amount_at_risk"],
                expected_recovered_value=r["expected_recovered_value"],
                policy_allowed=r["policy_allowed"],
                policy_reason=r.get("policy_reason"),
            )
            for r in recommendation.get("ranking", [])
        ]
        
        return NextBestActionDecisionResponse(
            case_id=recommendation["case_id"],
            decision_mode=recommendation["decision_mode"],
            recommended_action=recommendation["recommended_action"],
            selected_action=recommendation["recommended_action"],
            amount_at_risk=recommendation["amount_at_risk"],
            model_version=recommendation["model_version"],
            predicted_probability=recommendation["predicted_probability"],
            expected_recovery_probability=recommendation["predicted_probability"],
            expected_recovered_value=recommendation["expected_recovered_value"],
            expected_recovery_value=recommendation["expected_recovered_value"],
            ranking=ranking_items,
            candidate_actions=ranking_items,
            reason=recommendation["reason"],
        )
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ve))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate Next-Best-Action recommendation: {str(e)}",
        )


@router.get(
    "/{case_id}/timeline",
    status_code=status.HTTP_200_OK,
    summary="Get Case Audit Event Timeline",
    description="Retrieve chronological audit event history for a recovery case.",
)
def get_case_audit_timeline_endpoint(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> List[Dict[str, Any]]:
    """Fetch immutable audit timeline for recovery case."""
    from app.services.dashboard_service import DashboardService

    timeline = DashboardService.get_case_audit_timeline(case_id=str(case_id), db=db)
    return timeline
