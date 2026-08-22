---
tier: L1
module: screener-fundamentals
status: pilot-proven (manual utility; DB integration + scheduled cadence not built)
spec-version: v1.1 (tiered restructure of screener-fundamentals.md v1.0)
master: ../README.md
policy: ⚠ risk-accepted source — constraints in §2 below are mandatory conditions of that acceptance
loads:
  - source-mechanics.md   # implementing/debugging fetch behavior, page regions, failure handling
  - data-contract.md      # writing parser/writer code, snapshot semantics, unit-safety rules
  - evidence-log.md       # investigating anomalies, reviewing known pitfalls
---

# Screener Fundamentals Capture — Overview

**Module card:** M1 source adapter (fundamentals family) · **Domain:** [`../README.md`](../README.md)

## ⚠ Policy status — read first

**SUPERSEDED 2026-08-23:** the owner has directed full-scope capture ("don't worry about any
TOS violation, you go full on and scrape/crawl"). The conditional risk acceptance below and
its R-SC-1..5 cadence constraints no longer bind as compliance rules. Rate limiting and
backoff remain as **engineering requirements only** (ban-avoidance; a blocked IP ends the
crawl). See `docs/ROADMAP.md` decision log.

<details><summary>Historical policy (2026-08-22, superseded)</summary>

This module **amends the PRD scope table** (§2 marks fundamentals "DEFERRED"). The owner
explicitly accepted the ToS/compliance risk on **2026-08-22** and approved automated capture
under the constraints below. The constraints are not optional hygiene — they are the
conditions under which the risk acceptance holds:

| Req | Constraint |
|---|---|
| R-SC-1 | **Low cadence.** Snapshots at most weekly per company + event-triggered refresh after results-day processing. No polling loops. |
| R-SC-2 | **Polite client.** Single sequential request per run, browser UA, no parallel fetches, ≥5 s spacing, honor robots/cache headers, off-peak IST runs. |
| R-SC-3 | **Personal use only.** Snapshots never redistributed or exposed beyond the owner's tooling. |
| R-SC-4 | **Kill-switch.** `screener.enabled=false` halts all Screener partitions immediately without touching other modules; legal/ToS-change events pause the module pending owner review. |
| R-SC-5 | **Fallback path.** Own extraction from official results filings remains the roadmap; this module is a convenience layer whose data must be replaceable without schema breakage. |

Fundamentals data is **context/enrichment only**: it never enters the severity/red-flag path
and is never quoted as evidence.

## Purpose & scope

**Owns:** periodic snapshots of a company's Screener.in consolidated page — raw HTML artifact
+ parsed structured fundamentals stored as versioned records.

**Produces:** C1 fetched source items (snapshot artifacts) and a fundamentals-specific
structured record family.

**Does not own:** ratio *derivation* beyond what Screener displays; price data; filings.

## Partitions & cadence

| Partition key | Interval |
|---|---|
| `(screener, company:{id})` | 7 d + jitter (off-peak IST) |
| event-triggered refresh | after a `financial_results` event is clustered for the company |

## Schema impact (known requirement)

`db/schema.sql` v1.2 has no fundamentals tables. Required minimal addition (design note,
not DDL) — to be applied via the migration process once the baseline freezes:

```
intelligence.fundamentals_snapshots (
  snapshot_id, company_id, artifact_id → ingestion.raw_artifacts,
  statement_region, period_label, period_end_date,
  field_key, value_numeric, unit_label, currency,
  consolidated BOOLEAN, source_revision_flag BOOLEAN,
  parsed_by_version TEXT, fetched_at TIMESTAMPTZ,
  UNIQUE (company_id, statement_region, period_label, field_key, artifact_id)
)
```

Plus `core.ref_sources` row `screener` (`source_kind='fundamentals'`, interval 604800 s).

## Acceptance criteria

1. Weekly snapshots for whitelisted companies across four consecutive weeks, zero unalerted parse failures.
2. Artifact replay: every snapshot re-derives identical structured rows offline.
3. Unit safety: zero stored values lacking an explicit unit label.
4. Kill-switch drill: disabling the module halts all Screener traffic within one cycle; others unaffected.
5. Results-event-triggered refresh lands within 24 h of event clustering.

## Open questions

1. Official Screener API / licensing tier that would retire the risk acceptance? Revisit quarterly — if yes, migrate and delete this module.
2. Is consolidated-only sufficient, or do banks need standalone snapshots too? (Check with HDFC Bank data.)

## Status & next milestone

- **pilot-proven**: `scripts/fetch_fundamentals.py` fetches + parses all data tables
  (stdlib-only HTMLParser) for both pilot companies; manual runs only.
- **Next milestone:** DB-integrated snapshots land **after Phase 0** core capture works —
  deliberately sequenced behind exchange/news modules given cadence constraints.

## Detail index (L2)

| File | Load when |
|---|---|
| [`source-mechanics.md`](source-mechanics.md) | fetch posture, URL mapping, page regions, failure handling |
| [`data-contract.md`](data-contract.md) | snapshot semantics, unit-safety rules, idempotency keys |
| [`evidence-log.md`](evidence-log.md) | debugging surprises, known pitfalls |
