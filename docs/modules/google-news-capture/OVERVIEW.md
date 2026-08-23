---
tier: L1
module: google-news-capture
status: pilot-proven (file-only; no DB adapter; article fetch = Phase 2)
spec-version: v1.1 (tiered restructure of google-news-capture.md v1.0)
master: ../README.md
phase-note: PRD §11 places news breadth in Phase 2; feed capture mechanics are Phase-0-safe.
loads:
  - source-mechanics.md   # implementing/debugging feed queries, item parsing, redirect resolution
  - data-contract.md      # writing parser/writer code, touching C1/C3 fields or dedup keys
  - evidence-log.md       # investigating anomalies, reviewing known pitfalls
---

# Google News Capture — Overview

**Module card:** M1 source adapter (news family) · **Domain:** [`../README.md`](../README.md)

## Purpose & scope

Per-company news monitoring via Google News RSS: fetching the feed, persisting exact
artifacts, parsing items, and (Phase 2) resolving opaque redirect tokens to real article
URLs and fetching article content.

**Produces:** C1 fetched source items for news; C3 content objects for resolved articles.

**Does not own:** attribution beyond query-time aliasing (M3 review still applies);
paraphrase dedup across outlets (deferred Phase-2 experiment); sentiment/colour classification.

## What the two pilots established

| Property | Finding | Consequence |
|---|---|---|
| Access | Live RSS, no auth, no blocking observed | simplest source of the five |
| Volume cap | **~100 items regardless of date range**, spanning back ~5 months inconsistently | strictly forward-looking; no historical backfill |
| Links | opaque `news.google.com` redirect tokens | must be resolved before fetch/dedup by URL |
| Identity | query-string-driven — everything matches the query by construction | precision depends entirely on alias choice; collisions real ("HDFC" → Bank/Life/AMC/Securities) |

## Partitions & cadence

| Partition key | Interval | Notes |
|---|---|---|
| `(google_news, company:{id})` | 3600 s + jitter | overlap ≥ 2 h |

R-GN-4: given the 100-item cap, heavy-coverage companies can churn the window faster than
hourly polling drains it — detect via oldest-item timestamp regression and alert (the cap
makes silent loss possible in a way other sources don't have).

## Acceptance criteria

1. Per-stock partitions poll hourly for seven unattended days with zero stranded records.
2. Every persisted artifact reparses offline identically.
3. Measured redirect-resolution success rate (Phase 2 gate input).
4. Zero published mis-attributions on a deliberately collision-heavy test set (HDFC group, Tata group).
5. Duplicate-article rate within a digest measurable (feeds the <2% duplicate target).

## Open questions

1. Redirect-token durability — Google changes formats periodically; maintained resolver strategy needed (Phase 2 spike).
2. Sub-hourly partitions for high-churn companies during results season?
3. Do `when:30d`-style operators stabilize window behavior, or does the cap bind regardless? (Measure.)

## Status & next milestone

- **Pilot-proven, file-only:** feed capture ran in both pilots; the stored HDFC sample has
  100/100 structurally valid resolved HTTP(S) URLs. No Google News partition has run on the
  canonical DB path and article fetch was not attempted.
- **Known mismatch:** `scripts/resolve_news.py` writes transient failures to the same final
  token cache and excludes them forever, contrary to R-GN-8. It also logs full resolved
  URLs and lacks the URL/private-address/size policy required before article fetching.
- **Next milestone:** keep article resolution/fetch in Phase 2. A DB feed adapter may be
  added earlier only after shared capture correctness/test-isolation gates; make resolver
  failures retryable before promoting it to unattended use.

## Detail index (L2)

| File | Load when |
|---|---|
| [`source-mechanics.md`](source-mechanics.md) | feed URLs, query construction, item shapes, resolver flow |
| [`data-contract.md`](data-contract.md) | field mapping, idempotency keys, identity rules |
| [`evidence-log.md`](evidence-log.md) | debugging surprises, known pitfalls |
