"""Filing downloader → content objects (Phase 0, TODO 7).

Standalone backfill/recovery downloader that turns announcement PDF URLs seen
by the capture adapters into content objects in the immutable object store
with complete DB lineage. It *replaces* the legacy file-only
``scripts/download_pdfs.py`` (which wrote ``data/<slug>/pdfs/*.pdf`` and a
JSON manifest with no DB lineage): every byte now lands in the object store
and is registered in ``documents.content_objects`` before any consumer sees
it, linked from an ``ingestion.raw_artifacts`` row (HLD invariant 1).

Relationship to the capture adapters
------------------------------------
``nse.fetch`` / ``bse.fetch`` already download attachments inline during a
fetch run. This module covers what those runs left behind:

* URLs whose download failed transiently during the capture run;
* URLs captured before PDF downloading existed on the DB path;
* NSE invalid-content URLs, which the NSE adapter deliberately never retries
  but which may serve real bytes later.

It downloads ONLY URLs that no successful artifact of the same partition has
captured yet (``pending_urls``), so running it after a complete capture is a
cheap no-op, and running it concurrently with capture converges: same URL +
same bytes → same sha256 → the SAME content object (registry is UNIQUE on
sha256) and the same partition-scoped artifact idempotency key → one lineage
row. At-least-once redelivery of the same daily job reuses the day's
``fetch_runs`` row instead of duplicating it.

Job/worker pattern
------------------
Registered as durable job kind ``filings.download`` (payload
``{"partition_id": int}``), idempotency-keyed per ``(source, partition, IST
day)`` exactly like the capture adapters. Import via ``scripts/job_handlers``
so workers see it::

    venv/bin/python scripts/job_worker.py --queue filings.download --drain

Lineage written per run (ONE transaction per downloaded URL):

1. bytes → object store blob + ``documents.content_objects`` row
   (sha256 / storage_path / media_type / byte_size / magic_valid);
2. ``ingestion.raw_artifacts`` row (kind ``filing_pdf``, or
   ``filing_pdf_invalid`` for retained HTML-as-200 evidence) referencing the
   registered sha256/storage_path — the "link from raw artifacts";
3. the run's ``ingestion.fetch_runs`` row finalized (ok/partial/failed).

Magic-byte validation decides media_type/kind at insert time (HTTP 200 proves
nothing; BSE AttachLive trap). Invalid bytes are kept as evidence, never
discarded. Polite spacing between downloads is ban-avoidance engineering,
same spirit as the Screener guardrail.

Testability: HTTP is injectable (``fetch_fn(source, url)``), as are clock,
sleep and store/connection — see ``tests/test_download_filings.py``, which
drives the full cycle against the live dev DB with recorded fake bytes.

CLI
---
    venv/bin/python scripts/download_filings.py enqueue [--slug S | --all]
    venv/bin/python scripts/download_filings.py run [--slug S | --partition N | --all]
    venv/bin/python scripts/download_filings.py status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent))

import jobs  # noqa: E402  (handler registration via @jobs.register_handler)
from companies import COMPANIES  # noqa: E402

# Capture adapters provide the impersonated sessions, magic-byte check and
# attachment-path candidates; importing them also registers their handlers.
import nse_capture  # noqa: E402
import bse_capture  # noqa: E402
from object_store import ObjectStore  # noqa: E402

JOB_KIND = "filings.download"
PDF_DELAY_SEC = 0.4             # ban-avoidance spacing between downloads
SOURCES = ("nse_api", "bse_api")  # sources this downloader understands

IST = timezone(timedelta(hours=5, minutes=30))

#: ``(source, url) -> (data | None, http_status | None, err, resolved_url)``
#: ``resolved_url`` records which path variant actually served the bytes
#: (BSE tries AttachHis then AttachLive; identical to the original otherwise).
FetchFn = Callable[[str, str],
                   tuple[Optional[bytes], Optional[int], str, Optional[str]]]

Log = Callable[[str], None]


class FilingDownloadError(RuntimeError):
    """Partition-level failure of a filing-download run."""


def ist_day_key(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(IST)).astimezone(IST).strftime("%Y%m%d")


# --------------------------------------------------------------------- #
# default HTTP layer (real sessions, lazily built per source)
# --------------------------------------------------------------------- #

class _RealFetcher:
    """Lazily-constructed impersonated sessions per source; renew-on-garbage
    retry-once semantics mirror the capture adapters (R-NSE-12 / R-BSE-10)."""

    def __init__(self):
        self._nse = None
        self._bse = None

    def _nse_sess(self):
        if self._nse is None:
            self._nse = nse_capture.NseSession()
        return self._nse

    def _bse_sess(self):
        if self._bse is None:
            sess = bse_capture.BseSession()
            sess.warm_up()
            self._bse = sess
        return self._bse

    def fetch(self, source: str, url: str):
        if source == "nse_api":
            pdf = nse_capture.make_pdf_fetch_fn(self._nse_sess())
            data, status, err = pdf(None, url)
            if not data and self._nse is not None:
                try:
                    self._nse.renew()
                except nse_capture.NseFetchError as e:
                    return None, status, f"re-warm failed: {e}", url
                data, status, err = pdf(None, url)
            return data, status, err, url
        if source == "bse_api":
            pdf = bse_capture.make_pdf_fetch_fn(self._bse_sess())
            filename = url.rsplit("/", 1)[-1]
            last_status, last_err = None, "no candidate served bytes"
            for candidate in bse_capture.attachment_urls(filename):
                data, status, err = pdf(self._bse_sess(), candidate)
                if data:
                    return data, status, err, candidate
                last_status, last_err = status, err
                if self._bse is not None:
                    try:
                        self._bse.renew()
                        self._bse.warm_up()
                    except bse_capture.BseFetchError as e:
                        return None, status, f"re-warm failed: {e}", candidate
                    data, status, err = pdf(self._bse_sess(), candidate)
                    if data:
                        return data, status, err, candidate
                last_status, last_err = status, err
            return None, last_status, last_err, None
        raise FilingDownloadError(f"source {source!r} has no downloader")


def _default_fetch_fn() -> FetchFn:
    return _RealFetcher().fetch


# --------------------------------------------------------------------- #
# pending-URL selection (convergence point)
# --------------------------------------------------------------------- #

def pending_urls(conn: psycopg.Connection, partition_id: int) -> list[str]:
    """Announcement PDF URLs seen by this partition's feed items that no
    successful artifact of this partition has captured yet. Both valid PDFs
    AND retained invalid-content evidence count as handled (re-fetching
    known garbage adds nothing; BSE adapter semantics)."""
    rows = conn.execute(
        """
        SELECT DISTINCT i.content_url
        FROM ingestion.raw_feed_items i
        WHERE i.partition_id = %s
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
        (partition_id, partition_id),
    ).fetchall()
    return [r[0] for r in rows]


# --------------------------------------------------------------------- #
# persistence helpers (same shape as the capture adapters)
# --------------------------------------------------------------------- #

def _store_artifact(conn: psycopg.Connection, *, fetch_run_id: int,
                    idem_key: str, kind: str, media: str, sha: str,
                    path: str, size: int, meta: dict[str, Any]) -> tuple[int, int]:
    """Insert a raw_artifacts row (deduped across runs by its unique
    idempotency key) and return ``(artifact_id, first_seen_run_id)``."""
    with conn.transaction():  # savepoint: unique violation must not abort us
        row = conn.execute(
            """
            INSERT INTO ingestion.raw_artifacts
                (fetch_run_id, idempotency_key, artifact_kind, content_type,
                 sha256, storage_path, byte_size, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING artifact_id, fetch_run_id
            """,
            (fetch_run_id, idem_key, kind, media, sha, path, size, Jsonb(meta)),
        ).fetchone()
    if row is not None:
        return int(row[0]), int(row[1])
    row = conn.execute(
        "SELECT artifact_id, fetch_run_id FROM ingestion.raw_artifacts "
        "WHERE idempotency_key = %s",
        (idem_key,),
    ).fetchone()
    if row is None:
        raise FilingDownloadError(f"artifact row vanished for {idem_key}")
    return int(row[0]), int(row[1])


def _get_or_create_run(conn: psycopg.Connection, partition_id: int,
                       run_idem: str, items_seen: int) -> tuple[int, bool]:
    """The day's run row, created on first use — at-least-once redelivery of
    the same job attaches to the existing run instead of duplicating it."""
    row = conn.execute(
        "SELECT fetch_run_id FROM ingestion.fetch_runs "
        "WHERE idempotency_key = %s",
        (run_idem,),
    ).fetchone()
    if row is not None:
        return int(row[0]), False
    row = conn.execute(
        """
        INSERT INTO ingestion.fetch_runs
            (partition_id, idempotency_key, started_at, status, items_seen,
             config)
        VALUES (%s, %s, now(), 'running', %s, %s)
        RETURNING fetch_run_id
        """,
        (partition_id, run_idem, items_seen,
         Jsonb({"job_kind": JOB_KIND})),
    ).fetchone()
    return int(row[0]), True


def _finalize_run(conn: psycopg.Connection, fetch_run_id: int,
                  items_seen: int, items_new: int, failures: int) -> str:
    if items_seen == 0:
        status = "not_modified"
    elif failures >= items_seen:
        status = "failed"
    elif failures > 0:
        status = "partial"
    else:
        status = "ok"
    conn.execute(
        """
        UPDATE ingestion.fetch_runs
        SET finished_at = now(), status = %s,
            items_seen = GREATEST(items_seen, %s),
            items_new = %s,
            error_summary = CASE WHEN %s > 0
                THEN %s || ' download(s) failed' ELSE NULL END
        WHERE fetch_run_id = %s
        """,
        (status, items_seen, items_new, failures, failures, fetch_run_id),
    )
    return status


# --------------------------------------------------------------------- #
# the run itself
# --------------------------------------------------------------------- #

def run_partition(
    partition_id: int,
    *,
    conn: Optional[psycopg.Connection] = None,
    store: Optional[ObjectStore] = None,
    fetch_fn: Optional[FetchFn] = None,
    now: Optional[datetime] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    quiet: bool = False,
) -> dict[str, Any]:
    """Download every pending announcement PDF of one partition into the
    object store + registry, with full lineage.

    Returns a stats dict; ``skipped: True`` means nothing was pending (no
    fetch_run is created for a pure no-op). All collaborators are injectable
    for tests: ``conn`` (DB — opens+commits its own when omitted), ``store``,
    ``fetch_fn`` (HTTP), ``now`` (clock), ``sleep_fn`` (spacing), ``quiet``.
    """
    log: Log = (lambda m: None) if quiet else (lambda m: print(m, flush=True))
    store = store or ObjectStore.from_env()
    fetch = fetch_fn or _default_fetch_fn()
    day_key = ist_day_key(now)

    if conn is not None:
        return _run_impl(conn, store, fetch, partition_id, day_key,
                         sleep_fn=sleep_fn, log=log)
    with jobs.connect() as own_conn:
        try:
            stats = _run_impl(own_conn, store, fetch, partition_id,
                              day_key, sleep_fn=sleep_fn, log=log)
            own_conn.commit()
            return stats
        except Exception:
            own_conn.commit()  # keep any partial lineage as evidence
            raise


def _run_impl(conn: psycopg.Connection, store: ObjectStore, fetch: FetchFn,
              partition_id: int, day_key: str, *, sleep_fn, log) -> dict[str, Any]:
    part = conn.execute(
        """
        SELECT p.partition_id, p.source, p.partition_key, p.is_active
        FROM ingestion.source_partitions p
        WHERE p.partition_id = %s
        """,
        (partition_id,),
    ).fetchone()
    if part is None:
        raise FilingDownloadError(f"partition {partition_id} not found")
    _, source, partition_key, _active = part

    urls = pending_urls(conn, partition_id)
    if not urls:
        return {"partition_id": partition_id, "source": source,
                "status": "not_modified", "skipped": True, "day": day_key,
                "downloaded": 0, "invalid": 0, "skipped_urls": 0, "failed": 0}

    if source not in SOURCES:
        raise FilingDownloadError(
            f"source {source!r} is not a filing source ({', '.join(SOURCES)})")

    run_idem = f"{JOB_KIND}:{source}:{partition_id}:{day_key}"
    fetch_run_id, _created = _get_or_create_run(
        conn, partition_id, run_idem, len(urls))

    stats = {"partition_id": partition_id, "source": source,
             "status": "running", "skipped": False, "day": day_key,
             "run_idem": run_idem, "fetch_run_id": fetch_run_id,
             "items_seen": len(urls),
             "downloaded": 0, "invalid": 0, "skipped_urls": 0, "failed": 0}
    failures = 0

    for url in sorted(urls):
        sleep_fn(PDF_DELAY_SEC)
        data, http_status, err, resolved = fetch(source, url)
        if not data:
            failures += 1
            stats["failed"] += 1
            log(f"  DL FAIL {url} ({err})")
            continue
        valid = nse_capture.looks_like_pdf(data)
        media = "application/pdf" if valid else "text/html"
        try:
            with conn.transaction():
                # bytes → blob + registry FIRST (idempotent on sha256)…
                result = store.put_bytes(
                    data, media_type=media,
                    metadata={"source": source, "url": url,
                              **({"resolved_url": resolved} if resolved else {})},
                    conn=conn, magic_valid=valid)
                # …then the raw_artifacts row linking to it. The key is
                # partition-scoped and derived from the URL + exact bytes, so
                # crash-recovery re-downloads converge on the same row and
                # cross-module double-capture (adapter + downloader) cannot
                # duplicate the content object either.
                url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
                _store_artifact(
                    conn,
                    fetch_run_id=fetch_run_id,
                    idem_key=(f"filing_dl:{partition_key}:pdf:"
                              f"{url_hash}:{result.sha256[:16]}"),
                    kind="filing_pdf" if valid else "filing_pdf_invalid",
                    media=media,
                    sha=result.sha256, path=result.storage_path,
                    size=len(data),
                    meta={"url": url,
                          **({"resolved_url": resolved} if resolved else {}),
                          "downloader": JOB_KIND, "magic_valid": valid,
                          "http_status": http_status})
        except psycopg.Error as e:
            failures += 1
            stats["failed"] += 1
            log(f"  STORE FAIL {url}: {e}")
            continue
        if valid:
            stats["downloaded"] += 1
            log(f"  OK {'NEW' if result.created else 'dedup'} "
                f"{result.sha256[:12]} {url}")
        else:
            stats["invalid"] += 1
            log(f"  INVALID-CONTENT (kept as evidence) {url}")

    stats["status"] = _finalize_run(conn, fetch_run_id, len(urls),
                                    stats["downloaded"] + stats["invalid"],
                                    failures)
    return stats


# --------------------------------------------------------------------- #
# job-framework handler (kind family prefix "filings.download")
# --------------------------------------------------------------------- #

@jobs.register_handler(JOB_KIND)
@jobs.register_prefix_handler(JOB_KIND + ":")
def handle_filings_download(ctx: jobs.JobContext) -> None:
    """Durable-job entrypoint. Payload: ``{"partition_id": int}``
    (``company_slug``/``source`` informational). Deterministic outbox key so
    at-least-once redelivery cannot duplicate the completion event."""
    payload = ctx.payload
    partition_id = payload.get("partition_id")
    if partition_id is None:
        raise FilingDownloadError("payload missing partition_id")
    stats = run_partition(int(partition_id))
    event_key = f"{JOB_KIND}.completed:{stats.get('run_idem') or stats['day']}"
    ctx.outbox.append({
        "event_type": "filings.download.completed",
        "aggregate_type": "source_partition",
        "aggregate_id": str(partition_id),
        "event_key": event_key,
        "payload": stats,
    })


# --------------------------------------------------------------------- #
# enqueue API
# --------------------------------------------------------------------- #

def partitions_for_slug(conn: psycopg.Connection, slug: str) -> list[dict[str, Any]]:
    """All active filing partitions (NSE + BSE) of one company slug."""
    company = COMPANIES[slug]
    rows = conn.execute(
        """
        SELECT partition_id, source, partition_key
        FROM ingestion.source_partitions
        WHERE is_active
          AND ((source = 'nse_api' AND partition_key = %s)
            OR (source = 'bse_api' AND partition_key = %s))
        ORDER BY source
        """,
        (f"symbol:{company.nse_symbol}", f"scrip:{company.bse_scrip}"),
    ).fetchall()
    return [{"partition_id": r[0], "source": r[1], "partition_key": r[2]}
            for r in rows]


def all_filing_partitions(conn: psycopg.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT partition_id FROM ingestion.source_partitions "
        "WHERE is_active AND source = ANY(%s) ORDER BY partition_id",
        (list(SOURCES),),
    ).fetchall()
    return [r[0] for r in rows]


def enqueue(conn: psycopg.Connection, slug: str,
            day_key: Optional[str] = None) -> list[jobs.EnqueueResult]:
    """Enqueue the daily download job(s) for one slug (idempotent per day)."""
    day_key = day_key or ist_day_key()
    results = []
    with conn.transaction():
        for part in partitions_for_slug(conn, slug):
            res = jobs.enqueue(
                conn,
                job_kind=JOB_KIND,
                idempotency_key=f"{part['source']}:{part['partition_id']}:{day_key}",
                payload={"company_slug": slug,
                         "partition_id": part["partition_id"],
                         "source": part["source"]},
                priority=100,
            )
            results.append(res)
    return results


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #

def _select_partitions(args) -> list[int]:
    with jobs.connect() as conn:
        if args.partition:
            return [args.partition]
        if args.slug:
            return [p["partition_id"] for p in partitions_for_slug(conn, args.slug)]
        return all_filing_partitions(conn)


def _cmd_enqueue(args) -> int:
    slugs = list(COMPANIES) if args.all else [args.slug]
    day_key = ist_day_key()
    created = 0
    with jobs.connect() as conn:
        for slug in slugs:
            for res in enqueue(conn, slug, day_key):
                created += 1 if res.created else 0
                print(f"{'enqueued' if res.created else 'exists '} {slug} "
                      f"(job {res.job_id})")
    print(f"\n{created} new jobs (of {len(slugs)} slugs requested). Drain with: "
          f"venv/bin/python scripts/job_worker.py --queue {JOB_KIND} --drain")
    return 0


def _cmd_run(args) -> int:
    partition_ids = _select_partitions(args)
    if not partition_ids:
        print("no matching active filing partitions")
        return 1
    rc = 0
    for pid in partition_ids:
        stats = run_partition(pid)
        print(json.dumps(stats, indent=2, default=str))
        if stats.get("status") not in ("ok", "not_modified"):
            rc = 1
    return rc


def _cmd_status(_args) -> int:
    with jobs.connect() as conn:
        rows = conn.execute(
            """
            SELECT p.source, co.canonical_name, p.partition_key,
                   (SELECT count(*) FROM ingestion.raw_feed_items i
                     WHERE i.partition_id = p.partition_id
                       AND i.content_url LIKE 'http%%'
                       AND NOT EXISTS (
                           SELECT 1 FROM ingestion.raw_artifacts a
                           JOIN ingestion.fetch_runs r USING (fetch_run_id)
                           WHERE r.partition_id = p.partition_id
                             AND a.artifact_kind IN ('filing_pdf',
                                                     'filing_pdf_invalid')
                             AND a.metadata->>'url' = i.content_url)
                   ) AS pending,
                   (SELECT count(*) FROM ingestion.raw_artifacts a
                     JOIN ingestion.fetch_runs r USING (fetch_run_id)
                     WHERE r.partition_id = p.partition_id
                       AND a.artifact_kind = 'filing_pdf') AS pdfs
            FROM ingestion.source_partitions p
            LEFT JOIN core.companies co USING (company_id)
            WHERE p.is_active AND p.source = ANY(%s)
            ORDER BY pending DESC, p.source, co.canonical_name
            """,
            (list(SOURCES),),
        ).fetchall()
    total_pending = 0
    print(f"{'source':10s} {'company':45s} {'key':18s} {'pdfs':>5s} {'pending':>8s}")
    for source, name, key, pending, pdfs in rows:
        total_pending += pending
        print(f"{source:10s} {(name or '-')[:44]:45s} {key[:17]:18s} "
              f"{pdfs:>5d} {pending:>8d}")
    print(f"\ntotal pending: {total_pending}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Filing downloader → object-store content objects")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_enq = sub.add_parser("enqueue", help="enqueue durable download jobs")
    p_enq.add_argument("--slug")
    p_enq.add_argument("--all", action="store_true")

    p_run = sub.add_parser("run", help="download pending filings synchronously")
    p_run.add_argument("--slug")
    p_run.add_argument("--partition", type=int)
    p_run.add_argument("--all", action="store_true")

    sub.add_parser("status", help="per-partition pending/PDF counts")

    args = parser.parse_args(argv)
    if args.cmd == "enqueue":
        if not args.all and not args.slug:
            parser.error("enqueue needs --slug or --all")
        return _cmd_enqueue(args)
    if args.cmd == "run":
        if not args.all and not args.slug and not args.partition:
            parser.error("run needs --slug, --partition or --all")
        return _cmd_run(args)
    return _cmd_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
