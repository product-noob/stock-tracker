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

## 2026-08-23 — Comprehensive review supersession note

- The first-ingestion totals (including the 138/109/28 values formerly repeated in the L1
  status) are dated evidence, not evergreen live counts. A later registry-scoped read-only
  audit found 90 adapter-shaped BSE items, all normalized and assigned consistently.
- BSE envelope validation was hardened during the review: `ROWCNT` is now mandatory,
  non-negative, at least the page length and stable across pages; rows without `NEWSID` fail
  the run. Offline pagination/helper regressions pass (`ST-C02`, fixed).
- Remaining P0 gaps supersede broad “full lineage/crash convergence” claims: rows can point
  at the wrong page/run in multi-page mixed-reuse cases; parsing precedes durable artifact
  registration; invalid HTML was treated as final (**superseded by the next entry**);
  all-foreign results can bypass the canary; canonical content attempts/links and download
  bounds are absent.
- The IST-day key fetches at most daily, not every six hours. See review `ST-C01`,
  `ST-C03`–`ST-C07`.

## 2026-08-23 — GUID filings live only under AttachLive (owner-reported, verified)

- Sample: `6b000a79-c9db-4cfd-80d3-9edea86e1bdc.pdf`
  - AttachHis → **HTTP 404** + 8.4 KB HTML error page
  - AttachLive → **HTTP 200** + 300 KB valid PDF
- Newer BSE filings use GUID attachment names; the archive path does not hold
  them (at least within the observed window). Legacy numeric names remain
  His-first.
- **Related stranded-capture bug fixed**: `url_already_captured` treated an
  invalid (HTML) capture as "handled", so any attachment whose His attempt
  stored garbage before a run died never retried the Live variant. Fix: only
  VALID `filing_pdf` counts as captured; invalid-only URLs are retried each
  run (content-addressed dedup absorbs the repeated garbage bytes).
- Tests: ordering unit tests + retry-contract test; end-to-end fixture updated
  to the corrected semantics (91/91 pass).

## 2026-08-23 — Six-hour schedule decision implemented

- Owner selected a six-hour default with configuration support. Deterministic IST slot keys
  now begin at 00:00/06:00/12:00/18:00 for the default and encode the configured interval.
- `EXCHANGE_CAPTURE_INTERVAL_HOURS` must be a positive divisor of 24; invalid values fail
  closed. Job payloads carry their slot so delayed workers do not drift into a later slot.
- This supersedes the earlier daily-key cadence limitation; page provenance and exact-byte
  ordering remain open.
