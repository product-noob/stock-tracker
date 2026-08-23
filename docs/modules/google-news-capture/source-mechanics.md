---
tier: L2
module: google-news-capture
doc: source-mechanics
parent: OVERVIEW.md
updated: 2026-08-23
---

# Google News Capture — Source Mechanics

Feed URLs, query construction, item shapes, and the redirect-resolution flow.

## 1. Feed URL & query construction

```
https://news.google.com/rss/search?q={URL_ENCODED_QUERY}&hl=en-IN&gl=IN&ceid=IN:en
```

Queries are built per company from `core.company_aliases`:

| Req | Rule |
|---|---|
| R-GN-1 | Default query: exact-phrase legal name OR exchange symbol, e.g. `"HDFC Bank" OR HDFCBANK`. Ambiguous aliases (substrings matching sibling companies) MUST NOT enter queries without a disambiguating term. |
| R-GN-2 | Alias sets used for news queries are a distinct, reviewable config per company — never silently inherited from the general alias table. |
| R-GN-3 | One partition per company/query; staggered hourly schedule with jitter; never poll all companies in the same minute. |

## 2. Item shape

```xml
<item>
  <title>RBI allows LIC to increase holding in HDFC Bank... - The Hindu</title>
  <link>https://news.google.com/rss/articles/CBMi...</link>
  <pubDate>Thu, 20 Aug 2026 03:47:56 GMT</pubDate>
  <source url="...">The Hindu</source>
</item>
```

Title embeds the publisher after " - "; the `<source>` element is the reliable publisher field.

## 3. Redirect resolution (Phase 2)

Google wraps article URLs in opaque tokens (`news.google.com/rss/articles/{token}`).

Two proven resolution paths (`scripts/resolve_news.py`, 100/100 on HDFC Bank):

1. **Base64 decode**: many tokens embed the target URL directly (decode token payload).
2. **batchexecute signature flow**: fallback POST to Google's internal batchexecute endpoint
   using the token to obtain the decoded URL.

Resolver behavior requirements:

- cache per token (tokens repeat across polls);
- resolution failures keep the item retryable — exponential backoff, dead-letter visible in ops, never dropped;
- resolution is a separate durable step: token → final URL → article HTML/text → C3 content object.

| Req | Rule |
|---|---|
| R-GN-8 | Resolution + article fetch happen as a distinct durable stage after feed capture; failures never drop items. |

## 4. Failure modes & reliability

| Failure | Detection | Required behavior |
|---|---|---|
| Rate limiting / empty feeds under load | control-query canary (known-active query) | backoff + stagger widening; alert if >6 h |
| Feed 200 but zero items for active company | oldest-timestamp regression check | alert — possible window churn past cap |
| Redirect resolution failures | per-item retry state | backoff; visible dead-letter; never silent drop |
| Publisher-name ambiguity in titles | parse fallback to `<source>` element | prefer structured field; log title-parse misses |
| Query alias drift (company renames) | periodic alias-review reminder | owner-curated fix, not automatic |

### Implementation status (2026-08-23)

The two resolution algorithms and file cache are pilot-proven only. Current cache semantics
violate R-GN-8: a transient failure is cached and never retried. There is no DB feed/stage,
dead-letter state, publisher article fetch, private-address/credential URL policy, or size
bound. Do not use the utility as an unattended article-fetch boundary.
