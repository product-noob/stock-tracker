"""Integration tests for the durable job queue + outbox worker pattern
(scripts/jobs.py) — Phase 0, ROADMAP next-action 1.

Run with:
    venv/bin/python -m unittest tests.test_jobs -v

These tests exercise the REAL ``platform.processing_jobs`` and
``platform.outbox_events`` tables in the live ``stocktracker_db`` (start it
with ``docker compose up -d``). Both tables are append-only by design (DB
triggers forbid DELETE), so tests cannot clean up after themselves: every
test namespaces its rows under unique job_kind prefixes / idempotency keys
(UUIDs) so runs never collide, and leaves its rows behind as audit evidence.
They are skipped automatically if DATABASE_URL is missing or the DB is down.

Covered:
* enqueue idempotency-key dedup (same key twice -> one row)
* claim exclusivity + exactly-once logical effect under concurrent workers
* hard-crash mid-job (subprocess os._exit) leaving a 'running' attempt that
  lease-expiry recovery requeues and a retry completes (attempts=2)
* failure retry with exponential backoff, then poison-job quarantine to dead
* transactional outbox emission on success/failure/dead + handler-appended
  events committing in the same transaction as completion
* graceful SIGTERM: worker finishes the in-flight job, exits 0
"""
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import jobs  # noqa: E402
import job_handlers  # noqa: E402,F401  (registers demo/selftest handlers)

WORKER = [sys.executable, str(ROOT / "scripts" / "job_worker.py")]


def _database_url() -> str | None:
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "DATABASE_URL" and v.strip():
                    return v.strip()
    return os.environ.get("DATABASE_URL")


def _db_available(url: str) -> bool:
    try:
        with jobs.connect(url):
            return True
    except Exception:
        return False


_DB_URL = _database_url()
_DB_UP = bool(_DB_URL) and _db_available(_DB_URL)


def _uid() -> str:
    return uuid.uuid4().hex[:12]


@unittest.skipUnless(_DB_UP, "live stocktracker_db not reachable")
class JobsIntegrationTestCase(unittest.TestCase):
    """Base: live-DB helpers."""

    def url(self) -> str:
        assert _DB_URL is not None
        return _DB_URL

    def q1(self, sql: str, params=None):
        with jobs.connect(self.url()) as conn:
            return conn.execute(sql, params).fetchone()

    def qa(self, sql: str, params=None):
        with jobs.connect(self.url()) as conn:
            return conn.execute(sql, params).fetchall()

    def enqueue(self, kind: str, key: str, payload: dict, **kw) -> int:
        with jobs.connect(self.url()) as conn:
            res = jobs.enqueue(conn, job_kind=kind, idempotency_key=key,
                               payload=payload, **kw)
            conn.commit()
        self.assertTrue(res.created)
        return res.job_id

    def backdate_locked_at(self, job_id: int, seconds_ago: float) -> None:
        with jobs.connect(self.url()) as conn:
            with conn.transaction():
                conn.execute(
                    "UPDATE platform.processing_jobs "
                    "SET locked_at = now() - make_interval(secs => %s) "
                    "WHERE job_id = %s",
                    (seconds_ago, job_id),
                )

    def backdate_available_at(self, job_id: int, seconds_ago: float) -> None:
        with jobs.connect(self.url()) as conn:
            with conn.transaction():
                conn.execute(
                    "UPDATE platform.processing_jobs "
                    "SET available_at = now() - make_interval(secs => %s) "
                    "WHERE job_id = %s",
                    (seconds_ago, job_id),
                )

    def outbox_count(self, event_key_prefix: str) -> int:
        row = self.q1(
            "SELECT count(*) FROM platform.outbox_events "
            "WHERE event_key LIKE %s",
            (event_key_prefix + "%",),
        )
        return int(row[0])

    def claim(self, kind: str, worker_id: str = "test-worker"):
        with jobs.connect(self.url()) as conn:
            claimed = jobs.claim_next(conn, worker_id, [kind])
            conn.commit()
        return claimed

    def register_selftest_alias(self, kind: str) -> None:
        """Map a per-test kind onto the behaviour-switchable selftest handler."""
        jobs.register_handler(kind, jobs.get_handler("selftest"))


class TestEnqueueDedup(JobsIntegrationTestCase):
    def test_idempotency_key_dedup(self):
        kind = f"test_enqueue_{_uid()}"
        key = f"{kind}-key"
        with jobs.connect(self.url()) as conn:
            first = jobs.enqueue(conn, job_kind=kind, idempotency_key=key,
                                 payload={"a": 1})
            second = jobs.enqueue(conn, job_kind=kind, idempotency_key=key,
                                  payload={"a": 999})  # payload ignored on dup
            conn.commit()
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.job_id, second.job_id)
        row = self.q1(
            "SELECT payload FROM platform.processing_jobs WHERE job_id = %s",
            (first.job_id,),
        )
        self.assertEqual(row[0], {"a": 1})  # original payload immutable


class TestClaimExclusivity(JobsIntegrationTestCase):
    def test_concurrent_workers_execute_each_job_exactly_once(self):
        kind = f"test_concurrent_{_uid()}"
        n_jobs, n_workers = 12, 6
        keys = [f"{kind}-{i}" for i in range(n_jobs)]
        for key in keys:
            self.enqueue(kind, key, {})
        self.register_selftest_alias(kind)

        # In-process execution counter shared across simulated workers.
        executions: dict[str, int] = {}
        lock = threading.Lock()
        selftest = jobs.get_handler("selftest")

        def counting_handler(ctx):
            with lock:
                executions[ctx.idempotency_key] = (
                    executions.get(ctx.idempotency_key, 0) + 1)
            selftest(ctx)

        jobs.register_handler(kind, counting_handler)

        errors: list[Exception] = []

        def work():
            try:
                w = jobs.Worker(queues=[kind], db_url=self.url(),
                                poll_interval=0.05, heartbeat_interval=5.0,
                                lease_seconds=300.0, quiet=True)
                w.drain(idle_rounds=2)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=work) for _ in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        self.assertFalse(errors, f"worker threads raised: {errors}")

        statuses = dict(self.qa(
            "SELECT status, count(*) FROM platform.processing_jobs "
            "WHERE job_kind = %s GROUP BY 1", (kind,)))
        self.assertEqual(statuses.get("succeeded"), n_jobs,
                         f"statuses={statuses}")
        self.assertNotIn("running", statuses)
        self.assertNotIn("queued", statuses)
        # claim exclusivity (SKIP LOCKED): no job executed more than once
        over = {k: v for k, v in executions.items() if v != 1}
        self.assertEqual(len(executions), n_jobs, f"executions={executions}")
        self.assertFalse(over, f"jobs executed more than once: {over}")
        # exactly-once logical effect visible in the outbox too
        self.assertEqual(self.outbox_count(f"selftest:{kind}-"), n_jobs)


class TestCrashRecovery(JobsIntegrationTestCase):
    def test_hard_crash_midjob_then_retry_completes(self):
        # Unique kind under the ``selftest.`` prefix: the worker SUBPROCESS
        # resolves it via the prefix handler registered in
        # scripts/job_handlers.py, and the --queue filter guarantees this
        # worker only ever sees *this run's* job (stale hard_crash rows from
        # older runs can never be reclaimed into this drain).
        kind = f"selftest.crash.{_uid()}"
        key = f"{kind}-crashy"
        job_id = self.enqueue(kind, key, {"action": "hard_crash"})

        # Real worker subprocess that claims the job and os._exit(9)s mid-job.
        proc = subprocess.run(WORKER + ["--queue", kind, "--drain", "--quiet"],
                              capture_output=True, text=True, timeout=120,
                              env=os.environ.copy())
        self.assertEqual(proc.returncode, 9,
                         f"worker must die via os._exit(9); stderr="
                         f"{proc.stderr[-2000:]}")
        row = self.q1(
            "SELECT status, attempts, locked_by IS NOT NULL "
            "FROM platform.processing_jobs WHERE job_id = %s", (job_id,))
        self.assertEqual((row[0], row[1], row[2]), ("running", 1, True),
                         "crashed worker must leave the job 'running'")

        # Lease-expiry recovery requeues it; the crashed attempt already counted.
        self.backdate_locked_at(job_id, seconds_ago=400)
        with jobs.connect(self.url()) as conn:
            reclaimed = jobs.requeue_expired_leases(conn, lease_seconds=300)
            conn.commit()
        self.assertIn(job_id, reclaimed)
        row = self.q1("SELECT status, attempts FROM platform.processing_jobs "
                      "WHERE job_id = %s", (job_id,))
        self.assertEqual((row[0], row[1]), ("queued", 1))

        # A healthy retry now succeeds.
        proc = subprocess.run(WORKER + ["--queue", kind, "--drain", "--quiet"],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"retry worker must exit cleanly; stderr="
                         f"{proc.stderr[-2000:]}")
        row = self.q1("SELECT status, attempts FROM platform.processing_jobs "
                      "WHERE job_id = %s", (job_id,))
        self.assertEqual((row[0], row[1]), ("succeeded", 2))
        # at-least-once: handler ran twice; the dedup-safe logical effect is
        # recorded exactly once
        self.assertEqual(self.outbox_count(f"selftest:{key}"), 1)
        self.assertGreaterEqual(self.outbox_count(f"job.completed:{job_id}"), 1)


class TestRetryBackoffAndPoisonQuarantine(JobsIntegrationTestCase):
    def test_failure_backoff_then_dead(self):
        kind = f"test_poison_{_uid()}"
        key = f"{kind}-poison"
        job_id = self.enqueue(kind, key, {"action": "fail"}, max_attempts=2)

        # Attempt 1: handler raises -> retry scheduled with exponential backoff.
        claimed = self.claim(kind, "t")
        self.assertIsNotNone(claimed)
        outcome = None
        try:
            jobs.get_handler("selftest")(
                jobs.JobContext(job=claimed, worker_id="t", db_url=self.url()))
        except Exception as e:
            with jobs.connect(self.url()) as conn:
                outcome = jobs.fail_job(conn, claimed, "t", e)
        self.assertEqual(outcome, "retry")
        row = self.q1(
            "SELECT status, attempts, last_error, "
            "       available_at > now() AS backed_off "
            "FROM platform.processing_jobs WHERE job_id = %s", (job_id,))
        self.assertEqual(row[0], "queued")
        self.assertEqual(row[1], 1)
        self.assertIn("RuntimeError", row[2])
        self.assertTrue(row[3], "retry must be scheduled in the future")
        self.assertEqual(self.outbox_count(f"job.failed:{job_id}"), 1)

        # Attempt 2: fail again with attempts == max_attempts -> dead (quarantine).
        self.backdate_available_at(job_id, seconds_ago=1)
        claimed2 = self.claim(kind, "t")
        self.assertIsNotNone(claimed2)
        self.assertEqual(claimed2.attempts, 2)
        with jobs.connect(self.url()) as conn:
            outcome = jobs.fail_job(conn, claimed2, "t", RuntimeError("still bad"))
        self.assertEqual(outcome, "dead")
        row = self.q1(
            "SELECT status, finished_at IS NOT NULL, locked_at IS NULL "
            "FROM platform.processing_jobs WHERE job_id = %s", (job_id,))
        self.assertEqual(row, ("dead", True, True))
        # outbox: one failed event per attempt + one terminal dead event
        self.assertEqual(self.outbox_count(f"job.failed:{job_id}"), 2)
        self.assertEqual(self.outbox_count(f"job.dead:{job_id}"), 1)
        # terminal rows are immutable
        with self.assertRaises(Exception):
            with jobs.connect(self.url()) as conn:
                with conn.transaction():
                    conn.execute(
                        "UPDATE platform.processing_jobs SET status='queued' "
                        "WHERE job_id = %s", (job_id,))


class TestOutboxEmission(JobsIntegrationTestCase):
    def test_success_emits_outbox_event_with_payload(self):
        kind = f"test_outbox_{_uid()}"
        key = f"{kind}-ok"
        job_id = self.enqueue(kind, key, {"action": "ok"})
        self.register_selftest_alias(kind)
        w = jobs.Worker(queues=[kind], db_url=self.url(), quiet=True)
        w.drain()
        row = self.q1(
            "SELECT event_type, aggregate_type, aggregate_id, payload, status "
            "FROM platform.outbox_events WHERE event_key = %s",
            (f"job.completed:{job_id}",))
        self.assertIsNotNone(row, "completion must emit an outbox event")
        self.assertEqual(row[0], "job.completed")
        self.assertEqual((row[1], row[2]), ("processing_job", str(job_id)))
        self.assertEqual(row[4], "pending")
        self.assertEqual(row[3]["idempotency_key"], key)
        self.assertEqual(row[3]["attempt"], 1)
        # handler's own effect event exists too
        self.assertIsNotNone(self.q1(
            "SELECT 1 FROM platform.outbox_events WHERE event_key = %s",
            (f"selftest:{key}",)))

    def test_handler_appended_outbox_events_commit_with_completion(self):
        kind = f"test_outbox_extra_{_uid()}"
        key = f"{kind}-extra"
        job_id = self.enqueue(kind, key, {})

        extra_marker = f"{kind}.extra"

        @jobs.register_handler(kind)
        def handler(ctx):
            ctx.outbox.append({
                "event_type": "thing.seen",
                "aggregate_type": "record",
                "aggregate_id": "123",
                "payload": {"ok": True},
                "event_key": extra_marker,
            })

        w = jobs.Worker(queues=[kind], db_url=self.url(), quiet=True)
        w.drain()
        row = self.q1(
            "SELECT event_type, aggregate_type, aggregate_id, payload "
            "FROM platform.outbox_events WHERE event_key = %s",
            (extra_marker,))
        self.assertIsNotNone(row,
                             "handler outbox event must commit atomically")
        self.assertEqual(row[0], "thing.seen")
        self.assertEqual((row[1], row[2]), ("record", "123"))
        self.assertEqual(row[3], {"ok": True})
        self.assertEqual(
            self.q1("SELECT status FROM platform.processing_jobs "
                    "WHERE job_id = %s", (job_id,))[0],
            "succeeded")


class TestGracefulShutdown(JobsIntegrationTestCase):
    def test_sigterm_finishes_in_flight_job_and_exits_zero(self):
        # Unique kind under the ``selftest.`` prefix so this worker's queue
        # filter can never pick up a stale hard_crash row from older runs.
        kind = f"selftest.sigterm.{_uid()}"
        key = f"{kind}-sig"
        job_id = self.enqueue(kind, key,
                              {"action": "sleep", "sleep_sec": 4})

        proc = subprocess.Popen(
            WORKER + ["--queue", kind, "--quiet", "--poll-interval", "0.2"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            row = None
            deadline = time.time() + 30
            while time.time() < deadline:
                row = self.q1(
                    "SELECT status FROM platform.processing_jobs "
                    "WHERE job_id = %s", (job_id,))
                if row and row[0] == "running":
                    break
                time.sleep(0.1)
            self.assertEqual(row[0], "running", "worker should have claimed")
            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=60)
            self.assertEqual(rc, 0, "graceful SIGTERM exit must be 0")
            row = self.q1(
                "SELECT status FROM platform.processing_jobs "
                "WHERE job_id = %s", (job_id,))
            self.assertEqual(row[0], "succeeded",
                             "in-flight job must finish before exit")
            self.assertEqual(self.outbox_count(f"selftest:{key}"), 1)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
