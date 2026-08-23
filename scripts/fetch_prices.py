"""Yahoo market-data adapter → DB (Phase 0, ROADMAP action 4; TODO 6).

Implements ``docs/modules/yahoo-market-data/OVERVIEW.md`` (+ source-mechanics /
data-contract): daily OHLCV for every whitelisted listing via **yfinance**
(``.NS`` symbols per the ROADMAP decision log) into
``market.price_series`` / ``market.price_bars``, one run per
``ingestion.source_partitions`` row (``yfinance, listing:{listing_id}:daily``,
provisioned by ``seed_core.py``).

Lineage written per run (exact bytes first):

1. open/close an ``ingestion.fetch_runs`` row whose idempotency key is
   deterministic per partition + IST calendar day — re-running the same
   partition/day finds the completed run and returns early (no network), which
   makes re-runs idempotent at the run level;
2. fetch via yfinance ``Ticker.history(period=..., interval="1d",
   auto_adjust=True)`` — sequential per R-YF-6, with modest spacing;
3. persist an exact, deterministic serialization of the provider frame (CSV of
   precisely what the library returned) to the immutable object store and
   register it in ``documents.content_objects`` + ``ingestion.raw_artifacts``
   BEFORE any bar insert (HLD invariant 1, R-YF-4). Implementation note: the
   spec's TBD on raw transport is resolved as "serialize the returned frame"
   because intercepting yfinance's underlying HTTP bytes is version-fragile;
   the retained CSV plus the pinned library version recorded in the run's
   ``config`` is the audit trail against silent provider revisions;
4. validate bars (R-YF-3: OHLC sanity, positive prices, volume ≥ 0,
   weekday trade dates not in the future); violations fail the partition run
   loudly — nothing partial is stored. Zero-volume sessions are tolerated but
   reported (semantics unverified — YF evidence-log 2026-08-22);
5. insert bars append-only (R-YF-2/7/8): idempotency key
   ``{series_id}:{trade_date}:{observation_hash}`` where the hash covers the
   OHLCV values + trade date only, so re-fetching unchanged bars is a no-op
   while a changed value appends a NEW observation row that
   ``market.current_price_bars`` flips to (prior references keep their bar IDs);
6. only after artifacts + bars commit, upsert ``ingestion.ingestion_cursors``
   (high watermark = last session close IST).

Per-symbol failures are captured and do NOT abort the overall multi-symbol run
(TODO requirement); the job fails only when *every* symbol failed.

Gap detection (R-YF-5 precursor; trading-calendar source still TBD in the
spec): expected sessions = Mon–Fri between a series' first/last stored bar,
excluding today before the 15:30 IST close. Missing weekdays are classified
cross-sectionally: a date missing from ≥ 80 % of series is almost certainly an
exchange holiday (absence normal), anything else is flagged as a genuine gap.
The report lands in ``data/prices_gap_report.md`` (+ ``.json``).

Runs integrate with the durable job framework: handler registered for the
``yahoo.fetch`` job kind family below; enqueue with the CLI and drain with
``scripts/job_worker.py``.

Testability: pure helpers (hashing, validation, gap classification, session
maths) are unit-tested without network or DB (tests/test_fetch_prices.py);
``run_partition`` accepts injected fetcher/clock/store like nse_capture.py.

CLI
---
    venv/bin/python scripts/fetch_prices.py enqueue --all
    venv/bin/python scripts/fetch_prices.py run --slug hdfcbank
    venv/bin/python scripts/fetch_prices.py run --all [--force]
    venv/bin/python scripts/fetch_prices.py gaps
    venv/bin/python scripts/fetch_prices.py status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent))

import jobs  # noqa: E402  (handler registration via @jobs.register_handler)
from companies import COMPANIES  # noqa: E402
from object_store import ObjectStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# --- source mechanics constants (docs/modules/yahoo-market-data/*) ----------
JOB_KIND = "yahoo.fetch"
SOURCE = "yfinance"
INTERVAL = "1d"
ADJUSTMENT_MODE = "total_return_adjusted"     # auto_adjust=True ⇒ splits+dividends
ADJUSTMENT_VERSION = "yf_auto_adjust_v1"      # pinned methodology (R-YF-1)
CURRENCY = "INR"
PERIOD = "1y"                                  # backfill lookback (spec: 3–12 months)
FETCH_SPACING_SEC = 1.0                        # ban-avoidance spacing (R-YF-6)

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_CLOSE = dtime(15, 30, tzinfo=IST)       # NSE session close IST
CURSOR_OVERLAP_DAYS = 7                        # ≥5 trading days overlap

GAP_REPORT_PATH = ROOT / "data" / "prices_gap_report.md"
HOLIDAY_FRACTION = 0.8                         # ≥80% of series missing ⇒ holiday


class PriceFetchError(RuntimeError):
    """Partition-level fetch failure (drives job retry with backoff)."""


class BarValidationError(PriceFetchError):
    """R-YF-3 validation failure — fail loudly per run, never silently."""


# --------------------------------------------------------------------- #
# pure helpers (unit-tested)
# --------------------------------------------------------------------- #

def ist_now(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(IST)


def day_key_for(now: Optional[datetime] = None) -> str:
    return ist_now(now).strftime("%Y%m%d")


def partition_key(listing_id: int) -> str:
    return f"listing:{listing_id}:daily"


def to_trade_date(index_ts: Any) -> date:
    """A yfinance row-index timestamp → exchange DATE (IST calendar)."""
    ts = index_ts.tz_convert("Asia/Kolkata") if index_ts.tzinfo is not None \
        else index_ts.tz_localize("Asia/Kolkata")
    return ts.date()


def observation_hash(open_: float, high: float, low: float, close: float,
                     volume: int, trade_date: date) -> str:
    """Deterministic content hash of one bar observation (R-YF-7).

    Deliberately excludes wall-clock time: identical re-fetched bars must
    produce identical keys so re-runs are no-ops; a changed value produces a
    new key and therefore a new append-only observation (R-YF-8)."""
    payload = (f"{open_:.6f}|{high:.6f}|{low:.6f}|{close:.6f}"
               f"|{int(volume)}|{trade_date.isoformat()}")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bar_idempotency_key(series_id: int, trade_date: date, obs_hash: str) -> str:
    return f"{series_id}:{trade_date.isoformat()}:{obs_hash}"


def frame_to_rows(frame: Any) -> list[dict[str, Any]]:
    """yfinance history DataFrame → plain dict rows sorted by trade date."""
    rows: list[dict[str, Any]] = []
    for idx, rec in frame.iterrows():
        rows.append({
            "trade_date": to_trade_date(idx),
            "open": float(rec["Open"]),
            "high": float(rec["High"]),
            "low": float(rec["Low"]),
            "close": float(rec["Close"]),
            "volume": int(rec["Volume"]),
        })
    rows.sort(key=lambda r: r["trade_date"])
    return rows


def validate_frame(rows: list[dict[str, Any]], *, today: Optional[date] = None
                   ) -> tuple[list[str], list[str]]:
    """R-YF-3 validation over parsed rows.

    Returns ``(hard_errors, warnings)``. Hard errors make the run fail loudly
    (nothing partial stored); zero-volume sessions are warnings only."""
    today = today or ist_now().date()
    hard: list[str] = []
    warnings: list[str] = []
    for i, r in enumerate(rows):
        td: date = r["trade_date"]
        where = f"row {i} ({td})"
        if td > today:
            hard.append(f"{where}: trade date in the future")
            continue
        if td.weekday() >= 5:
            hard.append(f"{where}: weekend trade date")
            continue
        o, h, l, c = r["open"], r["high"], r["low"], r["close"]
        if min(o, h, l, c) <= 0:
            hard.append(f"{where}: non-positive price "
                        f"(o={o} h={h} l={l} c={c})")
            continue
        if h < max(o, c, l) - 1e-9 or l > min(o, c, l) + 1e-9:
            hard.append(f"{where}: OHLC inversion "
                        f"(o={o} h={h} l={l} c={c})")
            continue
        if r["volume"] < 0:
            hard.append(f"{where}: negative volume {r['volume']}")
        elif r["volume"] == 0:
            warnings.append(f"row {i} ({td}): zero-volume session "
                            "(tolerated; semantics unverified)")
    return hard, warnings


def expected_sessions(start: date, end: date, *, now_ist: datetime
                      ) -> set[date]:
    """Weekday sessions in [start, end]; today excluded until the market has
    closed IST (R-YF-5 precursor — calendar-aware, not wall-clock age)."""
    today = now_ist.date()
    after_close = now_ist.timetz() >= MARKET_CLOSE
    out: set[date] = set()
    d = start
    while d <= end:
        if d.weekday() < 5 and (d < today or (d == today and after_close)):
            out.add(d)
        d += timedelta(days=1)
    return out


def classify_gaps(missing_by_series: dict[int, set[date]], total_series: int,
                  holiday_fraction: float = HOLIDAY_FRACTION
                  ) -> tuple[dict[int, list[date]], dict[int, list[date]]]:
    """Split each series' missing dates into (likely_holidays, real_gaps).

    A missing weekday absent from ≥ ``holiday_fraction`` of series is an
    exchange-holiday candidate (absence normal, R-YF-5); anything else is a
    genuine per-series gap."""
    counts: dict[date, int] = {}
    for missing in missing_by_series.values():
        for d in missing:
            counts[d] = counts.get(d, 0) + 1
    threshold = holiday_fraction * total_series
    holidays: dict[int, list[date]] = {}
    gaps: dict[int, list[date]] = {}
    for sid, missing in missing_by_series.items():
        holidays[sid] = sorted(d for d in missing if counts[d] >= threshold)
        gaps[sid] = sorted(d for d in missing if counts[d] < threshold)
    return holidays, gaps


@dataclass(frozen=True)
class PartitionTarget:
    partition_id: int
    listing_id: int
    company_slug: str
    yahoo_symbol: str


# --------------------------------------------------------------------- #
# DB plumbing
# --------------------------------------------------------------------- #

_TARGET_SQL = """
    SELECT p.partition_id,
           (p.config->>'listing_id')::bigint AS listing_id,
           c.canonical_name AS slug,
           l.provider_symbols->>'yfinance' AS sym
    FROM ingestion.source_partitions p
    JOIN core.listings l ON l.listing_id = (p.config->>'listing_id')::bigint
    JOIN core.companies c ON c.company_id = l.company_id
    WHERE p.source = 'yfinance'
"""


def get_target(conn: psycopg.Connection, partition_id: int) -> PartitionTarget:
    row = conn.execute(_TARGET_SQL + " AND p.partition_id = %s",
                       (partition_id,)).fetchone()
    if row is None or row[3] is None:
        raise PriceFetchError(
            f"no active yfinance partition {partition_id} — run seed_core.py")
    return PartitionTarget(row[0], row[1], row[2], row[3])


def target_for_slug(conn: psycopg.Connection, slug: str) -> int:
    co = COMPANIES[slug]
    row = conn.execute(
        _TARGET_SQL + """
          AND l.exchange = 'NSE' AND l.valid_to IS NULL
          AND l.exchange_symbol = %s
        """, (co.nse_symbol,)).fetchone()
    if row is None or row[3] is None:
        raise PriceFetchError(
            f"no yfinance partition for {slug} ({co.yahoo_symbol}) — "
            f"run seed_core.py")
    return int(row[0])


def all_targets(conn: psycopg.Connection) -> list[PartitionTarget]:
    rows = conn.execute(
        _TARGET_SQL + " AND p.is_active ORDER BY p.partition_id").fetchall()
    out = []
    for r in rows:
        if r[3] is None:
            continue
        out.append(PartitionTarget(r[0], r[1], r[2], r[3]))
    if not out:
        raise PriceFetchError("no yfinance partitions provisioned — "
                              "run seed_core.py")
    return out


def ensure_series(conn: psycopg.Connection, listing_id: int,
                  yahoo_symbol: str) -> int:
    """Get-or-create THE series row for (listing, 1d, adjustment mode) —
    R-YF-1: exactly one series fixes provider/symbol/interval/methodology."""
    row = conn.execute(
        """
        SELECT series_id FROM market.price_series
        WHERE listing_id=%s AND source=%s AND interval_code=%s
          AND adjustment_mode=%s AND adjustment_version=%s
        """,
        (listing_id, SOURCE, INTERVAL, ADJUSTMENT_MODE, ADJUSTMENT_VERSION),
    ).fetchone()
    if row is not None:
        return int(row[0])
    row = conn.execute(
        """
        INSERT INTO market.price_series
            (listing_id, source, provider_symbol, interval_code,
             adjustment_mode, adjustment_version, currency)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (listing_id, source, provider_symbol, interval_code,
                     adjustment_mode, adjustment_version) DO NOTHING
        RETURNING series_id
        """,
        (listing_id, SOURCE, yahoo_symbol, INTERVAL, ADJUSTMENT_MODE,
         ADJUSTMENT_VERSION, CURRENCY),
    ).fetchone()
    assert row is not None
    return int(row[0])


def persist_frame_artifact(conn: psycopg.Connection, store: ObjectStore,
                           *, fetch_run_id: int, part_key: str,
                           symbol: str, csv_bytes: bytes) -> int:
    """Persist the exact serialized provider frame + register lineage
    (content-deduped by SHA-256 across runs, like NSE API responses)."""
    idem = f"{SOURCE}:{part_key}:history:"
    result = store.put_bytes(
        csv_bytes,
        media_type="text/csv",
        metadata={"source": SOURCE, "symbol": symbol,
                  "note": "exact serialization of yfinance Ticker.history() "
                          "frame (see module docstring)"},
    )
    with conn.transaction():  # savepoint: dedupe must not abort caller tx
        row = conn.execute(
            """
            INSERT INTO ingestion.raw_artifacts
                (fetch_run_id, idempotency_key, artifact_kind, content_type,
                 sha256, storage_path, byte_size, metadata)
            VALUES (%s, %s, 'price_history', 'text/csv', %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING artifact_id
            """,
            (fetch_run_id, idem + result.sha256, result.sha256,
             result.storage_path, len(csv_bytes), Jsonb({"symbol": symbol})),
        ).fetchone()
    if row is not None:
        return int(row[0])
    row = conn.execute(
        "SELECT artifact_id FROM ingestion.raw_artifacts WHERE idempotency_key=%s",
        (idem + result.sha256,),
    ).fetchone()
    return int(row[0])


def upsert_cursor(conn: psycopg.Connection, partition_id: int,
                  fetch_run_id: int, last_trade_date: Optional[date]) -> None:
    """Advance cursor in the SAME transaction as artifacts+bars (invariant 7).
    High watermark = last session close IST; overlap ≥5 trading days."""
    hw = overlap_from = None
    if last_trade_date is not None:
        hw = (datetime.combine(last_trade_date, dtime()) + timedelta(
            hours=MARKET_CLOSE.hour, minutes=MARKET_CLOSE.minute)) \
            .replace(tzinfo=IST).astimezone(timezone.utc)
        overlap_from = hw - timedelta(days=CURSOR_OVERLAP_DAYS)
    conn.execute(
        """
        INSERT INTO ingestion.ingestion_cursors
            (partition_id, checkpoint, high_watermark_at, overlap_from,
             last_success_at, last_fetch_run_id, consecutive_failures, updated_at)
        VALUES (%s, '{}'::jsonb, %s, %s, now(), %s, 0, now())
        ON CONFLICT (partition_id) DO UPDATE SET
            high_watermark_at = COALESCE(EXCLUDED.high_watermark_at,
                                         ingestion.ingestion_cursors.high_watermark_at),
            overlap_from = COALESCE(EXCLUDED.overlap_from,
                                    ingestion.ingestion_cursors.overlap_from),
            last_success_at = EXCLUDED.last_success_at,
            last_fetch_run_id = EXCLUDED.last_fetch_run_id,
            consecutive_failures = 0,
            updated_at = now()
        """,
        (partition_id, hw, overlap_from, fetch_run_id),
    )


def bump_cursor_failures(conn: psycopg.Connection, partition_id: int) -> None:
    conn.execute(
        """
        INSERT INTO ingestion.ingestion_cursors
            (partition_id, checkpoint, consecutive_failures, updated_at)
        VALUES (%s, '{}'::jsonb, 1, now())
        ON CONFLICT (partition_id) DO UPDATE SET
            consecutive_failures = ingestion.ingestion_cursors.consecutive_failures + 1,
            updated_at = now()
        """,
        (partition_id,),
    )


# --------------------------------------------------------------------- #
# the run itself
# --------------------------------------------------------------------- #

def default_history_fn(symbol: str, period: str):
    """Real yfinance fetch (auto_adjust pinned True — see ADJUSTMENT_*)."""
    import yfinance as yf
    ticker = yf.Ticker(symbol)
    return ticker.history(period=period, interval=INTERVAL, auto_adjust=True)


def _yfinance_version() -> str:
    try:
        from importlib.metadata import version
        return version("yfinance")
    except Exception:  # noqa: BLE001
        return "unknown"


def run_partition(
    partition_id: int,
    *,
    period: str = PERIOD,
    day_key: Optional[str] = None,
    force: bool = False,
    quiet: bool = False,
    conn: Optional[psycopg.Connection] = None,
    store: Optional[ObjectStore] = None,
    history_fn: Optional[Callable[[str, str], Any]] = None,
    now: Optional[datetime] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """One fetch for one ``yfinance`` partition. Idempotent per partition +
    IST day unless ``force`` (which bypasses the completed-run shortcut so
    bar-level dedup can be proven). Injectable collaborators for tests:
    ``conn``, ``store``, ``history_fn``, ``now``, ``sleep_fn``."""
    log = (lambda m: None) if quiet else (lambda m: print(m, flush=True))
    store = store or ObjectStore.from_env()
    history_fn = history_fn or default_history_fn

    def _impl(c: psycopg.Connection) -> dict[str, Any]:
        target = get_target(c, partition_id)
        part_key = partition_key(target.listing_id)
        suffix = ":force" if force else ""
        run_idem = f"{SOURCE}:{part_key}:{day_key or day_key_for(now)}{suffix}"

        prev = c.execute(
            "SELECT fetch_run_id, status, items_seen, items_new "
            "FROM ingestion.fetch_runs WHERE idempotency_key=%s",
            (run_idem,),
        ).fetchone()
        if prev is not None and prev[1] in ("ok", "not_modified"):
            log(f"[{target.yahoo_symbol}] {run_idem} already {prev[1]} "
                f"(run {prev[0]}, {prev[3]} new bars then) — skipped")
            return {"partition_id": partition_id,
                    "symbol": target.yahoo_symbol,
                    "fetch_run_id": prev[0], "status": prev[1],
                    "bars_seen": prev[2], "bars_new": prev[3],
                    "skipped": True, "run_idem": run_idem}

        with c.transaction():
            c.execute(  # close any stale running run of this partition
                "UPDATE ingestion.fetch_runs SET status='failed', "
                "finished_at=now(), error_summary='superseded by newer run' "
                "WHERE partition_id=%s AND status='running'", (partition_id,))
            fr = c.execute(
                """
                INSERT INTO ingestion.fetch_runs
                    (partition_id, idempotency_key, status, config)
                VALUES (%s, %s, 'running', %s)
                ON CONFLICT (idempotency_key) DO UPDATE SET
                    status='running', finished_at=NULL, started_at=now(),
                    http_status=NULL, items_seen=0, items_new=0,
                    error_summary=NULL, config=EXCLUDED.config
                RETURNING fetch_run_id
                """,
                (partition_id, run_idem,
                 Jsonb({
                     "symbol": target.yahoo_symbol,
                     "interval": INTERVAL,
                     "period": period,
                     "auto_adjust": True,
                     "adjustment_mode": ADJUSTMENT_MODE,
                     "adjustment_version": ADJUSTMENT_VERSION,
                     "library": "yfinance",
                     "yfinance_version": _yfinance_version(),
                 })),
            ).fetchone()
            fetch_run_id = int(fr[0])
        log(f"[{target.yahoo_symbol}] fetch_run {fetch_run_id} period={period} "
            f"(run_key={run_idem})")

        # --- network fetch outside transactions; bytes persisted first ---
        try:
            frame = history_fn(target.yahoo_symbol, period)
            rows = frame_to_rows(frame)
        except Exception as e:  # noqa: BLE001 — errors are run failures
            _finalize_failed(c, partition_id, fetch_run_id,
                             f"{type(e).__name__}: {e}")
            raise PriceFetchError(
                f"[{target.yahoo_symbol}] history fetch failed: {e}") from e

        if not rows:
            _finalize_failed(c, partition_id, fetch_run_id,
                             "empty frame for active listing")
            raise PriceFetchError(
                f"[{target.yahoo_symbol}] empty history frame")

        hard, warnings = validate_frame(rows, today=ist_now(now).date())
        if hard:
            summary = "; ".join(hard[:10])
            _finalize_failed(c, partition_id, fetch_run_id,
                             f"validation failed: {summary}")
            raise BarValidationError(
                f"[{target.yahoo_symbol}] R-YF-3 validation failed: {summary}")

        series_id = ensure_series(c, target.listing_id, target.yahoo_symbol)
        csv_bytes = frame.to_csv(date_format="%Y-%m-%d").encode("utf-8")

        # --- Tx: artifact + append-only bars + run close + cursor --------
        with c.transaction():
            persist_frame_artifact(c, store, fetch_run_id=fetch_run_id,
                                   part_key=part_key,
                                   symbol=target.yahoo_symbol,
                                   csv_bytes=csv_bytes)
            seen = new = 0
            for r in rows:
                seen += 1
                oh = observation_hash(r["open"], r["high"], r["low"],
                                      r["close"], r["volume"],
                                      r["trade_date"])
                ins = c.execute(
                    """
                    INSERT INTO market.price_bars
                        (series_id, idempotency_key, trade_date,
                         open, high, low, close, volume, fetch_run_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING bar_id
                    """,
                    (series_id,
                     bar_idempotency_key(series_id, r["trade_date"], oh),
                     r["trade_date"], r["open"], r["high"], r["low"],
                     r["close"], r["volume"], fetch_run_id),
                ).fetchone()
                if ins is not None:
                    new += 1
            status = "ok" if new > 0 else "not_modified"
            c.execute(
                "UPDATE ingestion.fetch_runs SET finished_at=now(), "
                "status=%s, items_seen=%s, items_new=%s, config=config || %s "
                "WHERE fetch_run_id=%s",
                (status, seen, new,
                 Jsonb({"zero_volume_warnings": len(warnings)}),
                 fetch_run_id))
            upsert_cursor(c, partition_id, fetch_run_id, rows[-1]["trade_date"])

        log(f"[{target.yahoo_symbol}] {seen} bars seen, {new} new → {status}"
            + (f", {len(warnings)} zero-volume warnings" if warnings else ""))
        return {"partition_id": partition_id, "symbol": target.yahoo_symbol,
                "fetch_run_id": fetch_run_id, "status": status,
                "bars_seen": seen, "bars_new": new,
                "warnings": len(warnings), "skipped": False,
                "run_idem": run_idem}

    if conn is not None:
        return _impl(conn)
    with jobs.connect() as own:
        try:
            return _impl(own)
        except Exception:
            own.commit()  # keep failed-run lineage committed
            raise


def _finalize_failed(conn: psycopg.Connection, partition_id: int,
                     fetch_run_id: int, summary: str) -> None:
    try:
        with conn.transaction():
            conn.execute(
                "UPDATE ingestion.fetch_runs SET finished_at=now(), "
                "status='failed', error_summary=%s WHERE fetch_run_id=%s",
                (summary[:2000], fetch_run_id))
            bump_cursor_failures(conn, partition_id)
    except psycopg.Error:
        pass  # DB unavailable — job-framework retry will re-record


# --------------------------------------------------------------------- #
# multi-symbol driver + handler
# --------------------------------------------------------------------- #

def run_all(*, slug: Optional[str] = None, period: str = PERIOD,
            force: bool = False, quiet: bool = False) -> dict[str, Any]:
    """Run one or all partitions sequentially (R-YF-6). Per-symbol errors are
    captured; the batch continues. Returns aggregate stats."""
    with jobs.connect() as conn:
        ids = ([get_target(conn, target_for_slug(conn, slug))] if slug
               else all_targets(conn))
    results: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for i, t in enumerate(ids):
        if i:
            time.sleep(FETCH_SPACING_SEC)
        try:
            results.append(run_partition(t.partition_id, period=period,
                                         force=force, quiet=quiet))
        except Exception as e:  # noqa: BLE001 — capture per-symbol, continue
            errors[t.yahoo_symbol] = f"{type(e).__name__}: {e}"
    ok = sum(1 for r in results if r.get("status") in ("ok", "not_modified"))
    skipped = sum(1 for r in results if r.get("skipped"))
    return {"requested": len(ids), "ok": ok, "skipped": skipped,
            "failed": len(errors), "errors": errors, "results": results}


@jobs.register_handler(JOB_KIND)
@jobs.register_prefix_handler(JOB_KIND + ":")
def handle_yahoo_fetch(ctx: jobs.JobContext) -> None:
    """Durable-job entrypoint. Payload ``{"company_slug": "...",
    "partition_id": n}``. Deterministic per partition + IST day ⇒ crash
    redeliveries converge instead of duplicating lineage."""
    payload = ctx.payload
    stats = run_all(slug=payload.get("company_slug"),
                    force=bool(payload.get("force")))
    ctx.outbox.append({
        "event_type": "yahoo.fetch.completed",
        "aggregate_type": "source_partition",
        "aggregate_id": str(payload.get("partition_id") or payload.get("company_slug") or "all"),
        "event_key": f"{JOB_KIND}.completed:{ctx.idempotency_key}",
        "payload": {k: v for k, v in stats.items() if k != "results"},
    })
    if stats["ok"] == 0 and stats["failed"] > 0:
        raise PriceFetchError(
            f"all partitions failed: {stats['errors']}")


def enqueue(conn: psycopg.Connection, slug: str, day_key: str
            ) -> jobs.EnqueueResult:
    sym = COMPANIES[slug].yahoo_symbol
    partition_id = target_for_slug(conn, slug)
    return jobs.enqueue(
        conn,
        job_kind=f"{JOB_KIND}:{slug}",
        idempotency_key=f"{SOURCE}:{slug}:{day_key}",
        payload={"company_slug": slug, "partition_id": partition_id,
                 "symbol": sym},
        priority=100,
    )


# --------------------------------------------------------------------- #
# gap detection + reporting
# --------------------------------------------------------------------- #

def build_gap_data(conn: psycopg.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT s.series_id, c.canonical_name AS slug, MIN(b.trade_date),
               MAX(b.trade_date),
               array_agg(DISTINCT b.trade_date)::date[] AS dates
        FROM market.current_price_bars b
        JOIN market.price_series s USING (series_id)
        JOIN core.listings l ON l.listing_id = s.listing_id
        JOIN core.companies c ON c.company_id = l.company_id
        GROUP BY s.series_id, c.canonical_name ORDER BY c.canonical_name
        """
    ).fetchall()
    now_ist = ist_now()
    per_series: list[dict[str, Any]] = []
    missing_by_series: dict[int, set[date]] = {}
    for series_id, slug, lo, hi, dates in rows:
        present = set(dates)
        expected = expected_sessions(lo, hi, now_ist=now_ist)
        missing = expected - present
        extra = present - expected  # weekend/odd rows — should be empty
        missing_by_series[int(series_id)] = missing
        per_series.append({
            "series_id": int(series_id), "slug": slug,
            "first": lo.isoformat(), "last": hi.isoformat(),
            "bars": len(present), "expected_sessions": len(expected),
            "missing": [d.isoformat() for d in sorted(missing)],
            "unexpected_dates": [d.isoformat() for d in sorted(extra)],
        })
    hol, gaps = classify_gaps(missing_by_series, len(per_series)) \
        if per_series else ({}, {})
    return {
        "generated_at": now_ist.isoformat(),
        "series_count": len(per_series),
        "series": per_series,
        "holiday_candidates": {str(k): [d.isoformat() for d in v]
                               for k, v in hol.items()},
        "real_gaps": {str(k): [d.isoformat() for d in v]
                      for k, v in gaps.items()},
        "real_gap_total": sum(len(v) for v in gaps.values()),
    }


def write_gap_report(conn: psycopg.Connection, path: Path = GAP_REPORT_PATH
                     ) -> Path:
    data = build_gap_data(conn)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Daily-bar gap report (Yahoo .NS daily)",
        "",
        f"Generated: {data['generated_at']}  |  series: "
        f"{data['series_count']}  |  genuine gap-days total: "
        f"{data['real_gap_total']}",
        "",
        "Expected = weekdays between each series' first/last stored bar, "
        "excluding today before the 15:30 IST close. A missing weekday absent "
        "from >=80% of series is classified a likely exchange holiday "
        "(calendar source still TBD per module spec).",
        "",
        "| slug | bars | span | missing (non-holiday) | holiday candidates |",
        "|---|---|---|---|---|",
    ]
    for s in data["series"]:
        sid = str(s["series_id"])
        gaps_s = data["real_gaps"].get(sid, [])
        hol_s = data["holiday_candidates"].get(sid, [])
        lines.append(
            f"| {s['slug']} | {s['bars']} | {s['first']}..{s['last']} | "
            f"{'none' if not gaps_s else ', '.join(gaps_s)} | "
            f"{len(hol_s)} days |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path = path.with_suffix(".json")
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #

def _cmd_enqueue(args) -> int:
    slugs = list(COMPANIES) if args.all else [args.slug]
    day_key = day_key_for()
    created = 0
    with jobs.connect() as conn:
        with conn.transaction():
            for slug in slugs:
                res = enqueue(conn, slug, day_key)
                created += 1 if res.created else 0
                print(f"{'enqueued' if res.created else 'exists '} {slug} "
                      f"(job {res.job_id})")
    print(f"\n{created} new jobs (of {len(slugs)} requested). Drain with: "
          f"venv/bin/python scripts/job_worker.py --queue {JOB_KIND} --drain")
    return 0


def _cmd_run(args) -> int:
    stats = run_all(slug=args.slug, period=args.period, force=args.force)
    print(json.dumps({k: v for k, v in stats.items() if k != "results"},
                     indent=2))
    return 0 if stats["ok"] > 0 or stats["failed"] == 0 else 1


def _cmd_status(_args) -> int:
    with jobs.connect() as conn:
        rows = conn.execute(
            """
            SELECT c.canonical_name AS slug, l.exchange_symbol,
                   count(b.bar_id) AS bars,
                   MIN(b.trade_date), MAX(b.trade_date)
            FROM market.price_series s
            JOIN core.listings l USING (listing_id)
            JOIN core.companies c ON c.company_id = l.company_id
            LEFT JOIN market.current_price_bars b ON b.series_id = s.series_id
            WHERE s.source='yfinance'
            GROUP BY c.canonical_name, l.exchange_symbol ORDER BY c.canonical_name
            """
        ).fetchall()
        cursors = dict(conn.execute(
            "SELECT p.partition_id, cu.consecutive_failures "
            "FROM ingestion.source_partitions p "
            "LEFT JOIN ingestion.ingestion_cursors cu USING (partition_id) "
            "WHERE p.source='yfinance'"
        ).fetchall())
    print(f"{'company':45s} {'symbol':12s} {'bars':>6s}  span  "
          f"cursor_failures")
    for slug, sym, bars, first, last in rows:
        print(f"{slug[:44]:45s} {(sym or ''):12s} {bars:>6d}  "
              f"{'' if first is None else f'{first}..{last}'}")
    fails = {k: v for k, v in cursors.items() if v}
    if fails:
        print(f"\npartitions with consecutive failures: {fails}")
    return 0


def _cmd_gaps(_args) -> int:
    with jobs.connect() as conn:
        path = write_gap_report(conn)
    print(f"gap report written: {path}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Yahoo (.NS) daily-bar fetcher")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_enq = sub.add_parser("enqueue", help="enqueue durable fetch jobs")
    p_enq.add_argument("--slug")
    p_enq.add_argument("--all", action="store_true")

    p_run = sub.add_parser("run", help="run synchronously (no queue)")
    p_run.add_argument("--slug")
    p_run.add_argument("--all", action="store_true")
    p_run.add_argument("--period", default=PERIOD)
    p_run.add_argument("--force", action="store_true",
                       help="ignore the completed-run shortcut to prove "
                            "bar-level idempotency")

    sub.add_parser("gaps", help="generate the gap report")

    args = parser.parse_args(argv)
    if args.cmd == "enqueue":
        if not args.all and not args.slug:
            parser.error("enqueue needs --slug or --all")
        return _cmd_enqueue(args)
    if args.cmd == "run":
        if not args.all and not args.slug:
            parser.error("run needs --slug or --all")
        return _cmd_run(args)
    return _cmd_gaps(args)


if __name__ == "__main__":
    raise SystemExit(main())
