---
tier: L0
role: roadmap
updated: 2026-08-22
authoritative-source: architecture-hld.md §13 (phases), product_note.html §11 (delivery)
---

# ROADMAP — Delivery Phases & Current Position

Condensed, agent-oriented view of the delivery plan. The authoritative text lives in the
HLD §13 / PRD §11; if this file disagrees with them, they win — then fix this file.

---

## Phase plan & gates

| Phase | Build | Gate to exit |
|---|---|---|
| **0 — Reliable capture** | object store; ingestion lineage (partitions/cursors/artifacts); durable jobs/outbox; five capture modules; normalizer + identity review; plain resolved-filing Telegram digest; reconciliation harness | 7 unattended days + worker-kill tests + measured official-source reconciliation, zero whitelisted filing loss |
| **1 — Trusted evidence** | text versions; versioned LLM extraction; quote verifier + retry/suppression; document/event taxonomies; multi-mention extraction; cluster fixtures; severity/red-flag engine | sampled claims have verified evidence; known six-document results case collapses to one event |
| **2 — Breadth & market context** | remaining category feeds; Google News redirect/article processing; optional embedding dedup experiment; daily prices; no-look-ahead reactions; anomaly/benchmark gate; bot history commands | digest duplicate rate < 2%; hand-worked market fixtures reproduce exactly |
| **3 — Scale/hardening** | grow whitelist toward 100; budget breaker; role grants; restore drills; results-season load test | measured coverage ≥95% before further growth |
| **4 — Measured need** | OCR; compliant fundamentals ratios | explicit measured justification only |

## Current position (2026-08-22)

**Phase 0, early build.** Done:

- Schema baseline applied; core security master seeded (30 SENSEX companies, listings, aliases, whitelist) via `scripts/seed_core.py`
- Dedicated Postgres 17 up (`docker-compose.yml`, port 5433); venv + `requirements.txt`
- All five sources pilot-proven as **file-based** scripts under `scripts/` (bytes land in `data/<slug>/`, not the DB)
- Capture-module spec set complete (`docs/modules/*/OVERVIEW.md` + L2)

Not started: object store, jobs/outbox workers, DB-writing fetchers, normalizer/resolver
services, digest/reconciliation harness.

## Next actions (ordered)

1. **Ingestion skeleton**: local immutable object store (atomic write + SHA-256 registry) and the job/outbox worker pattern against `platform.processing_jobs` / `platform.outbox_events`. Everything downstream assumes this exists.
2. **NSE + BSE adapters → DB**: reshape proven `fetch_announcements.py` / `download_pdfs.py` logic into partition-aware fetchers writing C1/C3 lineage (per module OVERVIEWs).
3. **Normalizer (M2) + identity resolver (M3)** with fail-closed review queue.
4. **Yahoo adapter**: daily-bar fetcher into `market.price_series`/`price_bars` (module spec ready).
5. **Digest + reconciliation harness** → start the Phase 0 soak clock.

Screener + Google News article-fetch stay deliberately behind (cadence/risk constraints;
Phase 2 breadth respectively) — see their OVERVIEWs.

## Decision log (durable, dated)

| Date | Decision | Rationale / supersedes |
|---|---|---|
| 2026-08-22 | Screener.in automated capture approved with mandatory constraints | owner risk acceptance; supersedes PRD §2 "fundamentals DEFERRED" (see screener module §policy) |
| 2026-08-22 | **yfinance = primary daily-price provider**; NSE bhavcopy deferred to Phase 2+ as reconciliation cross-check | resolves PRD §14 open question: `.NS` symbols proven in both pilots, no extra session/TLS machinery vs exchanges, fits `market.price_series` semantics; bhavcopy retained as independent truth for coverage checks |
| 2026-08-22 | Docs restructured into L0/L1/L2 tiers under `docs/` | agent context efficiency; replaces flat `docs/modules/*.md` spec set |
