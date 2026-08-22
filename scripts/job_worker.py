#!/usr/bin/env python3
"""Worker entrypoint for the durable job queue (Phase 0, ROADMAP next-action 1).

Loops over one or more queues (a queue == a ``job_kind`` value in
``platform.processing_jobs``), claiming jobs with FOR UPDATE SKIP LOCKED,
executing registered handlers (scripts/job_handlers.py), heartbeating long
jobs, retrying failures with exponential backoff, quarantining poison jobs
as dead, and committing completion/failure outbox events atomically.

SIGTERM/SIGINT are graceful: the worker finishes the job in flight, then
exits 0. Hard kills (kill -9, OOM) leave the row 'running' until its lease
expires and another worker's crash-recovery sweep requeues it — at-least-once.

Usage
-----
    # work everything this process has handlers for
    venv/bin/python scripts/job_worker.py

    # work a single queue
    venv/bin/python scripts/job_worker.py --queue demo

    # batch/backfill mode: exit when the queue is empty
    venv/bin/python scripts/job_worker.py --queue demo --drain

    # process at most one job then exit (cron-friendly / tests)
    venv/bin/python scripts/job_worker.py --queue demo --once

    # enqueue N idempotent demo jobs (the helper later fetchers mirror)
    venv/bin/python scripts/job_worker.py --enqueue-demo 5 [--prefix run1]
"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import jobs  # noqa: E402
import job_handlers  # noqa: E402,F401  (registers demo + selftest handlers)


def enqueue_demo(count: int, prefix: str) -> int:
    """Demo enqueuer: insert ``count`` uniquely-keyed demo jobs in one
    transaction. Re-running with the same prefix dedupes to zero new rows —
    the idempotency contract downstream fetchers rely on."""
    created = 0
    with jobs.connect() as conn:
        with conn.transaction():
            for i in range(count):
                res = jobs.enqueue(
                    conn,
                    job_kind="demo",
                    idempotency_key=f"{prefix}-{i}",
                    payload={"n": i},
                )
                created += 1 if res.created else 0
    print(f"enqueued {created} new demo job(s) with prefix {prefix!r} "
          f"({count - created} already existed)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queue", action="append", default=None,
                        help="job_kind to work; repeatable; default = all kinds")
    parser.add_argument("--worker-id", default=None,
                        help="override worker identity (default host:pid:rand)")
    parser.add_argument("--poll-interval", type=float,
                        default=jobs.DEFAULT_POLL_INTERVAL)
    parser.add_argument("--lease-seconds", type=float,
                        default=jobs.DEFAULT_LEASE_SECONDS,
                        help="running-job lease before crash reclaim")
    parser.add_argument("--heartbeat-interval", type=float,
                        default=jobs.DEFAULT_HEARTBEAT_INTERVAL)
    parser.add_argument("--max-jobs", type=int, default=None)
    parser.add_argument("--once", action="store_true",
                        help="process at most one job, then exit")
    parser.add_argument("--drain", action="store_true",
                        help="exit when the queue is empty")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--enqueue-demo", type=int, default=None, metavar="N",
                        help="enqueue N idempotent demo jobs and exit")
    parser.add_argument("--demo-prefix", default=None,
                        help="idempotency-key prefix for --enqueue-demo "
                             "(default: demo-<timestamp>)")
    args = parser.parse_args(argv)

    if args.enqueue_demo is not None:
        prefix = args.demo_prefix or f"demo-{time.strftime('%Y%m%dT%H%M%S')}"
        return enqueue_demo(args.enqueue_demo, prefix)

    worker = jobs.Worker(
        queues=args.queue,
        worker_id=args.worker_id,
        poll_interval=args.poll_interval,
        lease_seconds=args.lease_seconds,
        heartbeat_interval=args.heartbeat_interval,
        max_jobs=1 if args.once else args.max_jobs,
        quiet=args.quiet,
    )
    try:
        if args.drain:
            n = worker.drain()
            print(f"drained {n} job(s); queue empty")
        else:
            worker.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
