"""Run the durable scheduler outside the HTTP request lifecycle.

Usage::

    python scripts/run_worker.py --once            # one tick, then exit
    python scripts/run_worker.py --loop            # poll forever (Ctrl+C to stop)
    python scripts/run_worker.py --loop --ticks 5  # five ticks, then exit

The worker is the only component that publishes. The API can trigger a tick through
`/ops/run-due` when an operator wants one now, but a real deployment runs this process.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.publishers.registry import build_registry  # noqa: E402
from app.scheduler.worker import run_due_jobs  # noqa: E402


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Social Media Studio scheduler.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="run a single tick")
    group.add_argument("--loop", action="store_true", help="poll until interrupted")
    parser.add_argument("--ticks", type=int, default=None, help="stop after N ticks (with --loop)")
    args = parser.parse_args()

    settings = get_settings()
    _configure_logging(settings.log_level)
    logger = logging.getLogger("worker")

    registry = build_registry(settings)
    for status in registry.statuses():
        logger.info(
            "publisher %-14s platform=%-9s configured=%-5s enabled=%s",
            status.name,
            status.platform,
            status.configured,
            status.enabled,
        )

    stopping = False

    def _stop(_signum, _frame):  # noqa: ANN001
        nonlocal stopping
        stopping = True
        logger.info("stop requested; finishing the current tick")

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    ticks = 0
    while True:
        db = SessionLocal()
        try:
            report = run_due_jobs(
                db,
                registry,
                settings,
                now=datetime.now(timezone.utc),
            )
        finally:
            db.close()
        ticks += 1
        logger.info(
            "tick %s claimed=%s completed=%s retried=%s failed=%s skipped=%s recovered=%s",
            ticks,
            report.claimed,
            report.completed,
            report.retried,
            report.failed,
            report.skipped,
            report.recovered,
        )

        if args.once:
            return 0
        if stopping or (args.ticks is not None and ticks >= args.ticks):
            return 0
        # Sleep in one-second slices so Ctrl+C is responsive.
        for _ in range(settings.scheduler_poll_seconds):
            if stopping:
                break
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
