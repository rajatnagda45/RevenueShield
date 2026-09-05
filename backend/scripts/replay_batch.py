"""Replay a batch of Razorpay-shaped leak events through the real pipeline and print the scorecard.

Examples:
  python scripts/replay_batch.py --n 300 --days 10 --seed 7 --agent --provider null
  python scripts/replay_batch.py --n 1000 --days 14 --holdout 10 --provider openai --out reports/
  python scripts/replay_batch.py --n 50 --http http://127.0.0.1:8000      # drive a running server over HTTP with real HMAC
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.session import SessionLocal  # noqa: E402
from app.simulation.replay import BatchReplay, ChaosConfig, ReplayConfig  # noqa: E402
from app.simulation.report import render_markdown  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="RevenueShield batch replay harness")
    p.add_argument("--n", type=int, default=200, help="number of leak cases")
    p.add_argument("--days", type=int, default=10, help="virtual days to replay")
    p.add_argument("--tick-hours", type=int, default=12)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--holdout", type=float, default=10.0, help="holdout percent")
    p.add_argument("--agent", action="store_true", help="let the Recovery Agent drive plans (default: rules NBA)")
    p.add_argument("--provider", default="auto", help="auto | anthropic | openai | null")
    p.add_argument("--no-degradation", action="store_true", help="skip the issuer outage episode")
    p.add_argument("--no-chaos", action="store_true", help="disable duplicate webhooks and the LLM outage window")
    p.add_argument("--batch-id", default=None)
    p.add_argument("--http", default=None, help="base URL of a running server; events are POSTed with a real HMAC signature")
    p.add_argument("--out", default=None, help="directory to write <batch>.json and <batch>.md")
    p.add_argument("--init-db", action="store_true", help="create tables if missing (for a throwaway SQLite demo database)")
    args = p.parse_args()

    if args.init_db:
        import app.models  # noqa: F401  (registers all tables)
        from app.db.base import Base
        from app.db.session import engine
        Base.metadata.create_all(bind=engine)
        print(f"[REPLAY] tables ensured on {engine.url.render_as_string(hide_password=True)}")

    chaos = ChaosConfig() if not args.no_chaos else ChaosConfig(duplicate_webhook_rate=0.0, llm_outage_hours=None, malformed_webhooks=0)
    cfg = ReplayConfig(
        n_cases=args.n, days=args.days, tick_hours=args.tick_hours, seed=args.seed, holdout_percent=args.holdout,
        agent_enabled=args.agent, provider=args.provider, degradation_episode=not args.no_degradation, batch_id=args.batch_id,
        http_base_url=args.http, chaos=chaos,
    )
    db = SessionLocal()
    try:
        print(f"[REPLAY] {cfg.n_cases} cases, {cfg.days} days, seed {cfg.seed}, holdout {cfg.holdout_percent}%, agent={'on' if cfg.agent_enabled else 'off'} ({cfg.provider})")
        report = BatchReplay(db, cfg).run()
        db.commit()
    finally:
        db.close()

    md = render_markdown(report)
    print(md)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{report['batch_id']}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        (out / f"{report['batch_id']}.md").write_text(md, encoding="utf-8")
        print(f"[REPLAY] wrote {out / report['batch_id']}.json and .md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
