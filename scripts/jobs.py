"""Durable job queue + transactional outbox worker pattern (Phase 0, ROADMAP
next-action 1).

Everything downstream (content fetchers, text extraction, clustering) assumes
this exists: jobs live in ``platform.processing_jobs``, side-effect
notifications are committed transactionally into ``platform.outbox_events``.

Guarantees (HLD invariants)
---------------------------
* **At-least-once execution.** A job is claimed with ``FOR UPDATE SKIP
  LOCKED`` so exactly one worker owns it at a time; a worker that dies
  mid-job leaves the row ``running`` until its lease expires, after which
  :func:`requeue_expired_leases` puts it back in the queue and another
  attempt runs. Handlers must therefore be idempotent — they are keyed by
  the job's ``idempotency_key`` (UNIQUE per ``job_kind``).
* **Crash-safe transitions.** Claim (queued/failed → running), complete
  (running → succeeded), fail (running → queued retry or dead quarantine)
  each happen in one short transaction; DB triggers enforce the legal
  transition matrix and immutability of identity/payload columns.
* **No lost lineage / no dual-write loss.** Status change + outbox events
  commit atomically (transactional outbox): if the status update commits,
  the events exist; if it rolls back, neither happened.
* **Poison quarantine.** After ``max_attempts`` failed attempts the job goes
  to ``dead`` (terminal) instead of looping forever; every failure records
  ``last_error`` and schedules the retry with exponential backoff via
  ``available_at``.

Queues
------
There is no separate queue table: a "queue" is a ``job_kind`` value.
``--queue demo`` on the worker filters claims to that kind; no flag means
work all kinds this worker has handlers for.

Handler contract
----------------
Handlers are registered per ``job_kind``::

    from jobs import register_handler

    @register_handler("my_kind")
    def handle(ctx):
        ...  # ctx.payload, ctx.idempotency_key, ctx.db_url ...
        ctx.outbox.append({                      # optional extra events,
            "event_type": "thing.seen",          # committed with completion
            "aggregate_type": "record",
            "aggregate_id": "123",
            "payload": {"ok": True},
        })

Handlers raise on failure (retry with backoff) and must tolerate being run
more than once per idempotency key (at-least-once). See
``scripts/job_handlers.py`` for the built-in demo/self-test handlers and
``scripts/job_worker.py`` for the CLI entrypoint.
"""
from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_LEASE_SECONDS = 300.0        # running-job lease before crash reclaim
DEFAULT_POLL_INTERVAL = 2.0          # idle poll when queue is empty
DEFAULT_HEARTBEAT_INTERVAL = 15.0    # locked_at refresh while executing
DEFAULT_BACKOFF_BASE = 5.0           # seconds; doubled per attempt
DEFAULT_BACKOFF_CAP = 3600.0         # never wait longer than 1h for a retry


class JobsError(Exception):
    """Base error for the job/outbox library."""


class NoHandlerError(JobsError):
    """No handler registered for the claimed job_kind."""


# --------------------------------------------------------------------- #
# handler registry
# --------------------------------------------------------------------- #

HandlerFn = Callable[["JobContext"], Any]
_HANDLERS: dict[str, HandlerFn] = {}


def register_handler(job_kind: str, fn: Optional[HandlerFn] = None) -> Callable:
    """Register ``fn`` as the handler for ``job_kind``. Usable as a decorator
    or called directly."""
    if fn is not None:
        _HANDLERS[job_kind] = fn
        return fn

    def deco(f: HandlerFn) -> HandlerFn:
        _HANDLERS[job_kind] = f
        return f

    return deco


def get_handler(job_kind: str) -> HandlerFn:
    try:
        return _HANDLERS[job_kind]
    except KeyError:
        raise NoHandlerError(
            f"no handler registered for job_kind {job_kind!r}; "
            f"known: {sorted(_HANDLERS)}"
        ) from None


# --------------------------------------------------------------------- #
# connection helpers
# --------------------------------------------------------------------- #

def db_url_from_env(env_path: Path | str = ROOT / ".env") -> str:
    """Read DATABASE_URL from the repo .env (never log or print it)."""
    path = Path(env_path)
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "DATABASE_URL" and v.strip():
                    return v.strip()
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    raise JobsError("DATABASE_URL not found in .env or environment")


def connect(url: Optional[str] = None) -> psycopg.Connection:
    """Open a connection (autocommit off; callers own transactions)."""
    return psycopg.connect(url or db_url_from_env())


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------- #
# enqueue
# --------------------------------------------------------------------- #

@dataclass(frozen=True)
class EnqueueResult:
    job_id: int
    created: bool   # False if an existing (job_kind, idempotency_key) matched


def enqueue(
    conn: psycopg.Connection,
    *,
    job_kind: str,
    idempotency_key: str,
    payload: dict[str, Any],
    priority: int = 100,
    max_attempts: int = 5,
    available_at: Optional[datetime] = None,
) -> EnqueueResult:
    """Insert a job; deduplicates on ``(job_kind, idempotency_key)`` so
    at-least-once producers can re-enqueue freely. Does NOT commit — pass a
    connection inside your transaction (outbox pattern: enqueue alongside the
    business writes that justify the job)."""
    row = conn.execute(
        """
        INSERT INTO platform.processing_jobs
            (job_kind, idempotency_key, payload, priority, max_attempts, available_at)
        VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()))
        ON CONFLICT (job_kind, idempotency_key) DO NOTHING
        RETURNING job_id
        """,
        (job_kind, idempotency_key, Jsonb(payload), priority, max_attempts, available_at),
    ).fetchone()
    if row is not None:
        return EnqueueResult(row[0], True)
    existing = conn.execute(
        "SELECT job_id FROM platform.processing_jobs "
        "WHERE job_kind = %s AND idempotency_key = %s",
        (job_kind, idempotency_key),
    ).fetchone()
    return EnqueueResult(existing[0], False)


# --------------------------------------------------------------------- #
# claim / heartbeat / complete / fail / lease recovery
# --------------------------------------------------------------------- #

@dataclass(frozen=True)
class ClaimedJob:
    job_id: int
    job_kind: str
    idempotency_key: str
    payload: dict[str, Any]
    priority: int
    attempts: int
    max_attempts: int


def claim_next(
    conn: psycopg.Connection,
    worker_id: str,
    queues: Optional[list[str]] = None,
) -> Optional[ClaimedJob]:
    """Claim the next runnable job with ``FOR UPDATE SKIP LOCKED`` so
    concurrent workers never get the same row. One short transaction: lock,
    mark running (+1 attempt), commit, release. Retriable ``failed`` rows are
    claimable too (transition guard allows failed → running)."""
    with conn.transaction():
        row = conn.execute(
            """
            WITH picked AS (
                SELECT job_id
                FROM platform.processing_jobs
                WHERE status IN ('queued', 'failed')
                  AND available_at <= now()
                  AND (%(queues)s::text[] IS NULL OR job_kind = ANY(%(queues)s::text[]))
                ORDER BY priority, available_at, job_id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE platform.processing_jobs j
            SET status = 'running',
                locked_at = now(),
                locked_by = %(worker)s,
                attempts = j.attempts + 1
            FROM picked
            WHERE j.job_id = picked.job_id
            RETURNING j.job_id, j.job_kind, j.idempotency_key, j.payload,
                      j.priority, j.attempts, j.max_attempts
            """,
            {"worker": worker_id, "queues": queues},
        ).fetchone()
    if row is None:
        return None
    return ClaimedJob(row[0], row[1], row[2], row[3], row[4], row[5], row[6])


def heartbeat(conn: psycopg.Connection, job_id: int, worker_id: str) -> bool:
    """Refresh ``locked_at`` for a job this worker still owns. Returns False
    if the lease was lost (should not happen while the transition guards hold,
    but callers re-check ownership anyway)."""
    with conn.transaction():
        row = conn.execute(
            "UPDATE platform.processing_jobs SET locked_at = now() "
            "WHERE job_id = %s AND status = 'running' AND locked_by = %s "
            "RETURNING job_id",
            (job_id, worker_id),
        ).fetchone()
    return row is not None


def emit_outbox(
    conn: psycopg.Connection,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, Any],
    event_key: Optional[str] = None,
) -> Optional[int]:
    """Insert an outbox event (not committed — joins the caller's tx).
    Deterministic default ``event_key`` keeps retries from duplicating."""
    key = event_key or f"{event_type}:{aggregate_type}:{aggregate_id}"
    row = conn.execute(
        """
        INSERT INTO platform.outbox_events
            (event_key, event_type, aggregate_type, aggregate_id, payload)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (event_key) DO NOTHING
        RETURNING outbox_id
        """,
        (key, event_type, aggregate_type, aggregate_id, Jsonb(payload)),
    ).fetchone()
    return row[0] if row else None


def complete_job(
    conn: psycopg.Connection,
    job: ClaimedJob,
    worker_id: str,
    *,
    result: Optional[dict[str, Any]] = None,
    outbox_events: Optional[list[dict[str, Any]]] = None,
) -> None:
    """Mark succeeded and emit the completion outbox event(s) atomically.
    ``outbox_events`` entries may carry their own ``event_key``/``payload``;
    missing keys get a deterministic default."""
    with conn.transaction():
        updated = conn.execute(
            "UPDATE platform.processing_jobs "
            "SET status = 'succeeded', finished_at = now(), locked_at = NULL, "
            "    locked_by = NULL "
            "WHERE job_id = %s AND status = 'running' AND locked_by = %s "
            "RETURNING job_id",
            (job.job_id, worker_id),
        ).fetchone()
        if updated is None:
            raise JobsError(
                f"cannot complete job {job.job_id}: not owned by {worker_id} "
                "(lease lost?)"
            )
        _insert_outbox_events(
            conn, job, base_event="job.completed", extra=outbox_events,
            result_payload=result,
        )


def fail_job(
    conn: psycopg.Connection,
    job: ClaimedJob,
    worker_id: str,
    error: BaseException | str,
    *,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    backoff_cap: float = DEFAULT_BACKOFF_CAP,
    outbox_events: Optional[list[dict[str, Any]]] = None,
) -> str:
    """Record the failure and either schedule a retry (running → queued with
    exponential backoff) or quarantine the poison job (running → dead).
    Returns ``'retry'`` or ``'dead'``. Outbox failure/dead events commit in
    the same transaction as the status change."""
    message = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    dead = job.attempts >= job.max_attempts
    with conn.transaction():
        if dead:
            updated = conn.execute(
                "UPDATE platform.processing_jobs "
                "SET status = 'dead', finished_at = now(), locked_at = NULL, "
                "    locked_by = NULL, last_error = %s "
                "WHERE job_id = %s AND status = 'running' AND locked_by = %s "
                "RETURNING job_id",
                (message[:4000], job.job_id, worker_id),
            ).fetchone()
            outcome = "dead"
        else:
            delay_s = min(backoff_base * (2 ** max(0, job.attempts - 1)), backoff_cap)
            updated = conn.execute(
                "UPDATE platform.processing_jobs "
                "SET status = 'queued', available_at = now() + make_interval(secs => %s), "
                "    locked_at = NULL, locked_by = NULL, last_error = %s "
                "WHERE job_id = %s AND status = 'running' AND locked_by = %s "
                "RETURNING job_id",
                (delay_s, message[:4000], job.job_id, worker_id),
            ).fetchone()
            outcome = "retry"
        if updated is None:
            raise JobsError(
                f"cannot fail job {job.job_id}: not owned by {worker_id}"
            )
        _insert_outbox_events(
            conn, job, base_event="job.failed", extra=outbox_events,
            result_payload={"error": message[:2000], "outcome": outcome},
        )
        if dead:
            emit_outbox(
                conn,
                event_type="job.dead",
                aggregate_type="processing_job",
                aggregate_id=str(job.job_id),
                event_key=f"job.dead:{job.job_id}",
                payload={"job_kind": job.job_kind,
                         "idempotency_key": job.idempotency_key,
                         "attempts": job.attempts,
                         "error": message[:2000]},
            )
    return outcome


def requeue_expired_leases(
    conn: psycopg.Connection,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> list[int]:
    """Crash recovery: put ``running`` jobs whose lease (locked_at) expired
    back into the queue so another worker can pick them up. The attempt
    already counted, so repeated crashes eventually quarantine the job as
    dead. Returns reclaimed job_ids."""
    with conn.transaction():
        rows = conn.execute(
            "UPDATE platform.processing_jobs "
            "SET status = 'queued', locked_at = NULL, locked_by = NULL, "
            "    last_error = COALESCE(last_error || ' | ', '') || "
            "               'lease expired after ' || %s || 's (worker crash?)', "
            "    available_at = now() "
            "WHERE status = 'running' AND locked_at < now() - make_interval(secs => %s) "
            "RETURNING job_id",
            (int(lease_seconds), lease_seconds),
        ).fetchall()
    return [r[0] for r in rows]


def _insert_outbox_events(
    conn: psycopg.Connection,
    job: ClaimedJob,
    *,
    base_event: str,
    extra: Optional[list[dict[str, Any]]],
    result_payload: Optional[dict[str, Any]],
) -> None:
    payload: dict[str, Any] = {
        "job_kind": job.job_kind,
        "idempotency_key": job.idempotency_key,
        "attempt": job.attempts,
        **({"result": result_payload} if result_payload is not None else {}),
    }
    emit_outbox(
        conn,
        event_type=base_event,
        aggregate_type="processing_job",
        aggregate_id=str(job.job_id),
        event_key=f"{base_event}:{job.job_id}",
        payload=payload,
    )
    for spec in extra or []:
        emit_outbox(
            conn,
            event_type=spec.get("event_type") or base_event + ".extra",
            aggregate_type=spec.get("aggregate_type") or "processing_job",
            aggregate_id=spec.get("aggregate_id") or str(job.job_id),
            payload=spec.get("payload") or {},
            event_key=spec.get("event_key"),
        )


# --------------------------------------------------------------------- #
# worker runtime
# --------------------------------------------------------------------- #

@dataclass
class JobContext:
    """What a handler sees."""
    job: ClaimedJob
    worker_id: str
    db_url: str
    outbox: list[dict[str, Any]] = field(default_factory=list)

    @property
    def payload(self) -> dict[str, Any]:
        return self.job.payload

    @property
    def idempotency_key(self) -> str:
        return self.job.idempotency_key


class _Heartbeat(threading.Thread):
    def __init__(self, db_url: str, job_id: int, worker_id: str, interval: float):
        super().__init__(daemon=True, name=f"hb-{job_id}")
        self.db_url = db_url
        self.job_id = job_id
        self.worker_id = worker_id
        self.interval = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                with connect(self.db_url) as hc:
                    heartbeat(hc, self.job_id, self.worker_id)
            except Exception:
                pass  # transient DB hiccup; lease is long relative to interval

    def stop(self) -> None:
        self._stop_event.set()


class Worker:
    """Polls the given queues, executing handlers with heartbeats, retries
    with backoff, and graceful SIGTERM/SIGINT shutdown (finishes the current
    job, then exits)."""

    def __init__(
        self,
        queues: Optional[list[str]] = None,
        *,
        worker_id: Optional[str] = None,
        db_url: Optional[str] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        backoff_cap: float = DEFAULT_BACKOFF_CAP,
        max_jobs: Optional[int] = None,
        quiet: bool = False,
    ):
        self.queues = list(queues) if queues else None
        self.worker_id = worker_id or new_worker_id()
        self.db_url = db_url or db_url_from_env()
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.heartbeat_interval = heartbeat_interval
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_jobs = max_jobs
        self.quiet = quiet
        self.stop_requested = False
        self.processed = 0

    # -- logging ------------------------------------------------------- #

    def log(self, msg: str) -> None:
        if not self.quiet:
            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{stamp}] [{self.worker_id}] {msg}", flush=True)

    # -- signals -------------------------------------------------------- #

    def request_stop(self, signum: int = 0, frame: Any = None) -> None:
        self.stop_requested = True

    def install_signal_handlers(self) -> None:
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.request_stop)

    # -- single iteration ------------------------------------------------ #

    def process_one(self) -> bool:
        """Claim+execute at most one job. Returns True if a job was run."""
        with connect(self.db_url) as conn:
            requeue_expired_leases(conn, self.lease_seconds)
            job = claim_next(conn, self.worker_id, self.queues)
        if job is None:
            return False

        hb = None
        try:
            handler = get_handler(job.job_kind)
        except NoHandlerError as e:
            with connect(self.db_url) as conn:
                fail_job(conn, job, self.worker_id, e,
                         backoff_base=self.backoff_base, backoff_cap=self.backoff_cap)
            self.log(f"job {job.job_id} ({job.job_kind}) failed: no handler")
            return True

        self.log(f"claim job {job.job_id} kind={job.job_kind} "
                 f"key={job.idempotency_key} attempt={job.attempts}")
        if job.attempts > 1 or self.heartbeat_interval > 0:
            hb = _Heartbeat(self.db_url, job.job_id, self.worker_id,
                            self.heartbeat_interval)
            hb.start()
        ctx = JobContext(job=job, worker_id=self.worker_id, db_url=self.db_url)
        try:
            handler(ctx)
        except Exception as e:  # noqa: BLE001 — any failure is a job failure
            with connect(self.db_url) as conn:
                outcome = fail_job(conn, job, self.worker_id, e,
                                   backoff_base=self.backoff_base,
                                   backoff_cap=self.backoff_cap,
                                   outbox_events=ctx.outbox)
            self.log(f"job {job.job_id} failed ({outcome}): "
                     f"{type(e).__name__}: {e}")
            return True
        finally:
            if hb is not None:
                hb.stop()
                hb.join(timeout=self.heartbeat_interval + 5)

        with connect(self.db_url) as conn:
            complete_job(conn, job, self.worker_id,
                         outbox_events=ctx.outbox)
        self.log(f"job {job.job_id} succeeded")
        return True

    # -- main loops ------------------------------------------------------- #

    def run_forever(self) -> None:
        self.install_signal_handlers()
        self.log(f"starting; queues={self.queues or 'ALL'}")
        while not self.stop_requested:
            try:
                ran = self.process_one()
            except psycopg.Error as e:
                self.log(f"db error while processing: {e}")
                time.sleep(self.poll_interval)
                continue
            if ran:
                self.processed += 1
                if self.max_jobs is not None and self.processed >= self.max_jobs:
                    self.log("max_jobs reached; stopping")
                    return
            else:
                # sleep in small slices so SIGTERM lands promptly
                deadline = time.monotonic() + self.poll_interval
                while not self.stop_requested and time.monotonic() < deadline:
                    time.sleep(min(0.2, self.poll_interval))
        self.log("graceful shutdown complete")

    def drain(self, idle_rounds: int = 1) -> int:
        """Run until the queue is empty (``idle_rounds`` consecutive empty
        polls); used by tests and batch backfills. Returns jobs processed."""
        empty = 0
        while empty < idle_rounds and not self.stop_requested:
            if self.process_one():
                self.processed += 1
                empty = 0
                if self.max_jobs is not None and self.processed >= self.max_jobs:
                    break
            else:
                empty += 1
        return self.processed
