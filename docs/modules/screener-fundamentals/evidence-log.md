---
tier: L2
module: screener-fundamentals
doc: evidence-log
parent: OVERVIEW.md
policy: append-only — dated entries, newest last; supersede via new entry + marker, never rewrite
---

# Screener Fundamentals — Evidence Log

Empirical findings. Cite as `(SC evidence-log, YYYY-MM-DD)`.

## 2026-08-21 — Tata Elxsi pilot (Part 1)

- One-off consolidated-page pull succeeded with plain browser-UA request; no anti-bot challenge observed.
- Page structure stable enough for stdlib HTMLParser section/table extraction.

## 2026-08-22 — HDFC Bank run

- Second company parsed cleanly; fundamentals JSON produced for both pilots.
- Risk acceptance for automated capture granted by owner this date (see OVERVIEW §Policy);
  constraints R-SC-1..5 are conditions of that acceptance.

## Open field observations (to confirm)

- Sustained weekly polling untested — cadence deliberately unproven until Phase 0 core is done.
- Bank-specific standalone-vs-consolidated question untested (needs HDFC Bank comparison).

## 2026-08-23 — Owner policy supersession and review

- Owner approved full-scope crawling and explicitly lifted the 2026-08-22 ToS-conditioned
  R-SC-1..5 limits. That historical acceptance remains above as evidence; it no longer
  constrains cadence. Sequential spacing/backoff remains required to avoid an IP ban.
- Offline replay of tracked HDFC raw HTML reproduced its JSON exactly. This validates the
  historical sample parser only, not current source access or the target snapshot contract.
- The file utility has no title/company or interstitial canary, pacing/backoff, atomic
  versioned write, explicit unit/provenance representation, or duplicate-label protection.
  No DB snapshot adapter/schema or scheduled fleet crawl exists.
