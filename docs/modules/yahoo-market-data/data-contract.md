---
tier: L2
module: yahoo-market-data
doc: data-contract
parent: OVERVIEW.md
updated: 2026-08-23
---

# Yahoo Market Data — Data Contract

Mapping yfinance output into `market.price_series` / `market.price_bars`, and idempotency rules.
Schema context: [`../../database-schema.md`](../../database-schema.md) §9.

## 1. Series registration

One row per `(listing, interval, adjustment-mode)`:

| `price_series` column | Value |
|---|---|
| `provider` | `yfinance` |
| `provider_symbol` | `{NSE_SYMBOL}.NS` (or `.BO`) |
| `interval` | `1d` |
| `adjustment_mode` | pinned constant from implementation (e.g. `adjusted_close_v1`) — never mixed within a series |
| `currency` | `INR` |
| listing | FK to `core.listings` row for the exact venue |

## 2. Bar mapping

| Yahoo frame column | Canonical field | Notes |
|---|---|---|
| `Date` index | `trade_date` | exchange-calendar DATE, not timestamp |
| `Open/High/Low/Close` | ohlc numerics | validate OHLC sanity per R-YF-3 |
| `Volume` | `volume` | ≥ 0; zero allowed only on genuine trade dates with no trades? TBD verify semantics |
| `Adj Close` (when unadjusted mode) | separate series or derived — pin decision at implementation | |

## 3. Idempotency & revision model

| Req | Rule |
|---|---|
| R-YF-7 | Idempotency key: `(series_id, trade_date, observation_hash)` where `observation_hash = sha256(ohlc+volume+asof)`. Re-fetching unchanged bars is a no-op. |
| R-YF-8 | A changed hash on an existing `(series_id, trade_date)` inserts a NEW bar observation (append-only); `current_price_bars` view flips to the latest. Prior reaction signals keep their original bar IDs untouched. |

## 4. Implementation reconciliation (2026-08-23)

`scripts/fetch_prices.py` implements the series/bar mapping with `source='yfinance'`,
`auto_adjust=True`, adjustment mode `auto_adjusted`, and a deterministic observation hash.
The artifact is a canonical CSV serialization of the returned frame rather than exact HTTP
transport. Required validation is only partial: non-finite OHLC, duplicate dates and a
still-forming pre-close daily frame are not rejected. Append-only revision semantics exist
in code/schema but have not been integration-tested in an isolated database.
