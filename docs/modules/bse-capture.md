# Module Spec — BSE Capture

**Status:** v1.0 · **Module card:** M1 source adapter + fetch half of M4 · **Master:** [`README.md`](README.md)

---

## 1. Purpose & scope

**Owns:** retrieval of BSE corporate announcements for a whitelisted stock via two independent partition families (RSS + API), plus download of announcement PDFs into immutable content storage.

**Produces:** C1 fetched source items and C3 content objects, symmetric with [`nse-capture.md`](nse-capture.md).

**Does not own:** company attribution (M3), cross-exchange dedup (M2/M4 — byte-identical NSE/BSE filings collapse downstream by SHA-256), extraction (M5).

## 2. Sources & access mechanics

### 2.1 Access posture

BSE (`beta.bseindia.com`, `api.bseindia.com`, `www.bseindia.com`) has been **materially lighter-weight than NSE**: no cookie warm-up was needed in the Tata Elxsi pilot (§1). However, the API contract changed without notice between Aug 2026 pilot runs (§19), so:

- R-BSE-1: Use a browser-TLS-impersonating client (same client family as NSE module) with `Referer`/`Origin` headers. Do not rely on warm-up-free behavior as a guarantee; implement warm-up-on-403 the same as NSE.
- R-BSE-2: The API contract MUST be treated as **unversioned and changeable**. Response-shape validation (envelope keys present, expected field types) gates parsing; a shape mismatch fails the run loudly rather than yielding zero items.
- R-BSE-3: Handle UTF-8 BOM in responses (observed during feed testing — `nse-bse-rss.md` limitations).

### 2.2 RSS feeds (`bse_rss` partitions)

Host: `beta.bseindia.com`. Verified inventory in [`../../nse-bse-rss.md`](../../nse-bse-rss.md):

```
https://beta.bseindia.com/data/xml/announcements.xml        (main)
https://beta.bseindia.com/Data/XML/BoardMeetingsFeed.xml
https://beta.bseindia.com/Data/XML/FinancialResultsFeed.xml
https://beta.bseindia.com/Data/XML/InsiderTradingFeed.xml
https://beta.bseindia.com/Data/XML/CorpActionFeed.xml
https://beta.bseindia.com/Data/XML/ShareholdingPattern_Feed.xml
https://beta.bseindia.com/Data/XML/VotingResultFeed.xml     (+ AnnualReportFeed, notices)
```

Item shape — note the native `scripcode` element, which makes BSE RSS identity-stronger than NSE RSS:

```xml
<item>
  <title>NTPC Green Energy Ltd (544289)</title>
  <link>https://www.bseindia.com/xml-data/corpfiling/AttachLive/...pdf</link>
  <scripcode>544289</scripcode>
  <description>Announcement text</description>
  <pubDate>22-Aug-2026 14:23:53</pubDate>
</item>
```

**Requirements:**

- R-BSE-4: One partition per feed URL; main at 5 min, categories at 15 min (mirroring NSE cadence).
- R-BSE-5: `scripcode` is a first-class identity input: filter per-stock by scrip code against `core.listings` (tier-1 resolution ladder). Preserve `title` verbatim as `company_name_raw` regardless.
- R-BSE-6: Multiple securities can reference the same attachment (funds, dual-class instruments) — never treat link equality as record equality.

### 2.3 Announcements API (`bse_api` partitions)

Endpoint, **current contract (validated 2026-08-22, supersedes `nse-bse-rss.md` §API params):**

```
GET https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w
    ?pageno=1&strCat=-1&strPrevDate=YYYYMMDD&strScrip={SCRIP}
    &strSearch=P&strToDate=YYYYMMDD&strType=C&subcategory=-1
Headers: Referer: https://www.bseindia.com/corporates/ann.html
         Origin:   https://www.bseindia.com
         Accept:   application/json, text/plain, */*
```

Contract-change history (why R-BSE-2 exists): old params `strCategory`/`strSubCategory`/`strPageSize` now yield an empty `{}` with HTTP 200. New params reverse-engineered from the Angular bundle of `ann.html`.

Response envelope: `{"Table": [...], "Table1": [{"ROWCNT": n}]}` — items are under `Table`, total count under `Table1[0].ROWCNT`.

Item fields used downstream: `NEWSID` (native ID), `SCRIP_CD`, `SLONGNAME`, `HEADLINE`, `CATEGORYNAME`, `ATTACHMENTNAME`, `NEWS_DT` (ISO with fractional seconds).

**Requirements:**

- R-BSE-7: Unwrap the `Table` envelope; treat missing `Table` key as shape failure (R-BSE-2).
- R-BSE-8: Use `ROWCNT` for pagination completeness: fetch until collected == ROWCNT or pages exhausted; a shortfall is a run error, not silent truncation.
- R-BSE-9: An empty `Table` with HTTP 200 is ambiguous (no data vs contract drift) — disambiguate by validating against a known-active scrip control query before accepting "no data" on a partition.

### 2.4 PDF download — the AttachHis trap

BSE filing PDFs live under two paths depending on age:

```
https://www.bseindia.com/xml-data/corpfiling/AttachLive/{ATTACHMENTNAME}   ← recent filings only
https://www.bseindia.com/xml-data/corpfiling/AttachHis/{ATTACHMENTNAME}    ← archive (few days+)
```

Critical observed behavior (`pilot_learnings.md` §1, §6): **`AttachLive` returns HTTP 200 with an HTML error page when the file is absent.**

- R-BSE-10: Try `AttachHis` first, then `AttachLive` (archive-first ordering minimizes garbage downloads).
- R-BSE-11: Validate magic bytes (`%PDF`) after every download; a 200-with-HTML is a failed attempt whose bytes are retained as evidence.
- R-BSE-12: Register valid bytes as content objects keyed by SHA-256; failed attempts go to `documents.content_fetch_attempts` with resolved URL + outcome.

## 3. Partitions & cadence

| Partition key | Interval | Overlap |
|---|---|---|
| `(bse_rss, announcements.xml)` | 300 s | ≥ 2× interval |
| `(bse_rss, <Category>Feed.xml)` × ~8 | 900 s | same |
| `(bse_api, scrip:{SCRIP})` | 6 h rolling | 24 h overlap |
| `(bse_api, scrip:{SCRIP}) backfill` | manual / reconciliation | explicit range |

Cursor rules identical to NSE module (advance only after artifact + items commit).

## 4. Data contract

| Source field | Canonical field | Notes |
|---|---|---|
| `NEWSID` (API) / — (RSS) | idempotency key | native: `bse_ann:{NEWSID}`; RSS falls back to stable tuple `{link}+{pubDate}+{scripcode}` |
| `SCRIP_CD` / `scripcode` | listing identifier (tier-1) + preserved raw | |
| `SLONGNAME` / `title` | `company_name_raw` | verbatim |
| `HEADLINE` | `description` | |
| `CATEGORYNAME` | `category_raw` | metadata only, never event classification |
| `ATTACHMENTNAME` → URL | `content_url` | not a dedup key |
| `NEWS_DT` / `pubDate` | `published_at` | IST; store UTC |

- R-BSE-13: `dedup_scope = bse`; same-announcement multi-feed sightings collapse via `record_raw_items`.
- R-BSE-14: The API returns no ISIN field — `isin_raw` stays null from this module. ISIN arrives either via security-master join (M3) or PDF text extraction later (PRD §7 Tier C). Do not fabricate it.

## 5. Identity & dedup rules

- R-BSE-15: Scrip-code partition pre-fill follows the same agree-in-transaction rule as NSE (R-NSE-20).
- R-BSE-16: Cross-exchange dedup is downstream-only; this module never sees NSE data.
- R-BSE-17: One attachment shared by several securities produces several *records* but one *content object* — the M:N `record_contents` model must absorb this (schema already supports it).

## 6. Failure modes & reliability requirements

| Failure | Detection | Required behavior |
|---|---|---|
| Contract drift (params renamed again) | envelope/shape validation + control-scrip canary | fail loudly; alert; freeze cursor; do not write zero-item success |
| `{}` empty response ambiguity | control query (R-BSE-9) | distinguish "no news" from "broken" before checkpoint advance |
| AttachLive HTML-as-200 | magic bytes | failed attempt + evidence bytes; archive-path retry |
| beta hostname changes | DNS/TLS monitoring + canary | alert (flagged risk since `nse-bse-rss.md`); config-driven host override for fast patch |
| Feed item-count collapse | depth telemetry | widen overlap; hold cursor |
| BOM/encoding parse errors | strict parse with fallback decode | normalize once, log variant |

## 7. Acceptance criteria

1. Symmetric with NSE: seven unattended days, worker-kill tests, zero stranded records.
2. Sampled-week reconciliation vs BSE-API truth set ≥95% with explained misses.
3. Zero wrong-company attributions; scrip-code tier resolves ≥99% of records without review.
4. Every `AttachLive`-era HTML-garbage download retained as evidence and counted; invalid-content rate <1%.
5. Offline reparse of sampled artifacts reproduces identical items.

## 8. Open questions

1. Is there a documented BSE API changelog or partner program that would make contract changes observable in advance? (Current method is bundle-diffing.)
2. `notices.xml` and `AnnualReportFeed.xml`: include as partitions now or defer to breadth phase?
3. Rate-limit ceiling for per-scrip API polling across a 100-company whitelist — measure before Phase 3 scale-up.
