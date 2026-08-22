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
