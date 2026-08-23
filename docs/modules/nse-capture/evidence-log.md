---
tier: L2
module: nse-capture
doc: evidence-log
parent: OVERVIEW.md
policy: append-only — dated entries, newest last; supersede via new entry + marker, never rewrite
---

# NSE Capture — Evidence Log

Empirical findings from live runs. Cite as `(NSE evidence-log, YYYY-MM-DD)`.

## 2026-08-21 — Tata Elxsi pilot (Part 1)

- RSS inventory verified across both exchanges; NSE category feeds live at
  `nsearchives.nseindia.com/content/RSS/*.xml`. Cookie warm-up believed sufficient at this point
  (**superseded 2026-08-22**, see below).

## 2026-08-22 — HDFC Bank run (Part 5 §19)

- Akamai now 403s plain `curl`/requests on the homepage; `curl_cffi impersonate="chrome"`
  passes warm-up + API. Cookies necessary, no longer sufficient.
- `/api/corporate-announcements` rejects queries without `index=equities`
  (`Missing index` body); success shape is a bare JSON array.
- Full chain proven for one mega-cap symbol: announcements → PDFs → hash-dedup → text → LLM.

## Open field observations (to confirm under sustained polling)

- Archive-host RSS (`nsearchives`) appears lighter-protected than `www.` — unverified beyond one-off pulls.
- Multi-symbol API batching untested.

## 2026-08-23 — DB-integrated adapter first full run (30 SENSEX constituents)

- **Byte-identical PDFs across distinct announcements are real and common.** The very first
  multi-company run hit it twice: two Reliance announcements referenced different
  `attchmntFile` URLs whose bytes hashed identically (`kavinavora_*_SE_*.pdf` family), and a
  same-run collision on `UNIQUE (fetch_run_id, artifact_kind, sha256)` aborted the PDF lineage
  insert (superseded requirement: that UNIQUE constraint existed in the baseline schema).
  Consequence: the constraint conflated *fetch events* with *content* — each distinct download
  URL keeps its own `raw_artifacts` row; dedup happens solely via the shared
  `documents.content_objects` registration keyed by SHA-256. Constraint dropped from
  `db/schema.sql` + live DB (no persistent data at the time). Cross-exchange dedup (M2/M4) is
  unaffected: it joins on content SHA-256, which still collapses identical bytes to one
  `content_objects` row.
- **Akamai throttles warm-up bursts.** ~30 back-to-back per-symbol fetches with only 3 s
  spacing produced repeated partition-level failures (403/stale-session after the single
  re-warm retry); the same symbols succeeded minutes later on single runs. The durable-job
  exponential backoff is the correct driver for fleet-wide sweeps — avoid tight sequential
  loops of fresh sessions. Tune inter-partition spacing before the next full sweep.
- **Same-day skip key needs care during reconciliation.** Run idempotency is deterministic
  per `(partition, IST calendar day)`: an already-completed day-run skips network entirely.
  A manual cursor rewind (backfill/reconciliation) must also mark that day's run row
  `failed`/superseded, or the backfill silently no-ops. Recorded here because it WILL bite
  whoever runs the first coverage reconciliation.
- Crash-convergence path exercised live: a failed PDF download inside an otherwise-ok run was
  re-attempted automatically by the next same-day invocation via `_pending_pdf_urls`, and the
  missing artifact appeared without refetching the API.
- First full-fleet numbers (2026-08-23): 30/30 companies with ≥1 successful `fetch_runs` row,
  192 raw items, 61 API-response artifacts, 177 filing-PDF artifacts, all PDFs magic-valid;
  incremental re-runs produced `items_new=0` everywhere with zero duplicate sha256
  registrations.
