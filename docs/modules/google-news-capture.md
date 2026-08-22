# Module Spec — Google News Capture

**Status:** v1.0 · **Module card:** M1 source adapter (news family) · **Master:** [`README.md`](README.md)
**Phase note:** PRD §11 places news breadth in Phase 2; the capture mechanics below are Phase-0-safe, redirect resolution and article fetch are Phase 2.

---

## 1. Purpose & scope

**Owns:** per-company news monitoring via Google News RSS queries: fetching the feed, persisting exact artifacts, parsing items, and (Phase 2) resolving Google's opaque redirect tokens to real article URLs and fetching article content.

**Produces:** C1 fetched source items for news; C3 content objects for resolved articles.

**Does not own:** deciding whether an article is *about* the whitelisted company beyond query-time aliasing (identity review still applies), paraphrase dedup across outlets (deferred experiment, PRD §2), sentiment/colour classification (M5+).

## 2. What two pilots established about this source

| Property | Finding | Consequence |
|---|---|---|
| Access | Live RSS, no auth, no blocking observed on either pilot | simplest source of the four |
| Volume cap | **~100 items returned regardless of date range** — spans back ~5 months inconsistently (§1), reconfirmed in HDFC run (§19 correction #6) | strictly forward-looking; useless for historical backfill |
| Links | `<link>` values are `news.google.com/rss/articles/…` opaque redirect tokens | must be resolved before an article is fetchable or dedupable by URL |
| Identity | Query-string-driven: everything returned matches the query by construction | precision depends entirely on alias choice; collisions are a real risk (e.g. "HDFC" matches HDFC Bank, HDFC Life, HDFC AMC, HDFC Securities) |

## 3. Sources & access mechanics

### 3.1 Feed URL

```
https://news.google.com/rss/search?q={URL_ENCODED_QUERY}&hl=en-IN&gl=IN&ceid=IN:en
```

Query construction per company from `core.company_aliases`:

- R-GN-1: Default query: exact-phrase legal name OR exchange symbol, e.g. `"HDFC Bank" OR HDFCBANK`. Aliases are curated per company; ambiguous aliases (substrings that match sibling companies) MUST NOT enter news queries without a disambiguating term.
- R-GN-2: Alias sets used for news queries are a distinct, reviewable config per company — not silently inherited from the general alias table (PRD §5: ambiguous aliases fail closed).
- R-GN-3: One partition per company/query. Staggered hourly schedule with jitter; never poll all companies at the same minute (HLD M1, PRD open question on rate stability).

### 3.2 Item shape

```xml
<item>
  <title>RBI allows LIC to increase holding in HDFC Bank... - The Hindu</title>
  <link>https://news.google.com/rss/articles/CBMi...</link>
  <pubDate>Thu, 20 Aug 2026 03:47:56 GMT</pubDate>
  <source url="...">The Hindu</source>
</item>
```

Title embeds the publisher after " - ". The `<source>` element is the reliable publisher field.

## 4. Partitions & cadence

| Partition key | Interval | Notes |
|---|---|---|
| `(google_news, company:{id})` | 3600 s + jitter | overlap ≥ 2 h |

- R-GN-4: Poll overlap of at least 2× interval; given the 100-item cap, a company with very heavy coverage can churn the window faster than hourly polling drains it — detect via oldest-item timestamp regression and alert (the cap makes silent loss possible in a way other sources don't have).
- R-GN-5: Cursor advance only after artifact + items commit (standard rule).

## 5. Data contract

| Source field | Canonical field | Notes |
|---|---|---|
| resolved article URL (Phase 2) / redirect token hash (Phase 0) | idempotency key | native ID absent → stable tuple `{resolved_url}` or `{token}+{pubDate}` |
| `title` (split on last " - ") | `title` + `publisher_raw` | |
| `<source url>` | `publisher_raw` (authoritative) | |
| `link` | `content_url` (redirect token — NOT directly fetchable) | stored raw regardless |
| `pubDate` | `published_at` | RFC822 GMT |

- R-GN-6: `dedup_scope = google_news`. The same article appearing across consecutive polls is one canonical record with multiple sightings.
- R-GN-7: Until redirect resolution exists, records carry `content_status = 'redirect_unresolved'` and are excluded from cross-tier dedup and extraction — they can still be displayed as colour metadata.
- R-GN-8 (Phase 2): Redirect resolution is a separate durable step: resolve token → final URL → fetch article HTML/text → C3 content object. Resolution failures keep the item in retryable state, never drop it.

## 6. Identity & dedup rules

- R-GN-9: Query membership alone does not equal attribution. Every item passes through M3 like any other record; name-collision-prone companies (group names shared across listed entities: Tata, HDFC, Bajaj, Kotak, Adani…) require a precision pass — heuristics may *suggest* rejection candidates but only deterministic/exact matching publishes an assignment (PRD §5 hard rules).
- R-GN-10: Cross-tier dedup (news article vs filing about the same event) is explicitly **out of scope** for this module; it belongs to event clustering (M6) once articles are fetchable. Paraphrase dedup between outlets is the deferred Phase-2 embedding experiment — not built here.
- R-GN-11: This module MUST NOT attempt historical backfill via date-range queries — the cap makes results unreliable (documented twice). History begins when partition provisioning begins.

## 7. Failure modes & reliability requirements

| Failure | Detection | Required behavior |
|---|---|---|
| Rate limiting / empty feeds under load | control-query canary (known-active query) | backoff + stagger widening; alert if persists >6 h |
| Feed returns 200 but zero items for active company | oldest-timestamp regression check (R-GN-4) | alert — possible window churn past cap |
| Redirect resolution failures (Phase 2) | per-item retry state | exponential backoff; dead-letter visible in ops; never silently dropped |
| Publisher-name ambiguity in titles | parse fallback to `<source>` element | prefer structured field; log title-parse misses |
| Query alias drift (company renames) | periodic alias-review reminder | owner-curated fix, not automatic |

## 8. Acceptance criteria

1. Per-stock partitions poll hourly for seven unattended days with zero stranded records.
2. Every persisted artifact reparses offline identically (raw-replay NFR).
3. Measured and reported: redirect-resolution success rate (Phase 2 gate input for PRD §14 open question).
4. Zero published mis-attributions on a deliberately collision-heavy test set (HDFC group, Tata group).
5. Duplicate-article rate within a digest measurable (feeds the <2% duplicate target, PRD §13).

## 9. Open questions

1. Redirect-resolution mechanism durability — Google changes token formats periodically; needs a maintained resolver strategy (Phase 2 spike).
2. Should high-churn companies get sub-hourly partitions during results season?
3. Does adding `when:30d`-style query operators stabilize window behavior, or does the cap bind regardless? (Measure.)
