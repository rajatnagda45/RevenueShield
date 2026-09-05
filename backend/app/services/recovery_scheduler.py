"""RecoveryScheduler orchestrating the lifecycle, evaluation, execution, waiting, and stopping rules of RecoveryPlans."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.decision.base import DecisionContext
from app.decision.policy import PolicyEngine, PolicyEvaluationResult
from app.models.audit_log import AuditLog
from app.models.learning import LearningExample
from app.models.promise_to_pay import PromiseToPay
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep
from app.services.communication_service import CommunicationService
from app.services.next_best_action_engine import NextBestActionEngine
from app.experiments.assigner import ExperimentAssigner, HOLDOUT_BLOCKING_RULE
from app.degradation.monitor import DegradationMonitor

logger = logging.getLogger(__name__)


class RecoveryScheduler:
    """Core domain scheduler orchestrating multi-step adaptive recovery sequences."""

    @classmethod
    def create_or_get_plan(
        cls,
        db: Session,
        case_id: uuid.UUID,
        max_steps: Optional[int] = None,
    ) -> RecoveryPlan:
        """Idempotently create or retrieve an active RecoveryPlan for a case."""
        existing = db.scalar(
            select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case_id)
        )
        if existing:
            return existing

        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
        if not case:
            raise ValueError(f"RecoveryCase '{case_id}' not found.")

        steps_limit = max_steps if max_steps is not None else settings.MAX_RECOVERY_STEPS
        plan = RecoveryPlan(
            recovery_case_id=case.id,
            status="ACTIVE",
            current_step=0,
            max_steps=steps_limit,
        )
        db.add(plan)
        db.flush()

        cls._audit(
            db=db,
            case_id=case.id,
            plan_id=plan.id,
            action="RECOVERY_PLAN_CREATED",
            metadata={"max_steps": steps_limit, "amount_at_risk": str(case.amount_at_risk)},
        )
        db.commit()
        db.refresh(plan)
        logger.info(f"[PLAN_CREATED] Case={case_id} Plan={plan.id} MaxSteps={steps_limit}")
        return plan

    @classmethod
    def evaluate_and_advance_plan(
        cls,
        db: Session,
        plan_id: uuid.UUID,
        reference_time: Optional[datetime] = None,
        force_engagement_signal: Optional[bool] = None,
        dry_run: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Evaluate plan status, check policy & signals, compute NextBestAction, execute step, and set wait timer."""
        now = reference_time or datetime.now(timezone.utc)
        plan = db.scalar(select(RecoveryPlan).where(RecoveryPlan.id == plan_id))
        if not plan:
            raise ValueError(f"RecoveryPlan '{plan_id}' not found.")

        case = plan.recovery_case or db.scalar(select(RecoveryCase).where(RecoveryCase.id == plan.recovery_case_id))

        # 1. Monotonic Stopping Rule: Case already recovered or closed
        if case.status in ["RECOVERED", "CLOSED"] or plan.status in ["RECOVERED", "COMPLETED", "CANCELLED"]:
            if plan.status not in ["RECOVERED", "COMPLETED"]:
                plan.status = "COMPLETED"
                plan.completed_at = now
                plan.completion_reason = f"CASE_ALREADY_{case.status}"
                cls._cancel_pending_steps(plan, now)
                cls._audit(
                    db=db,
                    case_id=case.id,
                    plan_id=plan.id,
                    action="RECOVERY_PLAN_STOPPED_AFTER_RECOVERY",
                    metadata={"case_status": case.status},
                )
                db.commit()
            return {
                "success": True,
                "status": plan.status,
                "plan_id": str(plan.id),
                "case_id": str(case.id),
                "action": "STOPPED",
                "reason": f"Case is already {case.status}.",
            }

        # 1b. Measurement: HOLDOUT arm cases are observed only. One OBSERVE step is recorded so the
        #     audit trail shows the deliberate non-intervention; stopping rules above still apply.
        if ExperimentAssigner.is_holdout(case):
            observe_step = next((s for s in (plan.steps or []) if s.action_type == "OBSERVE"), None)
            if observe_step is None:
                step_num = plan.current_step + 1
                observe_step = RecoveryPlanStep(
                    recovery_plan_id=plan.id,
                    step_number=step_num,
                    action_type="OBSERVE",
                    channel="NONE",
                    status="COMPLETED",
                    scheduled_at=now,
                    executed_at=now,
                    completed_at=now,
                    reason="HOLDOUT arm: observe only, no outreach or retries (incremental measurement).",
                    step_metadata={"blocking_rule": HOLDOUT_BLOCKING_RULE, "experiment_arm": "HOLDOUT"},
                )
                db.add(observe_step)
                plan.current_step = step_num
                cls._audit(
                    db=db,
                    case_id=case.id,
                    plan_id=plan.id,
                    action="HOLDOUT_OBSERVE_ONLY",
                    metadata={"step_number": step_num, "rule": HOLDOUT_BLOCKING_RULE},
                )
            plan.status = "WAITING"
            plan.next_evaluation_at = now + timedelta(hours=settings.RECOVERY_REEVALUATION_HOURS)
            db.commit()
            return {
                "success": True,
                "status": "HOLDOUT_OBSERVE",
                "plan_id": str(plan.id),
                "case_id": str(case.id),
                "action": "OBSERVE",
                "blocking_rule": HOLDOUT_BLOCKING_RULE,
                "reason": "Case is in the HOLDOUT arm; observing without intervention.",
                "next_evaluation_at": plan.next_evaluation_at.isoformat() if plan.next_evaluation_at else None,
            }

        # 2. Check if Plan is PAUSED
        if plan.status == "PAUSED":
            logger.info(f"[PLAN_PAUSED] Plan {plan.id} is paused. Skipping evaluation.")
            return {
                "success": True,
                "status": "PAUSED",
                "plan_id": str(plan.id),
                "case_id": str(case.id),
                "action": "NO_ACTION",
                "reason": "Recovery plan is currently PAUSED.",
            }

        # 3. Check Plan Expiration (MAX_RECOVERY_DURATION_HOURS)
        plan_created_dt = plan.created_at
        if plan_created_dt.tzinfo is None:
            plan_created_dt = plan_created_dt.replace(tzinfo=timezone.utc)
        hours_elapsed = (now - plan_created_dt).total_seconds() / 3600.0
        if hours_elapsed >= settings.MAX_RECOVERY_DURATION_HOURS:
            plan.status = "EXPIRED"
            plan.completed_at = now
            plan.completion_reason = "MAX_RECOVERY_DURATION_HOURS_EXCEEDED"
            cls._cancel_pending_steps(plan, now)
            cls._audit(
                db=db,
                case_id=case.id,
                plan_id=plan.id,
                action="RECOVERY_PLAN_EXPIRED",
                metadata={"hours_elapsed": hours_elapsed, "max_hours": settings.MAX_RECOVERY_DURATION_HOURS},
            )
            db.commit()
            return {
                "success": True,
                "status": "EXPIRED",
                "plan_id": str(plan.id),
                "case_id": str(case.id),
                "action": "NO_ACTION",
                "reason": f"Plan exceeded max duration of {settings.MAX_RECOVERY_DURATION_HOURS} hours.",
            }

        # 4. Check Maximum Steps Cap (MAX_RECOVERY_STEPS)
        if plan.current_step >= plan.max_steps:
            plan.status = "COMPLETED"
            plan.completed_at = now
            plan.completion_reason = "MAX_RECOVERY_STEPS_REACHED"
            cls._cancel_pending_steps(plan, now)
            cls._audit(
                db=db,
                case_id=case.id,
                plan_id=plan.id,
                action="RECOVERY_PLAN_COMPLETED",
                metadata={"steps_completed": plan.current_step, "max_steps": plan.max_steps},
            )
            db.commit()
            return {
                "success": True,
                "status": "COMPLETED",
                "plan_id": str(plan.id),
                "case_id": str(case.id),
                "action": "NO_ACTION",
                "reason": f"Maximum steps ({plan.max_steps}) reached.",
            }

        # 4b. Agent-driven planning: the Recovery Agent (LLM with deterministic fallback) chooses the step.
        if settings.AGENT_DRIVES_PLANS:
            from app.agent.runner import RecoveryAgentRunner
            run = RecoveryAgentRunner.run(db, case.id, dry_run=bool(dry_run), now=now)
            plan_action = (run.final_plan or {}).get("action", "NO_ACTION")
            # Step numbers are unique per plan; WAIT/NO_ACTION steps are recorded but do not consume a touch.
            max_step = db.scalar(select(func.max(RecoveryPlanStep.step_number)).where(RecoveryPlanStep.recovery_plan_id == plan.id)) or 0
            step_num = int(max_step) + 1
            step_status = {"EXECUTED": "COMPLETED", "AWAITING_APPROVAL": "SCHEDULED", "BLOCKED": "BLOCKED", "HANDOFF": "COMPLETED", "NO_ACTION": "SKIPPED"}.get(run.status, "FAILED")
            step = RecoveryPlanStep(
                recovery_plan_id=plan.id, step_number=step_num, action_type=f"AGENT:{plan_action}",
                channel={"SEND_WHATSAPP": "WHATSAPP", "SEND_EMAIL": "EMAIL", "START_VOICE_CALL": "VOICE", "SEND_PAYMENT_LINK": "WHATSAPP", "OFFER_PAYMENT_PLAN": "WHATSAPP"}.get(plan_action, "NONE"),
                status=step_status, scheduled_at=now, executed_at=now, completed_at=now if step_status != "SCHEDULED" else None,
                reason=(run.final_plan or {}).get("rationale"), prediction_score=(run.final_plan or {}).get("confidence"),
                step_metadata={"agent_run_id": str(run.id), "provider": run.provider, "degraded_to_rules": run.degraded_to_rules, "execution": run.execution_result},
            )
            db.add(step)
            if plan_action not in ("WAIT", "NO_ACTION") and run.status in ("EXECUTED", "AWAITING_APPROVAL", "HANDOFF"):
                plan.current_step = (plan.current_step or 0) + 1
            if plan.status not in ("PAUSED", "COMPLETED", "RECOVERED", "CANCELLED", "EXPIRED"):
                plan.status = "WAITING"
                nea = plan.next_evaluation_at
                if nea is not None and nea.tzinfo is None:
                    nea = nea.replace(tzinfo=timezone.utc)
                if nea is None or nea <= now:
                    plan.next_evaluation_at = now + timedelta(hours=settings.RECOVERY_REEVALUATION_HOURS)
            cls._audit(db=db, case_id=case.id, plan_id=plan.id, action="RECOVERY_STEP_PLANNED_BY_AGENT", metadata={"step_number": step_num, "agent_run_id": str(run.id), "action": plan_action, "status": run.status})
            db.commit()
            return {
                "success": run.status in ("EXECUTED", "AWAITING_APPROVAL", "HANDOFF", "NO_ACTION"),
                "status": run.status, "plan_id": str(plan.id), "case_id": str(case.id), "step_number": step_num,
                "action": plan_action, "agent_run_id": str(run.id), "provider": run.provider, "degraded_to_rules": run.degraded_to_rules,
                "reason": (run.final_plan or {}).get("rationale"), "execution": run.execution_result,
                "next_evaluation_at": plan.next_evaluation_at.isoformat() if plan.next_evaluation_at else None,
            }

        # 5. Compute Next-Best-Action
        plan.status = "EVALUATING"
        cls._audit(
            db=db,
            case_id=case.id,
            plan_id=plan.id,
            action="RECOVERY_PLAN_EVALUATED",
            metadata={"current_step": plan.current_step, "evaluation_time": now.isoformat()},
        )

        nba = NextBestActionEngine.compute_next_best_action(
            db=db,
            case=case,
            reference_time=now,
            force_engagement_signal=force_engagement_signal,
        )

        cls._audit(
            db=db,
            case_id=case.id,
            plan_id=plan.id,
            action="NEXT_BEST_ACTION_SELECTED",
            metadata={
                "action_type": nba.action_type,
                "channel": nba.channel,
                "expected_recovery_value": str(nba.expected_recovery_value),
                "probability": nba.expected_recovery_probability,
                "reason": nba.reason,
            },
        )

        # If NBA is NO_ACTION
        if nba.action_type == "NO_ACTION":
            plan.status = "COMPLETED"
            plan.completed_at = now
            plan.completion_reason = nba.reason
            db.commit()
            return {
                "success": True,
                "status": "COMPLETED",
                "plan_id": str(plan.id),
                "case_id": str(case.id),
                "action": "NO_ACTION",
                "reason": nba.reason,
            }

        # 6. Policy Hard Constraints Check
        action_mapping = {
            "EMAIL_PAYMENT_RECOVERY": "SEND_PAYMENT_LINK",
            "EMAIL_FOLLOWUP": "SEND_PAYMENT_LINK",
        }
        policy_action = action_mapping.get(nba.action_type, "SEND_PAYMENT_LINK")
        
        has_ptp = bool(
            db.scalar(
                select(PromiseToPay).where(
                    PromiseToPay.recovery_case_id == case.id,
                    PromiseToPay.status == "ACTIVE",
                )
            )
        )

        context = DecisionContext(
            case_id=str(case.id),
            case_type=case.case_type or "PAYMENT_FAILURE",
            amount_at_risk=case.amount_at_risk or Decimal("0.00"),
            currency=case.currency or "INR",
            case_age_hours=round(hours_elapsed, 2),
            retry_count=plan.current_step,
            diagnosis_category="PAYMENT_METHOD_ISSUE",
            diagnosis_confidence=0.85,
            risk_score=0.25,
            recovery_probability=nba.expected_recovery_probability,
            customer_email_available=bool(case.customer and case.customer.email),
            customer_phone_available=bool(case.customer and case.customer.phone),
            promise_to_pay_active=has_ptp,
            current_time=now,
            metadata={
                "experiment_arm": case.experiment_arm,
                "leak_surface": case.leak_surface,
                "timezone": getattr(case.customer, "timezone", None) if case.customer else None,
                "systemic_incident_active": DegradationMonitor.case_is_held(db, case),
            },
        )

        policy_res: PolicyEvaluationResult = PolicyEngine.evaluate(
            action_type=policy_action,
            context=context,
            case_status=case.status,
            active_interventions_count=0,
        )

        step_num = plan.current_step + 1

        # Issuer degradation hold: wait without consuming a plan step; re-check when the incident may have cleared.
        if not policy_res.allowed and policy_res.blocking_rule == "ISSUER_DEGRADED_HOLD":
            plan.status = "WAITING"
            plan.next_evaluation_at = now + timedelta(minutes=settings.DEGRADATION_RECHECK_MINUTES)
            cls._audit(
                db=db,
                case_id=case.id,
                plan_id=plan.id,
                action="RECOVERY_STEP_HELD_SYSTEMIC_INCIDENT",
                metadata={"rule": policy_res.blocking_rule, "reason": policy_res.reason, "next_evaluation_at": plan.next_evaluation_at.isoformat()},
            )
            db.commit()
            return {
                "success": True,
                "status": "HELD",
                "plan_id": str(plan.id),
                "case_id": str(case.id),
                "action": "WAIT",
                "blocking_rule": policy_res.blocking_rule,
                "reason": policy_res.reason,
                "next_evaluation_at": plan.next_evaluation_at.isoformat(),
            }

        # If Policy Blocks
        if not policy_res.allowed:
            step = RecoveryPlanStep(
                recovery_plan_id=plan.id,
                step_number=step_num,
                action_type=nba.action_type,
                channel=nba.channel,
                status="BLOCKED",
                scheduled_at=now,
                executed_at=now,
                completed_at=now,
                reason=f"Policy Blocked: {policy_res.reason}",
                prediction_score=nba.expected_recovery_probability,
                expected_recovery_value=nba.expected_recovery_value,
                step_metadata={"blocking_rule": policy_res.blocking_rule},
            )
            db.add(step)
            plan.current_step = step_num
            plan.status = "WAITING"
            plan.next_evaluation_at = now + timedelta(hours=settings.RECOVERY_REEVALUATION_HOURS)

            cls._audit(
                db=db,
                case_id=case.id,
                plan_id=plan.id,
                action="RECOVERY_STEP_BLOCKED",
                metadata={"step_number": step_num, "rule": policy_res.blocking_rule, "reason": policy_res.reason},
            )
            db.commit()
            return {
                "success": False,
                "status": "BLOCKED",
                "plan_id": str(plan.id),
                "case_id": str(case.id),
                "step_number": step_num,
                "action": nba.action_type,
                "blocking_rule": policy_res.blocking_rule,
                "reason": policy_res.reason,
            }

        # 7. Create and Execute Step
        step = RecoveryPlanStep(
            recovery_plan_id=plan.id,
            step_number=step_num,
            action_type=nba.action_type,
            channel=nba.channel,
            status="RUNNING",
            scheduled_at=now,
            executed_at=now,
            reason=nba.reason,
            prediction_score=nba.expected_recovery_probability,
            expected_recovery_value=nba.expected_recovery_value,
            step_metadata={"confidence": nba.confidence, "ev": str(nba.expected_recovery_value)},
        )
        db.add(step)
        plan.status = "EXECUTING"
        db.flush()

        cls._audit(
            db=db,
            case_id=case.id,
            plan_id=plan.id,
            action="RECOVERY_STEP_CREATED",
            metadata={"step_number": step_num, "action_type": nba.action_type, "channel": nba.channel},
        )
        cls._audit(
            db=db,
            case_id=case.id,
            plan_id=plan.id,
            action="RECOVERY_STEP_EXECUTED",
            metadata={"step_number": step_num, "action_type": nba.action_type},
        )

        # Dispatch via CommunicationService
        dispatch_res = CommunicationService.dispatch_action(
            db=db,
            case_id=str(case.id),
            action_type=nba.action_type,
            channel=nba.channel,
            dry_run=dry_run,
            reference_time=now,
        )

        # 8. Complete Step & Set Waiting Timer
        step.status = "COMPLETED" if dispatch_res.get("success", False) else "FAILED"
        step.completed_at = datetime.now(timezone.utc)
        plan.current_step = step_num
        plan.status = "WAITING"
        plan.next_evaluation_at = now + timedelta(hours=settings.RECOVERY_REEVALUATION_HOURS)

        # Record Learning Outcome
        cls._record_step_learning(db, case, plan, step, nba)

        db.commit()
        db.refresh(plan)

        return {
            "success": dispatch_res.get("success", True),
            "status": plan.status,
            "plan_id": str(plan.id),
            "case_id": str(case.id),
            "step_number": step_num,
            "action": nba.action_type,
            "channel": nba.channel,
            "expected_recovery_value": float(nba.expected_recovery_value),
            "expected_recovery_probability": nba.expected_recovery_probability,
            "next_evaluation_at": plan.next_evaluation_at.isoformat() if plan.next_evaluation_at else None,
            "communication": dispatch_res,
        }

    @classmethod
    def pause_plan(
        cls,
        db: Session,
        plan_id: Optional[uuid.UUID] = None,
        case_id: Optional[uuid.UUID] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Pause automated recovery outreach for a plan by plan_id or case_id."""
        if plan_id:
            plan = db.scalar(select(RecoveryPlan).where(RecoveryPlan.id == plan_id))
        elif case_id:
            plan = db.scalar(select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case_id))
        else:
            raise ValueError("Must provide either plan_id or case_id.")

        if not plan:
            return {"status": "NO_PLAN", "message": "No plan found to pause."}

        plan.status = "PAUSED"
        cls._audit(
            db=db,
            case_id=plan.recovery_case_id,
            plan_id=plan.id,
            action="RECOVERY_PLAN_PAUSED",
            metadata={"reason": reason or "User requested pause."},
        )
        db.flush()
        return {"status": "PAUSED", "plan_id": str(plan.id)}

    @classmethod
    def resume_plan(
        cls,
        db: Session,
        plan_id: Optional[uuid.UUID] = None,
        case_id: Optional[uuid.UUID] = None,
    ) -> Dict[str, Any]:
        """Resume an active recovery plan by plan_id or case_id."""
        if plan_id:
            plan = db.scalar(select(RecoveryPlan).where(RecoveryPlan.id == plan_id))
        elif case_id:
            plan = db.scalar(select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case_id))
        else:
            raise ValueError("Must provide either plan_id or case_id.")

        if not plan:
            return {"status": "NO_PLAN", "message": "No plan found to resume."}

        plan.status = "WAITING" if plan.current_step > 0 else "ACTIVE"
        cls._audit(
            db=db,
            case_id=plan.recovery_case_id,
            plan_id=plan.id,
            action="RECOVERY_PLAN_RESUMED",
            metadata={},
        )
        db.flush()
        return {"status": plan.status, "plan_id": str(plan.id)}

    @classmethod
    def stop_plan_on_recovery(cls, db: Session, case_id: uuid.UUID, reason: str = "PAYMENT_CAPTURED") -> None:
        """Monotonic stopping rule: complete active plan and cancel any scheduled steps."""
        plan = db.scalar(select(RecoveryPlan).where(RecoveryPlan.recovery_case_id == case_id))
        if not plan:
            return
        now = datetime.now(timezone.utc)
        plan.status = "COMPLETED"
        plan.completed_at = now
        plan.completion_reason = reason
        cls._cancel_pending_steps(plan, now)
        cls._audit(
            db=db,
            case_id=case_id,
            plan_id=plan.id,
            action="RECOVERY_PLAN_STOPPED_AFTER_RECOVERY",
            metadata={"reason": reason},
        )
        db.flush()

    @classmethod
    def process_due_plans(cls, db: Session, reference_time: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Find all plans in WAITING status with next_evaluation_at <= now and advance them."""
        now = reference_time or datetime.now(timezone.utc)
        due_plans = db.scalars(
            select(RecoveryPlan).where(
                RecoveryPlan.status == "WAITING",
                RecoveryPlan.next_evaluation_at <= now,
            )
        ).all()
        results = []
        for plan in due_plans:
            try:
                res = cls.evaluate_and_advance_plan(db, plan.id, reference_time=now)
                results.append(res)
            except Exception as exc:
                logger.error(f"[PLAN_PROCESS_ERROR] Plan {plan.id}: {exc}")
                results.append({"plan_id": str(plan.id), "error": str(exc)})
        return results

    @classmethod
    def _cancel_pending_steps(cls, plan: RecoveryPlan, timestamp: datetime) -> None:
        """Cancel any non-completed steps on plan closure."""
        if plan.steps:
            for step in plan.steps:
                if step.status in ["PENDING", "SCHEDULED", "RUNNING"]:
                    step.status = "CANCELLED"
                    step.completed_at = timestamp

    @classmethod
    def _record_step_learning(
        cls,
        db: Session,
        case: RecoveryCase,
        plan: RecoveryPlan,
        step: RecoveryPlanStep,
        nba: Any,
    ) -> None:
        """Log a learning outcome telemetry record for Step 11 learning dataset."""
        learning = LearningExample(
            recovery_case_id=case.id,
            diagnosis_category="PAYMENT_METHOD_ISSUE",
            diagnosis_confidence=0.85,
            risk_score=0.25,
            recovery_probability=float(nba.expected_recovery_probability),
            amount_at_risk=case.amount_at_risk or Decimal("0.00"),
            case_age_at_decision_hours=0.0,
            customer_success_rate_at_decision=0.0,
            customer_failure_count_at_decision=1,
            previous_recovery_attempts=step.step_number,
            action_type=step.action_type,
            decision_score=float(nba.score),
            decision_confidence=float(nba.confidence),
            policy_allowed=True,
            feature_snapshot={
                "plan_id": str(plan.id),
                "step_id": str(step.id),
                "step_number": step.step_number,
                "action_type": step.action_type,
                "channel": step.channel,
                "prediction_probability": nba.expected_recovery_probability,
                "amount_at_risk": float(case.amount_at_risk or 0.0),
                "expected_recovery_value": float(nba.expected_recovery_value or 0.0),
            },
        )
        db.add(learning)

    @classmethod
    def _audit(
        cls,
        db: Session,
        case_id: uuid.UUID,
        plan_id: uuid.UUID,
        action: str,
        metadata: Dict[str, Any],
    ) -> None:
        """Record structured audit trail entry."""
        merged_meta = dict(metadata)
        merged_meta["plan_id"] = str(plan_id)
        audit = AuditLog(
            recovery_case_id=case_id,
            entity_type="RECOVERY_PLAN",
            entity_id=str(plan_id),
            action=action,
            actor_type="SYSTEM",
            actor_id="recovery_scheduler_v1",
            audit_metadata=merged_meta,
        )
        db.add(audit)
