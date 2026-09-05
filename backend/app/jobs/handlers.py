"""Job handlers and the recurring schedule.

Every handler takes (db, payload, now) and returns a JSON-serialisable result. Recurring jobs are
enqueued by the worker with an idempotency key derived from the interval bucket, so several
workers never run the same tick twice.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)

Handler = Callable[[Session, Dict[str, Any], datetime], Dict[str, Any]]
_HANDLERS: Dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[kind] = fn
        return fn
    return deco


def get_handler(kind: str) -> Optional[Handler]:
    return _HANDLERS.get(kind)


def known_kinds() -> List[str]:
    return sorted(_HANDLERS)


# ------------------------------------------------------------------ one-off jobs

@handler("plan.evaluate")
def plan_evaluate(db: Session, payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    from app.services.recovery_scheduler import RecoveryScheduler
    case_id = uuid.UUID(str(payload["case_id"]))
    plan = RecoveryScheduler.create_or_get_plan(db, case_id)
    if plan.status in ("PAUSED", "COMPLETED", "RECOVERED", "CANCELLED", "EXPIRED"):
        return {"skipped": plan.status}
    res = RecoveryScheduler.evaluate_and_advance_plan(db, plan.id, reference_time=now, dry_run=(settings.EXECUTION_MODE == "dry_run"))
    return {k: v for k, v in res.items() if k in ("status", "action", "step_number", "blocking_rule", "next_evaluation_at", "agent_run_id")}


@handler("agent.run")
def agent_run(db: Session, payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    from app.agent.runner import RecoveryAgentRunner
    run = RecoveryAgentRunner.run(db, uuid.UUID(str(payload["case_id"])), dry_run=(settings.EXECUTION_MODE == "dry_run"), now=now)
    return {"agent_run_id": str(run.id), "status": run.status, "action": (run.final_plan or {}).get("action")}


@handler("settlement.reconcile")
def settlement_reconcile(db: Session, payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    from app.ledger.settlement_reconciliation import SettlementReconciliationService
    if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
        return {"skipped": "razorpay credentials not configured"}
    day = payload.get("day")
    rows = SettlementReconciliationService.fetch_razorpay_recon(year=int(payload.get("year", now.year)), month=int(payload.get("month", now.month)), day=int(day) if day else None)
    summary = SettlementReconciliationService.reconcile_rows(db, rows)
    return {k: v for k, v in summary.items() if k != "matched"}


# ------------------------------------------------------------------ recurring ticks

@handler("plans.process_due")
def plans_process_due(db: Session, payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    from app.models.recovery_plan import RecoveryPlan
    from app.services.recovery_scheduler import RecoveryScheduler
    results = RecoveryScheduler.process_due_plans(db, reference_time=now)
    # Newly created plans are ACTIVE with no next_evaluation_at: give them their first evaluation.
    fresh = db.scalars(select(RecoveryPlan).where(RecoveryPlan.status == "ACTIVE").limit(settings.WORKER_BATCH_SIZE)).all()
    for plan in fresh:
        try:
            results.append(RecoveryScheduler.evaluate_and_advance_plan(db, plan.id, reference_time=now, dry_run=(settings.EXECUTION_MODE == "dry_run")))
        except Exception as exc:
            results.append({"plan_id": str(plan.id), "error": str(exc)})
    return {"evaluated": len(results), "errors": sum(1 for r in results if r.get("error"))}


@handler("sweep.checkout")
def sweep_checkout(db: Session, payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    from app.detectors.checkout_abandonment import CheckoutAbandonmentDetector
    res = CheckoutAbandonmentDetector.sweep(db, reference_time=now, abandon_after_minutes=int(payload.get("abandon_after_minutes", 30)))
    return {"candidates": res["candidates"], "opened": len(res["opened"])}


@handler("sweep.receivables")
def sweep_receivables(db: Session, payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    from app.detectors.receivables import ReceivablesDetector
    res = ReceivablesDetector.sweep(db, reference_time=now)
    return {"candidates": res["candidates"], "opened": len(res["opened"])}


@handler("sweep.mandates")
def sweep_mandates(db: Session, payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    from app.detectors.mandates import MandateRetrySequencer
    res = MandateRetrySequencer.sweep(db, now=now, dry_run=(settings.EXECUTION_MODE == "dry_run"))
    return {"notified": len(res["notified"]), "executed": len(res["executed"]), "cancelled": len(res["cancelled"])}


@handler("degradation.tick")
def degradation_tick(db: Session, payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    from app.degradation.monitor import DegradationMonitor
    res = DegradationMonitor.evaluate(db, now=now)
    return {k: (len(v) if isinstance(v, list) else v) for k, v in res.items() if k != "reference_time"}


@handler("approvals.expire")
def approvals_expire(db: Session, payload: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    from app.agent.approvals import ApprovalService
    return {"expired": len(ApprovalService.expire_due(db, now=now))}


# kind -> interval seconds. Idempotency key = f"{kind}:{bucket}" where bucket = floor(now / interval).
RECURRING: List[Tuple[str, int]] = [
    ("plans.process_due", 300),
    ("degradation.tick", 300),
    ("sweep.checkout", 900),
    ("sweep.mandates", 900),
    ("approvals.expire", 900),
    ("sweep.receivables", 3600),
    ("settlement.reconcile", 86400),
]


def recurring_key(kind: str, interval_seconds: int, now: datetime) -> str:
    bucket = int(now.timestamp()) // interval_seconds
    return f"{kind}:{bucket}"
