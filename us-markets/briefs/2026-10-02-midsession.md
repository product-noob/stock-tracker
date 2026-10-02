# Mid-session check — Fri 2026-10-02 (~1:45 PM IST)

> **Test run note:** this run was executed at ~1:45 PM IST, while the US market was
> closed (regular session opens 7:00 PM IST). Figures below reflect the last
> completed regular session (Thu 2026-10-01). There is no intraday movement to
> evaluate; this run validates the check format end-to-end.

## Snapshot (last session, via Finnhub)

| Ticker | Price | vs prev close | Move |
|--------|-------|---------------|------|
| MSFT (holding) | 512.80 | -0.10 | -0.02% |
| GOOGL (holding) | 338.24 | -5.84 | -1.70% |
| AAPL (holding) | 330.32 | -2.70 | -0.81% |
| NVDA (holding) | 230.86 | +2.48 | +1.09% |
| AMZN (watch) | 248.23 | -0.92 | -0.37% |
| META (watch) | 725.93 | +0.75 | +0.10% |
| TSM (watch) | 459.20 | +3.01 | +0.66% |
| ASML (watch) | 1,808.49 | -3.18 | -0.18% |

## Alert evaluation

Alert rule: flag a name if its intraday move is ≥3% in either direction, or ≥2%
with clear news behind it.

**No alerts.** Largest move in the universe was GOOGL at -1.70%, below both
thresholds. NVDA (+1.09%), AAPL (-0.81%), TSM (+0.66%), AMZN (-0.37%), ASML
(-0.18%), META (+0.10%), and MSFT (-0.02%) all traded within a normal range.

## Source

Prices: Finnhub via web search finance vertical, retrieved 2026-10-02 ~1:45 PM
IST. Data may be delayed ~15 min. No price was invented; all figures are quoted
from the source above.

## Notes for next live run

- The real 10:30 PM IST run should compare the 1:00 PM ET intraday quote against
  the same "Previous Close Price" figures recorded here.
- `data/state.json` `last_prices` has been seeded with this run's reference
  prices so the next run has a baseline.
