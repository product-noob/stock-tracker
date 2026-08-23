# AGENTS.md — Stock Market Insights Engine (`~/projects/stock-tracker`)

Entry point for coding agents working in this repository. Read this before making changes.

## Session restart context (read first, every session)

The **durable audit record is [`docs/reviews/2026-08-23-comprehensive-review.md`](docs/reviews/2026-08-23-comprehensive-review.md)** — a full-repository review with a stable finding ledger (`ST-*` IDs), evidence, dispositions, and prioritized recommendations. Restart from there:

1. Read the review's §1 (executive summary), §13 (prioritized recommendations + owner decisions), and §14 (limitations).
2. Then read `PROJECT_STATUS.md` (current phase + verified boundary table) and `docs/ROADMAP.md` (phase plan + decision log) — both stay current with the review.
3. Check the finding ledger before re-investigating anything: if an `ST-*` ID says *fixed*, confirm via its listed verification rather than redoing the analysis; if *open*, it is real known work.

State of play after the 2026-08-23 remediation: capture bytes-before-parse, BSE page provenance, fail-closed source validation, bounded/allowlisted downloads, and the content attempt/link lifecycle are implemented; test isolation is enforced via a marked disposable DB/object root (`docs/testing.md`); history was scrubbed of credential-bearing paths twice after concurrent-session incidents. Remaining P0s live in review §13 items 0–8 (fetch-event keys `ST-C06`, M2/M3 concurrency `ST-C07`, outbox dispatch `ST-P03`, migrations/roles `ST-P01`/`ST-P07`).

## What this is

A personal research tool for Indian listed companies (NSE/BSE): fetch official filings and curated news, extract evidence with an LLM, cluster documents into business events, rank explainable insights, join daily market reactions, and deliver a short Telegram briefing. No trading; association language only.

## Start here

1. **Load `docs/ROUTER.md` first** — it is the documentation map: what to read for which task, module inventory with status, and global invariants. `docs/ROADMAP.md` tracks phases and current position. For a human-friendly visual overview, open `docs/index.html`.
2. Read `PROJECT_STATUS.md` — current phase, what's done, and next steps. Update it when a meaningful milestone lands.
3. For design decisions, read `docs/architecture-hld.md` (authoritative) and `docs/database-schema.md`. `pilot_learnings.md` and `product_note.html` give product context.
4. Inspect live state before changing anything: `docker compose ps`, DB contents, existing scripts. Do not infer from docs alone.

## Environment

- **Database:** dedicated PostgreSQL 17 via this repo's `docker-compose.yml` (`stocktracker_db`), bound to `127.0.0.1:5433`, image pinned by digest with a healthcheck. DB/user `stocktracker`; password in `.env` (never display or commit it). Start with `docker compose up -d`. As of 2026-08-23 the container is healthy and holds persistent capture, market, pipeline, and append-only integration-test rows; it is not disposable.
- **Schema:** `db/schema.sql` is the applied baseline, not a safe live update mechanism now that persistent data exists. Do not reset/re-apply it or edit live DDL ad hoc. Freeze the baseline and introduce reviewed forward migrations before the next schema change (see `docs/reviews/2026-08-23-comprehensive-review.md`).
- **Python:** scripts use `curl_cffi`, `pdfplumber`, `openai`, `psycopg`, `yfinance`, and pandas (currently obtained transitively, a review gap) on top of the stdlib. Set up once with `python3 -m venv venv && venv/bin/pip install -r requirements.txt`, then run scripts as `venv/bin/python scripts/<script>.py`. The venv is gitignored; direct versions are pinned, but there is no transitive lock/hash workflow yet.
- **Secrets:** `.env` holds credentials (`POSTGRES_PASSWORD`, `OPENAI_API_KEY`, `DATABASE_URL` for DB-writing scripts; see `.env.example`). It is gitignored and must remain mode `0600`; keep it that way.

## Conventions

- Python scripts live in `scripts/`; they are generalized by company slug. The registry is `scripts/companies.py`: adding a new company = adding one entry there (with checksum-valid ISIN), then running the scripts with its slug. It currently holds all 30 SENSEX constituents; `scripts/seed_core.py` loads the registry into the DB core schema (idempotent, one transaction).
- `scripts/make_report.py <slug>` renders everything fetched/derived for a company into `data/<slug>/report.html` (throwaway debugging view).
- Module requirement specs live in `docs/modules/` (capture HLD + NSE/BSE/Google-News/Screener specs); `docs/schema-explained.html` is a plain-English schema walkthrough.
- Per-company pilot outputs land under `data/<slug>/` (`nse/`, `bse/`, `news/`, `extracted_text/`, `raw_meta/`, `prices/`, `fundamentals/`). They are archival/non-portable pilot evidence, not canonical runtime state or deterministic fixtures. Canonical immutable objects live under gitignored `data/objects/`; raw PDFs stay out of git, while some derived historical samples are committed. Generated live dashboards, database dumps, private logs, and volatile runtime snapshots must not be tracked. New committed fixtures must be small, portable, provenance-labelled, and deterministic.
- **Screener.in guardrail:** `scripts/fetch_fundamentals.py` scrapes Screener.in. **2026-08-23 owner directive: full-scope scraping approved — ToS constraints lifted** (see `docs/ROADMAP.md` decision log). Rate limiting remains as ban-avoidance engineering: sequential requests, sane spacing, backoff on 403s — getting the IP banned would defeat the crawl.
- `scripts/resolve_news.py` resolves opaque `news.google.com` redirects to publisher URLs (base64 decode or batchexecute flow), cached per token.
- Design principles in the HLD are binding: fetched bytes are persisted before parsing, at-least-once with idempotency keys, append-only versioned outputs, "the LLM extracts, code decides." Do not confuse a stated invariant with an implemented guarantee; the comprehensive review records current gaps.
- Commit style follows conventional commits (`feat:`, `chore(db):`, `docs:` …).
- **Keep `docs/BUILD_LOG.md` updated** with timestamped entries during working sessions (owner requirement): one entry per meaningful action, append-only, newest last.

## Operational learnings (broadly applicable, learned the hard way)

- **Job handlers register at import time.** A new `job_kind` only exists for a worker if its module is imported by `scripts/job_worker.py`. Forgetting the import produces `NoHandlerError` on every job of that kind (caught live 2026-08-23).
- **Capture runs are idempotent per partition + IST calendar day.** Re-running the same day short-circuits with zero network — that is correct behaviour, not a bug. To force a genuine live fetch (e.g. for testing), call `_run_partition_impl(..., day_key="<YYYYMMDDsuffix>")` programmatically instead of deleting run rows.
- **Filing lineage lives in `raw_artifacts.metadata`** (`seq_ids`/`news_ids` → raw-item idempotency keys), NOT in `documents.record_contents`, which stays empty until the M4 content processor is built. Joins for “which PDF belongs to which announcement” must go through artifact metadata.
- **Object-store URLs need an extra prefix**: store root is `data/objects/` but `storage_path` values already start with `objects/`, so served hrefs are `objects/<storage_path>` relative to `data/`. Verify blob links against disk before shipping UI code.
- **Append-only guards are `reject_mutation()` triggers on ~28 tables**, including ones you would not expect (`raw_feed_items`). Any authorized cleanup must disable those triggers inside ONE transaction and re-enable them after, and must respect FK order (`record_raw_items` before `raw_feed_items`). Never leave triggers disabled.
- **When spawning worker subprocesses from tests**, set `STOCKTRACKER_USE_TEST_DB=1` in the child env — `sys.modules` does not cross process boundaries, so unittest detection alone is insufficient.
- **BSE attachment paths are filename-shape-aware**: GUID-named filings exist only under `AttachLive` (His returns 404 HTML); legacy numeric names are archive-first. Only VALID pdf captures count as "captured" — see `docs/modules/bse-capture/evidence-log.md` (2026-08-23) before changing download logic.

## Safety

- The database volume (`stock-tracker_db_data`) and `data/objects/` contain persistent data and are not currently backed up. On 2026-08-23 the owner explicitly deferred backup/restore implementation until the system is stable and accepted that temporary risk; this is not evidence that the data is disposable. Destructive Docker/volume commands, schema resets, truncation, fixture cleanup, or object cleanup remain forbidden without fresh explicit authorization and a precise target.
- Do **not** run DB integration tests against the application database or `data/objects/`. Full tests require the positively marked disposable database and separate object root documented in `docs/testing.md`; absent or partial test configuration must fail closed.
- Do not print `.env` contents or connection strings with passwords in output or commits.
- Host-level context lives in `~/SERVER_CONTEXT.md`; update it if Docker service topology changes.
