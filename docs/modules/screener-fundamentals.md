# Module Spec — Screener Fundamentals Capture

**Status:** v1.0 · **Module card:** M1 source adapter (fundamentals family) · **Master:** [`README.md`](README.md)
**⚠ Policy status:** This module **amends the PRD scope table** (§2 marks fundamentals "DEFERRED — requires a compliant and reliable base"). The owner explicitly accepted the ToS/compliance risk on **2026-08-22** and approved automated capture with the constraints in §2. The constraints below are not optional hygiene — they are the conditions under which the risk acceptance holds.

---

## 1. Purpose & scope

**Owns:** periodic snapshots of a company's Screener.in fundamentals page — raw HTML artifact + parsed structured fundamentals (ratios, quarterly results, shareholding, etc.) stored as versioned records.

**Produces:** C1 fetched source items (snapshot artifacts) and a fundamentals-specific structured record family. Fundamentals data is **context/enrichment only**: it never enters the severity/red-flag path and is never quoted as evidence (PRD §6 evidence rules apply to filings/news, and this source is not an official disclosure).

**Does not own:** ratio *derivation* beyond what Screener itself displays (no computed metrics in v1 — PRD defers ratio products to measured need), price data, filings.

## 2. Risk acceptance & operating constraints

Screener.in's terms restrict automated access. The owner has accepted this risk for a **personal, single-user research tool**. The following constraints are mandatory conditions of that acceptance:

- R-SC-1: **Low cadence.** Snapshots at most weekly per company, plus event-triggered refresh after results-day processing. No polling loops; this module has no "5-minute" mode, by design.
- R-SC-2: **Polite client.** Single sequential request per run, browser UA, no parallel company fetches, ≥5 s spacing, honor robots/cache headers where present, run at off-peak hours (IST night).
- R-SC-3: **Personal use only.** Snapshots are never redistributed, republished, or exposed beyond the owner's own tooling (consistent with HLD §10 retention stance).
- R-SC-4: **Kill-switch.** A config flag (`screener.enabled=false`) halts all Screener partitions immediately without touching other modules; a legal/complaint contact event or ToS change pauses the module pending owner review.
- R-SC-5: **Fallback path.** The PRD's preferred long-term base — own extraction from official results filings — remains the roadmap; this module is explicitly a convenience layer, and its data must be replaceable by M5-extracted filing facts without schema breakage.

## 3. Sources & access mechanics

### 3.1 Endpoint

```
GET https://www.screener.in/company/{SYMBOL}/consolidated/
```

Observed (Tata Elxsi pilot, §1): plain request with a browser UA returned the real page — no blocking in a one-off pull. **No sustained-polling evidence exists**; the weekly cadence and canary below reflect that.

- R-SC-6: URL mapping per company comes from `core.listings` (NSE symbol) plus a curated `screener_url_slug` config field on the partition — Screener slugs occasionally differ from NSE symbols (e.g. suffixes like `-RESD`); never auto-construct blindly, always verify first-fetch title matches the company.
- R-SC-7: A browser-TLS-impersonating client (same family as NSE/BSE modules) with browser UA; on 403/anti-bot interstitial, back off and mark partition degraded — do not escalate evasion. This module's risk acceptance does not cover active protection circumvention.

### 3.2 Page regions captured

| Region | Structured fields (v1) |
|---|---|
| Top ratios strip | market cap, current price, high/low, stock P/E, book value, ROE, face value, dividend yield |
| Quarterly results table | per-quarter: sales, expenses, net profit, EPS, op margin, OPM %, other income, interest, depreciation, PBT, tax, net profit |
| Annual P&L (10-yr) | same columns annually |
| Balance sheet / cash flow | annual lines as displayed |
| Shareholding | promoter holding %, FII, DII, government, public — quarterly |
| Pros & cons, peer comparison | stored as text snapshot (context only) |

## 4. Partitions & cadence

| Partition key | Interval |
|---|---|
| `(screener, company:{id})` | 7 d + jitter (off-peak IST) |
| event-triggered refresh | after a `financial_results` event is clustered for the company |

- R-SC-8: Every snapshot response is persisted as an immutable artifact (SHA-256) before parsing — Screener revises displayed history silently, so the artifact history IS the revision history.
- R-SC-9: Parsed output is a **versioned snapshot record keyed by artifact**: a new snapshot appends; nothing in-place updates. Comparisons (e.g. "promoter holding changed X→Y") are computed across snapshot versions, never by mutating rows.

## 5. Data contract

- R-SC-10: `dedup_scope = screener`; idempotency key = `{company_id}+{snapshot_period_start}+{artifact_sha}`. Re-fetching an unchanged page yields a new artifact with identical hash → dedup absorbs it (no snapshot row).
- R-SC-11: Parsed numbers stored with their **displayed units and period labels verbatim** (e.g. `(Rs Cr)`, `Mar 2026`) alongside normalized numeric values + declared unit — Screener switches units (Cr/Lakh) and mis-parsing scale is the classic fundamentals bug.
- R-SC-12: Consolidated vs standalone is part of the partition config (default consolidated) and stored on every snapshot — never mixed.
- R-SC-13: Fields map to a `fundamentals_snapshots`-style extension (schema addition required — see §7) rather than being forced into `normalized_records`, which models announcements/news. The partition/fetch/artifact lineage rows are shared infrastructure.

## 6. Failure modes & reliability requirements

| Failure | Detection | Required behavior |
|---|---|---|
| Anti-bot interstitial / 403 | status + content sniff | mark degraded, backoff to next scheduled window, alert; **no** evasion escalation (R-SC-7) |
| Layout change breaks parser | schema validation on parsed output + field-null-rate monitor | keep artifact (replayable), fail parse loudly, alert |
| Slug mismatch / wrong company page | first-fetch title-vs-company check (R-SC-6) | hard fail partition provisioning |
| Unit/scale ambiguity | unit-label presence check (R-SC-11) | null the value rather than guess |
| Silent history revision by Screener | artifact-hash diff across snapshots | append new version; surface "revised" flag on comparison |

## 7. Schema impact (required addition)

`db/schema.sql` v1.2 has no fundamentals tables. Required minimal addition (design note, not DDL):

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

Plus `core.ref_sources` row `screener` (source_kind = `fundamentals`, `default_poll_interval_sec = 604800`).

## 8. Acceptance criteria

1. Weekly snapshots for all whitelisted companies for four consecutive weeks with zero parse-failures unalerted.
2. Artifact replay: every snapshot re-derives identical structured rows offline.
3. Unit-safety: zero stored values lacking an explicit unit label.
4. Kill-switch drill: disabling the module halts all Screener traffic within one poll cycle and other modules are unaffected (parallelism guarantee).
5. A results-event-triggered refresh lands within 24 h of event clustering, demonstrating the enrichment join.

## 9. Open questions

1. Does Screener offer an official API or data-licensing tier that would retire the risk acceptance? (Revisit quarterly — if yes, migrate and delete this module.)
2. Is consolidated-only sufficient, or do bank/financial companies require standalone snapshots too? (Banks' consolidated statements differ structurally — check with HDFC Bank data.)
3. Should event-triggered refresh extend to corporate-action events (dividend/buyback change yield-dependent displayed ratios)?
