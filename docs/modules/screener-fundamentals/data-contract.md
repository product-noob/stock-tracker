---
tier: L2
module: screener-fundamentals
doc: data-contract
parent: OVERVIEW.md
updated: 2026-08-22
---

# Screener Fundamentals — Data Contract

Snapshot semantics, unit-safety rules, idempotency keys.
Schema context: [`../../database-schema.md`](../../database-schema.md) §4; required schema
addition listed in [OVERVIEW](OVERVIEW.md) §Schema impact.

## 1. Snapshot semantics

| Req | Rule |
|---|---|
| R-SC-8 | Every snapshot response persisted as immutable artifact (SHA-256) before parsing — Screener revises displayed history silently, so **artifact history IS the revision history**. |
| R-SC-9 | Parsed output is a versioned snapshot record keyed by artifact: new snapshots append; nothing updates in place. Comparisons (e.g. "promoter holding changed X→Y") compute across snapshot versions. |

## 2. Idempotency

| Req | Rule |
|---|---|
| R-SC-10 | `dedup_scope = screener`; key = `{company_id}+{snapshot_period_start}+{artifact_sha}`. Re-fetching an unchanged page yields an identical-hash artifact → dedup absorbs it (no new snapshot row). |

## 3. Unit safety (the classic fundamentals bug)

| Req | Rule |
|---|---|
| R-SC-11 | Store numbers with their **displayed units and period labels verbatim** (e.g. `(Rs Cr)`, `Mar 2026`) alongside normalized numeric values + declared unit. Screener switches units (Cr/Lakh); scale mis-parsing is the canonical failure. Missing unit label → store null, never guess. |
| R-SC-12 | Consolidated vs standalone is partition config (default consolidated) and stored on every snapshot — never mixed. |

## 4. Table placement

| Req | Rule |
|---|---|
| R-SC-13 | Fields map to a `fundamentals_snapshots`-style extension (see OVERVIEW) rather than being forced into `normalized_records`, which models announcements/news. Partition/fetch/artifact lineage rows are shared infrastructure. |
