---
tier: L1
module: bse-capture
status: db-integrated (API family only; acceptance/reliability gate open)
spec-version: v1.1 (tiered restructure of bse-capture.md v1.0)
master: ../README.md
loads:
  - source-mechanics.md   # implementing/debugging API contract, AttachHis/AttachLive, failure handling
  - data-contract.md      # writing parser/writer code, touching C1/C3 fields or dedup keys
  - evidence-log.md       # investigating anomalies, checking known pitfalls before field work
---

# BSE Capture — Overview

**Module card:** M1 source adapter + fetch half of M4 · **Domain:** [`../README.md`](../README.md)
Symmetric with [`../nse-capture/OVERVIEW.md`](../nse-capture/OVERVIEW.md).

## Purpose & scope

Retrieval of BSE corporate announcements for whitelisted stocks via two independent
partition families (RSS + API), plus download of announcement PDFs into immutable content storage.

**Produces:** C1 fetched source items; C3 content objects.

**Does not own:** company attribution (M3), cross-exchange dedup (M2/M4 — byte-identical
NSE/BSE filings collapse downstream by SHA-256), extraction (M5).

## Partitions & cadence

| Partition key | Interval | Overlap |
|---|---|---|
| `(bse_rss, announcements.xml)` | 300 s | ≥ 2× interval |
| `(bse_rss, <Category>Feed.xml)` × ~8 | 900 s | same |
| `(bse_api, scrip:{SCRIP})` | 6 h rolling by default; configurable positive divisor of 24 via `EXCHANGE_CAPTURE_INTERVAL_HOURS` | 24 h overlap |
| `(bse_api, ...)/backfill` | manual / reconciliation | explicit range |

Cursor rules identical to NSE (advance only after artifact + items commit).

## Acceptance criteria (Phase 0 gate inputs)

1. Symmetric with NSE: seven unattended days, worker-kill tests, zero stranded records.
2. Sampled-week reconciliation vs BSE-API truth set ≥95% with explained misses.
3. Zero wrong-company attributions; scrip-code tier resolves ≥99% of records without review.
4. Every AttachLive-era HTML-garbage download retained as evidence; invalid-content rate <1%.
5. Offline reparse of sampled artifacts reproduces identical items.

## Open questions

1. Documented BSE API changelog or partner program? (Current method is bundle-diffing.)
2. `notices.xml` and `AnnualReportFeed.xml`: partition now or defer to breadth phase?
3. Rate-limit ceiling for per-scrip API polling at 100-company scale — measure before Phase 3.

## Status & next milestone

- **DB-integrated (API family), not gated:** `scripts/bse_capture.py` writes fetch runs,
  page/PDF artifacts, raw items, cursors, and content-object registrations as durable
  `bse.fetch` jobs for all 30 registry companies. A read-only audit found all 90
  adapter-shaped production BSE items normalized/assigned consistently. The 138/109/28
  values previously shown here remain a dated first-ingestion snapshot in the evidence log,
  not current production totals.
- **Contract gaps:** artifact keys can still collapse identical fetch events across runs
  (`ST-C06`). Closed 2026-08-23: each page's exact bytes are durably registered before that
  page is parsed; every raw item points at the artifact whose bytes contain it (per-page
  provenance, `ST-C03`); the empty-result canary runs AFTER scrip filtering so all-foreign
  responses cannot masquerade as genuine empties; rows missing `SCRIP_CD` fail closed;
  attachment downloads are host-allowlisted and size-capped; the filing downloader records
  immutable attempts and record→content links under a bounded retry budget.
- `bse_rss` is not DB-integrated. No seven-day soak/reconciliation gate has run.
- **Next milestone:** close `ST-C06`/`ST-C07` before RSS integration and the Phase 0 soak.

## Detail index (L2)

| File | Load when |
|---|---|
| [`source-mechanics.md`](source-mechanics.md) | touching endpoints, envelope handling, download paths, failure modes |
| [`data-contract.md`](data-contract.md) | field mapping, idempotency keys, dedup/identity pre-fill |
| [`evidence-log.md`](evidence-log.md) | debugging surprises, reviewing known pitfalls |
