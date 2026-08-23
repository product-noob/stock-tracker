---
tier: L2
module: yahoo-market-data
doc: source-mechanics
parent: OVERVIEW.md
updated: 2026-08-23
note: implementation exists; audited gaps are explicit below
---

# Yahoo Market Data — Source Mechanics

## 1. Implemented access method

Primary: pinned **`yfinance` Python library**, symbols `{NSE_SYMBOL}.NS`. The canonical
source code/partition is `yfinance` (`listing:{listing_id}:daily`); “Yahoo market data” is
the product/module name. `scripts/fetch_prices.py` uses
`Ticker.history(period=..., interval="1d", auto_adjust=True)`, records library and adjustment
versions in run config, and executes partitions sequentially with spacing.

The first implementation resolved the former transport TBD by serializing the returned
DataFrame deterministically as CSV. This is useful replay input for the mapping layer but is
**not exact provider HTTP transport**. The implementation must not claim R-YF-4's exact
transport guarantee unless transport interception is added or the approved contract is
explicitly changed.

Still required:

- [ ] explicit timeout/retry behavior around `Ticker.history`;
- [ ] measured rate-limit behavior at ~30 then 100 symbols;
- [ ] owner/design decision on returned-frame serialization versus transport bytes.

## 2. Implemented fetch shape

```text
per listing partition (yfinance, listing:{id}:daily):
  first load: history(period="1y", interval="1d", auto_adjust=True)
  refresh:    the same overlapping one-year frame; unchanged observations deduplicate
```

The adapter uses one deterministic run per partition/IST calendar day, stores the frame CSV
artifact, inserts append-only observation hashes, and advances the cursor after the DB work.
Bars are keyed by IST exchange trade date. A normal second invocation on the same day skips
the network.

## 3. Validation and failure modes

| Failure | Current detection | Required disposition / gap |
|---|---|---|
| Empty frame for active listing | hard error | retry/backoff and source-silence alert not supervised yet |
| Delisted / symbol renamed | no complete workflow | sustained-empty health + identity-correction signal, then explicit close/review |
| Adjusted-history rewrite | changed observation hash appends a bar | isolated DB revision/crash fixture still required |
| OHLC inversion / negative volume | rejected | finite-value and duplicate-date checks missing; NaN/Infinity can pass |
| Still-forming current-day bar | helper can calculate expected sessions | fetched frame is not rejected before market close |
| Library/API breakage | pinned direct version | no network smoke policy or transitive lock/update workflow |
| DB/object split | content object registration uses a separate connection | prove/rework transaction and orphan reconciliation behavior |

The read-only 2026-08-23 sample found 30 active series, 7,529 current bars, and no invalid
OHLC, weekend/future, missing-lineage, duplicate-series, or cursor-failure rows. That is a
timestamped data-quality observation, not evidence the missing validation paths are safe.

## 4. Trading calendar and reaction boundary

Current gap detection uses weekdays plus an 80%-of-series heuristic; no authoritative NSE
holiday calendar is selected. This is insufficient for production staleness/coverage and can
hide provider-wide loss. Benchmark/sector series and the no-look-ahead M8 reaction engine
remain Phase 2 work. The file-based `price_reaction.py` prototype uses future data and must
not be used for published attribution (review `ST-D01`).
