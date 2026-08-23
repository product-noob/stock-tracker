---
tier: L1
module: yahoo-market-data
status: db-integrated (daily yfinance adapter; acceptance/reliability gate open)
spec-version: v1.0
master: ../README.md
loads:
  - source-mechanics.md   # implementing/debugging fetch endpoints/params, session behavior
  - data-contract.md      # writing series/bar writer code, mapping to market.price_series/price_bars
  - evidence-log.md       # reviewing known data-quality gotchas before trusting bars
---

# Yahoo Market Data — Overview

**Module card:** M1 source adapter (prices family) feeding **M8** market/reaction engine ·
**Domain:** [`../README.md`](../README.md)

## Decision record (resolves PRD §14 open question)

**2026-08-22: Yahoo Finance (`yfinance`, `.NS`/`.BO` symbols) is the primary daily-price
provider. NSE bhavcopy is deferred to Phase 2+ as an independent reconciliation cross-check,
not a second live source.** Rationale: `.NS` symbols proven across both pilots with zero
access friction (no Akamai-class defenses); bhavcopy retained as independent truth for
coverage checks without doubling live-path complexity.

## Purpose & scope

**Owns:** retrieval and storage of listing-level **daily OHLCV** price series for whitelisted
listings, including corporate-action-adjusted history, into `market.price_series` /
`market.price_bars` for consumption by the reaction engine (M8).

**Produces:** C1 fetched source items (response artifacts) and price-series observations.

**Does not own:** reaction calculations (M8), intraday/streaming quotes (out of scope v1),
fundamentals, index/benchmark series beyond what attribution needs (Phase 2 decision).

## Scope boundaries (v1)

| In | Out (deferred) |
|---|---|
| daily OHLCV + volume per listing | intraday intervals |
| adjusted-close series with explicit adjustment semantics on the series row | fundamentals data |
| provisioning backfill (~3–12 months) + daily append after close | real-time quotes |
| benchmark series (e.g. ^NSEI) as explicitly separate series | currency/FX series |

## Partition & cadence model

| Partition key | Interval |
|---|---|
| `(yfinance, listing:{listing_id}:daily)` | once per day, after market close IST (+ jitter) |
| `(yfinance, listing:{id}:daily)/backfill` | at partition provisioning; explicit date range |

Standard rules apply: exact response bytes persisted before parsing; cursor advances only
after artifact + parsed bars commit; overlap ≥ 5 trading days on the rolling window.

## Key design requirements

| Req | Rule |
|---|---|
| R-YF-1 | Each `(listing, interval=1d)` combination maps to exactly one `market.price_series` row fixing provider=`yfinance`, provider symbol, adjustment mode and methodology version — never mix adjusted/unadjusted bars in one series. |
| R-YF-2 | Provider revisions append new bar *observations* (schema's append model via `current_price_bars`); old calculations keep referencing their original bar IDs. Never update/delete bars. |
| R-YF-3 | Validate bars before insert: OHLC relationships sane, volume ≥ 0, dates are expected trading sessions; violations fail loudly per-run, not silently per-bar. |
| R-YF-4 | Store the raw provider payload (exact bytes) as artifact even though it is derived data — provider backfills/corrections are silent, so retained responses are the audit trail. |
| R-YF-5 | Weekend/holiday sessions: absence is normal — "staleness" alerts must use a trading-calendar, not wall-clock age. |
| R-YF-6 | Rate-limit posture: sequential requests, modest spacing; Yahoo tolerates low-volume polling but behavior under 100-symbol scale must be measured before Phase 3. |

## Acceptance criteria

1. Provisioned listings have complete backfilled series matching a hand-checked sample (≥99% bar-level agreement on OHLC close).
2. Daily append runs unattended through a full results season with zero silent gaps (calendar-aware staleness checks).
3. A provider correction (observed or injected fixture) appends an observation without breaking prior reaction-signal references.
4. Hand-worked reaction fixtures reproduce exactly from stored bars (Phase 2 gate dependency).

## Open questions

1. Adjustment semantics: does yfinance auto-adjust flag map cleanly to our series-level adjustment-mode vocabulary? Pin exact flag + version at first implementation.
2. Symbol stability: `.NS` symbol changes on rename/delisting — needs monitoring hook into identity corrections.
3. Benchmark series choice for abnormal-return calculation (^NSEI vs sector indices) — decide in Phase 2 with M8.

## Status & next milestone

- **DB-integrated, not gated:** `scripts/fetch_prices.py` provisions yfinance partitions and
  listing-level `market.price_series`/append-only `price_bars`, stores a deterministic CSV
  artifact, advances a cursor, and registers durable `yfinance.fetch` jobs. The read-only
  audit sample had 30 active series and 7,529 current bars spanning 2025-08-21..2026-08-21,
  with no observed invalid OHLC/weekend/future rows or revisions.
- **Contract gaps:** the frame is parsed before artifact persistence; the artifact is pandas
  CSV serialization rather than exact transport bytes; content registration uses a separate
  transaction; history calls lack an explicit timeout/retry contract; NaN/Infinity,
  duplicate trade dates and still-forming pre-close bars can pass; holiday/benchmark and
  provider-revision integration gates are absent. Clean sampled data is not proof these
  prospective corruption paths are closed.
- **Next milestone:** finite/duplicate/session-close validation and offline fixtures, then
  prove append-only revisions and transaction/crash behavior in a guarded test DB. The M8
  reaction engine remains Phase 2 and the file prototype must not ship.

## Detail index (L2)

| File | Load when |
|---|---|
| [`source-mechanics.md`](source-mechanics.md) | implementing the fetcher: library/endpoints, params, failure modes *(partly placeholder until first implementation)* |
| [`data-contract.md`](data-contract.md) | mapping yfinance frames to `market.price_series`/`price_bars`; idempotency keys |
| [`evidence-log.md`](evidence-log.md) | known data-quality quirks observed in pilots |
