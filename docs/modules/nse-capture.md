# Module Spec — NSE Capture

**Status:** v1.0 · **Module card:** M1 source adapter + fetch half of M4 · **Master:** [`README.md`](README.md)

---

## 1. Purpose & scope

**Owns:** retrieval of NSE corporate announcements for a whitelisted stock, via two independent partition families (RSS + API), plus download of announcement PDFs/attachments into immutable content storage.

**Produces:** C1 fetched source items (exact response artifacts + parsed raw items), and C3 content objects + raw text candidates for filing PDFs.

**Does not own:** company attribution (resolver's job — M3), canonical dedup across exchanges (M2/M4), LLM extraction (M5).

Two partition families, both required:

| Family | Role | Coverage character |
|---|---|---|
| `nse_rss` | live capture, market-wide category streams | near-real-time; short rolling window |
| `nse_api` | per-symbol polling + historical backfill/reconciliation | complete date-window queries |

## 2. Sources & access mechanics

### 2.1 Anti-bot reality (supersedes earlier notes)

NSE (www.nseindia.com and nsearchives.nseindia.com) sits behind **Akamai**. Observed behavior, HDFC Bank run 2026-08-22 (`pilot_learnings.md` §19):

- plain `curl` and python-`requests` receive **HTTP 403 even on the homepage**, with only an `AKA_A2` cookie set;
- `curl_cffi` with `impersonate="chrome"` (browser TLS fingerprint) passes the warm-up and all subsequent API calls, receiving the full Akamai cookie set (`AKA_A2`, `_abck`, `ak_bmsc`, `bm_sz`);
- cookies alone are **not sufficient** — the TLS handshake must look like a browser too.

**Requirements:**

- R-NSE-1: All NSE HTTP traffic MUST use a browser-TLS-impersonating client (e.g. `curl_cffi` impersonating a current Chrome). A session warm-up GET to `https://www.nseindia.com/` MUST precede API calls in the same session.
- R-NSE-2: The adapter MUST treat a warm-up non-200 as a partition-level failure with exponential backoff — never retry the API without re-warming.
- R-NSE-3: Session/cookie state is per-partition, never shared across partitions or workers.
- R-NSE-4: Impersonation target version MUST be pinned in config and recorded in `fetch_runs.config`, because Akamai fingerprints evolve.

### 2.2 RSS feeds (`nse_rss` partitions)

Host: `nsearchives.nseindia.com`. Full verified feed inventory in [`../../nse-bse-rss.md`](../../nse-bse-rss.md). Main stream:

```
https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml   (~280 items observed)
```

Category feeds (each its own partition): `Board_Meetings`, `Financial_Results`, `InsiderTrading`, `Corporate_action`, `Corporate_Governance`, `Shareholding_Pattern`, `Voting_Results`, `Sast_*`, `Related_Party_Trans`, `Offer_Documents`, etc.

Item shape:

```xml
<item>
  <title>Balaji Phosphates Limited</title>
  <link>https://nsearchives.nseindia.com/corporate/...pdf</link>
  <description>Announcement text |SUBJECT: Trading Window</description>
  <pubDate>22-Aug-2026 14:26:26</pubDate>
</item>
```

**Requirements:**

- R-NSE-5: One partition per feed URL. Main feed at 5 min; category feeds at 15 min.
- R-NSE-6: Per-stock filtering happens at parse time by matching `title` against the security master (normalized name + aliases); a miss goes to review, never silently dropped (fail-closed, PRD §5).
- R-NSE-7: NSE RSS items carry **no symbol/scrip code** — identity resolution relies on company-name matching from the title. This makes RSS the *weakest* identity path; every RSS-sourced record MUST preserve `company_name_raw` verbatim for M3.
- R-NSE-8: Handle encoding variants (BOM, entity escapes) per `nse-bse-rss.md` limitations.

### 2.3 Announcements API (`nse_api` partitions)

Endpoint (validated 2026-08-22):

```
GET https://www.nseindia.com/api/corporate-announcements
    ?symbol={NSE_SYMBOL}&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY&index=equities
Headers: Referer: https://www.nseindia.com/all-corporate-announcements
         Accept: application/json, text/plain, */*
         X-Requested-With: XMLHttpRequest
```

**Gotchas (all mandatory to handle):**

- R-NSE-9: `index=equities` is REQUIRED. Without it the endpoint returns HTTP 200 with body `'Missing index'` (text/html). (§19)
- R-NSE-10: Success returns a **bare JSON array**, not an envelope. Adapter must not assume `{"data": [...]}`.
- R-NSE-11: An HTTP 200 with a non-JSON or empty body after warm-up means the session went stale → re-warm once, then fail the run. Never parse garbage as zero items silently; log it as an anomaly.

Response item fields used downstream: `seq_id` (native ID), `symbol`, `sm_isin`, `sm_name`, `desc` (subject), `attchmntText` (description), `attchmntFile` (PDF URL at `nsearchives.nseindia.com/corporate/...`), `an_dt` (`%d-%b-%Y %H:%M:%S`).

### 2.4 Attachment download (content fetch)

- R-NSE-12: Download `attchmntFile` URLs with `Referer: https://www.nseindia.com/` on an impersonated session. No separate warm-up has been needed for archive hosts, but the adapter MUST re-warm and retry once on any 403.
- R-NSE-13: Validate content by **magic bytes** (`%PDF` prefix for PDFs) and media type, never HTTP status alone (HLD §12 "HTTP 200 invalid content" risk).
- R-NSE-14: Failed/invalid downloads are persisted as failed attempts (`documents.content_fetch_attempts`) with the exact bytes retained — an HTML block page is evidence, not trash.
- R-NSE-15: Valid bytes are registered as content objects keyed by SHA-256 before any parsing.

## 3. Partitions & cadence

| Partition key | Interval | Overlap |
|---|---|---|
| `(nse_rss, Online_announcements.xml)` | 300 s | ≥ 2× poll interval worth of items |
| `(nse_rss, <Category>.xml)` × ~20 | 900 s | same rule |
| `(nse_api, symbol:{SYMBOL})` | 6 h rolling window | 24 h overlap |
| `(nse_api, symbol:{SYMBOL}) backfill` | manual / reconciliation-triggered | explicit date range |

- R-NSE-16: Cursor advances only after artifact + raw items commit (`database-schema.md` §4.1).
- R-NSE-17: The API partition doubles as the **reconciliation denominator** for coverage measurement (PRD §13 criterion 1): sampled dates are queried for real and diffed against captured items.

## 4. Data contract

Raw item fields → `ingestion.raw_feed_items` / `normalized_records` mapping:

| Source field | Canonical field | Notes |
|---|---|---|
| `seq_id` (API) | idempotency key | native ID: `nse_ann:{seq_id}` |
| — (RSS) | idempotency key | stable tuple `{link}+{pubDate}+{title-hash}` (documented, stable) |
| `desc` (API) / `SUBJECT:` fragment (RSS) | `subject` / `category_raw` | exchange category is metadata only, never event classification (PRD §6) |
| `attchmntText` / `description` | `description` | |
| `attchmntFile` / `link` | `content_url` | NOT a dedup key — distinct records can share one PDF |
| `an_dt` / `pubDate` | `published_at` | IST assumed; store UTC |
| `sm_isin` (API only) | `isin_raw` | RSS has none |
| `sm_name` / `title` | `company_name_raw` | verbatim |

- R-NSE-18: `dedup_scope = nse`; `dedup_key` per R-NSE-5 tuple rules. Multiple raw sightings of one announcement link to one `normalized_records` row via `record_raw_items`.
- R-NSE-19: All timestamps parsed defensively; unparseable times stored null with `published_at_estimated=false` semantics per schema (M2 permits missing timestamps).

## 5. Identity & dedup rules

- R-NSE-20: The adapter MAY pre-fill `company_id` when the API partition was provisioned per-symbol (partition key IS the listing); it MUST still emit resolution input fields so M3 can confirm or correct. Pre-fill and resolver must agree in one transaction (PRD §5).
- R-NSE-21: Cross-exchange duplication is resolved downstream by content SHA-256 — this module does NOT compare against BSE output, ever.
- R-NSE-22: Same announcement appearing in main + category RSS feeds yields multiple raw sightings of one canonical record (via dedup key), not duplicates.

## 6. Failure modes & reliability requirements

| Failure | Detection | Required behavior |
|---|---|---|
| Akamai blocks session (403 anywhere) | status code | re-warm new session once; then backoff (1m→5m→30m cap); raise source-silence alert after 2 h on a market day |
| `'Missing index'` / stale-session garbage | body sniff | treated as stale session, not "no data"; re-warm + retry once |
| 200 with HTML error page on PDF | magic-byte check | persist as failed attempt with bytes; retry later; count invalid-content rate |
| Feed truncated / item-count collapse | feed-depth telemetry vs trailing average | alert; widen overlap; do NOT advance cursor past suspicious region |
| Clock-skewed `an_dt` | sanity range check | keep raw value; flag item |
| Symbol delisted/renamed | API returns empty for known-active stock | alert to ops, do not mark partition complete |

Restart safety: any worker killed mid-run leaves either a completed transaction or nothing; the durable job redelivers and the idempotency keys absorb it (HLD §9).

## 7. Acceptance criteria

1. Seven consecutive unattended days on ≥3 stocks with zero stranded records after daily worker-kill tests (Phase 0 gate).
2. Sampled-week reconciliation: captured items ≥95% of NSE-API truth set, with every miss explainable (identity review, outside whitelist, etc.).
3. Zero announcements ingested with wrong company attribution (M3 audit).
4. Every stored artifact reparses offline to identical raw items (raw-replay NFR).
5. Invalid-content rate (200-but-not-PDF) measured and < 1%, with all instances retained as evidence.

## 8. Open questions

1. Do `nsearchives.nseindia.com` RSS endpoints have a separate Akamai policy from `www.`? (Current evidence: lighter protection; verify under sustained polling.)
2. Optimal RSS poll interval vs item churn — measure item arrival distribution over a results season before tuning below 5 min.
3. Does the API accept multi-symbol queries (batching) without tripping rate limits?
