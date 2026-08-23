---
tier: L1
module: nse-capture
status: db-integrated (API family only; acceptance/reliability gate open)
spec-version: v1.1 (tiered restructure of nse-capture.md v1.0)
master: ../README.md
loads:
  - source-mechanics.md   # implementing/debugging HTTP sessions, endpoints, failure handling
  - data-contract.md      # writing parser/writer code, touching C1/C3 fields or dedup keys
  - evidence-log.md       # investigating anomalies, reviewing known gotchas before field work
---

# NSE Capture — Overview

**Module card:** M1 source adapter + fetch half of M4 · **Domain:** [`../README.md`](../README.md)

## Purpose & scope

Retrieval of NSE corporate announcements for whitelisted stocks via two independent
partition families (RSS + API), plus download of announcement PDFs into immutable content storage.

**Produces:** C1 fetched source items (exact artifacts + parsed raw items); C3 content
objects for filing PDFs.

**Does not own:** company attribution (M3), cross-exchange dedup (M2/M4), LLM extraction (M5).

Both families are required and complementary:

| Family | Role | Character |
|---|---|---|
| `nse_rss` | live capture, market-wide category streams | near-real-time; short rolling window |
| `nse_api` | per-symbol polling + historical backfill/reconciliation | complete date-window queries |

## Partitions & cadence

| Partition key | Interval | Overlap |
|---|---|---|
| `(nse_rss, Online_announcements.xml)` | 300 s | ≥ 2× poll interval worth of items |
| `(nse_rss, <Category>.xml)` × ~20 | 900 s | same rule |
| `(nse_api, symbol:{SYMBOL})` | **required:** 6 h rolling; **implemented:** at most once per IST day | 24 h overlap |
| `(nse_api, ...)/backfill` | manual / reconciliation-triggered | explicit date range |

Cursor advances only after artifact + raw items commit. The API partition doubles as the
**reconciliation denominator** for coverage measurement.

## Acceptance criteria (Phase 0 gate inputs)

1. Seven consecutive unattended days on ≥3 stocks, zero stranded records after daily worker-kill tests.
2. Sampled-week reconciliation ≥95% vs NSE-API truth set, every miss explainable.
3. Zero wrong-company attributions (M3 audit).
4. Every stored artifact reparses offline to identical raw items.
5. Invalid-content rate (200-but-not-PDF) < 1%, all instances retained as evidence.

## Open questions

1. Do `nsearchives.nseindia.com` RSS hosts have separate Akamai policy from `www.`? Verify under sustained polling.
2. Optimal RSS poll interval — measure item arrival distribution over a results season before tuning below 5 min.
3. Does the API accept multi-symbol batched queries without tripping rate limits?

## Status & next milestone

- **DB-integrated (API family), not gated:** `scripts/nse_capture.py` writes fetch runs,
  response/PDF artifacts, raw items, cursors, and content-object registrations for all 30
  registry companies as durable `nse.fetch` jobs. A read-only audit found all 168
  adapter-shaped production NSE items normalized/assigned consistently; the larger totals
  in the 2026-08-23 evidence entry are a historical first-fleet snapshot, not current counts.
- **Contract gaps:** API JSON is parsed before artifact registration is durable; artifact
  keys can collapse fetch events; the once-per-day key contradicts this module's six-hour
  cadence; malformed/foreign rows and attachment hosts/sizes are insufficiently guarded;
  no-byte attempts and canonical `record_contents` links are absent. The API path therefore
  implements part of C1 plus content artifacts, not complete C3/M4 lineage.
- `nse_rss` is not DB-integrated. No seven-day soak or reconciliation acceptance gate has
  run.
- **Next milestone:** close review `ST-C01`, `ST-C04`–`ST-C07` and test isolation before
  adding RSS and beginning sampled-week reconciliation.

## Detail index (L2)

| File | Load when |
|---|---|
| [`source-mechanics.md`](source-mechanics.md) | touching HTTP/session/Akamai behavior, endpoints, download paths, failure-mode handling |
| [`data-contract.md`](data-contract.md) | mapping fields, defining/writing idempotency keys, dedup or identity pre-fill logic |
| [`evidence-log.md`](evidence-log.md) | debugging surprises, checking known pitfalls before running against the live site |
