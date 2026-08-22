---
tier: L2
module: bse-capture
doc: evidence-log
parent: OVERVIEW.md
policy: append-only — dated entries, newest last; supersede via new entry + marker, never rewrite
---

# BSE Capture — Evidence Log

Empirical findings from live runs. Cite as `(BSE evidence-log, YYYY-MM-DD)`.

## 2026-08-21 — Tata Elxsi pilot (Part 1)

- RSS feeds verified at `beta.bseindia.com`; main feed items carry native `<scripcode>`.
- **AttachLive trap discovered**: missing files return HTTP 200 with an HTML error page;
  naive downloads produced garbage that magic-byte checks had to catch.

## 2026-08-22 — HDFC Bank run (Part 5 §19)

- API contract changed without notice since Part 1: old params (`strCategory`,
  `strSubCategory`, `strPageSize`) now yield empty `{}` with HTTP 200. New params
  (`strCat`, `strType`, `subcategory`, `strSearch=P`) reverse-engineered from the
  ann.html Angular bundle. This is the canonical example of why shape validation +
  control-scrip canaries are mandatory requirements.
- Full chain proven for a second symbol; ~95% cross-exchange byte-identical duplication held.

## Open field observations (to confirm under sustained polling)

- No documented API changelog known; bundle-diffing is the only drift detector so far.
- Rate-limit behavior for per-scrip polling unmeasured beyond two symbols.
