---
tier: L2
module: bse-capture
doc: source-mechanics
parent: OVERVIEW.md
updated: 2026-08-23
---

# BSE Capture — Source Mechanics

Endpoints, contract-drift posture, download paths, failure modes.
Feed inventory: [`../../../nse-bse-rss.md`](../../../nse-bse-rss.md).

## 1. Access posture

BSE (`beta.bseindia.com`, `api.bseindia.com`, `www.bseindia.com`) has been **lighter-weight
than NSE** — no cookie warm-up needed in the Tata Elxsi pilot. But the API contract changed
without notice between Aug 2026 pilot runs, so:

| Req | Rule |
|---|---|
| R-BSE-1 | Browser-TLS-impersonating client (same family as NSE) with `Referer`/`Origin` headers. Implement warm-up-on-403 like NSE; do not rely on warm-up-free behavior. |
| R-BSE-2 | The API contract is **unversioned and changeable**. Response-shape validation gates parsing; shape mismatch fails the run loudly, never yields zero items silently. |
| R-BSE-3 | Handle UTF-8 BOM in responses (observed in feed testing). |

## 2. RSS feeds (`bse_rss`)

Host: `beta.bseindia.com`. Verified inventory:

```
https://beta.bseindia.com/data/xml/announcements.xml        (main)
https://beta.bseindia.com/Data/XML/BoardMeetingsFeed.xml
https://beta.bseindia.com/Data/XML/FinancialResultsFeed.xml
https://beta.bseindia.com/Data/XML/InsiderTradingFeed.xml
https://beta.bseindia.com/Data/XML/CorpActionFeed.xml
https://beta.bseindia.com/Data/XML/ShareholdingPattern_Feed.xml
https://beta.bseindia.com/Data/XML/VotingResultFeed.xml     (+ AnnualReportFeed, notices)
```

Item shape — native `scripcode` makes BSE RSS identity-stronger than NSE RSS:

```xml
<item>
  <title>NTPC Green Energy Ltd (544289)</title>
  <link>https://www.bseindia.com/xml-data/corpfiling/AttachLive/...pdf</link>
  <scripcode>544289</scripcode>
  <description>Announcement text</description>
  <pubDate>22-Aug-2026 14:23:53</pubDate>
</item>
```

| Req | Rule |
|---|---|
| R-BSE-4 | One partition per feed URL; main 5 min, categories 15 min (mirroring NSE). |
| R-BSE-5 | `scripcode` is a first-class identity input: per-stock filter against `core.listings` (tier-1 ladder). Preserve `title` verbatim as `company_name_raw` regardless. |
| R-BSE-6 | Multiple securities can reference the same attachment — never treat link equality as record equality. |

## 3. Announcements API (`bse_api`)

**Current contract (validated 2026-08-22, supersedes `nse-bse-rss.md` §API params):**

```
GET https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w
    ?pageno=1&strCat=-1&strPrevDate=YYYYMMDD&strScrip={SCRIP}
    &strSearch=P&strToDate=YYYYMMDD&strType=C&subcategory=-1
Headers: Referer: https://www.bseindia.com/corporates/ann.html
         Origin:   https://www.bseindia.com
         Accept:   application/json, text/plain, */*
```

Contract-change history (why R-BSE-2 exists): old params `strCategory`/`strSubCategory`/
`strPageSize` now yield empty `{}` with HTTP 200. New params reverse-engineered from the
Angular bundle of `ann.html`.

Envelope: `{"Table": [...], "Table1": [{"ROWCNT": n}]}` — items under `Table`, total count
under `Table1[0].ROWCNT`. Downstream fields: `NEWSID`, `SCRIP_CD`, `SLONGNAME`, `HEADLINE`,
`CATEGORYNAME`, `ATTACHMENTNAME`, `NEWS_DT` (ISO with fractional seconds).

| Req | Rule |
|---|---|
| R-BSE-7 | Unwrap the `Table` envelope; missing `Table` key = shape failure (R-BSE-2). |
| R-BSE-8 | Use `ROWCNT` for pagination completeness: fetch until collected == ROWCNT or pages exhausted; shortfall = run error, not silent truncation. |
| R-BSE-9 | Empty `Table` with HTTP 200 is ambiguous (no data vs contract drift) — disambiguate via a known-active scrip control query before accepting "no data". |

## 4. PDF download — the AttachHis trap

```
https://www.bseindia.com/xml-data/corpfiling/AttachLive/{ATTACHMENTNAME}   ← recent filings only
https://www.bseindia.com/xml-data/corpfiling/AttachHis/{ATTACHMENTNAME}    ← archive (few days+)
```

Critical: **AttachLive returns HTTP 200 with an HTML error page when the file is absent**
(Part 1 §1, §6).

| Req | Rule |
|---|---|
| R-BSE-10 | Try `AttachHis` first, then `AttachLive` (archive-first minimizes garbage downloads). |
| R-BSE-11 | Validate magic bytes (`%PDF`) after every download; 200-with-HTML = failed attempt, bytes retained as evidence. |
| R-BSE-12 | Register valid bytes as content objects keyed by SHA-256; failed attempts to `documents.content_fetch_attempts` with resolved URL + outcome. |

## 5. Failure modes & reliability

| Failure | Detection | Required behavior |
|---|---|---|
| Contract drift (params renamed again) | envelope/shape validation + control-scrip canary | fail loudly; alert; freeze cursor; no zero-item success |
| `{}` empty-response ambiguity | control query (R-BSE-9) | distinguish "no news" from "broken" before checkpoint advance |
| AttachLive HTML-as-200 | magic bytes | failed attempt + evidence bytes; archive-path retry |
| beta hostname changes | DNS/TLS monitoring + canary | alert; config-driven host override for fast patch |
| Feed item-count collapse | depth telemetry | widen overlap; hold cursor |
| BOM/encoding parse errors | strict parse with fallback decode | normalize once, log variant |

### Implementation status (2026-08-23)

Envelope/`ROWCNT` validation now enforces R-BSE-2/7/8, including stable denominators and
mandatory `NEWSID`. Other requirements remain incomplete: page-to-row artifact provenance
is wrong in mixed reuse, parsing precedes durable registration, the empty canary occurs
before foreign-scrip filtering, invalid HTML suppresses future retries, and downloads are
unbounded. Invalid bytes are raw evidence but no canonical failed-attempt row exists. See
review `ST-C01`–`ST-C05`.
