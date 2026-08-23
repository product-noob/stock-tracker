"""Normalizer (M2) — raw feed items → canonical normalized records (Phase 0,
ROADMAP action 3).

Implements the M2 capability card in ``docs/architecture-hld.md`` §5:
adapter-independent source-record shape, canonical source-record idempotency
and append-only versioning.

Behaviour (per HLD + ``db/schema.sql``):

* reads **un-normalized** raw feed items from ``ingestion.raw_feed_items``
  (those with no row yet in ``ingestion.record_raw_items``), joined to their
  partition to learn the source;
* maps NSE (``raw_payload`` from ``nse_api``) and BSE (``bse_api``)
  announcement shapes onto one canonical record shape. Exchange category is
  preserved as metadata only — never used as event classification (HLD
  invariant 6);
* identity is ``(dedup_scope, dedup_key)`` — the native source id, NOT the
  document URL (M2 card). Every duplicate sighting links to the one canonical
  record through ``ingestion.record_raw_items``, so at-least-once capture can
  never duplicate logical records;
* **append-only versioning:** ``normalized_records`` rows are immutable
  (trigger ``trg_guard_normalized_records``). If a later sighting of the same
  native id carries *changed* canonical content (title/subject/description/
  published_at/content_url/... changed upstream), the normalizer appends a
  NEW record with an incremented revision suffix (``<id>@v2``, ``@v3``...)
  instead of mutating. Unchanged re-sightings are idempotent no-ops that only
  add the sighting link;
* **fail closed:** unknown/invalid shapes raise :class:`NormalizationError`
  and are recorded in ``ingestion.stage_errors`` under a ``normalize``
  ``stage_run`` — quarantined and audit-visible, never silently dropped.
  Ambiguous input never becomes a guessed record;
* every created record emits an outbox event ``normalized_record.created``
  keyed by the record id (HLD job/outbox contract C2).

Identity resolution (company attribution) deliberately does NOT happen here —
that is M3's job; records stay unresolved with ``company_id = NULL``.

Testability: normalization itself is pure (``normalize_item`` +
``NormalizationError``); the DB run takes an injectable connection/clock like
the capture adapters (see ``tests/test_normalize.py``).

CLI
---
    venv/bin/python scripts/normalize.py run [--limit N] [--source nse_api]
    venv/bin/python scripts/normalize.py status

Or through the durable queue (handler kind ``normalize.run``):
    venv/bin/python scripts/job_worker.py --queue normalize.run --drain
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent))

import jobs  # noqa: E402  (handler registration via @jobs.register_handler)

ROOT = Path(__file__).resolve().parent.parent

#: bumped when mapping rules change in a way that affects fingerprints;
#: recorded on every stage_run for auditability
NORM_VERSION = "norm.v1"

STAGE_NAME = "normalize"
JOB_KIND = "normalize.run"

IST = timezone(timedelta(hours=5, minutes=30))

# Canonical fields compared for change detection (revision bump on drift).
# Everything the normalizer derives from raw_payload except external_id
# (which forms the base identity key).
_CANONICAL_FIELDS = (
    "record_type", "exchange", "company_name_raw", "exchange_symbol_raw",
    "exchange_code_raw", "isin_raw", "title", "subject", "description",
    "category_raw", "content_url", "published_at",
)


class NormalizationError(ValueError):
    """A raw item could not be normalized — fail-closed quarantine."""


# --------------------------------------------------------------------- #
# pure helpers (unit-tested)
# --------------------------------------------------------------------- #

def parse_an_dt(value: Any) -> Optional[datetime]:
    """NSE ``an_dt`` (%d-%b-%Y %H:%M:%S, IST assumed) → aware UTC datetime.
    Missing/unparseable → None (R-NSE-19: store null, keep raw)."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%d-%b-%Y %H:%M:%S") \
            .replace(tzinfo=IST).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_news_dt(value: Any) -> Optional[datetime]:
    """BSE ``NEWS_DT`` (ISO with optional fractional seconds, or
    %d-%b-%Y %H:%M:%S; IST assumed) → aware UTC datetime, else None."""
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


def _clean(value: Any) -> Optional[str]:
    """Trimmed non-empty string or None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def detect_shape(source: str, payload: Any) -> dict[str, Any]:
    """Fail-closed shape gate: payload must be a JSON object carrying the
    source's mandatory native id. Anything else is a NormalizationError
    (quarantine), never a guessed record."""
    if source not in ("nse_api", "bse_api"):
        raise NormalizationError(f"unknown source {source!r}: no adapter")
    if not isinstance(payload, dict):
        raise NormalizationError(
            f"raw_payload must be a JSON object, got {type(payload).__name__}")
    if source == "nse_api":
        missing = [k for k in ("seq_id",) if payload.get(k) in (None, "")]
        if missing:
            raise NormalizationError(f"nse item missing {missing[0]}")
    else:
        if payload.get("NEWSID") in (None, ""):
            raise NormalizationError("bse item missing NEWSID")
    return payload


def normalize_item(source: str, payload: Any) -> dict[str, Any]:
    """Map one raw payload onto the canonical record fields.

    Raises :class:`NormalizationError` for unknown/malformed shapes.
    Returns a dict with ``external_id`` plus the ``_CANONICAL_FIELDS``.
    """
    payload = detect_shape(source, payload)

    if source == "nse_api":
        seq_id = payload.get("seq_id")
        att = _clean(payload.get("attchmntFile"))
        return {
            "external_id": str(seq_id).strip(),
            "record_type": "exchange_announcement",
            "exchange": "NSE",
            "company_name_raw": _clean(payload.get("sm_name")),
            "exchange_symbol_raw": _clean(payload.get("symbol")),
            "exchange_code_raw": None,
            "isin_raw": _clean(payload.get("sm_isin")),
            # NSE has no headline column: sm_name names the company, desc /
            # attchmntText carry subject + description (pilot-proven mapping).
            "title": _clean(payload.get("sm_name")),
            "subject": _clean(payload.get("desc")),
            "description": _clean(payload.get("attchmntText")),
            "category_raw": None,
            "content_url": att if att and att.startswith(("http://",
                                                          "https://")) else None,
            "published_at": parse_an_dt(payload.get("an_dt")),
        }

    news_id = payload.get("NEWSID")
    attachment = _clean(payload.get("ATTACHMENTNAME"))
    return {
        "external_id": str(news_id).strip(),
        "record_type": "exchange_announcement",
        "exchange": "BSE",
        "company_name_raw": _clean(payload.get("SLONGNAME")),
        "exchange_symbol_raw": None,
        "exchange_code_raw": _clean(payload.get("SCRIP_CD")),
        "isin_raw": None,  # BSE API payload carries no ISIN (product note §7)
        "title": _clean(payload.get("SLONGNAME")),
        "subject": _clean(payload.get("CATEGORYNAME")),
        "description": _clean(payload.get("HEADLINE")),
        "category_raw": _clean(payload.get("CATEGORYNAME")),
        "content_url": (
            f"https://www.bseindia.com/xml-data/corpfiling/AttachHis/"
            f"{attachment}"
            if attachment else None),
        "published_at": parse_news_dt(payload.get("NEWS_DT")),
    }


def dedup_scope_for(source: str) -> str:
    """Scope = the capture source; native ids are unique within it."""
    return source


def base_key(norm: dict[str, Any]) -> str:
    """Base (revision-less) identity key: the native external id."""
    return norm["external_id"]


def revision_key(base: str, revision: int) -> str:
    return f"{base}@v{revision}"


def fingerprint(norm: dict[str, Any]) -> str:
    """Stable content hash over the canonical fields (change detection).
    Datetimes render in a fixed UTC format so equal instants hash equal."""
    material = {}
    for field in _CANONICAL_FIELDS:
        value = norm.get(field)
        if isinstance(value, datetime):
            value = value.astimezone(timezone.utc) \
                .strftime("%Y-%m-%dT%H:%M:%S%z")
        material[field] = value
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- #
# DB plumbing
# --------------------------------------------------------------------- #

_PENDING_SQL = """
SELECT i.item_id, i.external_id, i.idempotency_key, p.source AS part_source
FROM ingestion.raw_feed_items i
JOIN ingestion.source_partitions p USING (partition_id)
WHERE NOT EXISTS (
    SELECT 1 FROM ingestion.record_raw_items rri
    WHERE rri.item_id = i.item_id)
ORDER BY i.item_id
"""


def fetch_pending(conn: psycopg.Connection, *,
                  limit: Optional[int] = None,
                  source: Optional[str] = None) -> list[dict[str, Any]]:
    """Raw items with no canonical linkage yet (oldest first)."""
    sql, params = _PENDING_SQL, []
    if source:
        sql += " WHERE p.source = %s"
        params.append(source)
    sql += " LIMIT %s"
    params.append(limit if limit is not None else 1000000)
    rows = conn.execute(sql, params).fetchall()
    keys = ["item_id", "external_id", "idempotency_key", "part_source"]
    return [dict(zip(keys, r)) for r in rows]


def latest_revision(conn: psycopg.Connection, scope: str, base: str) \
        -> Optional[dict[str, Any]]:
    """Highest-revision existing record for this (scope, base id), if any."""
    row = conn.execute(
        """
        SELECT record_id, dedup_key,
               regexp_replace(dedup_key, '^.*@v', '')::int AS revision,
               record_type, exchange, company_name_raw, exchange_symbol_raw,
               exchange_code_raw, isin_raw, title, subject, description,
               category_raw, content_url, published_at
        FROM ingestion.normalized_records
        WHERE dedup_scope = %s AND dedup_key ~ ('^' || %s || '@v[0-9]+$')
        ORDER BY revision DESC
        LIMIT 1
        """,
        (scope, base),
    ).fetchone()
    if row is None:
        return None
    keys = ["record_id", "dedup_key", "revision"] + list(_CANONICAL_FIELDS)
    return dict(zip(keys, row))


def link_sighting(conn: psycopg.Connection, record_id: int, item_id: int) \
        -> bool:
    """Attach a raw sighting to its canonical record (idempotent)."""
    row = conn.execute(
        """
        INSERT INTO ingestion.record_raw_items (record_id, item_id)
        VALUES (%s, %s)
        ON CONFLICT (record_id, item_id) DO NOTHING
        RETURNING record_id
        """,
        (record_id, item_id),
    ).fetchone()
    return row is not None


def insert_record(conn: psycopg.Connection, scope: str, norm: dict[str, Any],
                  revision: int) -> int:
    """Append one canonical record row (immutable once committed)."""
    row = conn.execute(
        """
        INSERT INTO ingestion.normalized_records
            (record_type, dedup_scope, dedup_key, exchange, external_id,
             company_name_raw, exchange_symbol_raw, exchange_code_raw,
             isin_raw, title, subject, description, category_raw,
             content_url, published_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING record_id
        """,
        (norm["record_type"], scope, revision_key(norm["external_id"],
                                                  revision),
         norm["exchange"], norm["external_id"], norm["company_name_raw"],
         norm["exchange_symbol_raw"], norm["exchange_code_raw"],
         norm["isin_raw"], norm["title"], norm["subject"],
         norm["description"], norm["category_raw"], norm["content_url"],
         norm["published_at"]),
    ).fetchone()
    return int(row[0])


def record_matches(record: dict[str, Any], norm: dict[str, Any]) -> bool:
    """True if the stored immutable record's canonical fields equal what the
    current raw payload would produce (→ duplicate sighting, no new version)."""
    for field in _CANONICAL_FIELDS:
        if record[field] != norm[field]:
            return False
    return True


def quarantine(conn: psycopg.Connection, stage_run_id: int, item_id: int,
               key: str, exc: Exception) -> int:
    """Record an un-normalizable item in stage_errors (fail-closed)."""
    row = conn.execute(
        """
        INSERT INTO ingestion.stage_errors
            (stage_run_id, entity_type, entity_id, error_code, message,
             detail)
        VALUES (%s, 'raw_feed_item', %s, 'UNNORMALIZABLE_SHAPE', %s, %s)
        RETURNING error_id
        """,
        (stage_run_id, str(item_id),
         f"item {key}: {exc}",
         Jsonb({"idempotency_key": key})),
    ).fetchone()
    return int(row[0])


# --------------------------------------------------------------------- #
# the run itself
# --------------------------------------------------------------------- #

def run_normalize(
    conn: psycopg.Connection,
    *,
    limit: Optional[int] = None,
    source: Optional[str] = None,
    now: Optional[datetime] = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Normalize all pending raw feed items into canonical records.

    One ``ingestion.stage_runs`` row per batch; per-item SAVEPOINTs so a bad
    item cannot poison the batch. Idempotent end-to-end: already-linked items
    are never selected again, record inserts conflict on the revision key,
    and sighting links are ON CONFLICT DO NOTHING.

    Returns a stats dict: items_in / records_created / duplicates_linked /
    versions_appended / errors / stage_run_id.
    """
    log = (lambda m: None) if quiet else (lambda m: print(m, flush=True))
    pending = fetch_pending(conn, limit=limit, source=source)
    batch_max = max((p["item_id"] for p in pending), default=0)
    batch_key = f"{NORM_VERSION}:{batch_max}" + (f":{source}" if source else "")

    stats = {"items_in": len(pending), "records_created": 0,
             "duplicates_linked": 0, "versions_appended": 0, "errors": 0}

    with conn.transaction():
        row = conn.execute(
            """
            INSERT INTO ingestion.stage_runs
                (stage_name, idempotency_key, code_version, status)
            VALUES (%s, %s, %s, 'running')
            ON CONFLICT (stage_name, idempotency_key) DO UPDATE SET
                started_at = now(), status = 'running',
                finished_at = NULL
            RETURNING stage_run_id
            """,
            (STAGE_NAME, batch_key, NORM_VERSION),
        ).fetchone()
        stage_run_id = int(row[0])

    log(f"[{STAGE_NAME}] stage_run {stage_run_id}: "
        f"{stats['items_in']} pending item(s)"
        f"{' (source=' + source + ')' if source else ''}")

    for pending_item in pending:
        item_id = pending_item["item_id"]
        try:
            # Re-read inside the loop so concurrent workers cannot double-
            # process: skip items linked since the snapshot was taken.
            already = conn.execute(
                "SELECT 1 FROM ingestion.record_raw_items WHERE item_id=%s",
                (item_id,),
            ).fetchone()
            if already:
                continue
            with conn.transaction():  # savepoint per item
                _process_one(conn, stage_run_id, pending_item, stats, log)
        except NormalizationError as exc:
            with conn.transaction():
                quarantine(conn, stage_run_id, item_id,
                           pending_item["idempotency_key"], exc)
            stats["errors"] += 1
            log(f"  QUARANTINE item {item_id} "
                f"({pending_item['idempotency_key']}): {exc}")

    with conn.transaction():
        conn.execute(
            """
            UPDATE ingestion.stage_runs
            SET finished_at = now(),
                status = CASE WHEN %s > 0 THEN 'partial' ELSE 'ok' END,
                items_in = %s, items_out = %s, errors = %s,
                metrics = %s
            WHERE stage_run_id = %s
            """,
            (stats["errors"], stats["items_in"],
             stats["records_created"] + stats["versions_appended"]
             + stats["duplicates_linked"],
             stats["errors"], Jsonb(stats), stage_run_id),
        )
    log(f"[{STAGE_NAME}] done: {stats['records_created']} created, "
        f"{stats['versions_appended']} versioned, "
        f"{stats['duplicates_linked']} dup-linked, {stats['errors']} errors")
    stats["stage_run_id"] = stage_run_id
    return stats


def _process_one(conn: psycopg.Connection, stage_run_id: int,
                 pending_item: dict[str, Any], stats: dict[str, int],
                 log) -> None:
    """Normalize one pending item and write the canonical effect. Runs inside
    a savepoint transaction; NormalizationError propagates to quarantine."""
    item_id = pending_item["item_id"]

    row = conn.execute(
        "SELECT raw_payload FROM ingestion.raw_feed_items WHERE item_id=%s",
        (item_id,),
    ).fetchone()
    if row is None or row[0] is None:
        raise NormalizationError("raw_payload missing (fragment-only item "
                                 "has no supported adapter)")
    norm = normalize_item(pending_item["part_source"], row[0])

    scope = dedup_scope_for(pending_item["part_source"])
    base = base_key(norm)
    latest = latest_revision(conn, scope, base)

    if latest is None:
        record_id = insert_record(conn, scope, norm, revision=1)
        link_sighting(conn, record_id, item_id)
        emit_created(conn, record_id, revision=1)
        stats["records_created"] += 1
        log(f"  v1 record {record_id} ← item {item_id} ({base})")
        return

    if record_matches(latest, norm):
        if link_sighting(conn, latest["record_id"], item_id):
            stats["duplicates_linked"] += 1
            log(f"  dup sighting item {item_id} → record "
                f"{latest['record_id']}")
        return

    # Source content changed upstream → append the next immutable version.
    revision = latest["revision"] + 1
    record_id = insert_record(conn, scope, norm, revision=revision)
    link_sighting(conn, record_id, item_id)
    emit_created(conn, record_id, revision=revision,
                 supersedes=latest["record_id"])
    stats["versions_appended"] += 1
    log(f"  v{revision} record {record_id} ← item {item_id} "
        f"(supersedes {latest['record_id']})")


def emit_created(conn: psycopg.Connection, record_id: int, *, revision: int,
                 supersedes: Optional[int] = None) -> None:
    """Outbox event per HLD contract C2 (idempotent by event_key)."""
    jobs.emit_outbox(
        conn,
        event_type="normalized_record.created",
        aggregate_type="normalized_record",
        aggregate_id=str(record_id),
        event_key=f"normalized_record.created:{record_id}",
        payload={"record_id": record_id, "revision": revision,
                 "supersedes_record_id": supersedes,
                 "code_version": NORM_VERSION},
    )


# --------------------------------------------------------------------- #
# job-framework handler
# --------------------------------------------------------------------- #

@jobs.register_handler(JOB_KIND)
@jobs.register_prefix_handler(JOB_KIND + ":")
def handle_normalize(ctx: jobs.JobContext) -> None:
    """Durable-job entrypoint. Payload: ``{"limit": int?, "source": str?}``.

    The underlying writes are idempotent (pending-selection + unique keys),
    so redeliveries converge instead of duplicating."""
    payload = ctx.payload or {}
    with jobs.connect(ctx.db_url) as conn:
        stats = run_normalize(
            conn,
            limit=payload.get("limit"),
            source=payload.get("source"),
            quiet=True,
        )
    ctx.outbox.append({
        "event_type": "normalize.batch.completed",
        "aggregate_type": "stage_run",
        "aggregate_id": str(stats["stage_run_id"]),
        "event_key": f"normalize.batch.completed:{stats['stage_run_id']}",
        "payload": stats,
    })


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #

def _cmd_run(args) -> int:
    with jobs.connect() as conn:
        stats = run_normalize(conn, limit=args.limit,
                              source=args.source or None)
    print(json.dumps(stats, indent=2, default=str))
    return 0 if stats["errors"] == 0 else 2


def _cmd_status(_args) -> int:
    with jobs.connect() as conn:
        rows = conn.execute(
            """
            SELECT p.source,
                   (SELECT count(*) FROM ingestion.raw_feed_items i
                     JOIN ingestion.source_partitions pp
                       USING (partition_id)
                    WHERE pp.source = p.source) AS items,
                   (SELECT count(*) FROM ingestion.record_raw_items rri
                     JOIN ingestion.raw_feed_items i USING (item_id)
                     JOIN ingestion.source_partitions pp
                       USING (partition_id)
                    WHERE pp.source = p.source) AS linked,
                   (SELECT count(DISTINCT rri.record_id)
                      FROM ingestion.record_raw_items rri
                      JOIN ingestion.normalized_records nr
                        ON nr.record_id = rri.record_id
                     JOIN ingestion.raw_feed_items i USING (item_id)
                     JOIN ingestion.source_partitions pp
                       USING (partition_id)
                    WHERE pp.source = p.source) AS records
            FROM ingestion.source_partitions p
            WHERE p.source IN ('nse_api', 'bse_api')
            GROUP BY p.source ORDER BY p.source
            """
        ).fetchall()
        errs = conn.execute(
            """
            SELECT count(*) FROM ingestion.stage_errors e
            JOIN ingestion.stage_runs sr USING (stage_run_id)
            WHERE sr.stage_name = %s
            """,
            (STAGE_NAME,),
        ).fetchone()[0]
    print(f"{'source':10s} {'raw_items':>9s} {'linked':>7s} {'records':>8s}")
    for source, items, linked, records in rows:
        print(f"{source:10s} {items:>9d} {linked:>7d} {records:>8d}")
    print(f"\nquarantined (stage_errors): {errs}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Normalizer (M2): raw feed items → canonical records")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="normalize pending raw items")
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--source", default=None,
                       help="restrict to one source (e.g. nse_api)")

    sub.add_parser("status", help="normalization coverage per source")

    args = parser.parse_args(argv)
    if args.cmd == "run":
        return _cmd_run(args)
    return _cmd_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
