"""Built-in job handlers for the durable queue (see scripts/jobs.py).

Currently registers:

* ``demo``            — reference handler used to prove exactly-once logical
                        effect under concurrent workers: it writes an outbox
                        event keyed by the job's idempotency key
                        (``UNIQUE``), so even at-least-once re-execution
                        cannot duplicate the logical side effect.
* ``selftest``        — behaviour-switchable handler for integration tests:
                        ``{"action": "ok"|"fail"|"hard_crash"|"sleep", ...}``.
                        Also bound to the ``selftest.`` *prefix*, so per-test
                        kinds like ``selftest.crash.<uid>`` resolve to it in
                        separate worker subprocesses too.

Later fetchers register their own kinds here (or in their own modules via
``jobs.register_handler``) — importing this module is what makes handlers
visible to a worker process, so keep worker entrypoints importing it.
"""
from __future__ import annotations

import os
import time

import jobs
from jobs import JobContext, emit_outbox

# NSE capture adapter: importing registers the ``nse.fetch`` handler family.
import nse_capture  # noqa: F401

# BSE capture adapter: importing registers the ``bse.fetch`` handler family.
import bse_capture  # noqa: F401

# Normalizer (M2): importing registers the ``normalize.run`` handler family
# (kind ``normalize.run`` + prefix ``normalize.run:``).
import normalize  # noqa: F401

# Identity resolver (M3): importing registers the ``resolve.run`` handler
# family (kind ``resolve.run`` + prefix ``resolve.run:``).
import resolve_identity  # noqa: F401

# Yahoo market data (M1 prices family): importing registers the
# ``yahoo.fetch`` handler family (kind ``yahoo.fetch`` + prefix
# ``yahoo.fetch:<slug>``).
import fetch_prices  # noqa: F401


@jobs.register_handler("demo")
def handle_demo(ctx: JobContext) -> None:
    """Idempotent demo work unit.

    Logical effect = one outbox event ``event_key='demo:<idempotency_key>'``.
    The UNIQUE constraint + ON CONFLICT DO NOTHING make re-execution a no-op,
    which is exactly the contract at-least-once handlers must honour.
    Payload may contain ``{"sleep_sec": float}`` to hold the job open
    (heartbeat / SIGTERM tests).
    """
    sleep_sec = float(ctx.payload.get("sleep_sec") or 0)
    if sleep_sec > 0:
        time.sleep(sleep_sec)
    with jobs.connect(ctx.db_url) as conn:
        outbox_id = emit_outbox(
            conn,
            event_type="demo.executed",
            aggregate_type="demo",
            aggregate_id=ctx.idempotency_key,
            event_key=f"demo:{ctx.idempotency_key}",
            payload={"job_id": ctx.job.job_id, "attempt": ctx.job.attempts},
        )
        conn.commit()
    if outbox_id is None:
        # Already executed for this idempotency key — at-least-once re-run;
        # treat as success without duplicating the effect.
        return


@jobs.register_handler("selftest")
@jobs.register_prefix_handler("selftest.")
def handle_selftest(ctx: JobContext) -> None:
    """Test handler. Actions:
    * ok         — succeed (optionally after sleep_sec)
    * fail       — raise RuntimeError (drives retry/backoff → dead)
    * hard_crash — os._exit(9): simulates a worker killed mid-job; the row
                   stays 'running' until lease expiry requeues it
    * sleep      — just sleep sleep_sec then succeed
    """
    action = ctx.payload.get("action", "ok")
    sleep_sec = float(ctx.payload.get("sleep_sec") or 0)
    if sleep_sec:
        time.sleep(sleep_sec)
    if action == "fail":
        raise RuntimeError(ctx.payload.get("message", "selftest failure"))
    if action == "hard_crash":
        if ctx.job.attempts <= 1:
            os._exit(9)
        # Later attempts fall through to the success path so crash-recovery
        # tests can drive a full kill -> lease-expiry -> retry cycle while
        # the retried attempt completes normally.
    # "ok": record execution via the same dedup-safe outbox marker
    with jobs.connect(ctx.db_url) as conn:
        emit_outbox(
            conn,
            event_type="selftest.executed",
            aggregate_type="selftest",
            aggregate_id=ctx.idempotency_key,
            event_key=f"selftest:{ctx.idempotency_key}",
            payload={"job_id": ctx.job.job_id, "attempt": ctx.job.attempts},
        )
        conn.commit()
