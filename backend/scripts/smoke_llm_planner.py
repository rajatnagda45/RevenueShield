"""Smoke-test the configured LLM planner on one real case, without touching customers.

    cd backend
    set OPENAI_API_KEY=sk-...                     (or put it in backend/.env)
    set DATABASE_URL=sqlite:///../reports/replay_demo_1000.db
    python scripts/smoke_llm_planner.py           # picks an open treatment case, dry run
    python scripts/smoke_llm_planner.py --provider openai --case <uuid> --model gpt-4o-mini

Prints which provider `auto` resolved to, the plan the model submitted, the tool calls it made, whether the
run degraded to rules, token usage and cost. Exit code 1 if the planner was unavailable or degraded, so the
same script doubles as a pre-demo check that the key works.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.agent.providers.base import effective_provider_name, resolve_provider  # noqa: E402
from app.agent.runner import RecoveryAgentRunner  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.recovery_case import RecoveryCase  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--provider", default=None, help="auto | anthropic | openai | null (default: settings.LLM_PROVIDER)")
    p.add_argument("--model", default=None, help="override the model for this run")
    p.add_argument("--case", default=None, help="RecoveryCase UUID; default: newest open TREATMENT case")
    p.add_argument("--execute", action="store_true", help="really execute the chosen action (default: dry run)")
    args = p.parse_args()

    name = effective_provider_name(args.provider)
    print(f"[SMOKE] LLM_PROVIDER={args.provider or settings.LLM_PROVIDER} -> {name}")
    if name == "null":
        print("[SMOKE] No LLM key configured (set OPENAI_API_KEY or ANTHROPIC_API_KEY). The deterministic planner would be used.")
        return 1

    provider = resolve_provider(args.provider)
    if args.model:
        provider.model = args.model
    if provider.name != name:
        print(f"[SMOKE] provider '{name}' could not be constructed (missing SDK or key); got '{provider.name}'.")
        return 1
    print(f"[SMOKE] provider={provider.name} model={provider.model}")

    db = SessionLocal()
    try:
        if args.case:
            case = db.get(RecoveryCase, uuid.UUID(args.case))
        else:
            case = db.scalar(
                select(RecoveryCase)
                .where(RecoveryCase.status.in_(["OPEN", "IN_PROGRESS"]), RecoveryCase.experiment_arm != "HOLDOUT")
                .order_by(RecoveryCase.created_at.desc())
            )
        if case is None:
            print("[SMOKE] no suitable case found; run `make replay` first or pass --case.")
            return 1
        print(f"[SMOKE] case={case.id} surface={case.leak_surface or case.case_type} status={case.status} outstanding={case.amount_at_risk - (case.recovered_amount or 0)}")

        run = RecoveryAgentRunner.run(db, case.id, dry_run=not args.execute, provider=provider)
        db.commit()
        snapshot = {k: getattr(run, k) for k in ("id", "status", "degraded_to_rules", "turns", "input_tokens", "output_tokens", "cost_usd", "final_plan", "trace")}
    finally:
        db.close()

    print(f"[SMOKE] run={snapshot['id']} status={snapshot['status']} degraded_to_rules={snapshot['degraded_to_rules']} turns={snapshot['turns']}")
    print(f"[SMOKE] tokens in/out={snapshot['input_tokens']}/{snapshot['output_tokens']} cost_usd={snapshot['cost_usd']}")
    print("[SMOKE] plan:", json.dumps(snapshot["final_plan"], indent=1, default=str))
    for ev in snapshot["trace"] or []:
        kind = ev.get("event")
        if kind == "planner_turn":
            print(f"  turn {ev['turn']}: text={ev.get('text', '')[:160]!r} tool_calls={[c['name'] for c in ev.get('tool_calls', [])]}")
        elif kind == "tool_result":
            print(f"  tool {ev['tool']}: ok={ev['ok']} blocked_rule={ev.get('blocked_rule')} error={ev.get('error')}")
        elif kind in ("provider_unavailable", "provider_error", "fallback_plan", "message_rejected_fallback_to_template"):
            print(f"  ! {kind}: {ev.get('error') or ev.get('reason') or ev.get('errors')}")
    return 1 if snapshot["degraded_to_rules"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
