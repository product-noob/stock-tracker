"""BSE capture adapter → DB (Phase 0, ROADMAP action 2 / TODO 3).

Reshapes the proven file-based ``fetch_announcements.py`` (BSE half) +
``download_pdfs.py`` discovery logic into a partition-aware fetcher per
``docs/modules/bse-capture/OVERVIEW.md`` (+ ``source-mechanics.md`` /
``data-contract.md``). One run = one ``ingestion.source_partitions`` row
(``bse_api, scrip:{SCRIP}``), provisioned per company by ``seed_core.py``.

Lineage written per run (exact bytes first):

1. open/close an ``ingestion.fetch_runs`` row whose idempotency key is
   **deterministic per partition + IST calendar day** — re-running the same
   partition/day finds the completed run and returns early (no network, no
   new rows), which makes re-runs fully idempotent;
2. GET ``AnnSubCategoryGetData/w`` on a browser-TLS-impersonated session with
   Referer/Origin headers (R-BSE-1), one warm-up retry on stale/garbage
   responses (R-BSE-1), strict envelope validation (R-BSE-2: missing
   ``Table`` fails loudly, never yields silent zero), UTF-8-BOM tolerant
   (R-BSE-3);
3. persist the EXACT response bytes of every page to the immutable object
   store and register them in ``documents.content_objects`` +
   ``ingestion.raw_artifacts`` BEFORE any parsing use (HLD invariant 1);
4. paginate on ``ROWCNT`` (R-BSE-8): collect until ``len(items) >= ROWCNT``
   or pages exhaust; a shortfall raises instead of silently truncating. An
   empty result is disambiguated by a known-active control-scrip query
   before "no data" is accepted (R-BSE-9);
5. insert ``ingestion.raw_feed_items`` keyed ``bse_ann:{NEWSID}``
   (data-contract §1), ``ON CONFLICT DO NOTHING`` → at-least-once safe.
   RSS-style link equality is never treated as record equality (R-BSE-6);
   ``isin_raw`` stays null — the API returns none (R-BSE-14);
6. download each attachment trying ``AttachHis`` first, then ``AttachLive``
   (R-BSE-10), validating magic bytes after every attempt (R-BSE-11). Valid
   bytes become content objects keyed by SHA-256 + a ``filing_pdf`` artifact;
   HTML-as-200 garbage is ALSO persisted as evidence
   (``artifact_kind='filing_pdf_invalid'``, ``magic_valid=false``) (R-BSE-12).
   Attachment URLs already captured by any earlier successful run of the
   partition are skipped;
7. only after artifacts + items commit, upsert ``ingestion.ingestion_cursors``
   (high_watermark_at from newest ``NEWS_DT``, 24 h overlap_from) so runs are
   incremental and resume-safe (HLD invariant 7). A failed run bumps
   ``consecutive_failures`` and leaves the watermark untouched.

Runs integrate with the TODO-1 durable job framework: a handler is registered
for the ``bse.fetch`` job kind (see ``register_handler`` below); enqueue with
the CLI and drain with ``scripts/job_worker.py``.

Known Phase-0 limitation (same as NSE): PDF downloads commit in short
transactions AFTER the run row closes ``ok``. If the worker dies between those
two points, the next day-run's skip path re-enqueues just the still-missing
attachment URLs (``_pending_pdf_urls``), so the partition converges without
refetching the API.

Identity note (R-BSE-15): ``raw_feed_items`` carries no company column on
purpose — attribution belongs to M2/M3. The per-scrip partition row already
carries ``company_id``; raw fields (``SLONGNAME`` verbatim, ``SCRIP_CD``) are
preserved in ``raw_payload`` so the resolver can confirm or correct. The scrip
code itself is a first-class identity input: rows whose ``SCRIP_CD`` does not
match the partition scrip are dropped (defense against API drift).

Testability: every collaborator (DB connection, object store, HTTP fetchers,
clock) can be injected into :func:`run_partition`; unit tests drive it end to
end with recorded fixtures and never touch the network (see
``tests/test_bse_capture.py``).

CLI
---
    venv/bin/python scripts/bse_capture.py enqueue --slug hdfcbank
    venv/bin/python scripts/bse_capture.py enqueue --all
    venv/bin/python scripts/bse_capture.py run --slug hdfcbank   # direct, no queue
    venv/bin/python scripts/bse_capture.py status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
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
from capture_schedule import capture_interval_hours, capture_slot_key  # noqa: E402
from companies import COMPANIES  # noqa: E402
from object_store import ObjectStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# --- source mechanics constants (docs/modules/bse-capture/source-mechanics.md)
BSE_HOME = "https://www.bseindia.com/"
ANNOUNCEMENTS_URL = ("https://api.bseindia.com/BseIndiaAPI/api/"
                     "AnnSubCategoryGetData/w")
ATTACH_HIS_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"
ATTACH_LIVE_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
API_HEADERS = {
    "Referer": "https://www.bseindia.com/corporates/ann.html",
    "Origin": "https://www.bseindia.com",
    "Accept": "application/json, text/plain, */*",
}
PDF_REFERER = "https://www.bseindia.com/"
IMPERSONATE = "chrome"          # R-BSE-1: pinned, recorded in fetch_runs config
OVERLAP = timedelta(hours=24)   # OVERVIEW partition table: 24 h overlap
FIRST_RUN_WINDOW = timedelta(days=7)
PDF_DELAY_SEC = 0.4             # ban-avoidance spacing between downloads
MAX_PAGES = 40                  # pagination guardrail (page size ~50–100)
# R-BSE-9 canary: a scrip that reliably has announcements. Config-driven host/
# scrip override kept possible via fetch_fn injection in tests.
CONTROL_SCRIP = "500325"        # Reliance Industries

IST = timezone(timedelta(hours=5, minutes=30))

JOB_KIND = "bse.fetch"

FetchFn = Callable[[str, str, str, int, str], tuple[Optional[int], bytes]]
PdfFetchFn = Callable[[Any, str], tuple[Optional[bytes], Optional[int], str]]


class BseFetchError(RuntimeError):
    """Partition-level fetch failure (drives job retry with backoff).

    ``body`` optionally carries the exact offending response bytes so the
    failed run can retain them as evidence."""

    def __init__(self, msg: str, body: bytes = b""):
        super().__init__(msg)
        self.body = body


class EnvelopeError(BseFetchError):
    """API contract drift: response shape does not match the documented
    envelope (R-BSE-2 — fail loudly, never yield silent zero items)."""


# --------------------------------------------------------------------- #
# HTTP session (R-BSE-1)
# --------------------------------------------------------------------- #

class BseSession:
    """Browser-TLS-impersonated session. One per partition run — never shared
    across partitions. BSE has needed no cookie warm-up so far, but the
    contract changed without notice once already, so renew-on-garbage is
    built in rather than assumed away."""

    def __init__(self, impersonate: str = IMPERSONATE):
        from curl_cffi import requests as cr
        self._cr = cr
        self.impersonate = impersonate
        self.session = cr.Session(impersonate=impersonate)

    def warm_up(self) -> None:
        r = self.session.get(BSE_HOME, timeout=30)
        if r.status_code != 200:
            raise BseFetchError(f"BSE warm-up failed: HTTP {r.status_code}")

    def get(self, url: str, *, headers: dict[str, str], timeout: int = 30):
        return self.session.get(url, headers=headers, timeout=timeout)

    def renew(self) -> None:
        self.session = self._cr.Session(impersonate=self.impersonate)


def make_fetch_fn(sess: BseSession) -> FetchFn:
    """Real announcements-API fetcher: GET, and on any non-200 / non-JSON
    response renew the session once and retry (R-BSE-1)."""
    def fetch(scrip: str, frm: str, to: str, pageno: int, url: str):
        r = sess.get(url, headers=API_HEADERS)
        status, body = r.status_code, r.content or b""
        if status != 200 or sniff_stale_session(body):
            sess.renew()
            sess.warm_up()
            r = sess.get(url, headers=API_HEADERS)
            status, body = r.status_code, r.content or b""
        return status, body
    return fetch


def make_pdf_fetch_fn(sess: BseSession) -> PdfFetchFn:
    """Real attachment downloader (Referer per R-BSE-1; retry-once handled by
    the caller via ``sess.renew()``)."""
    def fetch(session: BseSession, url: str):
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

def strip_bom(body: bytes) -> str:
    """Decode UTF-8 text tolerating a leading BOM (R-BSE-3)."""
    text = body.decode("utf-8", errors="strict") if body else ""
    return text.lstrip("\ufeff")


def parse_envelope(body: bytes) -> tuple[list[dict[str, Any]], Optional[int]]:
    """Validate + unwrap the documented envelope
    ``{"Table": [...], "Table1": [{"ROWCNT": n}]}`` (R-BSE-7).

    Returns ``(table_rows, rowcnt)``. Raises :class:`EnvelopeError` on any
    shape mismatch — a missing/garbled envelope must fail the run loudly,
    never masquerade as "no announcements" (R-BSE-2)."""
    try:
        payload = json.loads(strip_bom(body))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise EnvelopeError(f"response is not valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise EnvelopeError(
            f"expected a JSON object envelope, got {type(payload).__name__}")
    table = payload.get("Table")
    if table is None:
        raise EnvelopeError("envelope missing 'Table' key (contract drift?)")
    if not isinstance(table, list):
        raise EnvelopeError("'Table' is not a list")
    rows: list[dict[str, Any]] = []
    for raw in table:
        if not isinstance(raw, dict):
            raise EnvelopeError("'Table' contains a non-object row")
        rows.append(raw)
    t1 = payload.get("Table1")
    if not (isinstance(t1, list) and t1 and isinstance(t1[0], dict)):
        raise EnvelopeError("envelope missing 'Table1[0].ROWCNT' (contract drift?)")
    rowcnt = t1[0].get("ROWCNT")
    if isinstance(rowcnt, bool) or not isinstance(rowcnt, int) or rowcnt < 0:
        raise EnvelopeError(f"'Table1[0].ROWCNT' is not a non-negative int: {rowcnt!r}")
    if rowcnt < len(rows):
        raise EnvelopeError(
            f"'Table1[0].ROWCNT' ({rowcnt}) is smaller than Table ({len(rows)})")
    for pos, row in enumerate(rows):
        if row.get("NEWSID") in (None, ""):
            raise EnvelopeError(f"'Table[{pos}]' is missing NEWSID")
    return rows, rowcnt


def sniff_stale_session(body: bytes) -> bool:
    """True if a 200 body is actually a stale-session/HTML/garbage response
    rather than the expected JSON object envelope (R-BSE-1/2). Tolerates a
    UTF-8 BOM and surrounding whitespace (R-BSE-3)."""
    head = body.lstrip()
    if head.startswith(b"\xef\xbb\xbf"):  # UTF-8 BOM
        head = head[3:].lstrip()
    return not head.startswith(b"{")


def parse_news_dt(value: Any) -> Optional[datetime]:
    """``NEWS_DT`` (ISO, fractional seconds optional, IST assumed) → aware
    UTC datetime. Unparseable/missing → None (keep raw in ``raw_payload``)."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%d-%b-%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt) \
                .replace(tzinfo=IST).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def item_key(news_id: Any) -> str:
    """Idempotency key per data-contract §1: native ID ``bse_ann:{NEWSID}``.
    (RSS fallback tuple lives in the future rss adapter; the API always has
    NEWSID.)"""
    return f"bse_ann:{news_id}"


def api_url(scrip: str, frm_yyyymmdd: str, to_yyyymmdd: str,
            pageno: int = 1) -> str:
    """Current API contract (validated 2026-08-22; supersedes
    nse-bse-rss.md §API params — see source-mechanics §3)."""
    params = {
        "pageno": pageno,
        "strCat": "-1",
        "strPrevDate": frm_yyyymmdd,
        "strScrip": scrip,
        "strSearch": "P",
        "strToDate": to_yyyymmdd,
        "strType": "C",
        "subcategory": "-1",
    }
    return ANNOUNCEMENTS_URL + "?" + urllib.parse.urlencode(params)


def attachment_urls(filename: str) -> list[str]:
    """Download-path candidates ordered by where the file most likely lives.

    R-BSE-10 archive-first (AttachHis) applies to legacy NUMERIC attachment
    names. 2026-08-23 owner finding: newer GUID-named filings exist ONLY under
    AttachLive — His returns HTTP 404 + HTML error page for them — so detect
    the GUID shape and try Live first instead.
    """
    filename = filename.strip()
    guid_like = bool(re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\.pdf",
        filename))
    bases = ([ATTACH_LIVE_BASE, ATTACH_HIS_BASE] if guid_like
             else [ATTACH_HIS_BASE, ATTACH_LIVE_BASE])
    return [b + filename for b in bases]


def looks_like_pdf(data: bytes) -> bool:
    """Magic-byte validation (R-BSE-11) — never trust HTTP status alone."""
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
        raise BseFetchError(f"partition {partition_id} not found")
    keys = ["partition_id", "source", "partition_key", "company_id", "is_active"]
    return dict(zip(keys, row))


def partition_for_slug(conn: psycopg.Connection, slug: str) -> int:
    scrip = COMPANIES[slug].bse_scrip
    row = conn.execute(
        "SELECT partition_id FROM ingestion.source_partitions "
        "WHERE source='bse_api' AND partition_key=%s",
        (f"scrip:{scrip}",),
    ).fetchone()
    if row is None:
        raise BseFetchError(
            f"no bse_api partition for {slug} (scrip:{scrip}) — run seed_core.py")
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
    """True if a prior run of this partition already stored VALID bytes for
    this content URL.

    2026-08-23 fix (owner-reported): invalid captures must NOT count as
    "handled". GUID-named BSE filings exist only under AttachLive; AttachHis
    serves them an HTTP-404 HTML error page, so an interrupted run could leave
    an invalid-only capture that blocked the valid Live download forever.
    Re-attempting invalid-only URLs converges: identical garbage re-registers
    to the same content-addressed row (no dup lineage), and the Live variant
    finally gets its chance.
    """
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
    """Attachment URLs seen by ``fetch_run_id`` that no successful run of this
    partition has stored yet — used to converge after a crash between the API
    commit and the downloads."""
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
                AND a.artifact_kind IN ('filing_pdf', 'filing_pdf_invalid')
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
    """One incremental fetch for one ``bse_api`` partition.

    Raises BseFetchError on partition-level failure (after recording a failed
    fetch_run + cursor failure bump). Returns a stats dict; ``skipped: True``
    means the partition/schedule slot had already completed and nothing was refetched.

    All collaborators are injectable for tests: ``conn`` (DB connection —
    opens+commits its own when omitted), ``store``, ``fetch_fn`` /
    ``pdf_fetch_fn`` (HTTP; default = real BSE clients on an impersonated
    session), ``now`` (clock), ``sleep_fn`` (download spacing).
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
    if part["source"] != "bse_api":
        raise BseFetchError(
            f"partition {partition_id} is {part['source']!r}, not bse_api")
    prefix, _, scrip = part["partition_key"].partition(":")
    if prefix != "scrip" or not scrip:
        raise BseFetchError(
            f"unexpected bse_api partition key {part['partition_key']!r}")

    now = now or datetime.now(timezone.utc)
    interval_hours = capture_interval_hours()
    day_key = day_key or capture_slot_key(now, interval_hours=interval_hours)
    # Deterministic per partition + configured IST slot: retries converge while
    # the default schedule permits four independent captures per day.
    run_idem = f"bse_api:{part['partition_key']}:{day_key}"

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
            # crash-recovery convergence: finish downloads the earlier run missed
            sess, _, pdf_fetch_fn = _real_session(
                impersonate, fetch_fn, pdf_fetch_fn)
            pdf_stats = _download_attachments(
                conn, store, sess, partition_id, prev_run_id,
                part["partition_key"], {u: ["recovery"] for u in pending},
                pdf_fetch_fn=pdf_fetch_fn, sleep_fn=sleep_fn, log=log)
        log(f"[{scrip}] partition/slot {day_key} already {prev_status} "
            f"(run {prev_run_id}) — skipped")
        return {"partition_id": partition_id, "scrip": scrip,
                "fetch_run_id": prev_run_id, "status": prev_status,
                "items_seen": seen, "items_new": new, "http_status": None,
                "skipped": True, "run_idem": run_idem, "pdfs": pdf_stats}

    cursor = get_cursor(conn, partition_id)
    from_dt, to_dt = window_from_cursor(cursor, now)
    frm = from_dt.astimezone(IST).strftime("%Y%m%d")
    to = to_dt.astimezone(IST).strftime("%Y%m%d")

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
                    "overlap_hours": int(OVERLAP.total_seconds() // 3600),
                    "capture_interval_hours": interval_hours,
                    "schedule_slot": day_key,
                    "control_scrip": CONTROL_SCRIP})),
        ).fetchone()
        fetch_run_id = fr[0]
    log(f"[{scrip}] fetch_run {fetch_run_id} window {frm}..{to} "
        f"(run_key={run_idem})")

    # --- fetch (network outside transactions; bytes persisted first) ---
    try:
        collected, rowcnt, page_meta = _collect_pages(
            scrip, frm, to, fetch_fn, log)
        # R-BSE-9: empty Table with HTTP 200 is ambiguous — disambiguate via a
        # known-active control scrip before accepting "no data".
        control_artifact = None
        if not collected:
            control_artifact = _run_control_query(
                conn, store, fetch_run_id, frm, to, fetch_fn, part, log)
    except BseFetchError as e:
        _finalize_failed(conn, store, partition_id, fetch_run_id, None,
                         str(e), getattr(e, "body", b""))
        raise

    items = [row for row in collected.values() if _row_matches_scrip(row, scrip)]
    foreign = len(collected) - len(items)
    if foreign:
        log(f"[{scrip}] dropping {foreign} rows whose SCRIP_CD ≠ {scrip}")

    # --- Tx2: response artifacts + raw items + fetch_run counts + cursor ---
    with conn.transaction():
        artifact_id: Optional[int] = None
        for pageno, (body, url) in sorted(page_meta.items()):
            resp_sha, resp_path, _ = _persist_bytes(
                conn, store, body,
                media_type="application/json",
                metadata={"source": "bse_api", "url": url, "scrip": scrip},
            )
            aid, first_seen_run = _store_artifact(
                conn,
                fetch_run_id=fetch_run_id,
                idem_key=(f"bse_api:{part['partition_key']}:api_response:"
                          f"p{pageno}:{resp_sha}"),
                kind="api_response",
                media="application/json",
                sha=resp_sha, path=resp_path, size=len(body),
                meta={"url": url, "scrip": scrip, "pageno": pageno})
            if pageno == 1:
                artifact_id = aid

        items_seen = 0
        items_new = 0
        published: list[datetime] = []
        att_urls: dict[str, list[str]] = {}
        for raw in items:
            items_seen += 1
            news_id = str(raw.get("NEWSID"))
            pub = parse_news_dt(raw.get("NEWS_DT"))
            if pub is not None:
                published.append(pub)
            attachment = (raw.get("ATTACHMENTNAME") or "").strip()
            content_url = ATTACH_HIS_BASE + attachment if attachment else None
            if content_url:
                att_urls.setdefault(content_url, []).append(news_id)
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
                 item_key(news_id), news_id,
                 pub,
                 raw.get("SLONGNAME"),
                 raw.get("HEADLINE"),
                 content_url,
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
            (status, 200, items_seen, items_new, oldest, newest,
             fetch_run_id),
        )
        _upsert_cursor(conn, partition_id, fetch_run_id, newest,
                       {"last_from": frm, "last_to": to,
                        "last_status": status,
                        "rowcnt": rowcnt,
                        "control_checked": control_artifact is not None})
    log(f"[{scrip}] {items_seen} announcements seen ({items_new} new, "
        f"ROWCNT={rowcnt}) → status {status}")

    # --- attachments (short transactions each; run row already closed) ---
    if att_urls:
        pdf_stats = _download_attachments(
            conn, store, sess, partition_id, fetch_run_id,
            part["partition_key"], att_urls,
            pdf_fetch_fn=pdf_fetch_fn, sleep_fn=sleep_fn, log=log)
        log(f"[{scrip}] attachments: {pdf_stats['downloaded']} downloaded, "
            f"{pdf_stats['skipped']} already stored, "
            f"{pdf_stats['invalid']} invalid-content, "
            f"{pdf_stats['failed']} failed")

    return {"partition_id": partition_id, "scrip": scrip,
            "fetch_run_id": fetch_run_id, "status": status,
            "items_seen": items_seen, "items_new": items_new,
            "http_status": 200, "skipped": False,
            "run_idem": run_idem, "pdfs": pdf_stats}


def _real_session(impersonate: str, fetch_fn: Optional[FetchFn],
                  pdf_fetch_fn: Optional[PdfFetchFn]
                  ) -> tuple[Optional[BseSession], FetchFn, PdfFetchFn]:
    """Build the real impersonated session + fetchers unless all were
    injected (tests). Constructing BseSession does NO network I/O."""
    if fetch_fn is not None and pdf_fetch_fn is not None:
        return None, fetch_fn, pdf_fetch_fn
    sess = BseSession(impersonate=impersonate)
    return sess, fetch_fn or make_fetch_fn(sess), \
        pdf_fetch_fn or make_pdf_fetch_fn(sess)


# --------------------------------------------------------------------- #
# fetching internals
# --------------------------------------------------------------------- #

def _fetch_page(fetch_fn: FetchFn, scrip: str, frm: str, to: str,
                pageno: int) -> tuple[bytes, list[dict[str, Any]],
                                      Optional[int], str]:
    """One validated page fetch. Returns ``(body, rows, rowcnt, url)``.
    Raises BseFetchError on network/HTTP/stale-session/envelope failure."""
    url = api_url(scrip, frm, to, pageno)
    try:
        status, body = fetch_fn(scrip, frm, to, pageno, url)
    except Exception as e:  # noqa: BLE001 — network errors are run failures
        raise BseFetchError(f"request failed: {type(e).__name__}: {e}") from e
    if status != 200 or sniff_stale_session(body):
        raise BseFetchError(
            f"API unusable after re-warm (HTTP {status}, "
            f"head={body[:80]!r})", body=body)
    try:
        rows, rowcnt = parse_envelope(body)
    except EnvelopeError as e:
        raise BseFetchError(f"page {pageno}: {e}", body=body) from e
    return body, rows, rowcnt, url


def _collect_pages(scrip: str, frm: str, to: str, fetch_fn: FetchFn,
                   log: Callable[[str], None]
                   ) -> tuple[dict[str, dict[str, Any]], Optional[int],
                              dict[int, tuple[bytes, str]]]:
    """Paginate until ``len(collected) >= ROWCNT`` or pages exhaust
    (R-BSE-8). A ROWCNT shortfall raises instead of silently truncating.
    Returns ``(collected_by_newsid, last_rowcnt, {pageno: (body, url)})``."""
    collected: dict[str, dict[str, Any]] = {}
    page_meta: dict[int, tuple[bytes, str]] = {}
    rowcnt: Optional[int] = None
    pageno = 1
    while pageno <= MAX_PAGES:
        body, rows, rc, url = _fetch_page(fetch_fn, scrip, frm, to, pageno)
        page_meta[pageno] = (body, url)
        if rowcnt is not None and rc != rowcnt:
            raise BseFetchError(
                f"pagination ROWCNT changed from {rowcnt} to {rc} on page "
                f"{pageno} (contract drift?)", body=body)
        rowcnt = rc
        fresh = 0
        for row in rows:
            key = str(row["NEWSID"])
            if key not in collected:
                collected[key] = row
                fresh += 1
        log(f"[{scrip}] page {pageno}: {len(rows)} rows "
            f"({fresh} new, total {len(collected)}, ROWCNT={rowcnt})")
        if rowcnt is not None and len(collected) >= rowcnt:
            break
        if not rows or fresh == 0:
            break  # API stopped returning rows — treat as exhaustion
        pageno += 1
    if rowcnt is not None and len(collected) < rowcnt:
        raise BseFetchError(
            f"pagination shortfall: ROWCNT={rowcnt} but collected "
            f"{len(collected)} items across {len(page_meta)} pages "
            f"(R-BSE-8: failing loudly instead of silent truncation)")
    return collected, rowcnt, page_meta


def _run_control_query(
    conn: psycopg.Connection, store: ObjectStore, fetch_run_id: int,
    frm: str, to: str, fetch_fn: FetchFn, part: dict[str, Any],
    log: Callable[[str], None]) -> int:
    """R-BSE-9 canary: query a known-active scrip over the same window. Its
    non-empty result proves the endpoint still works, so the target scrip's
    emptiness is genuine. Returns the control artifact id; raises
    BseFetchError when the canary comes back empty/garbled (contract drift)."""
    log(f"[{part['partition_key']}] empty result — running control-scrip "
        f"canary ({CONTROL_SCRIP}) before accepting 'no data'")
    body, rows, rc, url = _fetch_page(fetch_fn, CONTROL_SCRIP, frm, to, 1)
    if not rows:
        raise BseFetchError(
            "control scrip also returned no items — API contract drift "
            "suspected (R-BSE-9); refusing to accept 'no data'")
    sha, path, _ = _persist_bytes(
        conn, store, body, media_type="application/json",
        metadata={"source": "bse_api", "url": url,
                  "role": "control_canary", "scrip": CONTROL_SCRIP})
    artifact_id, _ = _store_artifact(
        conn,
        fetch_run_id=fetch_run_id,
        idem_key=(f"bse_api:{part['partition_key']}:api_control:{sha}"),
        kind="api_response_control",
        media="application/json",
        sha=sha, path=path, size=len(body),
        meta={"url": url, "control_scrip": CONTROL_SCRIP})
    return artifact_id


def _row_matches_scrip(row: dict[str, Any], scrip: str) -> bool:
    """Tier-1 identity guard (R-BSE-5): native scripcode must match the
    partition scrip."""
    cd = row.get("SCRIP_CD")
    return cd is not None and str(cd).strip() == scrip


# --------------------------------------------------------------------- #
# internals: persistence + failure + cursor + downloads
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
        raise BseFetchError(f"artifact row vanished for {idem_key}")
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
                    metadata={"source": "bse_api", "error": summary[:500]})
                _store_artifact(
                    conn,
                    fetch_run_id=fetch_run_id,
                    idem_key=f"bse_api:{partition_id}:err:{sha[:16]}",
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


def _download_one_attachment(
    conn: psycopg.Connection, store: ObjectStore, sess: Optional[BseSession],
    partition_id: int, fetch_run_id: int, partition_key: str,
    content_url: str, news_ids: list[str], *,
    pdf_fetch_fn: PdfFetchFn, log,
) -> str:
    """Download one attachment trying its path variants archive-first
    (R-BSE-10). Valid bytes → ``filing_pdf`` artifact + content object;
    HTML-as-200 garbage → retained as ``filing_pdf_invalid`` evidence with
    ``magic_valid=false`` (R-BSE-11/12); nothing retrievable → failure.
    Returns one of 'downloaded' | 'invalid' | 'failed'."""
    outcome = "failed"
    for candidate in attachment_urls(content_url.rsplit("/", 1)[-1]):
        data, http_status, err = pdf_fetch_fn(sess, candidate)
        if not data:
            # one session-renew retry before giving up on this variant
            if sess is not None:
                try:
                    sess.renew()
                    sess.warm_up()
                except BseFetchError as e:
                    log(f"  DL FAIL {candidate} (re-warm failed: {e})")
                    continue
                data, http_status, err = pdf_fetch_fn(sess, candidate)
        if not data:
            log(f"  DL FAIL {candidate} ({err})")
            continue
        valid = looks_like_pdf(data)
        media = "application/pdf" if valid else "text/html"
        sha, path, _ = _persist_bytes(
            conn, store, data, media_type=media,
            metadata={"source": "bse_api", "url": content_url,
                      "resolved_url": candidate},
            magic_valid=valid)
        kind = "filing_pdf" if valid else "filing_pdf_invalid"
        # Idempotency key is partition-scoped and derived from the resolved
        # URL + the exact downloaded bytes — NOT from the day or news_ids —
        # so a crash-recovery re-download converges on the same row instead
        # of duplicating lineage.
        url_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:16]
        try:
            with conn.transaction():
                _store_artifact(
                    conn,
                    fetch_run_id=fetch_run_id,
                    idem_key=(f"bse_api:{partition_key}:pdf:"
                              f"{url_hash}:{sha[:16]}"),
                    kind=kind,
                    media=media,
                    sha=sha, path=path, size=len(data),
                    meta={"url": content_url, "resolved_url": candidate,
                          "news_ids": news_ids, "magic_valid": valid,
                          "http_status": http_status})
        except psycopg.Error as e:
            log(f"  STORE FAIL {candidate}: {e}")
            continue
        if valid:
            return "downloaded"
        outcome = "invalid"
        log(f"  INVALID-CONTENT (kept as evidence) {candidate}")
    return outcome


def _download_attachments(
    conn: psycopg.Connection, store: ObjectStore, sess: Optional[BseSession],
    partition_id: int, fetch_run_id: int, partition_key: str,
    att_urls: dict[str, list[str]], *,
    pdf_fetch_fn: PdfFetchFn,
    sleep_fn: Callable[[float], None],
    log) -> dict[str, int]:
    """Download each announcement attachment (archive-first variants),
    magic-byte validate, persist exact bytes content-addressed + lineage
    rows. Invalid bytes are retained as evidence. Skips URLs already captured
    by an earlier successful run of this partition. Polite spacing between
    downloads (ban-avoidance engineering, Screener guardrail spirit)."""
    stats = {"downloaded": 0, "skipped": 0, "invalid": 0, "failed": 0}
    for url, news_ids in sorted(att_urls.items()):
        if url_already_captured(conn, partition_id, url):
            stats["skipped"] += 1
            continue
        sleep_fn(PDF_DELAY_SEC)  # ban-avoidance spacing
        outcome = _download_one_attachment(
            conn, store, sess, partition_id, fetch_run_id, partition_key,
            url, news_ids, pdf_fetch_fn=pdf_fetch_fn, log=log)
        stats[outcome] += 1
    return stats


# --------------------------------------------------------------------- #
# job-framework handler (kind family prefix "bse.fetch")
# --------------------------------------------------------------------- #

@jobs.register_handler(JOB_KIND)
@jobs.register_prefix_handler(JOB_KIND + ":")
def handle_bse_fetch(ctx: jobs.JobContext) -> None:
    """Durable-job entrypoint. Payload:
    ``{"partition_id": int}`` (``company_slug`` informational).

    The underlying run is keyed deterministically per partition + schedule
    slot, so crash redeliveries converge instead of duplicating."""
    payload = ctx.payload
    partition_id = payload.get("partition_id")
    if partition_id is None:
        raise BseFetchError("payload missing partition_id")
    stats = run_partition(int(partition_id), day_key=payload.get("slot_key"))
    event_key = f"{JOB_KIND}.completed:{stats['run_idem']}"
    ctx.outbox.append({
        "event_type": "bse.fetch.completed",
        "aggregate_type": "source_partition",
        "aggregate_id": str(partition_id),
        "event_key": event_key,
        "payload": stats,
    })


def enqueue(conn: psycopg.Connection, slug: str, slot_key: str) -> jobs.EnqueueResult:
    """Enqueue one fetch job for a deterministic schedule slot."""
    scrip = COMPANIES[slug].bse_scrip
    with conn.transaction():
        row = conn.execute(
            "SELECT partition_id FROM ingestion.source_partitions "
            "WHERE source='bse_api' AND partition_key=%s",
            (f"scrip:{scrip}",),
        ).fetchone()
        if row is None:
            raise BseFetchError(f"no bse_api partition for {slug}")
        return jobs.enqueue(
            conn,
            job_kind=JOB_KIND,
            idempotency_key=f"bse_api:{scrip}:{slot_key}",
            payload={"company_slug": slug, "partition_id": row[0],
                     "scrip": scrip, "slot_key": slot_key},
            priority=100,
        )


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #

def _cmd_enqueue(args) -> int:
    slugs = list(COMPANIES) if args.all else [args.slug]
    slot_key = capture_slot_key()
    created = 0
    with jobs.connect() as conn:
        with conn.transaction():
            for slug in slugs:
                res = enqueue(conn, slug, slot_key)
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
            LEFT JOIN core.companies co USING (company_id)
            LEFT JOIN ingestion.ingestion_cursors cu USING (partition_id)
            WHERE p.source = 'bse_api'
            ORDER BY co.canonical_name NULLS LAST
            """
        ).fetchall()
    print(f"{'company':45s} {'scrip':10s} {'runs':>4s} {'ok':>4s} "
          f"{'items':>6s}  high_watermark")
    for name, key, runs, ok, items, hw in rows:
        print(f"{(name or '-')[:44]:45s} {key.replace('scrip:', ''):10s} "
              f"{runs:>4d} {ok:>4d} {items:>6d}  {hw}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="BSE capture adapter → DB")
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
