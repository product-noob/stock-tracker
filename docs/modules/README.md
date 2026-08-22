# Source Capture Modules — Master HLD

**Status:** v1.0 · **Companions:** [`../architecture-hld.md`](../architecture-hld.md) (system HLD v1.2) · [`../product_note.html`](../../product_note.html) (PRD v0.4) · [`../../nse-bse-rss.md`](../../nse-bse-rss.md) (feed inventory) · [`../../pilot_learnings.md`](../../pilot_learnings.md) (empirical findings)

---

## 1. Purpose of this doc set

Detailed, build-ready requirements for the four **source capture modules**. Each module is independently runnable for any single whitelisted stock, and all four run **in parallel** for a given stock with no ordering dependency between them.

| Module | Spec | Feeds |
|---|---|---|
| NSE capture | [`nse-capture.md`](nse-capture.md) | NSE RSS + corporate-announcements API + PDFs |
| BSE capture | [`bse-capture.md`](bse-capture.md) | BSE RSS + announcements API + PDFs |
| Google News capture | [`google-news-capture.md`](google-news-capture.md) | Google News RSS per company |
| Screener fundamentals | [`screener-fundamentals.md`](screener-fundamentals.md) | Screener.in company page snapshots |

## 2. Terminology correction (binding)

These modules are **capture** modules, not "extraction" modules. In the PRD (§6) and system HLD (M5), *extraction* exclusively means the LLM evidence-extraction stage that runs **after** text exists. Calling fetchers "extraction modules" collides with that vocabulary and will confuse contract boundaries. The specs below use:

- **capture / fetch** — network retrieval of feed/API responses and documents (M1, M4),
- **normalization** — raw items → canonical source records (M2),
- **extraction** — LLM fact extraction from text versions (M5). Out of scope here.

## 3. How the modules relate to the system HLD

Each module implements the **M1 source-adapter card** plus the fetch half of **M4 content processor**, for its source family:

```
M1 (this doc set)                    M2+ (downstream, shared)
┌──────────────┐
│ NSE capture  │──► partitions ─┐
├──────────────┤                │
│ BSE capture  │──► partitions ─┼──► normalize ──► resolve identity ──► content ──► extract ──► ...
├──────────────┤                │         (M2)           (M3)                (M4)        (M5+)
│ Google News  │──► partitions ─┤
├──────────────┤                │
│ Screener     │──► partitions ─┘
└──────────────┘
```

Modules own everything up to and including **C1 (fetched source item)**: session management, conditional GET, exact-response persistence, magic-byte content validation, and item-level idempotency. Everything from canonical records onward is shared pipeline, not module code.

## 4. Parallel execution model

### 4.1 Per-stock fan-out

Adding a stock means provisioning one partition per module, keyed off the security master (`core.listings` / `core.companies`):

```
company: HDFC Bank Limited (company_id=42)
├── (nse_rss,        category-feed partitions — market-wide, not per-stock)
├── (nse_api,        symbol:HDFCBANK)
├── (bse_rss,        category-feed partitions — market-wide)
├── (bse_api,        scrip:500180)
├── (google_news,    company:42)
└── (screener,       company:42)
```

RSS partitions are **market-wide streams** (one partition per feed URL, all companies mixed); API/news/screener partitions are **per-stock**. A stock's NSE/BSE coverage comes from the per-stock API partitions plus filtering of the shared RSS streams.

### 4.2 Independence guarantees (requirements)

1. **No cross-module ordering.** A module must never wait on another module's output. NSE and BSE both write; dedup happens downstream by content SHA-256 (proven: 95% of cross-exchange filing pairs are byte-identical — `pilot_learnings.md` §3, §19).
2. **Independent failure.** One module being down (NSE blocked, Google rate-limiting) must not stall or corrupt the others. Each partition has its own checkpoint, backoff and health signal.
3. **Idempotent writes.** Every stage derives its idempotency key per `database-schema.md` §4.3: source-native ID where present (NSE `seq_id`, BSE `NEWSID`/`scripcode`), else a documented stable tuple. Never the whole response body, never the document URL alone.
4. **Exact bytes first.** Every response (XML, JSON, PDF, HTML) is persisted as an immutable `raw_artifacts` row + object-store blob, SHA-256 registered, **before** parsing. Parsed JSONB is a query aid, never the replay source of truth.
5. **Overlap polling.** Rolling source windows are polled with an overlap margin; cursors advance only after artifact + parsed items commit (`database-schema.md` §4.1).

### 4.3 Where parallel streams converge

| Join point | Mechanism | Owner |
|---|---|---|
| Same filing on NSE + BSE | byte-identical content → same `content_objects.sha256` | M4 content processor |
| Same announcement, multiple feeds/categories | `(dedup_scope, dedup_key)` on `normalized_records` + `record_raw_items` sightings | M2 normalizer |
| Same company across sources | identity resolution ladder (M3) — scrip code → byte anchor → exact symbol/ISIN → normalized name → alias → review | M3 resolver |

Modules therefore have a hard requirement to **preserve raw identifier fields** (`company_name_raw`, `exchange_symbol_raw`, `scrip_code_raw`, `isin_raw`) even when the module "already knows" the company — the resolver, not the adapter, owns attribution (PRD §5 hard rules).

## 5. Cadence summary (per PRD §2 / HLD §8)

| Partition type | Interval | Notes |
|---|---|---|
| NSE/BSE main announcement feeds | 5 min | short rolling windows; overlap mandatory |
| NSE/BSE category feeds | 15 min | |
| Per-stock NSE/BSE API reconciliation | daily + on-demand gap fill | not a live path |
| Google News per-company query | hourly, staggered | forward-looking only |
| Screener snapshots | weekly + event-triggered | low-frequency by design |
| Prices (out of scope here) | after close | see M8 |

## 6. Corrections against v1.2 docs

Findings from the Tata Elxsi and HDFC Bank pilots (Aug 2026) that supersede statements in the companion docs. The module specs encode the corrected behavior; the system HLD is annotated, not rewritten.

| # | Doc statement | Correction (evidence) |
|---|---|---|
| 1 | HLD M1: "NSE session behavior can change" (cookie warm-up sufficient) | Akamai now 403s plain HTTP clients on the **homepage**; browser-TLS impersonation (`curl_cffi chrome`) is required for warm-up *and* API. Cookies necessary, no longer sufficient. (§19) |
| 2 | — (undocumented) | NSE `/api/corporate-announcements` rejects queries without `index=equities` (`Missing index`); response is a bare JSON array. |
| 3 | `nse-bse-rss.md`: BSE API params `strCategory/strSubCategory/strPageSize` | Contract changed: now `strCat`, `strType`, `subcategory`; `strSearch=P` for scrip-code lookup. Reverse-engineered from ann.html bundle. (§19) |
| 4 | `nse-bse-rss.md`: BSE PDFs at `AttachLive/` | Filings older than a few days live at `AttachHis/`. `AttachLive` returns **HTTP 200 with an HTML error page** for missing files — magic-byte validation is mandatory. (§1, §6) |
| 5 | PRD §2: fundamentals deferred to Phase 4 (ToS risk) | **Owner accepted the risk on 2026-08-22**: Screener automation is approved with constraints (low cadence, personal research use, kill-switch). See `screener-fundamentals.md` §2. This consciously amends the PRD scope table. |
| 6 | PRD §3: Google News "hourly" | Confirmed ~100-item cap regardless of date range — strictly forward-looking; cannot backfill history. (§1, §19) |
| 7 | — (undocumented) | Windows/console cp1252 encoding breaks on `₹` and unicode quotes; all module text I/O must be explicit UTF-8. (§19) |

## 7. Module-spec template

Every module doc follows the same skeleton so requirements are comparable:

1. **Purpose & scope** — what it owns, what it explicitly does not
2. **Sources & access mechanics** — endpoints, auth/session, observed behavior
3. **Partitions & cadence**
4. **Data contract** — fields captured, idempotency keys, mapping to C1/C2/C3
5. **Identity & dedup rules**
6. **Failure modes & reliability requirements**
7. **Acceptance criteria**
8. **Open questions**

## 8. References

- PRD: sources §3, flow §4, identity §5, data principles §9, NFR §10
- System HLD: M1/M2/M4 cards §5, contracts C1–C3 §6, timing §8, reliability §9
- Schema: `ingestion.source_partitions`, `fetch_runs`, `raw_artifacts`, `raw_feed_items`, `ingestion_cursors`, `normalized_records`, `record_raw_items`; `documents.*` for content lifecycle
- Empirical: `pilot_learnings.md` §1–§7 (Part 1), §19–§23 (Part 4); `nse-bse-rss.md` (feed URLs and item shapes)
