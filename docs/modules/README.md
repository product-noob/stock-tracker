---
tier: L1
role: domain-master (capture)
updated: 2026-08-23
system-hld: architecture-hld.md §5 (M1/M4 cards)
---

# Capture Modules — Domain Master

Build-ready requirements for the five source-capture modules. This document describes the
approved contract; it is not a claim that all modules satisfy it. Per-module scope and
implementation gaps live in each `OVERVIEW.md` and the comprehensive review.

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

## 4. Implementation status (verified 2026-08-23)

| Module | Implemented path | Missing / non-conforming boundary |
|---|---|---|
| NSE | per-company `nse_api` DB adapter + PDF artifacts | RSS absent; daily key suppresses six-hour runs; exact-bytes ordering, validation and canonical M4 attempts/links open |
| BSE | per-company `bse_api` DB adapter + PDF/invalid artifacts | RSS absent; daily key, page provenance, invalid retry, validation and canonical M4 attempts/links open |
| Google News | file feed pilot and file redirect cache | no DB adapter/article fetch; transient resolver failures become permanently cached |
| Screener | file-based manual HTML/parser utility | no DB snapshot schema/adapter/schedule; parser/content canaries incomplete |
| Yahoo/yfinance | DB-integrated daily adapter, 30 listing series at audit sample | returned-frame CSV is not exact transport; finite/current-session/calendar/revision/benchmark gates open |

The object store, processing-job worker, normalizer and resolver exist. Outbox dispatch,
scheduler/supervision, canonical content attempts/links, and downstream M4–M10 remain
incomplete. “DB-integrated” does not mean an acceptance gate passed.

## 5. Cadence summary

This table is the **required cadence**, except where noted. Current NSE/BSE API code uses a
per-IST-day run key and therefore fetches at most once normally per day; reconcile that gap
before unattended scheduling.

| Partition type | Required interval |
|---|---|
| NSE/BSE main announcement feeds | 5 min |
| NSE/BSE category feeds | 15 min |
| Per-stock NSE/BSE API reconciliation | design decision open: module L1 says 6 h; current key is daily; product framing says daily + on-demand |
| Google News per-company query | hourly, staggered (forward-looking only) |
| Screener snapshots | owner approved full scope; choose measured ban-safe cadence before DB integration (historical weekly compliance constraint superseded) |
| Yahoo daily bars | once per day after close (+ backfill on provisioning) |
| Prices via exchange bhavcopy (future reconciliation) | weekly sample, Phase 2+ |

## 6. Cross-source corrections ledger

Findings from the Aug-2026 pilots that supersede statements in companion docs. Module-level
detail lives in each module's `evidence-log.md`; this ledger records only cross-module or
doc-amending items.

| # | Doc statement superseded | Correction (evidence) |
|---|---|---|
| 1 | HLD M1: cookie warm-up sufficient for NSE | Akamai 403s plain clients on homepage; browser-TLS impersonation required for warm-up AND API. (Part 5 §19) |
| 2 | — | NSE `/api/corporate-announcements` requires `index=equities`; returns bare JSON array. (Part 5 §19) |
| 3 | `nse-bse-rss.md`: BSE API params `strCategory`/`strSubCategory`/`strPageSize` | Now `strCat`, `strType`, `subcategory`; `strSearch=P` for scrip lookup. (Part 5 §19) |
| 4 | `nse-bse-rss.md`: BSE PDFs at `AttachLive/` | Older filings at `AttachHis/`; AttachLive returns HTTP 200 HTML error page when absent — magic-byte validation mandatory. (Part 1 §1, §6) |
| 5 | PRD §2: fundamentals DEFERRED to Phase 4 | Historical: owner accepted risk 2026-08-22 under mandatory constraints; superseded by ledger item 10. |
| 6 | PRD §3: Google News "hourly" implies depth | ~100-item cap regardless of date range — strictly forward-looking. (Part 1 §1, Part 5 §19) |
| 7 | — | Windows cp1252 encoding breaks on `₹`; all module text I/O explicit UTF-8. (Part 5 §19) |
| 8 | PRD §14 open question "NSE bhavcopy or yfinance?" | Resolved: yfinance primary daily provider; bhavcopy = Phase 2+ reconciliation cross-check. See ROADMAP decision log. |
| 9 | BSE module: archive-first ordering for all filings | GUID-named filings (newer) exist only under AttachLive (His = 404 HTML); variant order is now filename-shape-aware. Invalid captures no longer count as "handled". (Part 5 evidence-log 2026-08-23) |
| 9 | HLD/module contracts could be read as implementation status | 2026-08-23 review proved material gaps in exact-byte ordering, outbox dispatch, attempts/links, cadence and test isolation. Requirements remain binding; status lives in §4 and module OVERVIEWs. |
| 10 | 2026-08-22 Screener weekly constraints | Owner's 2026-08-23 full-scope directive supersedes them as compliance conditions; pacing/backoff remains required to avoid a ban. |
| 11 | R-BSE-10 archive-first for every attachment | 2026-08-23 verified GUID-named filings live under AttachLive: GUIDs are Live-first; legacy numeric names remain His-first. Invalid-only captures retry. |

## 7. References

- System HLD: M1/M2/M4 cards §5, contracts C0–C7 §6, timing §8, reliability §9
- Schema: `ingestion.source_partitions`, `fetch_runs`, `raw_artifacts`, `raw_feed_items`,
  `ingestion_cursors`, `normalized_records`, `record_raw_items`; `documents.*`;
  `market.price_series`/`price_bars`
- Empirical: `pilot_learnings.md` Parts 1–5 (cite as Part N §M)
