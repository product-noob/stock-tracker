---
tier: L2
module: nse-capture
doc: data-contract
parent: OVERVIEW.md
updated: 2026-08-23
---

# NSE Capture — Data Contract & Identity Rules

Field mappings into C1/C2/C3, idempotency keys, dedup and identity pre-fill rules.
Schema context: [`../../database-schema.md`](../../database-schema.md) §4.

## 1. Field mapping

Raw item fields → `ingestion.raw_feed_items` / `normalized_records`:

| Source field | Canonical field | Notes |
|---|---|---|
| `seq_id` (API) | idempotency key | native ID: `nse_ann:{seq_id}` |
| — (RSS) | idempotency key | stable tuple `{link}+{pubDate}+{title-hash}` (documented, stable) |
| `desc` (API) / `SUBJECT:` fragment (RSS) | `subject` / `category_raw` | exchange category is metadata only, never event classification |
| `attchmntText` / `description` | `description` | |
| `attchmntFile` / `link` | `content_url` | NOT a dedup key — distinct records can share one PDF |
| `an_dt` / `pubDate` | `published_at` | IST assumed; store UTC |
| `sm_isin` (API only) | `isin_raw` | RSS has none |
| `sm_name` / `title` | `company_name_raw` | verbatim |

## 2. Dedup rules

| Req | Rule |
|---|---|
| R-NSE-18 | `dedup_scope = nse`; `dedup_key` per §1 tuple rules. Multiple raw sightings of one announcement link to one `normalized_records` row via `record_raw_items`. |
| R-NSE-19 | All timestamps parsed defensively; unparseable times stored null (`published_at_estimated=false` semantics; M2 permits missing timestamps). |
| R-NSE-21 | Cross-exchange duplication resolved downstream by content SHA-256 — this module NEVER compares against BSE output. |
| R-NSE-22 | Same announcement in main + category feeds yields multiple sightings of one canonical record, not duplicates. |

## 3. Identity rules

| Req | Rule |
|---|---|
| R-NSE-20 | The adapter MAY pre-fill `company_id` when the API partition was provisioned per-symbol (partition key IS the listing); it MUST still emit resolution input fields so M3 can confirm or correct. Pre-fill and resolver agree in one transaction. |

## 4. Implementation reconciliation (2026-08-23)

The adapter writes raw fields and M2/M3 later resolve them; it does not establish complete
C3. Canonical `record_contents` and `content_fetch_attempts` were empty at audit time.
Same-partition source corrections with an existing native ID are dropped by raw-item
uniqueness before M2's advertised revision path, so correction semantics require an explicit
forward design (`ST-C06`).
