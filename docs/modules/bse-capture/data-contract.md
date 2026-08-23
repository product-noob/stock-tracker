---
tier: L2
module: bse-capture
doc: data-contract
parent: OVERVIEW.md
updated: 2026-08-23
---

# BSE Capture — Data Contract & Identity Rules

Field mappings, idempotency keys, dedup and identity rules.
Schema context: [`../../database-schema.md`](../../database-schema.md) §4.

## 1. Field mapping

| Source field | Canonical field | Notes |
|---|---|---|
| `NEWSID` (API) / — (RSS) | idempotency key | native: `bse_ann:{NEWSID}`; RSS falls back to stable tuple `{link}+{pubDate}+{scripcode}` |
| `SCRIP_CD` / `scripcode` | listing identifier (tier-1) + preserved raw | |
| `SLONGNAME` / `title` | `company_name_raw` | verbatim |
| `HEADLINE` | `description` | |
| `CATEGORYNAME` | `category_raw` | metadata only, never event classification |
| `ATTACHMENTNAME` → URL | `content_url` | not a dedup key |
| `NEWS_DT` / `pubDate` | `published_at` | IST; store UTC |

## 2. Dedup & identity rules

| Req | Rule |
|---|---|
| R-BSE-13 | `dedup_scope = bse`; same-announcement multi-feed sightings collapse via `record_raw_items`. |
| R-BSE-14 | The API returns no ISIN — `isin_raw` stays null from this module. ISIN arrives via security-master join (M3) or PDF text extraction later. Do not fabricate it. |
| R-BSE-15 | Scrip-code partition pre-fill follows the agree-in-transaction rule (cf. R-NSE-20). |
| R-BSE-16 | Cross-exchange dedup is downstream-only; this module never sees NSE data. |
| R-BSE-17 | One attachment shared by several securities produces several *records* but one *content object* — the M:N `record_contents` model absorbs this. |

## 3. Implementation reconciliation (2026-08-23)

The API adapter preserves raw identity fields and the resolver handles production rows
consistently, but current attachment lineage stops at raw artifacts/content registrations.
No canonical `record_contents` or `content_fetch_attempts` rows existed at audit time, so
R-BSE-17 is a target schema contract, not a working M:N path. Multi-page rows can also carry
the wrong page/run artifact (`ST-C03`).
