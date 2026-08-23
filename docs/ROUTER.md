---
tier: L0
role: router
updated: 2026-08-23
---

# ROUTER — Documentation Map & Loading Protocol

You are an agent working on the Stock Market Insights Engine. **This file is your entry
point.** It tells you what to load, in what order, and what to trust. Never load more than
the task requires; never work from memory when a deeper tier exists.

> **Human?** Resuming after a break — open [`docs/index.html`](index.html) for the visual hub
> (status badges, next actions, decision log, full doc map). This file remains the machine-facing truth.
>
> **Active audit:** [`reviews/2026-08-23-comprehensive-review.md`](reviews/2026-08-23-comprehensive-review.md)
> is the durable finding ledger and coverage record for the current repository-wide review.

---

## 1. Tier model

| Tier | Location | Contents | Load when |
|---|---|---|---|
| **L0** | `docs/ROUTER.md`, `docs/ROADMAP.md` | map, status, plan, invariants | always (this file + ROADMAP) |
| **L1** | `docs/modules/<name>/OVERVIEW.md` | one module: scope, boundaries, cadence, acceptance, status | touching that module |
| **L2** | `docs/modules/<name>/<topic>.md` | deep detail: mechanics, contracts, evidence logs | implementing/debugging that specific aspect |

**Reference direction is strictly downward (L0→L1→L2).** No sideways or upward links.
Every doc carries YAML front matter (`tier`, `module`, `loads:`) so you can walk the graph.

### Loading protocol

1. Read this file + `ROADMAP.md` (both short).
2. Identify the module(s) your task touches → load its `OVERVIEW.md`.
3. From OVERVIEW's §"Detail index", load only the L2 files whose *load-when* matches your task.
4. Authoritative design for anything not covered here: `docs/architecture-hld.md` (system),
   `docs/database-schema.md` (storage), `product_note.html` (product requirements).
   These three are heavy — consult sections, don't ingest whole.

---

## 2. System one-screen

Personal research tool: watch a whitelist of Indian listed companies → capture official
filings + curated news → extract evidence with an LLM → cluster documents into business
events → rank explainable insights → join daily market reactions → Telegram briefing.
No trading. Association language only. One owner, PostgreSQL + local object store,
modular Python monolith with durable jobs.

```
[nse] [bse] [google-news] [screener] [yahoo]     ← capture modules (this doc set)
      └────────────┬─────────── exact bytes first (C1/C3)
                   ▼
        normalize → resolve identity → extract → cluster → severity/rank
                   ▼
        market reaction (attribution-gated) → digest → Telegram
```

## 3. Module inventory

Status vocabulary: `planned` · `spec-complete` · `pilot-proven` (works as file-based scripts,
DB integration not built) · `db-integrated` · `gated` (passing its acceptance gate).

| Module | L1 entry | Status | Owns |
|---|---|---|---|
| NSE capture | [`modules/nse-capture/OVERVIEW.md`](modules/nse-capture/OVERVIEW.md) | db-integrated (API) | NSE announcements: RSS + API + PDF download |
| BSE capture | [`modules/bse-capture/OVERVIEW.md`](modules/bse-capture/OVERVIEW.md) | db-integrated (API) | BSE announcements: RSS + API + PDF download |
| Google News capture | [`modules/google-news-capture/OVERVIEW.md`](modules/google-news-capture/OVERVIEW.md) | pilot-proven* | per-company news feeds + redirect resolution (*article fetch = Phase 2) |
| Screener fundamentals | [`modules/screener-fundamentals/OVERVIEW.md`](modules/screener-fundamentals/OVERVIEW.md) | pilot-proven | weekly fundamentals snapshots (risk-accepted source) |
| Yahoo market data | [`modules/yahoo-market-data/OVERVIEW.md`](modules/yahoo-market-data/OVERVIEW.md) | planned | listing-level daily OHLCV price series |

Shared capture-domain rules (parallel execution, convergence points, terminology,
cross-source corrections ledger): [`modules/README.md`](modules/README.md).

Downstream pipeline stages (normalize M2, resolve M3, extract M5, cluster/rank M6–M7,
reaction M8, digest/deliver M9–M10) are specified as capability cards in
[`architecture-hld.md`](architecture-hld.md) §5. They receive L1/L2 doc directories here
as implementation begins, following the same template.

## 4. Global invariants (non-negotiable, apply everywhere)

1. **Exact bytes before parsing.** Every response is persisted to immutable storage +
   SHA-256-registered before any parser sees it. JSONB is a query aid, never truth.
2. **Idempotent writes at every stage**, keyed per documented rules — at-least-once
   delivery must not duplicate logical results.
3. **Append-only evidence.** Text versions, extractions, event snapshots, reactions:
   corrections supersede via new rows, never updates. DB triggers enforce this.
4. **The LLM extracts; code decides.** Severity, ranking, classification, attribution are
   deterministic versioned code. Model confidence is never stored or trusted.
5. **Identity fails closed.** Ambiguity → review queue, never a guess. Fuzzy matching may
   suggest, only exact matching publishes. Resolver owns attribution, adapters preserve raw fields.
6. **Exchange category ≠ event kind.** Document kinds and business-event kinds are separate vocabularies.
7. **Cursor advances only after artifact + parsed items commit.** Overlap polling everywhere.
8. **HTTP 200 proves nothing.** Magic-byte/media validation mandatory (BSE AttachLive trap).
9. **Causal language is gated.** "Fell after filing on abnormal volume": yes. "Caused": never.
10. **Terminology:** *capture/fetch* = network retrieval (M1/M4); *extraction* = LLM stage
    from text versions (M5). Never call a fetcher an "extraction module".
11. **UTF-8 explicitly everywhere** (cp1252 broke on `₹`; Windows consoles need reconfigure).

## 5. Doc maintenance rules

- New empirical finding → append to the module's `evidence-log.md` (dated). Never rewrite history.
- A finding that contradicts an existing requirement → add a dated superseding entry in the
  log AND update the requirement in place with a `(supersedes ..., date)` marker.
- Status changes (inventory §3, ROADMAP) happen in the same commit as the work they describe.
