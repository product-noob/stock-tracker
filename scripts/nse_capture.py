"""NSE capture adapter → DB (Phase 0, ROADMAP action 2).

Reshapes the proven file-based ``fetch_announcements.py`` logic into a
partition-aware fetcher per ``docs/modules/nse-capture/OVERVIEW.md`` (+
``source-mechanics.md`` / ``data-contract.md``). One run = one
``ingestion.source_partitions`` row (``nse_api, symbol:<SYMBOL>``),
provisioned per company by ``seed_core.py``.

Lineage written per run (exact bytes first):

1. open/close an ``ingestion.fetch_runs`` row whose idempotency key is
   **deterministic per partition + IST calendar day** — re-running the same
   partition/day finds the completed run and returns early (no network, no
   new rows), which makes re-runs fully idempotent;
2. GET ``/api/corporate-announcements`` on a browser-TLS-impersonated session
   with homepage warm-up (R-NSE-1..4), one stale-session re-warm retry
   (R-NSE-11), ``index=equities`` required (R-NSE-9), bare JSON array
   expected (R-NSE-10);
3. persist the EXACT response bytes to the immutable object store and
   register them in ``documents.content_objects`` + ``ingestion.raw_artifacts``
   BEFORE any parsing (HLD invariant 1). Artifacts are content-deduped by
   SHA-256 across runs of a partition;
4. insert ``ingestion.raw_feed_items`` keyed ``nse_ann:{seq_id}``
   (data-contract §1), ``ON CONFLICT DO NOTHING`` → at-least-once safe;
5. download each announcement PDF (``attchmntFile``) with Referer on an
   impersonated session, validate by magic bytes (R-NSE-12/13), persist the
   exact bytes content-addressed (``content_objects`` + a ``filing_pdf``
   ``raw_artifacts`` row). Invalid/HTML bytes are ALSO persisted as evidence
   with ``artifact_kind='filing_pdf_invalid'`` and ``magic_valid=false``
   (R-NSE-14/15). URLs already captured by any earlier successful run of the
   partition are skipped;
6. only after artifacts + items commit, upsert ``ingestion.ingestion_cursors``
   (high_watermark_at, 24 h overlap_from) so runs are incremental and
   resume-safe (HLD invariant 7). A failed run bumps
   ``consecutive_failures`` and leaves the watermark untouched.

Runs integrate with the TODO-1 durable job framework: a handler is registered
for the ``nse.fetch`` job kind (see ``register_handler`` below); enqueue with
the CLI and drain with ``scripts/job_worker.py``.

Known Phase-0 limitation: PDF downloads commit in short transactions AFTER the
run row closes ``ok``. If the worker dies between those two points, the next
day-run's skip path re-enqueues just the still-missing PDF URLs
(``_pending_pdf_urls``), so the partition converges without refetching the API.

Identity note (R-NSE-20): ``raw_feed_items`` carries no company column on
purpose — attribution belongs to M2/M3. The per-symbol partition row already
carries ``company_id``; raw fields (``sm_name``, ``symbol``, ``sm_isin``) are
preserved verbatim in ``raw_payload`` so the resolver can confirm or correct.

Testability: every collaborator (DB connection, object store, HTTP fetchers,
clock) can be injected into :func:`run_partition`; unit tests drive it end to
end with recorded fixtures and never touch the network (see
``tests/test_nse_capture.py``).

CLI
---
    venv/bin/python scripts/nse_capture.py enqueue --slug hdfcbank
    venv/bin/python scripts/nse_capture.py enqueue --all
    venv/bin/python scripts/nse_capture.py run --slug hdfcbank   # direct, no queue
    venv/bin/python scripts/nse_capture.py status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent))

import jobs  # noqa: E402  (handler registration via @jobs.register_handler)
from companies import COMPANIES  # noqa: E402
from object_store import ObjectStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# --- source mechanics constants (docs/modules/nse-capture/source-mechanics.md)
NSE_HOME = "https://www.nseindia.com/"
ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"
API_HEADERS = {
    "Referer": "https://www.nseindia.com/all-corporate-announcements",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}
PDF_REFERER = "https://www.nseindia.com/"
IMPERSONATE = "chrome"          # R-NSE-4: pinned, recorded in fetch_runs config
OVERLAP = timedelta(hours=24)   # OVERVIEW partition table: 24 h overlap
FIRST_RUN_WINDOW = timedelta(days=7)
PDF_DELAY_SEC = 0.4             # ban-avoidance spacing between PDF downloads

IST = timezone(timedelta(hours=5, minutes=30))

JOB_KIND = "nse.fetch"

FetchFn = Callable[[str, str, str, str], tuple[Optional[int], bytes]]
PdfFetchFn = Callable[[Any, str], tuple[Optional[bytes], Optional[int], str]]


class NseFetchError(RuntimeError):
    """Partition-level fetch failure (drives job retry with backoff)."""


# --------------------------------------------------------------------- #
# HTTP session (R-NSE-1..4, R-NSE-11, R-NSE-12)
# --------------------------------------------------------------------- #

class NseSession:
    """Browser-TLS-impersonated session with homepage warm-up. One per
    partition run — never shared across partitions (R-NSE-3)."""

    def __init__(self, impersonate: str = IMPERSONATE):
        from curl_cffi import requests as cr
        self._cr = cr
        self.impersonate = impersonate
        self.session = cr.Session(impersonate=impersonate)

    def warm_up(self) -> None:
        r = self.session.get(NSE_HOME, timeout=30)
        if r.status_code != 200:
            raise NseFetchError(f"NSE warm-up failed: HTTP {r.status_code}")

    def get(self, url: str, *, headers: dict[str, str], timeout: int = 30):
        return self.session.get(url, headers=headers, timeout=timeout)

    def renew(self) -> None:
        """Fresh session + warm-up (stale Akamai cookies → 403/garbage)."""
        self.session = self._cr.Session(impersonate=self.impersonate)
        self.warm_up()


def make_fetch_fn(sess: NseSession) -> FetchFn:
    """Real announcements-API fetcher: warm up, GET, and on any non-200 /
    non-JSON response re-warm once and retry (R-NSE-2/11)."""
    def fetch(symbol: str, frm: str, to: str, url: str):
        sess.warm_up()
        r = sess.get(url, headers=API_HEADERS)
        status, body = r.status_code, r.content or b""
        if status != 200 or sniff_stale_session(body):
            sess.renew()
            r = sess.get(url, headers=API_HEADERS)
            status, body = r.status_code, r.content or b""
        return status, body
    return fetch


def make_pdf_fetch_fn(sess: NseSession) -> PdfFetchFn:
    """Real attachment downloader (Referer per R-NSE-12; retry-once handled
    by the caller via ``sess.renew()``)."""
    def fetch(session: NseSession, url: str):
        try:
            r = session.get(url, headers={"Referer": PDF_REFERER}, timeout=60)
            if r.status_code != 200:
                return None, r.status_code, f"HTTP {r.status_code}"
            return r.content or b"", r.status_code, ""
        except Exception as e:  # noqa: BLE001 — network errors are attempts
            return None, None, f"{type(e).__name__}: {e}"
    return fetch


# --------------------------------------------------------------------- #
# pure helpers (unit-tested)
# --------------------------------------------------------------------- #

def parse_an_dt(value: Any) -> Optional[datetime]:
    """``an_dt`` (%d-%b-%Y %H:%M:%S, IST assumed) → aware UTC datetime.
    Unparseable/missing → None (data-contract R-NSE-19: store null, keep raw)."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%d-%b-%Y %H:%M:%S") \
            .replace(tzinfo=IST).astimezone(timezone.utc)
    except ValueError:
        return None


def item_key(seq_id: Any) -> str:
    """Idempotency key per data-contract §1: native ID ``nse_ann:{seq_id}``."""
    return f"nse_ann:{seq_id}"


def sniff_stale_session(body: bytes) -> bool:
    """True if a 200 body is actually a stale-session/HTML/garbage response
    rather than the expected bare JSON array (R-NSE-10/11)."""
    head = body.lstrip()[:64]
    return not head.startswith(b"[")


def looks_like_pdf(data: bytes) -> bool:
    """Magic-byte validation (R-NSE-13) — never trust HTTP status alone."""
    return data.startswith(b"%PDF")


def window_from_cursor(cursor_row: Optional[dict[str, Any]],
                       now: datetime) -> tuple[datetime, datetime]:
    """(from_dt, to_dt) for this run. First run: 7-day backfill; afterwards
    high_watermark − 24 h overlap (OVERVIEW partition table)."""
    to_dt = now
    if cursor_row and cursor_row.get("high_watermark_at"):
        from_dt = cursor_row["high_watermark_at"] - OVERLAP
    else:
        from_dt = now - FIRST_RUN_WINDOW
    if from_dt >= to_dt:  # clock skew guard
        from_dt = to_dt - OVERLAP
    return from_dt, to_dt


# --------------------------------------------------------------------- #
# DB plumbing
# --------------------------------------------------------------------- #

def get_partition(conn: psycopg.Connection, partition_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT p.partition_id, p.source, p.partition_key, p.company_id,
               p.is_active
        FROM ingestion.source_partitions p
        WHERE p.partition_id = %s
        """,
        (partition_id,),
    ).fetchone()
    if row is None:
        raise NseFetchError(f"partition {partition_id} not found")
    keys = ["partition_id", "source", "partition_key", "company_id", "is_active"]
    return dict(zip(keys, row))


def partition_for_slug(conn: psycopg.Connection, slug: str) -> int:
    symbol = COMPANIES[slug].nse_symbol
    row = conn.execute(
        "SELECT partition_id FROM ingestion.source_partitions "
        "WHERE source='nse_api' AND partition_key=%s",
        (f"symbol:{symbol}",),
    ).fetchone()
    if row is None:
        raise NseFetchError(
            f"no nse_api partition for {slug} (symbol:{symbol}) — run seed_core.py")
    return row[0]


def get_cursor(conn: psycopg.Connection, partition_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT checkpoint, high_watermark_at, overlap_from, last_success_at "
        "FROM ingestion.ingestion_cursors WHERE partition_id=%s",
        (partition_id,),
    ).fetchone()
    if row is None:
        return None
    return {"checkpoint": row[0], "high_watermark_at": row[1],
            "overlap_from": row[2], "last_success_at": row[3]}


def url_already_captured(conn: psycopg.Connection, partition_id: int, url: str) -> bool:
    """True if a prior successful run of this partition already stored a valid
    PDF for this content URL (keeps re-runs from re-downloading)."""
    row = conn.execute(
        """
        SELECT 1
        FROM ingestion.raw_artifacts a
        JOIN ingestion.fetch_runs r USING (fetch_run_id)
        WHERE r.partition_id = %s
          AND a.artifact_kind = 'filing_pdf'
          AND a.metadata->>'url' = %s
        LIMIT 1
        """,
        (partition_id, url),
    ).fetchone()
    return row is not None


def _pending_pdf_urls(conn: psycopg.Connection, partition_id: int,
                      fetch_run_id: int) -> list[str]:
    """Announcement PDF URLs seen by ``fetch_run_id`` that no successful run
    of this partition has stored yet — used to converge after a crash between
    the API commit and the PDF downloads."""
    rows = conn.execute(
        """
        SELECT DISTINCT i.content_url
        FROM ingestion.raw_feed_items i
        WHERE i.partition_id = %s
          AND i.first_seen_run_id = %s
          AND i.content_url LIKE 'http%%'
          AND NOT EXISTS (
              SELECT 1
              FROM ingestion.raw_artifacts a
              JOIN ingestion.fetch_runs r USING (fetch_run_id)
              WHERE r.partition_id = %s
                AND a.artifact_kind = 'filing_pdf'
                AND a.metadata->>'url' = i.content_url)
        ORDER BY 1
        """,
        (partition_id, fetch_run_id, partition_id),
    ).fetchall()
    return [r[0] for r in rows]


# --------------------------------------------------------------------- #
# the run itself
# --------------------------------------------------------------------- #

def run_partition(
    partition_id: int,
    *,
    day_key: Optional[str] = None,
    impersonate: str = IMPERSONATE,
    quiet: bool = False,
    conn: Optional[psycopg.Connection] = None,
    store: Optional[ObjectStore] = None,
    fetch_fn: Optional[FetchFn] = None,
    pdf_fetch_fn: Optional[PdfFetchFn] = None,
    now: Optional[datetime] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """One incremental fetch for one ``nse_api`` partition.

    Raises NseFetchError on partition-level failure (after recording a failed
    fetch_run + cursor failure bump). Returns a stats dict; ``skipped: True``
    means the partition/day had already completed and nothing was refetched.

    All collaborators are injectable for tests: ``conn`` (DB connection —
    opens+commits its own when omitted), ``store``, ``fetch_fn`` /
    ``pdf_fetch_fn`` (HTTP; default = real NSE clients on an impersonated
    session), ``now`` (clock), ``sleep_fn`` (PDF spacing).
    """
    log = (lambda m: None) if quiet else (lambda m: print(m, flush=True))
    store = store or ObjectStore.from_env()

    if conn is not None:
        return _run_partition_impl(
            conn, store, partition_id, day_key=day_key,
            impersonate=impersonate, quiet=quiet, fetch_fn=fetch_fn,
            pdf_fetch_fn=pdf_fetch_fn, now=now, sleep_fn=sleep_fn, log=log)
    with jobs.connect() as own_conn:
        try:
            return _run_partition_impl(
                own_conn, store, partition_id, day_key=day_key,
                impersonate=impersonate, quiet=quiet, fetch_fn=fetch_fn,
                pdf_fetch_fn=pdf_fetch_fn, now=now, sleep_fn=sleep_fn, log=log)
        except Exception:
            # A partition-level failure must still leave its failed fetch_run
            # + cursor-failure lineage committed (the connection context
            # manager would otherwise roll the whole implicit tx back).
            own_conn.commit()
            raise


def _run_partition_impl(
    conn: psycopg.Connection,
    store: ObjectStore,
    partition_id: int,
    *,
    day_key: Optional[str],
    impersonate: str,
    quiet: bool,
    fetch_fn: Optional[FetchFn],
    pdf_fetch_fn: Optional[PdfFetchFn],
    now: Optional[datetime],
    sleep_fn: Callable[[float], None],
    log: Callable[[str], None],
) -> dict[str, Any]:
    part = get_partition(conn, partition_id)
    if part["source"] != "nse_api":
        raise NseFetchError(
            f"partition {partition_id} is {part['source']!r}, not nse_api")
    symbol = part["partition_key"].split(":", 1)[1]

    now = now or datetime.now(timezone.utc)
    day_key = day_key or now.astimezone(IST).strftime("%Y%m%d")
    # Deterministic per partition + IST day → re-runs of the same day find the
    # completed run below and no-op (TODO acceptance: idempotent re-runs).
    run_idem = f"nse_api:{part['partition_key']}:{day_key}"

    pdf_stats = {"downloaded": 0, "skipped": 0, "invalid": 0, "failed": 0}

    # --- already completed today? skip network entirely -----------------
    prev = conn.execute(
        "SELECT fetch_run_id, status, items_seen, items_new "
        "FROM ingestion.fetch_runs WHERE idempotency_key = %s",
        (run_idem,),
    ).fetchone()
    if prev is not None and prev[1] in ("ok", "not_modified"):
        prev_run_id, prev_status, seen, new = prev
        pending = _pending_pdf_urls(conn, partition_id, prev_run_id)
        if pending:
            # crash-recovery convergence: finish PDFs the earlier run missed
            sess, _, pdf_fetch_fn = _real_session(
                impersonate, fetch_fn, pdf_fetch_fn)
            pdf_stats = _download_pdfs(
                conn, store, sess, partition_id, prev_run_id,
                part["partition_key"], {u: ["recovery"] for u in pending},
                pdf_fetch_fn=pdf_fetch_fn, sleep_fn=sleep_fn, log=log)
        log(f"[{symbol}] partition/day {day_key} already {prev_status} "
            f"(run {prev_run_id}) — skipped")
        return {"partition_id": partition_id, "symbol": symbol,
                "fetch_run_id": prev_run_id, "status": prev_status,
                "items_seen": seen, "items_new": new, "http_status": None,
                "skipped": True, "run_idem": run_idem, "pdfs": pdf_stats}

    cursor = get_cursor(conn, partition_id)
    from_dt, to_dt = window_from_cursor(cursor, now)
    frm = from_dt.astimezone(IST).strftime("%d-%m-%Y")
    to = to_dt.astimezone(IST).strftime("%d-%m-%Y")

    sess, fetch_fn, pdf_fetch_fn = _real_session(
        impersonate, fetch_fn, pdf_fetch_fn)

    # Tx1: close any stale 'running' run of this partition (worker crash?),
    # then open/reuse THIS run row.
    with conn.transaction():
        conn.execute(
            "UPDATE ingestion.fetch_runs SET status='failed', finished_at=now(), "
            "error_summary='superseded by a newer run (worker crash?)' "
            "WHERE partition_id=%s AND status='running'", (partition_id,))
        fr = conn.execute(
            """
            INSERT INTO ingestion.fetch_runs
                (partition_id, idempotency_key, status, config)
            VALUES (%s, %s, 'running', %s)
            ON CONFLICT (idempotency_key) DO UPDATE SET
                status = 'running',
                finished_at = NULL,
                started_at = now(),
                http_status = NULL,
                items_seen = 0,
                items_new = 0,
                oldest_published_at = NULL,
                newest_published_at = NULL,
                error_summary = NULL,
                config = EXCLUDED.config
            RETURNING fetch_run_id
            """,
            (partition_id, run_idem,
             Jsonb({"impersonate": impersonate, "from": frm, "to": to,
                    "overlap_hours": int(OVERLAP.total_seconds() // 3600)})),
        ).fetchone()
        fetch_run_id = fr[0]
    log(f"[{symbol}] fetch_run {fetch_run_id} window {frm}..{to} "
        f"(run_key={run_idem})")

    # --- fetch (network outside transactions; bytes persisted first) ---
    params = {"symbol": symbol, "from_date": frm, "to_date": to,
              "index": "equities"}
    full_url = ANNOUNCEMENTS_URL + "?" + urllib.parse.urlencode(params)
    http_status: Optional[int] = None
    body = b""
    try:
        http_status, body = fetch_fn(symbol, frm, to, full_url)
    except Exception as e:  # noqa: BLE001 — network errors are run failures
        _finalize_failed(conn, store, partition_id, fetch_run_id,
                         http_status, f"{type(e).__name__}: {e}", body)
        raise NseFetchError(f"[{symbol}] request failed: {e}") from e

    if http_status != 200 or sniff_stale_session(body):
        _finalize_failed(conn, store, partition_id, fetch_run_id,
                         http_status,
                         f"stale session / non-JSON body after re-warm "
                         f"(head={body[:80]!r})", body)
        raise NseFetchError(
            f"[{symbol}] API unusable after re-warm (HTTP {http_status}, "
            f"head={body[:80]!r})")

    try:
        items = json.loads(body.decode("utf-8"))
        if not isinstance(items, list):
            raise ValueError("expected a bare JSON array")
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as e:
        _finalize_failed(conn, store, partition_id, fetch_run_id,
                         http_status, f"unparseable body: {e}", body)
        raise NseFetchError(f"[{symbol}] unparseable response: {e}") from e

    # --- Tx2: response artifact + raw items + fetch_run counts + cursor ---
    resp_sha, resp_path, _ = _persist_bytes(
        conn, store, body,
        media_type="application/json",
        metadata={"source": "nse_api", "url": full_url, "symbol": symbol},
    )
    with conn.transaction():
        artifact_id, first_seen_run = _store_artifact(
            conn,
            fetch_run_id=fetch_run_id,
            idem_key=f"nse_api:{part['partition_key']}:api_response:{resp_sha}",
            kind="api_response",
            media="application/json",
            sha=resp_sha, path=resp_path, size=len(body),
            meta={"url": full_url, "symbol": symbol})

        items_seen = 0
        items_new = 0
        published: list[datetime] = []
        pdf_urls: dict[str, list[str]] = {}
        for raw in items:
            if not isinstance(raw, dict):
                continue
            items_seen += 1
            seq_id = raw.get("seq_id")
            pub = parse_an_dt(raw.get("an_dt"))
            if pub is not None:
                published.append(pub)
            att = raw.get("attchmntFile") or ""
            if att.startswith("http"):
                pdf_urls.setdefault(att, []).append(str(seq_id))
            inserted = conn.execute(
                """
                INSERT INTO ingestion.raw_feed_items
                    (partition_id, first_seen_run_id, artifact_id,
                     idempotency_key, external_id, published_at, title,
                     description, content_url, raw_payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (partition_id, idempotency_key) DO NOTHING
                RETURNING item_id
                """,
                (partition_id, first_seen_run, artifact_id,
                 item_key(seq_id), str(seq_id) if seq_id is not None else None,
                 pub,
                 raw.get("sm_name") or raw.get("symbol"),
                 raw.get("attchmntText") or raw.get("desc"),
                 att or None,
                 Jsonb(raw)),
            ).fetchone()
            if inserted is not None:
                items_new += 1

        oldest = min(published) if published else None
        newest = max(published) if published else None
        status = "ok" if items_new > 0 else "not_modified"
        conn.execute(
            """
            UPDATE ingestion.fetch_runs
            SET finished_at=now(), status=%s, http_status=%s,
                items_seen=%s, items_new=%s,
                oldest_published_at=%s, newest_published_at=%s
            WHERE fetch_run_id=%s
            """,
            (status, http_status, items_seen, items_new, oldest, newest,
             fetch_run_id),
        )
        _upsert_cursor(conn, partition_id, fetch_run_id, newest,
                       {"last_from": frm, "last_to": to,
                        "last_status": status})
    log(f"[{symbol}] {items_seen} announcements seen, {items_new} new "
        f"→ status {status}")

    # --- PDFs (short transactions each; run row already closed) ---
    if pdf_urls:
        pdf_stats = _download_pdfs(
            conn, store, sess, partition_id, fetch_run_id,
            part["partition_key"], pdf_urls,
            pdf_fetch_fn=pdf_fetch_fn, sleep_fn=sleep_fn, log=log)
        log(f"[{symbol}] PDFs: {pdf_stats['downloaded']} downloaded, "
            f"{pdf_stats['skipped']} already stored, "
            f"{pdf_stats['invalid']} invalid-content, "
            f"{pdf_stats['failed']} failed")

    return {"partition_id": partition_id, "symbol": symbol,
            "fetch_run_id": fetch_run_id, "status": status,
            "items_seen": items_seen, "items_new": items_new,
            "http_status": http_status, "skipped": False,
            "run_idem": run_idem, "pdfs": pdf_stats}


def _real_session(impersonate: str, fetch_fn: Optional[FetchFn],
                  pdf_fetch_fn: Optional[PdfFetchFn]
                  ) -> tuple[Optional[NseSession], FetchFn, PdfFetchFn]:
    """Build the real impersonated session + fetchers unless all were
    injected (tests). Constructing NseSession does NO network I/O."""
    if fetch_fn is not None and pdf_fetch_fn is not None:
        return None, fetch_fn, pdf_fetch_fn
    sess = NseSession(impersonate=impersonate)
    return sess, fetch_fn or make_fetch_fn(sess), \
        pdf_fetch_fn or make_pdf_fetch_fn(sess)


# --------------------------------------------------------------------- #
# internals: persistence + failure + cursor + PDFs
# --------------------------------------------------------------------- #

def _persist_bytes(conn: psycopg.Connection, store: ObjectStore,
                   data: bytes, *, media_type: str,
                   metadata: dict[str, Any],
                   magic_valid: Optional[bool] = None) -> tuple[str, str, bool]:
    """Content-address the bytes and register them in ``content_objects``
    (idempotent) inside the caller's transaction. ``magic_valid`` must be
    decided at insert time — the table is append-only."""
    result = store.put_bytes(data, media_type=media_type, metadata=metadata,
                             conn=conn, magic_valid=magic_valid)
    return result.sha256, result.storage_path, result.created


def _store_artifact(conn: psycopg.Connection, *, fetch_run_id: int,
                    idem_key: str, kind: str, media: str, sha: str,
                    path: str, size: int, meta: dict[str, Any],
                    ) -> tuple[int, int]:
    """Insert a raw_artifacts row (deduped across runs by its unique
    idempotency key, which embeds a content/URL digest) and return
    ``(artifact_id, first_seen_run_id)``. When the key was already stored
    under an earlier run of this partition, that earlier run stays the
    first-seen run (FK pair artifact/run must stay consistent). The schema
    deliberately has NO UNIQUE (fetch_run_id, artifact_kind, sha256):
    byte-identical attachments under distinct keys keep separate lineage
    rows; content-level dedup happens via the shared ``content_objects``
    registration."""
    with conn.transaction():  # savepoint: a unique violation must not
        row = conn.execute(   # abort the caller's surrounding transaction
            """
            INSERT INTO ingestion.raw_artifacts
                (fetch_run_id, idempotency_key, artifact_kind, content_type,
                 sha256, storage_path, byte_size, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING artifact_id, fetch_run_id
            """,
            (fetch_run_id, idem_key, kind, media, sha, path, size,
             Jsonb(meta)),
        ).fetchone()
    if row is not None:
        return int(row[0]), int(row[1])
    row = conn.execute(
        "SELECT artifact_id, fetch_run_id FROM ingestion.raw_artifacts "
        "WHERE idempotency_key = %s",
        (idem_key,),
    ).fetchone()
    if row is None:
        raise NseFetchError(f"artifact row vanished for {idem_key}")
    return int(row[0]), int(row[1])


def _finalize_failed(conn: psycopg.Connection, store: ObjectStore,
                     partition_id: int, fetch_run_id: int,
                     http_status: Optional[int], summary: str,
                     body: bytes) -> None:
    """Persist whatever bytes we got as evidence, close the run as failed,
    bump the cursor failure counter."""
    try:
        with conn.transaction():
            if body:
                sha, path, _ = _persist_bytes(
                    conn, store, body, media_type="application/octet-stream",
                    metadata={"source": "nse_api", "error": summary[:500]})
                _store_artifact(
                    conn,
                    fetch_run_id=fetch_run_id,
                    idem_key=f"nse_api:{fetch_run_id}:err:{sha[:16]}",
                    kind="api_response_error",
                    media="application/octet-stream",
                    sha=sha, path=path, size=len(body),
                    meta={"error": summary[:500]})
            conn.execute(
                "UPDATE ingestion.fetch_runs SET finished_at=now(), "
                "status='failed', http_status=%s, error_summary=%s "
                "WHERE fetch_run_id=%s",
                (http_status, summary[:2000], fetch_run_id))
            _bump_cursor_failures(conn, partition_id)
    except psycopg.Error:
        pass  # DB unavailable — the job framework retry will re-record


def _upsert_cursor(conn: psycopg.Connection, partition_id: int,
                   fetch_run_id: int, high_watermark: Optional[datetime],
                   checkpoint: dict[str, Any]) -> None:
    """Advance the cursor — called in the SAME transaction that committed the
    artifacts + items (HLD invariant 7). Overlap = high_watermark − 24 h."""
    conn.execute(
        """
        INSERT INTO ingestion.ingestion_cursors
            (partition_id, checkpoint, high_watermark_at, overlap_from,
             last_success_at, last_fetch_run_id, consecutive_failures, updated_at)
        VALUES (%s, %s, %s,
                %s, now(), %s, 0, now())
        ON CONFLICT (partition_id) DO UPDATE SET
            checkpoint = EXCLUDED.checkpoint,
            high_watermark_at = COALESCE(EXCLUDED.high_watermark_at,
                                         ingestion.ingestion_cursors.high_watermark_at),
            overlap_from = COALESCE(EXCLUDED.overlap_from,
                                    ingestion.ingestion_cursors.overlap_from),
            last_success_at = EXCLUDED.last_success_at,
            last_fetch_run_id = EXCLUDED.last_fetch_run_id,
            consecutive_failures = 0,
            updated_at = now()
        """,
        (partition_id, Jsonb(checkpoint), high_watermark,
         (high_watermark - OVERLAP) if high_watermark else None, fetch_run_id),
    )


def _bump_cursor_failures(conn: psycopg.Connection, partition_id: int) -> None:
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


def _download_pdfs(conn: psycopg.Connection, store: ObjectStore,
                   sess: Optional[NseSession], partition_id: int,
                   fetch_run_id: int, partition_key: str,
                   pdf_urls: dict[str, list[str]], *,
                   pdf_fetch_fn: PdfFetchFn,
                   sleep_fn: Callable[[float], None],
                   log) -> dict[str, int]:
    """Download each announcement PDF, magic-byte validate, persist exact
    bytes content-addressed + lineage row. Invalid bytes are retained as
    evidence (artifact_kind='filing_pdf_invalid', magic_valid=false).
    Skips URLs already captured by an earlier successful run of this
    partition. One re-warm retry on failure (R-NSE-12)."""
    stats = {"downloaded": 0, "skipped": 0, "invalid": 0, "failed": 0}
    for url, seq_ids in sorted(pdf_urls.items()):
        if url_already_captured(conn, partition_id, url):
            stats["skipped"] += 1
            continue
        sleep_fn(PDF_DELAY_SEC)  # ban-avoidance spacing
        data, http_status, err = pdf_fetch_fn(sess, url)
        if not data:
            # one re-warm retry (R-NSE-12)
            if sess is not None:
                try:
                    sess.renew()
                except NseFetchError as e:
                    stats["failed"] += 1
                    log(f"  PDF FAIL {url} (re-warm failed: {e})")
                    continue
            data, http_status, err = pdf_fetch_fn(sess, url)
        if not data:
            stats["failed"] += 1
            log(f"  PDF FAIL {url} ({err})")
            continue
        valid = looks_like_pdf(data)
        media = "application/pdf" if valid else "text/html"
        sha, path, _ = _persist_bytes(
            conn, store, data, media_type=media,
            metadata={"source": "nse_api", "url": url},
            magic_valid=valid)
        kind = "filing_pdf" if valid else "filing_pdf_invalid"
        # Idempotency key is partition-scoped and derived from the URL + the
        # exact downloaded bytes — NOT from the day or seq_ids — so a
        # crash-recovery re-download of the same URL converges on the same
        # row instead of duplicating lineage (attempt-2 defect).
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        try:
            with conn.transaction():
                _store_artifact(
                    conn,
                    fetch_run_id=fetch_run_id,
                    idem_key=(f"nse_api:{partition_key}:pdf:"
                              f"{url_hash}:{sha[:16]}"),
                    kind=kind,
                    media=media,
                    sha=sha, path=path, size=len(data),
                    meta={"url": url, "seq_ids": seq_ids,
                          "magic_valid": valid, "http_status": http_status})
        except psycopg.Error as e:
            stats["failed"] += 1
            log(f"  PDF STORE FAIL {url}: {e}")
            continue
        if valid:
            stats["downloaded"] += 1
        else:
            stats["invalid"] += 1
            log(f"  PDF INVALID-CONTENT (kept as evidence) {url}")
    return stats


# --------------------------------------------------------------------- #
# job-framework handler (kind family prefix "nse.fetch")
# --------------------------------------------------------------------- #

@jobs.register_handler(JOB_KIND)
@jobs.register_prefix_handler(JOB_KIND + ":")
def handle_nse_fetch(ctx: jobs.JobContext) -> None:
    """Durable-job entrypoint. Payload:
    ``{"partition_id": int}`` (``company_slug`` informational).

    The underlying run is keyed deterministically per partition + IST day, so
    crash redeliveries on the same day converge instead of duplicating."""
    payload = ctx.payload
    partition_id = payload.get("partition_id")
    if partition_id is None:
        raise NseFetchError("payload missing partition_id")
    stats = run_partition(int(partition_id))
    event_key = f"{JOB_KIND}.completed:{stats['run_idem']}"
    ctx.outbox.append({
        "event_type": "nse.fetch.completed",
        "aggregate_type": "source_partition",
        "aggregate_id": str(partition_id),
        "event_key": event_key,
        "payload": stats,
    })


def enqueue(conn: psycopg.Connection, slug: str, day_key: str) -> jobs.EnqueueResult:
    """Enqueue the daily fetch job for one slug (idempotent per day)."""
    symbol = COMPANIES[slug].nse_symbol
    with conn.transaction():
        row = conn.execute(
            "SELECT partition_id FROM ingestion.source_partitions "
            "WHERE source='nse_api' AND partition_key=%s",
            (f"symbol:{symbol}",),
        ).fetchone()
        if row is None:
            raise NseFetchError(f"no nse_api partition for {slug}")
        return jobs.enqueue(
            conn,
            job_kind=JOB_KIND,
            idempotency_key=f"nse_api:{symbol}:{day_key}",
            payload={"company_slug": slug, "partition_id": row[0],
                     "symbol": symbol},
            priority=100,
        )


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #

def _cmd_enqueue(args) -> int:
    slugs = list(COMPANIES) if args.all else [args.slug]
    day_key = datetime.now(IST).strftime("%Y%m%d")
    created = 0
    with jobs.connect() as conn:
        with conn.transaction():
            for slug in slugs:
                res = enqueue(conn, slug, day_key)
                created += 1 if res.created else 0
                print(f"{'enqueued' if res.created else 'exists '} {slug} "
                      f"(job {res.job_id})")
    print(f"\n{created} new jobs (of {len(slugs)} requested). "
          f"Drain with: venv/bin/python scripts/job_worker.py --queue {JOB_KIND} --drain")
    return 0


def _cmd_run(args) -> int:
    with jobs.connect() as conn:
        partition_id = partition_for_slug(conn, args.slug)
    stats = run_partition(partition_id)
    print(json.dumps(stats, indent=2, default=str))
    return 0 if stats["status"] in ("ok", "not_modified") else 1


def _cmd_status(_args) -> int:
    with jobs.connect() as conn:
        rows = conn.execute(
            """
            SELECT co.canonical_name, p.partition_key,
                   (SELECT count(*) FROM ingestion.fetch_runs f
                     WHERE f.partition_id = p.partition_id) AS runs,
                   (SELECT count(*) FROM ingestion.fetch_runs f
                     WHERE f.partition_id = p.partition_id
                       AND f.status IN ('ok','not_modified')) AS ok_runs,
                   (SELECT count(*) FROM ingestion.raw_feed_items i
                     WHERE i.partition_id = p.partition_id) AS items,
                   cu.high_watermark_at
            FROM ingestion.source_partitions p
            JOIN core.companies co USING (company_id)
            JOIN ingestion.ingestion_cursors cu USING (partition_id)
            WHERE p.source = 'nse_api'
            ORDER BY co.canonical_name
            """
        ).fetchall()
    print(f"{'company':45s} {'symbol':12s} {'runs':>4s} {'ok':>4s} "
          f"{'items':>6s}  high_watermark")
    for name, key, runs, ok, items, hw in rows:
        print(f"{name[:44]:45s} {key.replace('symbol:', ''):12s} {runs:>4d} "
              f"{ok:>4d} {items:>6d}  {hw}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="NSE capture adapter → DB")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_enq = sub.add_parser("enqueue", help="enqueue durable fetch jobs")
    p_enq.add_argument("--slug")
    p_enq.add_argument("--all", action="store_true")

    p_run = sub.add_parser("run", help="run one partition synchronously (no queue)")
    p_run.add_argument("--slug", required=True)

    sub.add_parser("status", help="per-partition fetch/item counts")

    args = parser.parse_args(argv)
    if args.cmd == "enqueue":
        if not args.all and not args.slug:
            parser.error("enqueue needs --slug or --all")
        return _cmd_enqueue(args)
    if args.cmd == "run":
        return _cmd_run(args)
    return _cmd_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
