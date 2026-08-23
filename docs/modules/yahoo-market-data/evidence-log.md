---
tier: L2
module: yahoo-market-data
doc: evidence-log
parent: OVERVIEW.md
policy: append-only — dated entries, newest last; supersede via new entry + marker, never rewrite
---

# Yahoo Market Data — Evidence Log

Empirical findings. Cite as `(YF evidence-log, YYYY-MM-DD)`.

## 2026-08-21 — Tata Elxsi pilot (Part 3)

- `TATAELXSI.NS` daily bars fetched ad hoc (`ohlcv_3mo.csv`) and used in the price-reaction
  prototype; z-score math reproduced hand checks.
- Corporate-action sensitivity noted: adjusted vs raw close materially changes multi-month
  baselines → motivates the series-level adjustment-mode rule (R-YF-1).

## 2026-08-22 — HDFC Bank run (Part 5 §21–22)

- `.NS` bars supported the modal-anchor validation: Saturday board outcome correctly mapped
  to Monday 20 Jul session (−5.12%, z=−3.79). Bars were clean enough that all discrepancies
  traced to anchor logic, not data quality.
- No access friction observed (contrast NSE/BSE Akamai posture).

## Open observations (recorded before implementation)

- Volume-zero semantics on some sessions unverified.
- Provider silent-history-revision behavior not yet observed firsthand; fixture test planned.
- Trading-calendar source for holiday handling not chosen yet.

## 2026-08-23 — DB-integrated yfinance adapter and review

- `scripts/fetch_prices.py` now writes `yfinance` partitions, 30 listing series, append-only
  bars and deterministic returned-frame CSV artifacts. Read-only audit sample: 7,529 current
  bars across 30 series, 2025-08-21..2026-08-21, with no observed invalid OHLC,
  weekend/future bars, missing artifact lineage, revisions or cursor failures.
- **Contract clarification:** retained CSV is an exact deterministic serialization of the
  returned DataFrame, not exact Yahoo HTTP transport bytes. R-YF-4 remains unmet unless the
  architecture accepts this weaker provenance contract.
- Synthetic review proved `validate_frame()` accepts NaN/Infinity; duplicate dates and the
  current still-forming pre-close frame are also insufficiently gated. These are prospective
  corruption risks; no bad live bars were observed.
- Trading-calendar, explicit history timeout/retry, provider-revision integration fixture,
  benchmark series and no-look-ahead M8 remain open.
