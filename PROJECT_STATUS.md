# Project Status — Stock Market Insights Engine

**Last updated:** 2026-08-23

**Durable audit:** [`docs/reviews/2026-08-23-comprehensive-review.md`](docs/reviews/2026-08-23-comprehensive-review.md)

## Current phase

**Phase 0 — reliable capture, partially implemented; gate not started.** The project is no
longer a docs/pilot-only repository: it has persistent PostgreSQL and object-store state and
DB-integrated API capture, normalization, identity, jobs, and daily prices. It is also not an
unattended service yet: major Phase 0 correctness and operational gates remain open.

## Verified implementation boundary

| Area | Current status | Important boundary |
|---|---|---|
| PostgreSQL/schema | Applied v1.2 baseline; 30-company registry/whitelist seeded | Persistent, not disposable; no migration framework or backup/restore coverage |
| Object store | Implemented and content-addressed | Registered objects reconcile cleanly after owner-authorized orphan cleanup; stored with DB on root-disk workflow and not backed up |
| Jobs/outbox | Job claim/lease/retry/dead-letter worker implemented | Outbox rows are inserted, but no dispatcher exists; no scheduler/service supervision |
| NSE capture | `nse_api` DB-integrated for all 30 registry companies | Configurable IST schedule slots default to six hours; RSS absent and bytes-before-parse plus validation/provenance gates remain open |
| BSE capture | `bse_api` DB-integrated for all 30 registry companies | Configurable six-hour default; invalid-only URLs retry and GUID paths are ordered correctly, but per-page provenance, canonical attempt/linkage, bounded retry and validation gaps remain open |
| Normalizer/identity | M2 and M3 DB-integrated; exact, fail-closed ladder | Manual corrections are whitelist-gated and emit versioned events; parallel claims and one cross-exchange tier need hardening |
| Filing content | Adapter PDFs and recovery utility register objects/artifacts | No canonical `content_fetch_attempts` or `record_contents` rows at audit sample; invalid/no-byte retry semantics are incomplete |
| Yahoo daily bars | `scripts/fetch_prices.py` DB-integrated through yfinance | 30 series / 7,529 current bars at audit sample; provenance, finite/current-session checks, calendar, revisions and benchmark gates remain open |
| Google News | File-based feed pilot + redirect resolver | No DB adapter/article fetch; transient resolver failures are cached as final |
| Screener | File-based manual parser/fetch utility | No DB snapshot schema/adapter/schedule; owner lifted prior ToS cadence constraints, but ban-avoidance controls remain needed |
| M4–M10 intelligence/delivery | File-based extraction/ranking/reaction/report prototypes only | Canonical text/extraction/event/insight/reaction/delivery pipeline is not implemented and prototypes must not ship |
| Dashboard | DB-backed generated debugging utility exists | Volatile generated `data/dashboard.html` is gitignored; this is not an operational health/production UI |

### Timestamped audit facts, not evergreen status counters

The explicitly read-only 2026-08-23 review sample found 44 application tables; 350
registered objects matching 350 files with zero hash/size/path/orphan problems; 258
adapter-shaped production NSE/BSE rows linked to 258 normalized and assigned records with
zero assignment/cache/whitelist mismatches; and 30 price series with 7,529 current bars.
The database also contained dominant append-only integration-test fixtures, 67 dead jobs,
18 runnable test-family jobs, one stale running fetch, and 2,330 unpublished outbox events.
Re-query read-only state before operational decisions; do not copy these counts forward as
current totals.

## Critical fixes completed in the 2026-08-23 review

- Expired jobs on their final counted attempt now dead-letter instead of becoming
  permanently unclaimable (`ST-P04`), with a rollback-only regression test.
- Idempotent object puts verify an existing file's SHA-256, not only its size (`ST-P05`),
  with regression coverage and a clean full-store verification.
- BSE pagination now rejects missing/changing/invalid `ROWCNT` and rows without `NEWSID`
  (`ST-C02`); GUID attachments try AttachLive first and invalid-only captures no longer block
  a later valid retry (`ST-C04`, partially fixed), with regression coverage.
- Project `.env` permissions were tightened to `0600` without reading or printing values
  (`ST-P06`).

## Next steps

1. Establish forward migrations before more DDL. Coordinated PostgreSQL/object-store backup
   and restore remain required before unattended operation, but the owner deferred this work
   until the system is stable and explicitly accepts the temporary risk.
2. Keep all DB-backed tests behind the positively marked disposable DB and separate object
   root in `docs/testing.md`; the isolated full suite passed without production mutation.
3. Close capture P0 defects: bytes-before-parse, BSE page/run lineage, bounded and
   allowlisted downloads, retry/link lifecycle, source validation, run/correction semantics,
   and M2/M3 concurrency.
4. Implement the outbox dispatcher, scheduler/supervision, least-privilege runtime role,
   and actionable aggregate health reporting.
5. Add DB-integrated NSE/BSE main feeds, canonical content attempts/links, a plain
   resolved-filing Telegram digest, and a reconciliation harness; then begin the seven-day
   Phase 0 gate.

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for ordered phase work and the review's finding
ledger for stable IDs, evidence, recommendations, limitations, and owner questions.

## Operational safety

- Run scripts as `venv/bin/python scripts/<script>.py`; install with
  `python3 -m venv venv && venv/bin/pip install -r requirements.txt`.
- Start PostgreSQL with `docker compose up -d`. Use `docker compose stop` to stop it when
  needed; do not use `down -v`, reset/reapply `db/schema.sql`, truncate, or clean fixtures.
- The database is loopback-bound at `127.0.0.1:5433`; credentials remain only in private
  `.env`.
- `db/schema.sql` is the frozen applied baseline. Future DDL requires reviewed forward
  migrations and a backup/restore plan.
- PostgreSQL and `data/objects/` are persistent and not covered by current host backups.
  Backup implementation is owner-deferred until system stability (decision 2026-08-23),
  not cancelled; avoid additional destructive operations without explicit authorization.
