---
tier: L2
module: nse-capture
doc: source-mechanics
parent: OVERVIEW.md
updated: 2026-08-23
---

# NSE Capture — Source Mechanics

Endpoints, session/TLS behavior, download paths, and failure-mode handling.
Feed inventory: [`../../../nse-bse-rss.md`](../../../nse-bse-rss.md).

## 1. Anti-bot reality (supersedes earlier notes)

NSE (`www.nseindia.com`, `nsearchives.nseindia.com`) sits behind **Akamai**. Observed,
HDFC Bank run 2026-08-22 (pilot Part 5 §19):

- plain `curl` / python-`requests` → **HTTP 403 even on the homepage**, only an `AKA_A2` cookie set;
- `curl_cffi impersonate="chrome"` (browser TLS fingerprint) passes warm-up and all API calls,
  receiving the full Akamai cookie set (`AKA_A2`, `_abck`, `ak_bmsc`, `bm_sz`);
- cookies alone are **not sufficient** — the TLS handshake must look like a browser too.

| Req | Rule |
|---|---|
| R-NSE-1 | All NSE HTTP traffic MUST use a browser-TLS-impersonating client. A session warm-up GET to `https://www.nseindia.com/` MUST precede API calls in the same session. |
| R-NSE-2 | Warm-up non-200 = partition-level failure with exponential backoff — never retry the API without re-warming. |
| R-NSE-3 | Session/cookie state is per-partition, never shared across partitions or workers. |
| R-NSE-4 | Impersonation target version pinned in config and recorded in `fetch_runs.config` (Akamai fingerprints evolve). |

## 2. RSS feeds (`nse_rss`)

Host: `nsearchives.nseindia.com`. Main stream:

```
https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml   (~280 items observed)
```

Category feeds (each its own partition): `Board_Meetings`, `Financial_Results`,
`InsiderTrading`, `Corporate_action`, `Corporate_Governance`, `Shareholding_Pattern`,
`Voting_Results`, `Sast_*`, `Related_Party_Trans`, `Offer_Documents`, etc.

Item shape:

```xml
<item>
  <title>Balaji Phosphates Limited</title>
  <link>https://nsearchives.nseindia.com/corporate/...pdf</link>
  <description>Announcement text |SUBJECT: Trading Window</description>
  <pubDate>22-Aug-2026 14:26:26</pubDate>
</item>
```

| Req | Rule |
|---|---|
| R-NSE-5 | One partition per feed URL. Main at 5 min; category feeds at 15 min. |
| R-NSE-6 | Per-stock filtering at parse time by matching `title` against security master (normalized name + aliases); a miss goes to review, never silently dropped. |
| R-NSE-7 | NSE RSS items carry **no symbol/scrip code** — identity relies on company-name matching from title, making RSS the *weakest* identity path; preserve `company_name_raw` verbatim for M3. |
| R-NSE-8 | Handle encoding variants (BOM, entity escapes) per `nse-bse-rss.md` limitations. |

## 3. Announcements API (`nse_api`)

Endpoint (validated 2026-08-22):

```
GET https://www.nseindia.com/api/corporate-announcements
    ?symbol={NSE_SYMBOL}&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY&index=equities
Headers: Referer: https://www.nseindia.com/all-corporate-announcements
         Accept: application/json, text/plain, */*
         X-Requested-With: XMLHttpRequest
```

Gotchas (all mandatory):

| Req | Rule |
|---|---|
| R-NSE-9 | `index=equities` REQUIRED. Without it: HTTP 200 with body `'Missing index'` (text/html). |
| R-NSE-10 | Success returns a **bare JSON array**, not an envelope. Never assume `{"data": [...]}`. |
| R-NSE-11 | HTTP 200 with non-JSON/empty body after warm-up = stale session → re-warm once, then fail the run. Never parse garbage as zero items silently; log as anomaly. |

Downstream-used item fields: `seq_id`, `symbol`, `sm_isin`, `sm_name`, `desc`,
`attchmntText`, `attchmntFile` (PDF URL), `an_dt` (`%d-%b-%Y %H:%M:%S`).

## 4. Attachment download

| Req | Rule |
|---|---|
| R-NSE-12 | Download `attchmntFile` with `Referer: https://www.nseindia.com/` on an impersonated session; re-warm + retry once on any 403. |
| R-NSE-13 | Validate content by **magic bytes** (`%PDF`) and media type, never HTTP status alone. |
| R-NSE-14 | Failed/invalid downloads persist as failed attempts with exact bytes retained — an HTML block page is evidence, not trash. |
| R-NSE-15 | Valid bytes registered as content objects keyed by SHA-256 before any parsing. |

## 5. Failure modes & reliability

| Failure | Detection | Required behavior |
|---|---|---|
| Akamai blocks session (403 anywhere) | status code | re-warm new session once; then backoff (1m→5m→30m cap); source-silence alert after 2 h on a market day |
| `'Missing index'` / stale-session garbage | body sniff | stale session, not "no data"; re-warm + retry once |
| 200 with HTML error page on PDF | magic-byte check | persist failed attempt with bytes; retry later; count invalid-content rate |
| Feed truncated / item-count collapse | depth telemetry vs trailing average | alert; widen overlap; do NOT advance cursor past suspicious region |
| Clock-skewed `an_dt` | sanity range check | keep raw value; flag item |
| Symbol delisted/renamed | empty API result for known-active stock | alert ops; never mark partition complete |

### Implementation status (2026-08-23)

These remain binding requirements, but current API code is not fully restart-safe at the
stated boundaries: it parses JSON before durable artifact registration, often operates
inside an outer transaction/savepoints, collapses some fetch-event artifacts, permits some
malformed/foreign items, and does not bound/allowlist attachment downloads. Invalid bytes
are retained as raw artifacts, but no-byte failures are not canonical
`content_fetch_attempts`. See review `ST-C01`, `ST-C04`–`ST-C06`; do not infer compliance
from the existence of `nse.fetch` jobs.
