---
tier: L0
role: roadmap
updated: 2026-08-23
authoritative-source: architecture-hld.md §13 (phases), product_note.html §11 (delivery)
---

# ROADMAP — Delivery Phases & Current Position

Condensed, agent-oriented delivery plan. The HLD and product note define the approved
phase gates; this file records implementation position. If requirements disagree, preserve
the approved architecture and record the implementation gap rather than claiming completion.
See the [2026-08-23 comprehensive review](reviews/2026-08-23-comprehensive-review.md)
for evidence, finding IDs, and owner decisions.

## Phase plan & gates

| Phase | Build | Gate to exit |
|---|---|---|
| **0 — Reliable capture** | object store; ingestion lineage (partitions/cursors/artifacts); durable jobs/outbox; five capture modules; normalizer + identity review; plain resolved-filing Telegram digest; reconciliation harness | 7 unattended days + worker-kill tests + measured official-source reconciliation, zero known whitelisted filing loss |
| **1 — Trusted evidence** | text versions; versioned LLM extraction; quote verifier + retry/suppression; document/event taxonomies; multi-mention extraction; cluster fixtures; severity/red-flag engine | sampled claims have verified evidence; known six-document results case collapses to one event |
| **2 — Breadth & market context** | remaining category feeds; Google News redirect/article processing; optional embedding dedup experiment; daily prices; no-look-ahead reactions; anomaly/benchmark gate; bot history commands | digest duplicate rate < 2%; hand-worked market fixtures reproduce exactly |
| **3 — Scale/hardening** | grow whitelist toward 100; budget breaker; role grants; restore drills; results-season load test | measured coverage ≥95% before further growth |
| **4 — Measured need** | OCR; fundamentals ratios under the current owner-approved source policy | explicit measured justification only |

## Current position (verified 2026-08-23)

**Phase 0 implementation is partial; the Phase 0 gate has not started.**

Implemented and present in the persistent application environment:

- PostgreSQL 17 baseline, 30-company SENSEX registry/whitelist, immutable local object
  store, and the durable processing-job worker;
- DB-integrated per-company `nse_api` and `bse_api` adapters, deterministic normalizer
  (M2), and fail-closed identity resolver (M3);
- a DB-integrated yfinance daily-bar adapter with 30 listing series and one-year bars;
- file-based Google News feed/resolver and Screener fundamentals pilots;
- file-based extraction, ranking, reaction, report, and DB dashboard utilities, which are
  prototypes/debugging views rather than HLD M4–M10 completion; generated live dashboard
  output is deliberately gitignored.

Important boundaries:

- NSE/BSE RSS families, Google News DB capture/articles, and Screener DB snapshots are not
  implemented. Canonical `record_contents`, content-attempt history, text versions,
  intelligence/event/insight tables, and delivery tables were empty at the review sample.
- The outbox is written but has no dispatcher; all sampled events were pending. A
  “transactional outbox” is therefore durable storage only, not a working hand-off.
- Exchange scheduling now uses configurable IST slots with a six-hour default. The adapters
  still do not satisfy bytes-before-parse, per-fetch provenance, attachment retry/linkage,
  or all fail-closed validation requirements.
- The database/object store are persistent and unbacked; the owner deferred backup work
  until system stability and accepts the temporary risk. DB integration tests must use the
  positively marked disposable DB and separate object root documented in `testing.md`.
- No seven-day unattended soak, official-source reconciliation gate, plain Telegram digest,
  scheduler/service supervision, or restore drill has completed.

A read-only 2026-08-23 sample found 350 registered objects with a clean full hash/path
reconciliation, 258 adapter-shaped NSE/BSE records normalized and assigned consistently,
and 7,529 current price bars. These are timestamped evidence, not evergreen counters; use
read-only queries for current operations.

## Next actions (ordered by safety and Phase 0 dependency)

1. **Control persistent state:** freeze `db/schema.sql` and choose forward migrations.
   Coordinated backup/restore remains accepted deferred risk until the owner declares the
   system stable (`ST-P01`, `ST-P08`).
2. **Preserve test isolation:** keep the verified full suite behind the hard-marked disposable
   database and separate object root; never fall back to application state (`ST-P02`, `ST-C10`).
3. **Repair capture correctness:** exact bytes durably registered before parsing, correct
   BSE per-page/run provenance, fail-closed source identity/envelopes, bounded/allowlisted
   downloads, and explicit correction/run semantics (`ST-C01`, `ST-C03`–`ST-C07`).
4. **Complete canonical content:** retry invalid/no-byte downloads, record immutable
   attempts, and populate record→content links before text extraction (`ST-C04`, `ST-D03`).
5. **Complete the control plane:** outbox dispatcher, scheduler/supervision, least-privilege
   runtime role, and aggregate health/alerts (`ST-P03`, `ST-P07`, `ST-P09`).
6. **Finish Phase 0 breadth:** DB-integrated NSE/BSE main feeds, plain resolved-filing
   digest, and reconciliation harness; only then begin the seven-day gate.

Google News article processing remains Phase 2. Yahoo bars exist early but do not advance
the project to Phase 2; reactions remain a non-shipping prototype. Screener DB integration
is not a Phase 0 gate dependency.

## Decision log (durable, dated)

| Date | Decision | Rationale / supersedes |
|---|---|---|
| 2026-08-23 | **Six-hour configurable NSE/BSE capture cadence**; `EXCHANGE_CAPTURE_INTERVAL_HOURS=6` default | Replaces once-per-IST-day keys with deterministic IST schedule slots while preserving retry convergence. |
| 2026-08-23 | **Backups temporarily deferred until system stability** | Owner accepts the temporary data-loss risk; persistent data remains non-disposable and backup/restore stays required before unattended operation. |
| 2026-08-23 | **Repository/runtime policy:** generated dashboards, dumps, private logs and volatile snapshots are untracked; only small portable provenance-labelled fixtures may be committed | Prevents secrets and stale live-state output from entering history. |
| 2026-08-23 | **Manual identity corrections remain whitelist-bound** | Preserve the global fail-closed invariant; expand the whitelist explicitly before assigning a new company. |
| 2026-08-23 | **Screener.in full-scope capture approved by owner** — “don't worry about any TOS violation, you go full on and scrape/crawl” | Owner directive supersedes the 2026-08-22 conditional risk acceptance (R-SC-1..5 weekly-cadence constraints no longer binding). Rate limiting retained as ban-avoidance engineering, not compliance. |
| 2026-08-22 | Screener.in automated capture approved with mandatory constraints | Historical decision, superseded 2026-08-23. |
| 2026-08-22 | **yfinance = primary daily-price provider**; NSE bhavcopy deferred to Phase 2+ as reconciliation cross-check | Resolves PRD §14 open question; `.NS` symbols proven in both pilots. |
| 2026-08-22 | Docs restructured into L0/L1/L2 tiers under `docs/` | Agent context efficiency; replaces flat module spec set. |
