---
tier: L2
module: yahoo-market-data
doc: source-mechanics
parent: OVERVIEW.md
updated: 2026-08-22
note: partly placeholder — mechanics marked TBD harden during first implementation (ROADMAP action 4)
---

# Yahoo Market Data — Source Mechanics

## 1. Access method (decided)

Primary: **`yfinance` Python library**, symbols `{NSE_SYMBOL}.NS` (BSE `.BO` fallback).
Proven in both pilots for `.NS` daily bars; no session warm-up or TLS impersonation needed.

TBD at implementation time:

- [ ] pin `yfinance` version in `requirements.txt`; record version + adjustment flag in `fetch_runs.config`
- [ ] decide raw transport for artifact retention: persist the library's underlying HTTP
      response if accessible, else wrap the endpoint call ourselves for byte capture
      (R-YF-4 requires exact bytes; the wrapper approach is likely necessary)
- [ ] measure rate-limit behavior at ~30 then 100 symbols

## 2. Fetch shape (planned)

```
per listing partition (yahoo, listing:{id}:daily):
  backfill:   history(period="1y", interval="1d", auto_adjust=<pinned>)
  daily:      history(start={last_cursor-5td}, interval="1d")   # ≥5 trading days overlap
```

Session-mapping rule: bars are keyed by exchange trade date (Yahoo returns date-indexed
rows in IST calendar for `.NS`); store as `DATE` trade dates, not timestamps.

## 3. Failure modes (planned handling)

| Failure | Detection | Required behavior |
|---|---|---|
| Empty frame for active listing | row-count sanity vs calendar | retry next cycle ×N; alert after threshold; hold cursor |
| Delisted / symbol renamed | sustained empty + identity-correction signal | mark series closed; alert ops (links into M3 correction flow) |
| Adjusted-history rewrite by provider | hash-diff of overlapping window vs prior fetch | append revised observations; do NOT overwrite; log revision event |
| Bad bars (OHLC inversions, zero-volume on trade day) | R-YF-3 validation | reject bar loudly per run; retain raw bytes for diagnosis |
| Library/API breakage (Yahoo changes internals) | pinned-version CI smoke test | fail visibly; upgrade deliberately with re-validation |

## 4. Trading calendar

Staleness and gap detection need an NSE/BSE trading-calendar source (weekends + exchange
holidays). TBD: pick maintained holiday list (NSE website static file vs package) — required
before R-YF-5 alerts can be trusted.
