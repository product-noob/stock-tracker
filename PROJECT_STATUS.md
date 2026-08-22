# Project Status — Stock Market Insights Engine

**Last updated:** 2026-08-22

## What this is

A personal research tool that watches a whitelist of Indian listed companies, captures official NSE/BSE filings and curated news, extracts evidence with an LLM, clusters documents into business events, ranks explainable insights, joins daily market reactions, and delivers a short Telegram briefing. No trading; association language only.

## Current phase

Moving from **docs + pilot scripts** to a working pipeline backed by the real PostgreSQL schema.

## Done so far

- Architecture HLD (v1.2), database schema design docs, and pilot learnings captured in-repo
- Pilot pipeline scripts run end-to-end for HDFC Bank and Tata Elxsi (`scripts/`, sample data in `data/`)
- **Screener.in fundamentals utility** (`scripts/fetch_fundamentals.py`): fetches consolidated page + parses all data tables (stdlib-only). Hobby-use only — occasional manual runs
- **Google News redirect resolver** (`scripts/resolve_news.py`): resolves `news.google.com` opaque tokens to publisher URLs via direct base64 decode or the batchexecute signature flow; cached per token, 100/100 resolved for HDFC Bank
- **Database live:** dedicated PostgreSQL 17 container (`stocktracker_db`) via `docker-compose.yml`, bound to `127.0.0.1:5433`, creds in `.env`
- Baseline schema applied from `db/schema.sql`: 7 planes (`core`, `ingestion`, `documents`, `intelligence`, `market`, `platform`, `delivery`)
- **Core schema seeded** via `scripts/seed_core.py`: 30 SENSEX-constituent companies, 60 listings, 86 aliases, whitelist + ingestion source partitions (idempotent — re-run inserts nothing)
- Company registry (`scripts/companies.py`) expanded to all 30 SENSEX constituents with checksum-validated ISINs
- Per-company HTML report generator (`scripts/make_report.py`); sample at `data/hdfcbank/report.html`
- Module requirement specs captured as tiered docs (`docs/ROUTER.md` → `docs/modules/<mod>/OVERVIEW.md` → L2 detail); Yahoo market-data module specified (yfinance chosen as primary daily-price provider, ROADMAP decision log); plain-English schema explainer in `docs/schema-explained.html`
- **Durable job/outbox worker — DONE (Phase 0 next-action 1)** (`scripts/jobs.py`, `scripts/job_handlers.py`, `scripts/job_worker.py`) on `platform.processing_jobs` / `platform.outbox_events`: idempotency-key dedup on enqueue, FOR UPDATE SKIP LOCKED claim with heartbeat lease, lease-expiry crash recovery, exponential-backoff retry → dead-letter quarantine, transactional outbox emission (one `job.failed` event per attempt, terminal `job.dead` on quarantine); exact + prefix (`selftest.`-style family kinds) handler resolution; CLI worker with graceful SIGTERM/SIGINT (`--queue`, `--drain`, `--once`, `--enqueue-demo`). Integration tests in `tests/test_jobs.py` (`venv/bin/python -m unittest tests.test_jobs -v`, 7/7 pass) prove claim exclusivity under 6 concurrent workers, hard-crash recovery via subprocess `os._exit(9)` + lease reclaim + successful retry, poison-job quarantine to dead with immutable terminal rows, outbox emission/dedup, and SIGTERM finishing the in-flight job. Verified live: two concurrent CLI workers drained 10 demo jobs 5/5 with exactly-once logical effects and zero duplicates

## Next steps (rough order — tracks HLD Phase 0 "reliable capture")

1. ~~Seed the whitelist~~ ✅ done (`seed_core.py`)
2. ~~Durable job queue + outbox worker pattern~~ ✅ done (`scripts/jobs.py` + `job_worker.py`; downstream fetchers register handlers per job_kind and run inside this framework)
3. Object store + ingestion plumbing: partition-aware fetchers writing exact response bytes to immutable storage + `fetch_runs`/`raw_artifacts`/`raw_feed_items`/cursors lineage in the DB, running as jobs in the TODO-1 framework (per `docs/modules/*/OVERVIEW.md` specs)
4. Normalizer (M2) + identity resolver (M3) with fail-closed review flow
5. Plain resolved-filing Telegram digest + reconciliation harness → Phase 0 gate: 7 unattended days, worker-kill tests, measured coverage
6. Text extraction → LLM fact extraction → event clustering stages (Phase 1)
7. Market data join and reaction calculation; ranking + digest delivery (Phase 2)

## Ops notes

- Run scripts: `venv/bin/python scripts/<script>.py` (setup: `python3 -m venv venv && venv/bin/pip install -r requirements.txt`)
- Start/stop DB: `cd ~/projects/stock-tracker && docker compose up -d / down`
- Connect: `psql -h localhost -p 5433 -U stocktracker -d stocktracker` (password in `.env`)
- Schema changes: edit `db/schema.sql` directly while no persistent data exists (no migrations yet, by design)
- Database is not yet covered by host backup scripts
