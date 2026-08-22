---
tier: L1
role: domain-master (capture)
updated: 2026-08-22
system-hld: architecture-hld.md §5 (M1/M4 cards)
---

# Capture Modules — Domain Master

Build-ready requirements for the five source-capture modules. Each module is independently
runnable per whitelisted stock; all five run **in parallel** with no cross-module ordering.
Per-module scope lives in each `OVERVIEW.md`; this file holds only what is true across modules.

| Module | Entry |
|---|---|
| NSE announcements | [`nse-capture/OVERVIEW.md`](nse-capture/OVERVIEW.md) |
| BSE announcements | [`bse-capture/OVERVIEW.md`](bse-capture/OVERVIEW.md) |
| Google News | [`google-news-capture/OVERVIEW.md`](google-news-capture/OVERVIEW.md) |
| Screener fundamentals | [`screener-fundamentals/OVERVIEW.md`](screener-fundamentals/OVERVIEW.md) |
| Yahoo market data | [`yahoo-market-data/OVERVIEW.md`](yahoo-market-data/OVERVIEW.md) |

## 1. Terminology correction (binding)

These are **capture** modules, not "extraction" modules. In the PRD (§6) and system HLD (M5),
*extraction* exclusively means the LLM evidence-extraction stage that runs after text exists.
Use:

- **capture / fetch** — network retrieval of feed/API responses and documents (M1, fetch half of M4);
- **normalization** — raw items → canonical source records (M2);
- **extraction** — LLM fact extraction from text versions (M5). Out of scope for this entire doc set.

## 2. Relation to the system HLD

Each module implements the **M1 source-adapter card** plus the **fetch half of M4**
(content processor): session management, conditional GET, exact-response persistence,
magic-byte validation, item idempotency. Modules own everything up to and including
C1 (fetched source items) / C3 (content objects). Everything from canonical records onward
is shared pipeline, not module code.

```
┌──────────────┐
│ NSE capture  │──► partitions ─┐
├──────────────┤                │
│ BSE capture  │──► partitions ─┼──► normalize ──► resolve identity ──► content ──► extract ──► ...
├──────────────┤                │         (M2)           (M3)                (M4)        (M5+)
│ Google News  │──► partitions ─┤
├──────────────┤                │
│ Screener     │──► partitions ─┘
│ Yahoo prices │──► partitions ─┘   (feeds M8 directly: market.price_series/price_bars)
└──────────────┘
```

## 3. Parallel execution model

### Per-stock fan-out

Adding a stock provisions one partition per module off the security master
(`core.companies` / `core.listings`):

```
company: HDFC Bank Limited (company_id=42)
├── (nse_rss,   category-feed partitions)   ← market-wide streams, filtered at parse time
├── (nse_api,   symbol:HDFCBANK)
├── (bse_rss,   category-feed partitions)   ← market-wide
├── (bse_api,   scrip:500180)
├── (google_news, company:42)
├── (screener,  company:42)
└── (yahoo,     listing:NSE:HDFCBANK series)
```

RSS partitions are market-wide (one partition per feed URL); API/news/screener/yahoo
partitions are per-stock.

### Independence guarantees (requirements)

1. **No cross-module ordering.** A module never waits on another module's output.
2. **Independent failure.** One module down must not stall or corrupt others; each partition
   has its own checkpoint, backoff and health signal.
3. **Idempotent writes.** Idempotency keys per `database-schema.md` §4.3: source-native ID
   where present, else a documented stable tuple. Never the whole response body, never a
   bare document URL.
4. **Exact bytes first.** Every response persisted as immutable artifact + SHA-256 registration
   before parsing.
5. **Overlap polling.** Cursors advance only after artifact + parsed items commit.

### Where parallel streams converge

| Join point | Mechanism | Owner |
|---|---|---|
| Same filing on NSE + BSE | byte-identical content → same `content_objects.sha256` (proven ~95% of pairs identical: pilot Part 5 §19, Part 1 §3) | M4 |
| Same announcement, multiple feeds/categories | `(dedup_scope, dedup_key)` + `record_raw_items` sightings | M2 |
| Same company across sources | identity resolution ladder (scrip code → byte anchor → exact symbol/ISIN → normalized name → alias → review) | M3 |

Hard consequence: every module MUST preserve raw identifier fields (`company_name_raw`,
`exchange_symbol_raw`, `scrip_code_raw`, `isin_raw`) even when it "already knows" the
company — the resolver, not the adapter, owns attribution.

## 4. Cadence summary

| Partition type | Interval |
|---|---|
| NSE/BSE main announcement feeds | 5 min |
| NSE/BSE category feeds | 15 min |
| Per-stock NSE/BSE API reconciliation | daily + on-demand gap fill |
| Google News per-company query | hourly, staggered (forward-looking only) |
| Screener snapshots | weekly + event-triggered (risk-accepted constraints) |
| Yahoo daily bars | once per day after close (+ backfill on provisioning) |
| Prices via exchange bhavcopy (future reconciliation) | weekly sample, Phase 2+ |

## 5. Cross-source corrections ledger

Findings from the Aug-2026 pilots that supersede statements in companion docs. Module-level
detail lives in each module's `evidence-log.md`; this ledger records only cross-module or
doc-amending items.

| # | Doc statement superseded | Correction (evidence) |
|---|---|---|
| 1 | HLD M1: cookie warm-up sufficient for NSE | Akamai 403s plain clients on homepage; browser-TLS impersonation required for warm-up AND API. (Part 5 §19) |
| 2 | — | NSE `/api/corporate-announcements` requires `index=equities`; returns bare JSON array. (Part 5 §19) |
| 3 | `nse-bse-rss.md`: BSE API params `strCategory`/`strSubCategory`/`strPageSize` | Now `strCat`, `strType`, `subcategory`; `strSearch=P` for scrip lookup. (Part 5 §19) |
| 4 | `nse-bse-rss.md`: BSE PDFs at `AttachLive/` | Older filings at `AttachHis/`; AttachLive returns HTTP 200 HTML error page when absent — magic-byte validation mandatory. (Part 1 §1, §6) |
| 5 | PRD §2: fundamentals DEFERRED to Phase 4 | Owner accepted risk 2026-08-22; Screener approved under mandatory constraints. Amends PRD scope table. |
| 6 | PRD §3: Google News "hourly" implies depth | ~100-item cap regardless of date range — strictly forward-looking. (Part 1 §1, Part 5 §19) |
| 7 | — | Windows cp1252 encoding breaks on `₹`; all module text I/O explicit UTF-8. (Part 5 §19) |
| 8 | PRD §14 open question "NSE bhavcopy or yfinance?" | Resolved: yfinance primary daily provider; bhavcopy = Phase 2+ reconciliation cross-check. See ROADMAP decision log. |

## 6. References

- System HLD: M1/M2/M4 cards §5, contracts C0–C7 §6, timing §8, reliability §9
- Schema: `ingestion.source_partitions`, `fetch_runs`, `raw_artifacts`, `raw_feed_items`,
  `ingestion_cursors`, `normalized_records`, `record_raw_items`; `documents.*`;
  `market.price_series`/`price_bars`
- Empirical: `pilot_learnings.md` Parts 1–5 (cite as Part N §M)
