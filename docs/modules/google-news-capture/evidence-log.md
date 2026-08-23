---
tier: L2
module: google-news-capture
doc: evidence-log
parent: OVERVIEW.md
policy: append-only — dated entries, newest last; supersede via new entry + marker, never rewrite
---

# Google News Capture — Evidence Log

Empirical findings. Cite as `(GN evidence-log, YYYY-MM-DD)`.

## 2026-08-21 — Tata Elxsi pilot (Part 1)

- Live RSS worked with no auth and no blocking; discovered opaque redirect tokens.
- Observed ~100-item cap regardless of date range — window spans back inconsistently (~5 months).
  Conclusion: source is forward-looking only; cannot backfill history.

## 2026-08-22 — HDFC Bank run (Part 5 §19) + resolver build

- Cap reconfirmed on a second, much higher-coverage company.
- `scripts/resolve_news.py` built: resolves tokens via direct base64 decode with the
  batchexecute signature flow as fallback; per-token cache; **100/100 resolved** for HDFC Bank.
  Resolution mechanism works today but token formats are known to change periodically —
  treat as maintained component, not fire-and-forget.

## Open field observations (to confirm)

- Sustained hourly polling across many companies untested (rate-limit stability = PRD open question).
- Article HTML/text fetch not yet attempted (Phase 2 scope).

## 2026-08-23 — Static/sample review

- The tracked 100-entry HDFC cache contained structurally valid HTTP(S) URLs with no URL
  credentials, literal private IPs or non-HTTP schemes; no network refetch was performed.
- `resolve_news.py` caches failure results under the token and excludes every cached token
  from future work. A transient resolution failure is therefore permanent, contrary to
  R-GN-8's retry requirement. Full URLs/errors are also logged and the future article-fetch
  trust boundary (private-address denial, canonicalization and size limits) is unspecified.
- Keep the utility pilot-only until retryable state and safe URL/content handling exist.
