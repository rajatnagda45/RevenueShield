"""Agent runner: bounded planner loop, validation, approvals, execution, trace.

One run = one evaluation of one case:
1. build the dossier (with precomputed verdicts),
2. loop with the planner (LLM or rules) for at most `LLM_MAX_TURNS`, executing gated tool calls,
3. validate the final plan deterministically (message, envelope, structure),
4. either request approval or execute the chosen action through the toolbox,
5. persist the run with every tool call, verdict, token count and the final outcome.
If the LLM is unreachable the run degrades to the rules planner and says so in the trace.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.dossier import DossierBuilder
from app.agent.providers.base import LLMProvider, resolve_provider
from app.agent.providers.null_provider import NullLLMProvider
from app.agent.schemas import AgentPlan, ProviderUnavailable, ToolCall, ToolResult
from app.agent.tools import AgentToolbox
from app.agent.validators import PlanValidator
from app.core.config import settings
from app.models.agent_run import AgentRun
from app.models.audit_log import AuditLog
from app.models.recovery_case import RecoveryCase

logger = logging.getLogger(__name__)

# List prices (USD per million tokens, input / output) for cost accounting in the trace. Longest keys first so
# "gpt-4o-mini" is not priced as "gpt-4o".
PRICE_PER_MTOK = {
    "claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
    "gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00), "gpt-4.1-mini": (0.40, 1.60), "gpt-4.1": (2.00, 8.00),
}


class PlanExecutor:
    """Turns a validated plan into exactly one gated toolbox call."""

    @classmethod
    def needs_approval(cls, plan: AgentPlan, outstanding: Decimal) -> Optional[str]:
        if plan.requires_human_approval:
            return plan.approval_reason or "planner requested human approval"
        if plan.action == "ESCALATE_TO_MERCHANT":
            return "merchant escalation always requires a human decision"
        if plan.action == "START_VOICE_CALL" and float(outstanding) >= settings.APPROVAL_VOICE_AMOUNT_THRESHOLD:
            return f"voice call on a Rs {float(outstanding):,.0f} case (threshold Rs {settings.APPROVAL_VOICE_AMOUNT_THRESHOLD:,.0f})"
        if plan.action == "CLOSE_CASE" and float(outstanding) >= settings.APPROVAL_CLOSE_CASE_AMOUNT_THRESHOLD:
            return f"closing a case with Rs {float(outstanding):,.0f} outstanding"
        if plan.action == "OFFER_PAYMENT_PLAN" and plan.payment_plan and plan.payment_plan.waiver_pct > settings.NEGOTIATION_MAX_WAIVER_PCT:
            return f"waiver of {plan.payment_plan.waiver_pct:.1f}% exceeds the envelope"
        return None

    @classmethod
    def execute(cls, db: Session, case: RecoveryCase, plan: AgentPlan, *, dry_run: bool, now: datetime, approved: bool = False) -> Dict[str, Any]:
        box = AgentToolbox(db, case, dry_run=dry_run, now=now, actor="recovery_agent_v1" if not approved else "recovery_agent_v1+operator")
        a = plan.action
        call_id = f"exec_{uuid.uuid4().hex[:8]}"
        body = plan.message.body if plan.message else None
        if a == "SEND_WHATSAPP":
            res = box.execute(ToolCall(id=call_id, name="send_whatsapp", arguments={"body": body, "language": plan.message.language if plan.message else "ENGLISH"}))
        elif a == "SEND_EMAIL":
            res = box.execute(ToolCall(id=call_id, name="send_email", arguments={"body": body}))
        elif a == "SEND_PAYMENT_LINK":
            res = box.execute(ToolCall(id=call_id, name="send_payment_link", arguments={}))
        elif a == "OFFER_PAYMENT_PLAN":
            pp = plan.payment_plan
            res = box.execute(ToolCall(id=call_id, name="offer_payment_plan", arguments={"installments": pp.installments, "first_payment_pct": pp.first_payment_pct, "due_days": pp.due_days, "waiver_pct": pp.waiver_pct}))
        elif a == "START_VOICE_CALL":
            res = box.execute(ToolCall(id=call_id, name="start_voice_call", arguments={}))
        elif a == "SCHEDULE_RETRY":
            res = box.execute(ToolCall(id=call_id, name="schedule_retry", arguments={"hours": plan.retry_after_hours or 24, "reason": plan.rationale}))
        elif a == "RECORD_PROMISE_TO_PAY":
            p = plan.promise
            res = box.execute(ToolCall(id=call_id, name="record_promise_to_pay", arguments={"promised_date": p.promised_date.isoformat(), "amount": p.amount, "note": p.note}))
        elif a == "WAIT":
            res = box.execute(ToolCall(id=call_id, name="wait", arguments={"hours": plan.wait_hours or 24, "reason": plan.rationale}))
        elif a == "HANDOFF_TO_HUMAN":
            res = box.execute(ToolCall(id=call_id, name="handoff_to_human", arguments={"reason": plan.handoff_reason or plan.rationale}))
        elif a == "ESCALATE_TO_MERCHANT":
            res = box.execute(ToolCall(id=call_id, name="escalate_to_merchant", arguments={"reason": plan.rationale}))
        elif a == "CLOSE_CASE":
            res = box.execute(ToolCall(id=call_id, name="close_case", arguments={"reason": plan.rationale}))
        else:
            res = ToolResult(tool_call_id=call_id, name="no_action", ok=True, result={"ok": True, "status": "NO_ACTION"})
        out = {"action": a, "tool": res.name, "ok": res.ok, "blocking_rule": res.blocked_rule, "error": res.error, "result": res.result}
        return out


class RecoveryAgentRunner:
    # Test/replay hook: when set, used instead of resolve_provider() (e.g. to simulate an outage window).
    provider_override: Optional[LLMProvider] = None

    @classmethod
    def run(
        cls,
        db: Session,
        case_id: uuid.UUID,
        *,
        dry_run: bool = True,
        provider: Optional[LLMProvider] = None,
        provider_name: Optional[str] = None,
        now: Optional[datetime] = None,
        max_turns: Optional[int] = None,
    ) -> AgentRun:
        now = now or datetime.now(timezone.utc)
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.id == case_id))
        if not case:
            raise ValueError(f"RecoveryCase '{case_id}' not found.")
        planner: LLMProvider = provider or cls.provider_override or resolve_provider(provider_name)
        max_turns = max_turns or settings.LLM_MAX_TURNS

        run = AgentRun(recovery_case_id=case.id, provider=planner.name, model=planner.model, prompt_version=cls._prompt_version(planner), status="RUNNING", dry_run=dry_run, started_at=now)
        db.add(run)
        db.flush()

        dossier = DossierBuilder.build(db, case, now=now)
        run.dossier = dossier
        run.dossier_hash = dossier.get("hash")
        outstanding = Decimal(str(dossier["case"]["outstanding_amount"] or 0))
        first_name = dossier["customer"].get("first_name")

        box = AgentToolbox(db, case, dry_run=dry_run, now=now)
        tools = box.specs()
        transcript: List[Dict[str, Any]] = []
        trace: List[Dict[str, Any]] = []
        plan: Optional[AgentPlan] = None
        degraded = False
        input_tokens = output_tokens = 0

        for turn_no in range(max_turns):
            try:
                turn = planner.plan(dossier, tools, transcript)
            except ProviderUnavailable as exc:
                degraded = True
                trace.append({"turn": turn_no, "event": "provider_unavailable", "provider": planner.name, "error": str(exc)})
                planner = NullLLMProvider()
                transcript = [t for t in transcript if t.get("role") != "assistant" or True]  # keep tool results for context
                continue
            except Exception as exc:  # planner bug: never take the case down with it
                degraded = True
                trace.append({"turn": turn_no, "event": "provider_error", "provider": planner.name, "error": repr(exc)})
                planner = NullLLMProvider()
                continue

            input_tokens += int(turn.usage.get("input_tokens", 0) or 0)
            output_tokens += int(turn.usage.get("output_tokens", 0) or 0)
            transcript.append({"role": "assistant", "content": turn.usage.get("assistant_content") or (turn.raw_text or "")})
            trace.append({"turn": turn_no, "event": "planner_turn", "provider": planner.name, "text": (turn.raw_text or "")[:1000], "tool_calls": [c.model_dump() for c in turn.tool_calls], "usage": {k: v for k, v in turn.usage.items() if k != "assistant_content"}})

            if turn.final_plan is not None:
                plan = turn.final_plan
                break
            if not turn.tool_calls:
                trace.append({"turn": turn_no, "event": "no_tool_calls_no_plan"})
                break

            results: List[ToolResult] = []
            for call in turn.tool_calls:
                if call.name == "__invalid_plan__":
                    results.append(ToolResult(tool_call_id=call.id, name="submit_plan", ok=False, error=f"plan rejected: {call.arguments.get('error')}"))
                    continue
                res = box.execute(call)
                results.append(res)
                trace.append({"turn": turn_no, "event": "tool_result", "tool": call.name, "arguments": call.arguments, "ok": res.ok, "blocked_rule": res.blocked_rule, "error": res.error, "result": cls._trim(res.result)})
            transcript.append({"role": "tool_results", "results": [r.model_dump() for r in results]})
        run.turns = len([t for t in trace if t["event"] == "planner_turn"])

        if plan is None:
            degraded = True
            trace.append({"event": "fallback_plan", "reason": "planner produced no plan within the turn budget"})
            plan = NullLLMProvider().plan(dossier, tools, [{"role": "assistant", "content": "x"}]).final_plan

        # Deterministic validation of the plan
        validation = PlanValidator.validate(plan, outstanding_amount=outstanding, first_name=first_name)
        if not validation.ok and plan.message is not None and plan.action in ("SEND_WHATSAPP", "SEND_EMAIL", "OFFER_PAYMENT_PLAN"):
            trace.append({"event": "message_rejected_fallback_to_template", "errors": validation.errors})
            plan = plan.model_copy(update={"message": None})
            validation = PlanValidator.validate(plan, outstanding_amount=outstanding, first_name=first_name)
        run.final_plan = plan.model_dump(mode="json")
        run.validation = validation.to_dict()
        run.degraded_to_rules = degraded
        run.input_tokens, run.output_tokens = input_tokens, output_tokens
        run.cost_usd = cls._cost(run.model, input_tokens, output_tokens)

        if not validation.ok:
            run.status = "BLOCKED"
            run.execution_result = {"ok": False, "blocking_rule": "PLAN_VALIDATION_FAILED", "errors": validation.errors}
            run.trace = trace
            run.completed_at = datetime.now(timezone.utc)
            cls._audit(db, case, run)
            db.flush()
            return run

        approval_reason = PlanExecutor.needs_approval(plan, outstanding)
        if approval_reason:
            from app.agent.approvals import ApprovalService
            approval = ApprovalService.request(db, case=case, action=plan.action, payload={"plan": plan.model_dump(mode="json")}, reason=approval_reason, agent_run=run, now=now)
            run.approval_id = approval.id
            run.status = "AWAITING_APPROVAL"
            run.execution_result = {"ok": False, "awaiting_approval": True, "approval_id": str(approval.id), "reason": approval_reason}
            trace.append({"event": "approval_requested", "approval_id": str(approval.id), "reason": approval_reason})
        else:
            result = PlanExecutor.execute(db, case, plan, dry_run=dry_run, now=now)
            run.execution_result = result
            trace.append({"event": "executed", **cls._trim(result)})
            if plan.action == "HANDOFF_TO_HUMAN":
                run.status = "HANDOFF"
            elif plan.action == "NO_ACTION":
                run.status = "NO_ACTION"
            else:
                run.status = "EXECUTED" if result.get("ok") else "BLOCKED"

        run.trace = trace
        run.completed_at = datetime.now(timezone.utc)
        cls._audit(db, case, run)
        db.flush()
        try:
            from app.observability.metrics import metrics
            metrics.agent_run(run.provider, run.status, run.degraded_to_rules, run.input_tokens, run.output_tokens)
        except Exception:
            pass
        return run

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _prompt_version(planner: LLMProvider) -> str:
        try:
            from app.agent.providers.prompt import PROMPT_VERSION
            return PROMPT_VERSION if planner.name in ("anthropic", "openai") else "rules-v1"
        except Exception:
            return "rules-v1"

    @staticmethod
    def _cost(model: str, in_tok: int, out_tok: int) -> float:
        for key, (pi, po) in PRICE_PER_MTOK.items():
            if key in (model or ""):
                return round(in_tok / 1e6 * pi + out_tok / 1e6 * po, 6)
        return 0.0

    @staticmethod
    def _trim(obj: Any, limit: int = 4000) -> Any:
        import json
        try:
            s = json.dumps(obj, default=str)
        except Exception:
            return {"repr": repr(obj)[:limit]}
        if len(s) <= limit:
            return obj
        return {"truncated": s[:limit]}

    @staticmethod
    def _audit(db: Session, case: RecoveryCase, run: AgentRun) -> None:
        db.add(AuditLog(
            recovery_case_id=case.id, actor_type="AGENT", actor_id=f"{run.provider}:{run.model}", action="AGENT_RUN_COMPLETED",
            entity_type="AgentRun", entity_id=str(run.id),
            audit_metadata={"status": run.status, "action": (run.final_plan or {}).get("action"), "degraded_to_rules": run.degraded_to_rules, "turns": run.turns, "dossier_hash": run.dossier_hash, "cost_usd": run.cost_usd},
        ))

    @staticmethod
    def serialize(run: AgentRun, include_dossier: bool = False) -> Dict[str, Any]:
        out = {
            "id": str(run.id), "case_id": str(run.recovery_case_id), "provider": run.provider, "model": run.model, "prompt_version": run.prompt_version,
            "status": run.status, "dry_run": run.dry_run, "degraded_to_rules": run.degraded_to_rules, "dossier_hash": run.dossier_hash, "turns": run.turns,
            "trace": run.trace, "final_plan": run.final_plan, "validation": run.validation, "execution_result": run.execution_result,
            "approval_id": str(run.approval_id) if run.approval_id else None, "input_tokens": run.input_tokens, "output_tokens": run.output_tokens, "cost_usd": run.cost_usd,
            "error": run.error, "started_at": run.started_at.isoformat() if run.started_at else None, "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        }
        if include_dossier:
            out["dossier"] = run.dossier
        return out
