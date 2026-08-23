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

## 2026-08-23 — First full 30-company DB ingestion (`bse_capture.py`)

- AttachHis serves real 404s (no bytes) for many older attachments; the retry
  then hits AttachLive, which for **absent** files returns HTTP 200 with one of
  two generic HTML block pages (`<html>Access Denied</html>`,
  `<html>Not Found</html>`). These pages are byte-identical across companies —
  28 retained `filing_pdf_invalid` evidence artifacts collapse to ~6 distinct
  sha256 digests. Magic-byte validation caught all of them; no garbage entered
  the valid-PDF population. Implication for M2/M4: invalid-evidence digest
  equality is NOT document identity — it's shared block-page boilerplate.
- R-BSE-9 canary validated: 5 of 30 SENSEX scrips (Eternal, ITC, Maruti,
  NTPC, Trent) returned genuinely empty Tables over a 7-day backfill; each run
  produced an `api_response_control` artifact proving endpoint health.
- Per-scrip API + attachment downloads for all 30 companies completed in under
  ~2 minutes of wall clock with 0.4 s download spacing — no rate limiting
  observed at this cadence (single pass; sustained polling still unmeasured).
