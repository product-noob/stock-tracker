---
tier: L2
module: screener-fundamentals
doc: source-mechanics
parent: OVERVIEW.md
updated: 2026-08-22
---

# Screener Fundamentals — Source Mechanics

Endpoint behavior, client posture, page regions captured, failure modes.

## 1. Endpoint & access mechanics

```
GET https://www.screener.in/company/{SYMBOL}/consolidated/
```

Observed (Tata Elxsi pilot): plain request with browser UA returned the real page —
no blocking in one-off pulls. **No sustained-polling evidence exists**; the weekly cadence
and canary reflect that.

| Req | Rule |
|---|---|
| R-SC-6 | URL mapping per company from `core.listings` (NSE symbol) plus curated `screener_url_slug` partition config — slugs occasionally differ from NSE symbols (suffixes like `-RESD`); never auto-construct blindly; verify first-fetch title matches the company. |
| R-SC-7 | Browser-TLS-impersonating client (same family as NSE/BSE modules) with browser UA; on 403/anti-bot interstitial back off and mark partition degraded — **no evasion escalation**; the risk acceptance does not cover active-protection circumvention. |

## 2. Page regions captured

| Region | Structured fields (v1) |
|---|---|
| Top ratios strip | market cap, current price, high/low, stock P/E, book value, ROE, face value, dividend yield |
| Quarterly results table | per-quarter: sales, expenses, net profit, EPS, op margin, OPM %, other income, interest, depreciation, PBT, tax, net profit |
| Annual P&L (10-yr) | same columns annually |
| Balance sheet / cash flow | annual lines as displayed |
| Shareholding | promoter holding %, FII, DII, government, public — quarterly |
| Pros & cons, peer comparison | stored as text snapshot (context only) |

Parser note: current implementation (`scripts/fetch_fundamentals.py`) is stdlib-only
(`html.parser.HTMLParser`); every `<h2>` starts a section, every `table.data-table` under it
becomes `{row_label: {col_key: value}}` keyed by `data-date-key`.

## 3. Failure modes & reliability

| Failure | Detection | Required behavior |
|---|---|---|
| Anti-bot interstitial / 403 | status + content sniff | mark degraded, backoff to next window, alert; no evasion escalation (R-SC-7) |
| Layout change breaks parser | schema validation on parsed output + field-null-rate monitor | keep artifact (replayable), fail parse loudly, alert |
| Slug mismatch / wrong company page | first-fetch title-vs-company check (R-SC-6) | hard-fail partition provisioning |
| Unit/scale ambiguity | unit-label presence check | null the value rather than guess |
| Silent history revision by Screener | artifact-hash diff across snapshots | append new version; surface "revised" flag on comparison |
