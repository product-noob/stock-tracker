"""Identity resolver (M3) — normalized records → canonical companies
(Phase 0, ROADMAP action 3).

Implements the M3 capability card in ``docs/architecture-hld.md`` §5:
deterministic resolution ladder over the security master, fail-closed review
queue, immutable decisions with an explicit current-assignment pointer.

Resolution ladder (HLD §5 M3; tier codes match the schema CHECK):

==================  =======================================================
tier                rule (exact matching only — never fuzzy)
==================  =======================================================
``A_scripcode``     record ``exchange_code_raw`` equals a current listing's
                    exchange code on the same exchange.
``B_cross_exchange``record ``exchange_symbol_raw`` equals a current listing's
                    symbol on the *opposite* exchange (cross-exchange anchor).
``C_document_identifier``
                    record ``isin_raw`` equals an instrument ISIN, else
                    ``exchange_symbol_raw`` equals a same-exchange symbol.
``D_exact_name``    normalized company name equals exactly one whitelisted
                    company's canonical name.
``E_alias_name``    normalized company name equals a curated alias value.
``F_manual``        human decision via the CLI ``assign`` command
                    (supersedes the previous decision append-only).
==================  =======================================================

Fail-closed behaviour (HLD invariant 5 — "ambiguity → review queue, never a
guess"):

* only **whitelisted** companies are ever assigned; a hit against a
  non-whitelisted master entity is quarantined to review, never published;
* any tier matching **more than one** distinct company → unresolved +
  ``needs_review = true``;
* no tier matching at all → unresolved + ``needs_review = true`` (a miss is
  never silently dropped);
* there is deliberately NO fuzzy/similarity tier: substring or prefix names
  ("HDFC" vs "HDFC Bank") can only land in review, never auto-link.

Every resolved record writes, in ONE transaction: the immutable
``identity_resolutions`` row, the mutable ``identity_assignments`` pointer,
and the ``normalized_records.company_id`` cache (HLD M3: "must agree in one
transaction"; DB triggers enforce the agreement). Review rows carry
``company_id IS NULL`` everywhere by construction.

Idempotency: a record with any existing ``identity_resolutions`` row is never
re-attempted by ``run_resolve`` (at-least-once safe). Corrections go through
:func:`apply_manual_resolution`, which appends a superseding tier-F row and
atomically repoints assignment + cache. Every run also writes an
``ingestion.stage_runs`` audit row and per-record outbox events.

Testability: the ladder itself is pure (:class:`Master` + :func:`resolve_record`),
unit-tested offline; the DB run takes an injectable connection like every
other stage script (see ``tests/test_resolve_identity.py``).

CLI
---
    venv/bin/python scripts/resolve_identity.py run [--limit N]
    venv/bin/python scripts/resolve_identity.py status
    venv/bin/python scripts/resolve_identity.py assign --record ID \
        --company ID --by NAME

Or through the durable queue (handler kind ``resolve.run``):
    venv/bin/python scripts/job_worker.py --queue resolve.run --drain
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent))

import jobs  # noqa: E402  (handler registration via @jobs.register_handler)

#: bumped when ladder rules change; recorded on every stage_run / resolution
IDENT_VERSION = "ident.v1"

STAGE_NAME = "resolve_identity"
JOB_KIND = "resolve.run"

#: confidence published per tier when exactly one company matches.
#: All tiers above the resolve threshold are deterministic exact hits; the
#: ordering mirrors "how directly the evidence identifies the security".
TIER_CONFIDENCE = {
    "A_scripcode": 0.990,
    "B_cross_exchange": 0.970,
    "C_document_identifier": 0.950,   # ISIN sub-rule
    "D_exact_name": 0.900,
    "E_alias_name": 0.850,
    "F_manual": 1.000,
}
#: same-exchange symbol match is slightly weaker than ISIN inside tier C
SYMBOL_CONFIDENCE = 0.930

#: decisions at/above this publish an assignment; below it go to review.
#: Kept explicit even though every current tier score exceeds it — lowering
#: a tier's confidence later must not silently start publishing guesses.
RESOLVE_THRESHOLD = 0.800


# --------------------------------------------------------------------- #
# pure layer: security master + deterministic ladder (unit-tested)
# --------------------------------------------------------------------- #

def normalize_name(value: Any) -> str:
    """Canonical text form for exact name/alias comparison.

    Mirrors the seeding normalization (``scripts/seed_core.py``): strip
    surrounding quotes, lowercase, collapse internal whitespace. Deliberately
    NOT stemming/punctuation-folding — that would be fuzzy matching.
    """
    if value is None:
        return ""
    return " ".join(str(value).strip().strip('"').lower().split())


@dataclass(frozen=True)
class Master:
    """Immutable snapshot of the security master relevant to resolution."""
    listings_by_code: dict          # (exchange, code) -> {(company_id, listing_id)}
    listings_by_symbol: dict        # (exchange, symbol) -> {(company_id, listing_id)}
    isin_to_companies: dict         # isin -> {company_id}
    name_to_companies: dict         # normalized canonical name -> {company_id}
    alias_to_companies: dict        # normalized alias -> {company_id}
    whitelist: frozenset            # actively whitelisted company_ids

    def whitelisted(self, company_ids) -> frozenset:
        return frozenset(company_ids) & self.whitelist


def empty_master() -> Master:
    return Master({}, {}, {}, {}, {}, frozenset())


@dataclass
class Decision:
    """Outcome of the ladder for one record (pure — applied by the DB layer)."""
    outcome: str                    # 'resolved' | 'unresolved'
    tier: str                       # A..F code, or the best tier examined
    company_id: Optional[int] = None
    listing_id: Optional[int] = None
    confidence: float = 0.0
    needs_review: bool = False
    reason: Optional[str] = None


def _candidate_listing(candidates) -> Optional[int]:
    """The single listing_id shared by all candidates of one company."""
    listings = {lid for _cid, lid in candidates if lid is not None}
    return listings.pop() if len(listings) == 1 else None


def _decide(tier: str, candidates, *, note: Optional[str] = None) -> Decision:
    """Turn a candidate set {(company_id, listing_id)} into a Decision."""
    companies = {cid for cid, _lid in candidates}
    if len(companies) == 1:
        company_id = companies.pop()
        conf = float(TIER_CONFIDENCE[tier])
        if conf < RESOLVE_THRESHOLD:
            return Decision("unresolved", tier, confidence=conf,
                            needs_review=True,
                            reason=f"{tier} below resolve threshold")
        return Decision("resolved", tier, company_id=company_id,
                        listing_id=_candidate_listing(candidates),
                        confidence=conf)
    # ambiguous: more than one distinct company matched this tier
    listed = ", ".join(str(c) for c in sorted(companies))
    reason = f"{tier} ambiguous across companies [{listed}]"
    if note:
        reason += f" ({note})"
    return Decision("unresolved", tier, confidence=0.500,
                    needs_review=True, reason=reason)


def resolve_record(record: dict[str, Any], master: Master) -> Decision:
    """Run the deterministic ladder. Pure: no I/O, no guessing.

    ``record`` needs: exchange, exchange_code_raw, exchange_symbol_raw,
    isin_raw, company_name_raw (title used as a fallback carrier).
    """
    exchange = record.get("exchange")
    code = (record.get("exchange_code_raw") or "").strip() or None
    symbol = (record.get("exchange_symbol_raw") or "").strip() or None
    isin = (record.get("isin_raw") or "").strip().upper() or None
    raw_name = (record.get("company_name_raw")
                or record.get("title") or "").strip() or None
    name = normalize_name(raw_name)

    # ---- tier A: scrip code on the record's own exchange ---------------
    if code and exchange:
        candidates = master.whitelisted(
            master.listings_by_code.get((exchange, code), set()))
        if candidates:
            return _decide("A_scripcode", candidates)
        if master.listings_by_code.get((exchange, code)):
            return _review_non_whitelisted("A_scripcode",
                                           master.listings_by_code[(exchange, code)])

    # ---- tier B: cross-exchange symbol anchor ---------------------------
    if symbol and exchange:
        other = "BSE" if exchange == "NSE" else "NSE"
        candidates = master.whitelisted(
            master.listings_by_symbol.get((other, symbol), set()))
        if candidates:
            return _decide("B_cross_exchange", candidates)

    # ---- tier C: document identifier — ISIN first, then own symbol ------
    if isin:
        companies = master.whitelisted(master.isin_to_companies.get(isin, set()))
        if companies:
            return _decide("C_document_identifier", companies)
        if master.isin_to_companies.get(isin):
            return _review_non_whitelisted("C_document_identifier",
                                           {(c, None) for c in
                                            master.isin_to_companies[isin]})
    if symbol and exchange:
        candidates = master.whitelisted(
            master.listings_by_symbol.get((exchange, symbol), set()))
        if candidates:
            decision = _decide("C_document_identifier", candidates)
            if decision.outcome == "resolved":
                decision.confidence = SYMBOL_CONFIDENCE
            return decision
        if master.listings_by_symbol.get((exchange, symbol)):
            return _review_non_whitelisted("C_document_identifier",
                                           master.listings_by_symbol[(exchange, symbol)])

    # ---- tier D: exact canonical company name ---------------------------
    if name:
        companies = master.whitelisted(master.name_to_companies.get(name, set()))
        if companies:
            return _decide("D_exact_name", {(c, None) for c in companies})
        if master.name_to_companies.get(name):
            return _review_non_whitelisted("D_exact_name",
                                           {(c, None) for c in
                                            master.name_to_companies[name]})

        # ---- tier E: curated alias --------------------------------------
        companies = master.whitelisted(master.alias_to_companies.get(name, set()))
        if companies:
            return _decide("E_alias_name", {(c, None) for c in companies})
        if master.alias_to_companies.get(name):
            return _review_non_whitelisted("E_alias_name",
                                           {(c, None) for c in
                                            master.alias_to_companies[name]})

    # ---- nothing matched anywhere: fail closed to review -----------------
    return Decision("unresolved", "E_alias_name", confidence=0.0,
                    needs_review=True,
                    reason=(f"no whitelisted master match "
                            f"(name={raw_name!r}, symbol={symbol!r}, "
                            f"code={code!r}, isin={isin!r})"))


def _review_non_whitelisted(tier: str, candidates) -> Decision:
    """A deterministic hit exists but points outside the active whitelist.

    Fail-closed: the engine watches whitelisted companies only, so such a
    record is parked for review instead of being linked (or dropped).
    """
    companies = sorted({cid for cid, _lid in candidates})
    return Decision("unresolved", tier, confidence=float(TIER_CONFIDENCE[tier]),
                    needs_review=True,
                    reason=(f"{tier} matched non-whitelisted "
                            f"compan{'y' if len(companies) == 1 else 'ies'} "
                            f"[{', '.join(map(str, companies))}]"))


# --------------------------------------------------------------------- #
# DB plumbing
# --------------------------------------------------------------------- #

def fetch_master(conn: psycopg.Connection) -> Master:
    """Load current security-master state (current listings, active
    whitelist, all aliases, active companies' canonical names)."""
    listings_by_code: dict = {}
    listings_by_symbol: dict = {}
    for cid, lid, exch, code, sym in conn.execute(
        """
        SELECT company_id, listing_id, exchange, exchange_code, exchange_symbol
        FROM core.listings WHERE valid_to IS NULL
        """
    ).fetchall():
        if code:
            listings_by_code.setdefault((exch, code.strip()), set()) \
                .add((cid, lid))
        if sym:
            listings_by_symbol.setdefault((exch, sym.strip()), set()) \
                .add((cid, lid))

    isin_to_companies: dict = {}
    for cid, isin in conn.execute(
        "SELECT company_id, isin FROM core.instruments "
        "WHERE isin IS NOT NULL AND status <> 'delisted'"
    ).fetchall():
        isin_to_companies.setdefault(isin.strip().upper(), set()).add(cid)

    name_to_companies: dict = {}
    for cid, cname in conn.execute(
        "SELECT company_id, canonical_name FROM core.companies "
        "WHERE status = 'active'"
    ).fetchall():
        name_to_companies.setdefault(normalize_name(cname), set()).add(cid)

    alias_to_companies: dict = {}
    for cid, norm in conn.execute(
        "SELECT company_id, normalized_value FROM core.company_aliases"
    ).fetchall():
        alias_to_companies.setdefault(normalize_name(norm), set()).add(cid)

    whitelist = frozenset(
        r[0] for r in conn.execute(
            "SELECT company_id FROM core.whitelist_memberships "
            "WHERE removed_at IS NULL").fetchall())

    return Master(listings_by_code, listings_by_symbol, isin_to_companies,
                  name_to_companies, alias_to_companies, whitelist)


_PENDING_SQL_BASE = """
SELECT n.record_id, n.exchange, n.exchange_code_raw, n.exchange_symbol_raw,
       n.isin_raw, n.company_name_raw, n.title
FROM ingestion.normalized_records n
WHERE n.company_id IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM ingestion.identity_resolutions r
      WHERE r.record_id = n.record_id)
"""


def fetch_pending(conn: psycopg.Connection, *,
                  limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Unresolved records with no prior resolution attempt (oldest first)."""
    sql = _PENDING_SQL_BASE + " ORDER BY n.record_id LIMIT %s"
    params = [limit if limit is not None else 1000000]
    rows = conn.execute(sql, params).fetchall()
    keys = ["record_id", "exchange", "exchange_code_raw",
            "exchange_symbol_raw", "isin_raw", "company_name_raw", "title"]
    return [dict(zip(keys, r)) for r in rows]


def insert_resolution(conn: psycopg.Connection, record_id: int,
                      decision: Decision, *, method_version: str = IDENT_VERSION,
                      decided_by: str = "system",
                      supersedes_resolution_id: Optional[int] = None) -> int:
    """Append one immutable identity_resolutions row; return its id.

    Fail-closed by construction: unresolved rows carry NULL company/listing,
    which the schema CHECK constraints enforce alongside needs_review.
    """
    row = conn.execute(
        """
        INSERT INTO ingestion.identity_resolutions
            (record_id, company_id, listing_id, tier, confidence, outcome,
             needs_review, method_version, reason, decided_by,
             supersedes_resolution_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING resolution_id
        """,
        (record_id,
         decision.company_id if decision.outcome == "resolved" else None,
         decision.listing_id if decision.outcome == "resolved" else None,
         decision.tier, decision.confidence, decision.outcome,
         decision.needs_review, method_version, decision.reason, decided_by,
         supersedes_resolution_id),
    ).fetchone()
    return int(row[0])


def apply_decision(conn: psycopg.Connection, record_id: int,
                   decision: Decision) -> int:
    """Write one record's decision so all three stores agree in-tx:

    * immutable ``identity_resolutions`` row (always);
    * on resolve: ``identity_assignments`` pointer + ``normalized_records``
      company cache update (deferred consistency triggers verify agreement);
    * on review: both stay NULL — the record simply remains unassigned and
      visible in the partial index ``idx_resolutions_review``.

    Also emits the stage's outbox event. Caller owns the transaction.
    """
    resolution_id = insert_resolution(conn, record_id, decision)
    if decision.outcome == "resolved":
        conn.execute(
            """
            INSERT INTO ingestion.identity_assignments
                (record_id, resolution_id, company_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (record_id) DO UPDATE SET
                resolution_id = EXCLUDED.resolution_id,
                company_id = EXCLUDED.company_id,
                assigned_at = now()
            """,
            (record_id, resolution_id, decision.company_id),
        )
        conn.execute(
            "UPDATE ingestion.normalized_records SET company_id = %s "
            "WHERE record_id = %s",
            (decision.company_id, record_id),
        )
    jobs.emit_outbox(
        conn,
        event_type=("identity.resolved" if decision.outcome == "resolved"
                    else "identity.review_required"),
        aggregate_type="normalized_record",
        aggregate_id=str(record_id),
        event_key=("identity.resolved" if decision.outcome == "resolved"
                   else "identity.review_required") + f":{record_id}",
        payload={"record_id": record_id,
                 "resolution_id": resolution_id,
                 "outcome": decision.outcome,
                 "tier": decision.tier,
                 "needs_review": decision.needs_review,
                 "confidence": decision.confidence,
                 "code_version": IDENT_VERSION},
    )
    return resolution_id


def apply_manual_resolution(conn: psycopg.Connection, record_id: int,
                            company_id: int, *, decided_by: str) -> int:
    """Tier-F human correction: append a superseding decision and atomically
    repoint the current-assignment pointer + company cache (HLD M3).

    The previous decision row stays untouched (append-only history); the new
    row references it via ``supersedes_resolution_id``.
    """
    latest = conn.execute(
        """
        SELECT r.resolution_id, r.outcome, r.company_id
        FROM ingestion.identity_resolutions r
        WHERE r.record_id = %s
        ORDER BY r.attempted_at DESC, r.resolution_id DESC LIMIT 1
        """,
        (record_id,),
    ).fetchone()
    if latest is None:
        raise ValueError(f"record {record_id} has no prior resolution to "
                         f"supersede — run the resolver first")
    prev_resolution_id, prev_outcome, prev_company = latest
    if prev_outcome == "resolved":
        # sanity: the row being superseded must currently be the assignment
        cur = conn.execute(
            "SELECT resolution_id FROM ingestion.identity_assignments "
            "WHERE record_id = %s",
            (record_id,),
        ).fetchone()
        if cur and cur[0] != prev_resolution_id:
            raise ValueError(
                f"record {record_id}: latest decision {prev_resolution_id} "
                f"is not the current assignment ({cur[0]})")

    with conn.transaction():
        decision = Decision("resolved", "F_manual", company_id=company_id,
                            confidence=TIER_CONFIDENCE["F_manual"])
        resolution_id = insert_resolution(
            conn, record_id, decision, decided_by=decided_by,
            supersedes_resolution_id=prev_resolution_id)
        conn.execute(
            """
            INSERT INTO ingestion.identity_assignments
                (record_id, resolution_id, company_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (record_id) DO UPDATE SET
                resolution_id = EXCLUDED.resolution_id,
                company_id = EXCLUDED.company_id,
                assigned_at = now()
            """,
            (record_id, resolution_id, company_id),
        )
        conn.execute(
            "UPDATE ingestion.normalized_records SET company_id = %s "
            "WHERE record_id = %s",
            (company_id, record_id),
        )
    return resolution_id


# --------------------------------------------------------------------- #
# the run itself
# --------------------------------------------------------------------- #

def run_resolve(
    conn: psycopg.Connection,
    *,
    limit: Optional[int] = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Resolve all pending normalized records (see module docstring).

    One ``ingestion.stage_runs`` row per batch keyed by the highest pending
    record id; per-record SAVEPOINTs so one bad record cannot poison the
    batch. Idempotent end-to-end: already-attempted records are never
    re-selected, and re-running after new records arrive processes only those.

    Returns stats: records_in / resolved / review / errors / stage_run_id.
    """
    log = (lambda m: None) if quiet else (lambda m: print(m, flush=True))
    master = fetch_master(conn)
    pending = fetch_pending(conn, limit=limit)
    batch_max = max((p["record_id"] for p in pending), default=0)
    batch_key = f"{IDENT_VERSION}:{batch_max}"

    stats = {"records_in": len(pending), "resolved": 0, "review": 0,
             "errors": 0}

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
            (STAGE_NAME, batch_key, IDENT_VERSION),
        ).fetchone()
        stage_run_id = int(row[0])

    log(f"[{STAGE_NAME}] stage_run {stage_run_id}: "
        f"{stats['records_in']} pending record(s)")

    for record in pending:
        record_id = record["record_id"]
        try:
            # Re-check under concurrency: skip records resolved since the
            # snapshot was taken (the unique assignment keeps us honest).
            already = conn.execute(
                "SELECT 1 FROM ingestion.identity_resolutions "
                "WHERE record_id=%s",
                (record_id,),
            ).fetchone()
            if already:
                continue
            decision = resolve_record(record, master)
            with conn.transaction():   # savepoint per record
                apply_decision(conn, record_id, decision)
            if decision.outcome == "resolved":
                stats["resolved"] += 1
                log(f"  resolved record {record_id} → company "
                    f"{decision.company_id} ({decision.tier}, "
                    f"{decision.confidence:.3f})")
            else:
                stats["review"] += 1
                log(f"  REVIEW record {record_id}: {decision.reason}")
        except Exception as exc:  # noqa: BLE001 — quarantine, keep batching
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO ingestion.stage_errors
                        (stage_run_id, entity_type, entity_id, error_code,
                         message)
                    VALUES (%s, 'normalized_record', %s,
                            'RESOLUTION_FAILED', %s)
                    """,
                    (stage_run_id, str(record_id),
                     f"record {record_id}: {exc}"),
                )
            stats["errors"] += 1
            log(f"  ERROR record {record_id}: {exc}")

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
            (stats["errors"], stats["records_in"],
             stats["resolved"] + stats["review"], stats["errors"],
             Jsonb(stats), stage_run_id),
        )
    log(f"[{STAGE_NAME}] done: {stats['resolved']} resolved, "
        f"{stats['review']} to review, {stats['errors']} errors")
    stats["stage_run_id"] = stage_run_id
    return stats


# --------------------------------------------------------------------- #
# job-framework handler
# --------------------------------------------------------------------- #

@jobs.register_handler(JOB_KIND)
@jobs.register_prefix_handler(JOB_KIND + ":")
def handle_resolve(ctx: jobs.JobContext) -> None:
    """Durable-job entrypoint. Payload: ``{"limit": int?}``.

    The underlying writes are idempotent (pending-selection excludes any
    record with an existing resolution; assignments are upserts), so
    redeliveries converge instead of duplicating decisions."""
    payload = ctx.payload or {}
    with jobs.connect(ctx.db_url) as conn:
        stats = run_resolve(conn, limit=payload.get("limit"), quiet=True)
    ctx.outbox.append({
        "event_type": "resolve.batch.completed",
        "aggregate_type": "stage_run",
        "aggregate_id": str(stats["stage_run_id"]),
        "event_key": f"resolve.batch.completed:{stats['stage_run_id']}",
        "payload": stats,
    })


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #

def _cmd_run(args) -> int:
    with jobs.connect() as conn:
        stats = run_resolve(conn, limit=args.limit)
    print(json.dumps(stats, indent=2, default=str))
    return 0 if stats["errors"] == 0 else 2


def _cmd_status(_args) -> int:
    with jobs.connect() as conn:
        total, resolved, review, unattempted = conn.execute(
            """
            SELECT count(DISTINCT n.record_id),
                   count(*) FILTER (WHERE a.record_id IS NOT NULL),
                   count(*) FILTER (WHERE r.needs_review),
                   count(*) FILTER (WHERE r.resolution_id IS NULL)
            FROM ingestion.normalized_records n
            LEFT JOIN ingestion.identity_resolutions r
                   ON r.record_id = n.record_id
            LEFT JOIN ingestion.identity_assignments a
                   ON a.record_id = n.record_id
            """,
        ).fetchone()
    print(f"records:            {total}")
    print(f"  resolved:         {resolved}")
    print(f"  review queue:     {review}")
    print(f"  unattempted:      {unattempted}")
    return 0


def _cmd_assign(args) -> int:
    with jobs.connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM core.companies WHERE company_id=%s",
            (args.company,)).fetchone()
        if not exists:
            print(f"company {args.company} does not exist")
            return 2
        resolution_id = apply_manual_resolution(
            conn, args.record, args.company, decided_by=args.by)
        conn.commit()
    print(f"record {args.record} manually assigned to company "
          f"{args.company} (resolution {resolution_id}, tier F_manual)")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Identity resolver (M3): normalized records → companies")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="resolve pending normalized records")
    p_run.add_argument("--limit", type=int, default=None)

    sub.add_parser("status", help="resolution coverage summary")

    p_as = sub.add_parser("assign",
                          help="manual tier-F correction of one record")
    p_as.add_argument("--record", type=int, required=True)
    p_as.add_argument("--company", type=int, required=True)
    p_as.add_argument("--by", default="manual",
                      help="who decided (recorded as decided_by)")

    args = parser.parse_args(argv)
    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "assign":
        return _cmd_assign(args)
    return _cmd_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
