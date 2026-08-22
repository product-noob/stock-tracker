# AGENTS.md — Stock Market Insights Engine (`~/projects/stock-tracker`)

Entry point for coding agents working in this repository. Read this before making changes.

## What this is

A personal research tool for Indian listed companies (NSE/BSE): fetch official filings and curated news, extract evidence with an LLM, cluster documents into business events, rank explainable insights, join daily market reactions, and deliver a short Telegram briefing. No trading; association language only.

## Start here

1. **Load `docs/ROUTER.md` first** — it is the documentation map: what to read for which task, module inventory with status, and global invariants. `docs/ROADMAP.md` tracks phases and current position.
2. Read `PROJECT_STATUS.md` — current phase, what's done, and next steps. Update it when a meaningful milestone lands.
3. For design decisions, read `docs/architecture-hld.md` (authoritative) and `docs/database-schema.md`. `pilot_learnings.md` and `product_note.html` give product context.
4. Inspect live state before changing anything: `docker compose ps`, DB contents, existing scripts. Do not infer from docs alone.

## Environment

- **Database:** dedicated PostgreSQL 17 via this repo's `docker-compose.yml` (`stocktracker_db`), bound to `127.0.0.1:5433`, image pinned by digest with a healthcheck. DB/user `stocktracker`; password in `.env` (never display or commit it). Start with `docker compose up -d`. As of 2026-08-22 the container is healthy, the baseline schema is applied, and tables are still empty (no persistent data).
- **Schema:** `db/schema.sql` is the single editable baseline while no persistent data exists — edit it directly and re-apply to a fresh database (`psql -v ON_ERROR_STOP=1 -f db/schema.sql`); do not write migrations yet.
- **Python:** scripts use `curl_cffi`, `pdfplumber`, `openai`, and `psycopg` on top of the stdlib (parsing/fetching helpers are deliberately stdlib-only where possible). Set up once with `python3 -m venv venv && venv/bin/pip install -r requirements.txt`, then run scripts as `venv/bin/python scripts/<script>.py`. The venv is gitignored.
- **Secrets:** `.env` holds credentials (`POSTGRES_PASSWORD`, `OPENAI_API_KEY`, `DATABASE_URL` for DB-writing scripts; see `.env.example`). It is gitignored; keep it that way.

## Conventions

- Python scripts live in `scripts/`; they are generalized by company slug. The registry is `scripts/companies.py`: adding a new company = adding one entry there (with checksum-valid ISIN), then running the scripts with its slug. It currently holds all 30 SENSEX constituents; `scripts/seed_core.py` loads the registry into the DB core schema (idempotent, one transaction).
- `scripts/make_report.py <slug>` renders everything fetched/derived for a company into `data/<slug>/report.html` (throwaway debugging view).
- Module requirement specs live in `docs/modules/` (capture HLD + NSE/BSE/Google-News/Screener specs); `docs/schema-explained.html` is a plain-English schema walkthrough.
- Per-company outputs land under `data/<slug>/` (`nse/`, `bse/`, `news/`, `extracted_text/`, `raw_meta/`, `prices/`, `fundamentals/`). Raw PDFs stay out of git; derived sample outputs are committed.
- **Screener.in guardrail:** `scripts/fetch_fundamentals.py` scrapes Screener.in under an explicit owner risk acceptance (`docs/modules/screener-fundamentals/OVERVIEW.md` §Policy). Constraints are mandatory: low cadence (≈weekly + event-triggered), polite sequential client, personal use only, config kill-switch, and the official-results extraction path remains the long-term replacement. Never poll in a loop.
- `scripts/resolve_news.py` resolves opaque `news.google.com` redirects to publisher URLs (base64 decode or batchexecute flow), cached per token.
- Design principles in the HLD are binding: fetched bytes are persisted before parsing, at-least-once with idempotency keys, append-only versioned outputs, "the LLM extracts, code decides."
- Commit style follows conventional commits (`feat:`, `chore(db):`, `docs:` …).

## Safety

- The database volume (`stock-tracker_db_data`) is disposable **only** while the project holds no real data. Once real data exists, treat destructive Docker/volume commands as forbidden without explicit authorization.
- Do not print `.env` contents or connection strings with passwords in output or commits.
- Host-level context lives in `~/SERVER_CONTEXT.md`; update it if Docker service topology changes.
