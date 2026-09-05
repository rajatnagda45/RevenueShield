"""API router exposing endpoints for Recovery Plans, adaptive sequencing, and plan progression."""
from datetime import datetime
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.audit_log import AuditLog
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan
from app.services.next_best_action_engine import NextBestActionEngine
from app.services.recovery_scheduler import RecoveryScheduler

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Recovery Plans & Sequencer"])


class PlanStepResponse(BaseModel):
    """Details of a single step within a recovery plan."""

    step_number: int
    action_type: str
    channel: str
    status: str
    scheduled_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    reason: Optional[str] = None
    prediction_score: Optional[float] = None
    expected_recovery_value: Optional[float] = None


class RecoveryPlanResponse(BaseModel):
    """Full representation of a recovery plan including history and next action."""

    plan_id: str
    case_id: str
    status: str
    current_step: int
    max_steps: int
    next_evaluation_at: Optional[datetime] = None
    completion_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    steps: List[PlanStepResponse] = []
    next_action: Optional[Dict[str, Any]] = None


class PlanEvaluateRequest(BaseModel):
    """Payload to trigger an adaptive plan re-evaluation."""

    reference_time: Optional[datetime] = Field(default=None, description="Mock evaluation timestamp")
    force_engagement_signal: Optional[bool] = Field(default=None, description="Simulate customer engagement (e.g. payment link clicked)")
    dry_run: Optional[bool] = Field(default=None, description="Dry run override")


class PlanPauseRequest(BaseModel):
    """Payload to pause a recovery plan."""

    reason: Optional[str] = Field(default="User requested pause", description="Reason for pausing plan")


class PlanTimelineEvent(BaseModel):
    """Event in the recovery plan progression timeline."""

    action: str
    timestamp: str
    actor: str
    metadata: Dict[str, Any] = {}


class PlansDashboardMetrics(BaseModel):
    """Aggregated metrics across all recovery plans."""

    total_plans: int
    active_plans: int
    waiting_plans: int
    completed_plans: int
    expired_plans: int
    paused_plans: int
    total_recovered_amount: float
    timeline: List[PlanTimelineEvent] = []


@router.get(
    "/recovery-cases/{case_id}/plan",
    response_model=RecoveryPlanResponse,
    status_code=status.HTTP_200_OK,
    summary="Get recovery plan status and step history",
)
def get_recovery_plan(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    """Retrieve the active or completed recovery plan and upcoming recommended action for a case."""
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"RecoveryCase '{case_id}' not found.")

    plan = db.scalar(select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case_id))
    if not plan:
        # Idempotently initialize plan
        plan = RecoveryScheduler.create_or_get_plan(db=db, case_id=case_id)

    # Compute Next Action Preview if plan is active/waiting
    next_action_data = None
    if plan.status in ["ACTIVE", "WAITING", "EVALUATING"] and case.status not in ["RECOVERED", "CLOSED"]:
        try:
            nba = NextBestActionEngine.compute_next_best_action(db=db, case=case)
            next_action_data = {
                "action_type": nba.action_type,
                "channel": nba.channel,
                "expected_recovery_probability": nba.expected_recovery_probability,
                "expected_recovery_value": float(nba.expected_recovery_value),
                "reason": nba.reason,
                "confidence": nba.confidence,
            }
        except Exception as exc:
            logger.warning(f"[NBA_PREVIEW_ERROR] Case {case_id}: {exc}")

    step_items = [
        PlanStepResponse(
            step_number=s.step_number,
            action_type=s.action_type,
            channel=s.channel,
            status=s.status,
            scheduled_at=s.scheduled_at,
            executed_at=s.executed_at,
            completed_at=s.completed_at,
            reason=s.reason,
            prediction_score=s.prediction_score,
            expected_recovery_value=float(s.expected_recovery_value) if s.expected_recovery_value else None,
        )
        for s in plan.steps
    ]

    return RecoveryPlanResponse(
        plan_id=str(plan.id),
        case_id=str(case.id),
        status=plan.status,
        current_step=plan.current_step,
        max_steps=plan.max_steps,
        next_evaluation_at=plan.next_evaluation_at,
        completion_reason=plan.completion_reason,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        completed_at=plan.completed_at,
        steps=step_items,
        next_action=next_action_data,
    )


@router.post(
    "/recovery-cases/{case_id}/plan/evaluate",
    status_code=status.HTTP_200_OK,
    summary="Re-evaluate plan and execute next recommended action",
)
def evaluate_recovery_plan(
    case_id: uuid.UUID,
    payload: PlanEvaluateRequest = PlanEvaluateRequest(),
    db: Session = Depends(get_db),
):
    """Re-evaluate the recovery plan against fresh signals, policies, and ML predictions."""
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"RecoveryCase '{case_id}' not found.")

    plan = RecoveryScheduler.create_or_get_plan(db=db, case_id=case_id)
    res = RecoveryScheduler.evaluate_and_advance_plan(
        db=db,
        plan_id=plan.id,
        reference_time=payload.reference_time,
        force_engagement_signal=payload.force_engagement_signal,
        dry_run=payload.dry_run,
    )
    return res


@router.post(
    "/recovery-cases/{case_id}/plan/pause",
    status_code=status.HTTP_200_OK,
    summary="Pause active recovery plan outreach",
)
def pause_recovery_plan(
    case_id: uuid.UUID,
    payload: PlanPauseRequest = PlanPauseRequest(),
    db: Session = Depends(get_db),
):
    """Pause automated outreach for a recovery plan."""
    plan = db.scalar(select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case_id))
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No plan found for case '{case_id}'.")

    return RecoveryScheduler.pause_plan(db=db, plan_id=plan.id, reason=payload.reason)


@router.post(
    "/recovery-cases/{case_id}/plan/resume",
    status_code=status.HTTP_200_OK,
    summary="Resume a paused recovery plan",
)
def resume_recovery_plan(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    """Resume automated outreach for a paused recovery plan."""
    plan = db.scalar(select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case_id))
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No plan found for case '{case_id}'.")

    return RecoveryScheduler.resume_plan(db=db, plan_id=plan.id)


@router.get(
    "/admin/plans/dashboard",
    response_model=PlansDashboardMetrics,
    status_code=status.HTTP_200_OK,
    summary="Recovery plans dashboard metrics and progression timeline",
)
def get_plans_dashboard(
    db: Session = Depends(get_db),
):
    """Aggregated metrics across all recovery plans and progression events."""
    total_plans = db.scalar(select(func.count(RecoveryPlan.id))) or 0
    active_plans = db.scalar(select(func.count(RecoveryPlan.id)).where(RecoveryPlan.status == "ACTIVE")) or 0
    waiting_plans = db.scalar(select(func.count(RecoveryPlan.id)).where(RecoveryPlan.status == "WAITING")) or 0
    completed_plans = db.scalar(select(func.count(RecoveryPlan.id)).where(RecoveryPlan.status == "COMPLETED")) or 0
    expired_plans = db.scalar(select(func.count(RecoveryPlan.id)).where(RecoveryPlan.status == "EXPIRED")) or 0
    paused_plans = db.scalar(select(func.count(RecoveryPlan.id)).where(RecoveryPlan.status == "PAUSED")) or 0

    total_recovered = db.scalar(
        select(func.sum(RecoveryCase.recovered_amount)).where(RecoveryCase.status == "RECOVERED")
    ) or Decimal("0.00")

    timeline_logs = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.action.in_([
                "RECOVERY_PLAN_CREATED",
                "RECOVERY_PLAN_EVALUATED",
                "NEXT_BEST_ACTION_SELECTED",
                "RECOVERY_STEP_CREATED",
                "RECOVERY_STEP_EXECUTED",
                "RECOVERY_STEP_BLOCKED",
                "RECOVERY_PLAN_PAUSED",
                "RECOVERY_PLAN_RESUMED",
                "RECOVERY_PLAN_COMPLETED",
                "RECOVERY_PLAN_EXPIRED",
                "RECOVERY_PLAN_STOPPED_AFTER_RECOVERY",
            ])
        )
        .order_by(desc(AuditLog.timestamp))
        .limit(25)
    ).all()

    timeline_items = [
        PlanTimelineEvent(
            action=log.action,
            timestamp=log.timestamp.isoformat() if log.timestamp else "",
            actor=f"{log.actor_type} ({log.actor_id})",
            metadata=log.audit_metadata or {},
        )
        for log in reversed(timeline_logs)
    ]

    return PlansDashboardMetrics(
        total_plans=total_plans,
        active_plans=active_plans,
        waiting_plans=waiting_plans,
        completed_plans=completed_plans,
        expired_plans=expired_plans,
        paused_plans=paused_plans,
        total_recovered_amount=float(total_recovered),
        timeline=timeline_items,
    )


class CandidateActionItem(BaseModel):
    """Candidate action scoring breakdown."""

    action: str
    probability: float
    expected_recovery_value: float
    policy_allowed: bool
    policy_blocking_rule: Optional[str] = None
    contributing_factors: List[str] = []


class NextBestActionCaseResponse(BaseModel):
    """Detailed Next Best Action recommendation response."""

    case_id: str
    amount_at_risk: float
    candidate_actions: List[CandidateActionItem]
    selected_action: str
    selected_channel: str
    expected_recovery_value: float
    expected_recovery_probability: float
    model_version: str
    reason: str


@router.get(
    "/recovery-cases/{case_id}/next-best-action",
    response_model=NextBestActionCaseResponse,
    status_code=status.HTTP_200_OK,
    summary="Evaluate candidate recovery actions and return optimal decision",
)
def get_case_next_best_action(
    case_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    """Evaluate all candidate actions with ML probability estimates, policy validation, and expected value ranking."""
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
    if not case:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"RecoveryCase '{case_id}' not found.")

    candidates_raw = NextBestActionEngine.evaluate_candidate_actions(db=db, case=case)
    nba = NextBestActionEngine.compute_next_best_action(db=db, case=case)

    # Check policy for each candidate
    candidate_items = []
    for c in candidates_raw:
        act = c["action"]
        policy_action = "SEND_PAYMENT_LINK" if "EMAIL" in act else act
        policy_res = RecoveryScheduler._evaluate_policy_for_case(db, case, policy_action) if hasattr(RecoveryScheduler, "_evaluate_policy_for_case") else None

        candidate_items.append(
            CandidateActionItem(
                action=act,
                probability=c["probability"],
                expected_recovery_value=c["expected_recovery_value"],
                policy_allowed=True,
                contributing_factors=c.get("contributing_factors", []),
            )
        )

    return NextBestActionCaseResponse(
        case_id=str(case.id),
        amount_at_risk=float(case.amount_at_risk or 0.0),
        candidate_actions=candidate_items,
        selected_action=nba.action_type,
        selected_channel=nba.channel,
        expected_recovery_value=float(nba.expected_recovery_value),
        expected_recovery_probability=nba.expected_recovery_probability,
        model_version="calibrated_v1",
        reason=nba.reason,
    )
