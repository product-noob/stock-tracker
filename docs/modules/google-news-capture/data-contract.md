---
tier: L2
module: google-news-capture
doc: data-contract
parent: OVERVIEW.md
updated: 2026-08-23
---

# Google News Capture — Data Contract & Identity Rules

Field mappings, idempotency keys, dedup and identity rules.
Schema context: [`../../database-schema.md`](../../database-schema.md) §4.

## 1. Field mapping

| Source field | Canonical field | Notes |
|---|---|---|
| resolved article URL (Phase 2) / redirect-token hash (Phase 0) | idempotency key | no native ID → stable tuple `{resolved_url}` or `{token}+{pubDate}` |
| `title` (split on last " - ") | `title` + `publisher_raw` | |
| `<source url>` | `publisher_raw` (authoritative) | |
| `link` | `content_url` (redirect token — NOT directly fetchable) | stored raw regardless |
| `pubDate` | `published_at` | RFC822 GMT |

## 2. Dedup & identity rules

| Req | Rule |
|---|---|
| R-GN-6 | `dedup_scope = google_news`. Same article across consecutive polls = one canonical record with multiple sightings. |
| R-GN-7 | Until redirect resolution exists, records carry `content_status='redirect_unresolved'`; excluded from cross-tier dedup and extraction; may display as colour metadata. |
| R-GN-9 | Query membership ≠ attribution. Every item passes M3; collision-prone families (Tata, HDFC, Bajaj, Kotak, Adani…) need a precision pass — heuristics suggest candidates, only deterministic/exact matching publishes. |
| R-GN-10 | Cross-tier dedup (article vs filing about same event) is out of scope here — belongs to event clustering (M6). Paraphrase dedup between outlets is the deferred embedding experiment. |
| R-GN-11 | MUST NOT attempt historical backfill via date-range queries — cap makes results unreliable (documented twice). History begins when partition provisioning begins. |

## 3. Implementation reconciliation (2026-08-23)

No rows implementing this contract exist on the DB path. The tracked feed/resolver files are
historical pilot products; the cache is not a durable retry model and query membership has
not been stress-tested on a collision-heavy DB feed. All rules above remain requirements.
