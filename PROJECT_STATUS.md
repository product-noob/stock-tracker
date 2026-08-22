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
- Baseline schema applied from `db/schema.sql`: 7 planes (`core`, `ingestion`, `documents`, `intelligence`, `market`, `platform`, `delivery`) with reference/seed data loaded

## Next steps (rough order)

1. Seed the whitelist: companies, instruments, listings, aliases for the pilot names
2. Build source fetchers (NSE/BSE RSS, announcements) writing raw bytes + ingestion lineage into the DB
3. Text extraction → LLM fact extraction → event clustering stages
4. Market data join (daily bars per listing) and reaction calculation
5. Ranking + Telegram digest delivery

## Ops notes

- Run scripts: `venv/bin/python scripts/<script>.py` (setup: `python3 -m venv venv && venv/bin/pip install -r requirements.txt`)
- Start/stop DB: `cd ~/projects/stock-tracker && docker compose up -d / down`
- Connect: `psql -h localhost -p 5433 -U stocktracker -d stocktracker` (password in `.env`)
- Schema changes: edit `db/schema.sql` directly while no persistent data exists (no migrations yet, by design)
- Database is not yet covered by host backup scripts
