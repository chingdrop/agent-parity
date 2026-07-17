"""Dispatch a real Celery chord and wait for it to complete.

This proves the worker/broker/result-backend actually work end to end —
``tests/test_tasks.py`` runs the same tasks with ``task_always_eager``, which
never touches a real broker, never involves a second process picking work
up, and can't catch a misconfigured ``CELERY_BROKER_URL`` or a worker that
isn't actually running. Only meaningful inside the Docker Compose stack,
where a real Redis and real worker/beat containers exist; used by
``docker/smoke_test.sh``, not part of the demo or production flow.

Usage: CELERY_BROKER_URL=... CELERY_RESULT_BACKEND=... AGENT_PARITY_DB_URL=...
       uv run python docker/smoke_check_celery.py [--timeout SECONDS]
"""

from __future__ import annotations

import argparse
import sys
import time

from agent_parity.scheduling.db import CorrelationRun, get_engine, init_db, session_factory
from agent_parity.scheduling.tasks import dispatch_all_clients

TERMINAL_STATUSES = ("complete", "partial")


def _terminal_run_count(session_factory_) -> int:
    with session_factory_() as session:
        return session.query(CorrelationRun).filter(CorrelationRun.status.in_(TERMINAL_STATUSES)).count()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=60, help="Seconds to wait for the chord to complete.")
    args = parser.parse_args()

    engine = get_engine()
    init_db(engine)
    Session = session_factory(engine)
    baseline = _terminal_run_count(Session)

    # .delay() + .get() round-trips dispatch_all_clients itself through the
    # real broker/worker — a plain function call would prove nothing.
    result = dispatch_all_clients.delay(force=True)
    dispatched = result.get(timeout=args.timeout)
    if not dispatched:
        print("FAIL: dispatch_all_clients ran but dispatched no clients", file=sys.stderr)
        return 1
    print(f"dispatch_all_clients ran via the real worker; dispatched: {dispatched}")

    # dispatch_all_clients returning only proves *it* ran — the group+chord
    # it registered fires asynchronously, so the actual fan-out/fan-in has
    # to be polled for separately.
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        count = _terminal_run_count(Session)
        if count > baseline:
            print(
                f"chord completed: {count - baseline} new run(s) reached a terminal "
                f"status via the real broker/worker"
            )
            return 0
        time.sleep(2)

    print(
        f"FAIL: no CorrelationRun reached a terminal status within {args.timeout}s of "
        f"dispatch — the chord fan-out/fan-in didn't complete via the real worker",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
