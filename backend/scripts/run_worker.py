"""Run the background worker: recurring ticks (plans, sweeps, degradation, approvals, settlements) + one-off jobs.

    python scripts/run_worker.py            # loop forever
    python scripts/run_worker.py --once     # one cycle (useful in CI / demos)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.session import SessionLocal  # noqa: E402
from app.jobs.worker import Worker  # noqa: E402
from app.observability.logging import configure_logging  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--poll", type=float, default=None)
    args = p.parse_args()
    configure_logging()
    worker = Worker(SessionLocal)
    if args.once:
        db = SessionLocal()
        try:
            worker.enqueue_recurring(db)
            print(worker.run_once(db))
        finally:
            db.close()
        return 0
    worker.run_forever(poll_seconds=args.poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
