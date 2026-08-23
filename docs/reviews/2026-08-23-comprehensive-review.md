---
tier: L1
role: durable-review
status: complete
review-date: 2026-08-23
baseline-revision: cc445b03b94a7ab78aaed93e7ca97590d15f588f
---

# Comprehensive Review — 2026-08-23

## 1. Executive summary and review state

This is the durable record for the completed repository-wide correctness, reliability,
security, data-quality, operations, test, and documentation review requested on
2026-08-23. It covers every tracked implementation, test, schema/configuration file,
project/module document, tracked sample class, and relevant agent guidance, plus
explicitly read-only PostgreSQL and full object-store reconciliation. Future audits
should append evidence/dispositions here or create a clearly dated successor linked from
the router rather than creating an unconnected finding ledger.

**Executive conclusion:** the project has progressed well beyond docs/pilots—persistent
PostgreSQL/object storage, a robust processing-job core, NSE/BSE API paths, M2/M3 and
30 yfinance series exist—but it is not an unattended Phase 0 service. The gate has not
started. Backups/migrations, test isolation, exact-byte crash boundaries, BSE page
provenance, canonical content attempts/links, source trust bounds, outbox dispatch,
least-privilege roles, supervision and health reporting are P0 prerequisites. The M4–M7,
M8 reaction, and M9–M10 paths remain non-shipping file prototypes. Three confirmed Critical
defects were fixed; no
nice-to-have refactor was undertaken.

**Baseline time:** 2026-08-23 05:10 UTC

**Repository:** `/home/prince/projects/stock-tracker`

**Baseline revision:** `cc445b03b94a7ab78aaed93e7ca97590d15f588f`
(`docs(policy): yahoo daily-bar fetcher`)

**Review status:** complete, including persistence/control-plane, DB-integrated M1–M3,
remaining source/pilot/dependency/market/reporting/sample passes, and comprehensive
project/module/architecture/product/status documentation reconciliation.

### Scope in this pass

Deep inspection covered:

- `db/schema.sql`, `docs/architecture-hld.md`, and `docs/database-schema.md`, with
  emphasis on exact immutable bytes, append-only/versioned outputs, identity,
  jobs/outbox, transaction boundaries, recovery, and security;
- `scripts/object_store.py`, `scripts/jobs.py`, `scripts/job_handlers.py`,
  `scripts/job_worker.py`, and `scripts/seed_core.py`;
- `docker-compose.yml`, `.env.example`, `requirements.txt`, and secret-file
  metadata (never secret values);
- `tests/test_object_store.py`, `tests/test_jobs.py`, and the database-selection /
  persistence behavior of every other test module;
- read-only PostgreSQL catalog/data aggregates and a full hash/size/path
  reconciliation of the local object store.

This pass did **not** inspect stored bodies, raw object bytes, private URLs, `.env`
contents, or credentials. It did not crawl/refetch, advance a cursor, drain a
queue, delete test fixtures, reset schema, mutate application rows, or run the
unsafe persistent integration suite.

## 2. Method and evidence rules

1. Read `~/SERVER_CONTEXT.md`, `AGENTS.md`, `docs/ROUTER.md`,
   `docs/ROADMAP.md`, `PROJECT_STATUS.md`, the capture-domain master, and the HLD /
   schema contracts before inspection.
2. Captured Git state and a tracked-file inventory before edits.
3. Inspected source and tests statically, distinguishing documented guarantees
   from guarantees actually enforced by code/DDL.
4. Connected through the configured `DATABASE_URL` without printing it. Every
   audit connection began with `BEGIN TRANSACTION READ ONLY`; `SHOW
   transaction_read_only` returned `on`, and each was rolled back.
5. Compared all 44 application-table row counts before and after the audit and
   focused rollback-only regression tests. Every delta was zero.
6. Ran object reconciliation through `ObjectStore.verify(conn=read_only_conn)`;
   only aggregate results are recorded here.
7. Applied only confirmed P0 integrity/security/recovery fixes. Nice-to-have
   refactors remain recommendations.

### Evidence labels

- **Fact:** directly observed in tracked code, Git, filesystem metadata, or a
  read-only query.
- **Risk/inference:** a failure mode implied by facts, not evidence that the
  failure has happened.
- **Disposition:** fixed, accepted/deferred, or owner decision required.

## 3. Severity and priority rubric

| Severity | Meaning |
|---|---|
| **Critical** | Direct secret exposure, data loss/corruption path, or a control-plane failure that can permanently strand canonical work. |
| **High** | Material reliability/security/restore risk likely to impair this homelab service, but with a mitigating boundary or recovery path. |
| **Medium** | Real correctness, observability, maintenance, or test weakness that should be planned but is not an immediate incident. |
| **Low** | Localized quality/documentation issue with limited operational effect. |

Priorities are independent: **P0** before unattended operation or the next
schema/data expansion; **P1** next hardening milestone; **P2** planned cleanup;
**P3** optional/measured need.

## 4. Pre-existing worktree warning

The worktree was already dirty. These entries existed before this review and
were preserved untouched:

- modified tracked Pi artifacts:
  - `tmp/pi-goal-task/goal-2026-08-22T22-38-38-467Z-392e80bd/GOAL_RESULT.md`
  - `tmp/pi-goal-task/goal-2026-08-22T22-38-38-467Z-392e80bd/GOAL_STATE.json`
  - `tmp/pi-goal-task/goal-2026-08-22T22-38-38-467Z-392e80bd/GOAL_TRACE.jsonl`
  - `tmp/pi-goal-task/goal-2026-08-22T22-38-38-467Z-392e80bd/iterations/01/ITERATION_STATE.json`
  - `tmp/pi-long-task/goal-2026-08-22T22-16-15-255Z-ede4c999-todo-worker-01/TASK_RESULT.md`
- untracked owner file: `docs/BUILD_LOG.md`;
- untracked Pi result/progress trees under `tmp/pi-goal-task/` and
  `tmp/pi-long-task/`, including this review run.

No `tmp/pi-*` file was edited by this review. At baseline Git tracked 182 files:
161 outside `tmp/pi-*` and 21 historical Pi artifacts. The review file did not
exist at baseline. Additional untracked `backups/pre_cleanup_20260823_0541.sql` and
`tmp/full_ingest.log` appeared during the documentation pass, consistent with concurrent
owner/worker activity; they were not opened, edited, staged, summarized, or folded into
review evidence.

## 5. Runtime and service baseline

| Item | Observed fact |
|---|---|
| Host | Ubuntu 22.04.5 LTS, Linux 5.15.0-186-generic, x86_64 |
| Git | 2.34.1 |
| Python | system and venv 3.10.12 |
| Docker / Compose | 29.7.1 / v5.3.1 |
| PostgreSQL | container healthy and loopback-bound at `127.0.0.1:5433`; server 17.11 |
| Host `psql` | not installed; catalog queries used psycopg and server version used `docker compose exec` |
| Installed direct packages | `curl-cffi 0.16.1`, `pdfplumber 0.11.10`, `openai 3.3.1`, `psycopg 3.2.13`, `yfinance 1.6.0`, `pytest 9.1.1` (match direct pins) |
| Object store filesystem | root filesystem ext4, 350 files / 371 MiB under `data/objects`, root volume 55% used |
| Secret-file metadata | project `.env` was mode `0664` at baseline; fixed to `0600` without reading/printing values |

Compose positives: PostgreSQL image is digest-pinned, has a healthcheck and 2 GiB
memory cap, uses a named volume, and publishes only to loopback. Gaps are recorded
below.

## 6. Tracked component/file inventory

This inventory excludes historical tracked `tmp/pi-*` artifacts from product
coverage, but records them in §4. Tracked pilot/sample data is inventoried as a
separate non-runtime input class.

### 6.1 Implementation source (23)

- Capture/integration: `scripts/bse_capture.py`, `scripts/download_filings.py`,
  `scripts/download_pdfs.py`, `scripts/fetch_announcements.py`,
  `scripts/fetch_fundamentals.py`, `scripts/fetch_prices.py`,
  `scripts/nse_capture.py`, `scripts/resolve_news.py`
- Persistence/control: `scripts/object_store.py`, `scripts/jobs.py`,
  `scripts/job_handlers.py`, `scripts/job_worker.py`, `scripts/seed_core.py`
- Pipeline/identity: `scripts/normalize.py`, `scripts/resolve_identity.py`,
  `scripts/extract_text.py`, `scripts/extract_facts.py`,
  `scripts/price_reaction.py`, `scripts/rank_insights.py`
- Registry/reporting: `scripts/companies.py`, `scripts/make_report.py`,
  `scripts/make_dashboard.py`

### 6.2 Tests and fixtures (8)

- `tests/test_bse_capture.py`
- `tests/test_fetch_prices.py`
- `tests/test_jobs.py`
- `tests/test_normalize.py`
- `tests/test_nse_capture.py`
- `tests/test_object_store.py`
- `tests/test_resolve_identity.py`
- `tests/fixtures/nse_api_announcements.json`

### 6.3 Schema and configuration (5)

- `db/schema.sql`
- `.env.example`
- `.gitignore`
- `docker-compose.yml`
- `requirements.txt`

### 6.4 System/product documentation (11)

- `AGENTS.md`, `PROJECT_STATUS.md`, `nse-bse-rss.md`, `pilot_learnings.md`,
  `product_note.html`
- `docs/ROUTER.md`, `docs/ROADMAP.md`, `docs/architecture-hld.md`,
  `docs/database-schema.md`, `docs/index.html`, `docs/schema-explained.html`

### 6.5 Module documentation (21)

- Domain master: `docs/modules/README.md`
- For each of `bse-capture`, `google-news-capture`, `nse-capture`,
  `screener-fundamentals`, and `yahoo-market-data`:
  `OVERVIEW.md`, `data-contract.md`, `evidence-log.md`, and
  `source-mechanics.md`.

### 6.6 Other tracked material

- 93 pilot/sample files under `data/hdfcbank/`, `data/tataelxsi/`, and the two
  `data/prices_gap_report.*` summaries. They include 66 extracted-text files,
  JSON/XML/CSV/HTML pilot outputs, and reports; they are sample evidence, not the
  live object store. `data/dashboard.html` is one additional tracked generated
  database snapshot (94 tracked `data/` files total).
- Two tracked agent-memory documents:
  `.claude/agent-memory/docs-updater/MEMORY.md` and
  `.claude/agent-memory/docs-updater/feedback_doc_edits.md`.
- This review adds the trackable, non-ignored documentation file
  `docs/reviews/2026-08-23-comprehensive-review.md`; it was absent from the
  baseline inventory.
- The live `data/objects/` tree and `.env` are gitignored operational state.

## 7. Coverage matrix

“Deep complete” means the assigned pass reviewed implementation and its governing
contract. This matrix assigns every tracked implementation area so omission is visible.

| Area / tracked files | This pass | Remaining assigned review |
|---|---|---|
| Schema: `db/schema.sql` | **Deep complete**: constraints, FKs, indexes, trigger guards, queue/outbox and identity invariants | Fresh isolated-schema DDL/replay test once a safe disposable DB exists |
| Object storage: `object_store.py`, `test_object_store.py` | **Deep complete**; P0 integrity fix and regression | Backup/restore drill and large-store performance measurement |
| Queue/outbox: `jobs.py`, `job_handlers.py`, `job_worker.py`, `test_jobs.py` | **Deep complete**; lease/failure/signal/transaction semantics and P0 fix | Implement/test dispatcher and service supervision after owner alignment |
| Core seeding: `seed_core.py`, `companies.py` | **Deep complete** for transaction/idempotency/config safety; registry/sample mismatch recorded | Owner decision on non-whitelist pilot entries/defaults |
| NSE/BSE DB capture: `nse_capture.py`, `bse_capture.py`; corresponding tests and all NSE/BSE L1/L2 docs | **Deep complete**: source mechanics, exact-byte/crash ordering, pagination, idempotency, cursors, attachments, malformed input, and live reconciliation; BSE fail-open envelope defect fixed | Legacy file utilities reviewed below; soak/reconciliation needs safe operations design |
| Normalization/identity: `normalize.py`, `resolve_identity.py`; corresponding tests | **Deep complete**: mapping/versioning, append-only decisions, fail-closed ladder, manual/retry flow, concurrency, tests, and live reconciliation | Isolated concurrency/schema regressions after guarded test DB exists |
| Filing recovery: `download_filings.py` | **Deep complete**: pending selection, network/session behavior, URL/magic handling, run/job idempotency, transaction boundaries and failure reporting | Attempt/link model and retry policy need owner/design alignment; the referenced regression module is absent |
| Legacy file capture: `fetch_announcements.py`, `download_pdfs.py` | **Deep complete**: pagination, exact-byte/crash behavior, URL trust, status/CLI semantics and output layout | Pilot-only; do not promote these overlapping utilities to a service path |
| Yahoo/prices: `fetch_prices.py`, `test_fetch_prices.py`, all Yahoo L1/L2 docs | **Deep complete**: frame mapping, validation, revision keys, timezone/calendar/gap logic, lineage, runtime behavior and live read-only data audit | Isolated DB revision/crash tests and an authoritative trading calendar remain open |
| Google News/Screener: `resolve_news.py`, `fetch_fundamentals.py`, all corresponding L1/L2 docs | **Deep complete**: parser/resolver boundaries, cache/retry behavior, URL trust, snapshot semantics and sample replay | Both remain pilot/file-based; DB adapters are not built |
| Pilot extraction/ranking/reaction: `extract_text.py`, `extract_facts.py`, `price_reaction.py`, `rank_insights.py`; HLD M4–M9 and pilot learnings | **Deep complete**: parser bounds, evidence/quote gate, determinism, event semantics, no-look-ahead and causal wording | These are prototypes, not the DB-integrated M4–M9 pipeline |
| Reporting: `make_report.py`, `make_dashboard.py`, and tracked `data/dashboard.html` | **Deep complete**: escaping, query validity, privacy, read behavior and status claims | Dashboard files were committed concurrently by another task and preserved untouched by this pass |
| Root config/dependencies | **Deep complete** for compose exposure, direct/transitive pins, imports, runtime versions, env handling and secret metadata | Dependency lock/hash/update policy and service unit review |
| System and module docs | **Complete:** every tracked project/module Markdown/HTML document and relevant `.claude` guidance reviewed; current-state claims reconciled while historical evidence logs/build/pilot records were preserved and labelled | Re-audit status whenever a milestone or live gate changes |
| Tracked pilot/sample data and agent metadata | **Deep complete structurally and in aggregate**; provenance, relationships, staleness, path portability, secret patterns and causal/status claims checked without emitting source bodies | Owner decision on tracked public-source bodies and portable sample-fixture policy |

### 7.1 Documentation fact table (authoritative for this reconciliation)

| Topic | Verified fact used across current-state docs | Evidence boundary |
|---|---|---|
| Phase | Phase 0 is partially implemented; no seven-day soak/reconciliation gate has started | HLD gate + missing digest/RSS/reconciliation/supervision |
| Persistence | PostgreSQL and `data/objects/` contain durable application and append-only test state; baseline is frozen | live read-only/catalog/object audit; `ST-P01` |
| Control plane | processing-job claim/lease/retry/dead-letter exists; outbox dispatch and supervision do not | code review + 2,330/2,330 sampled events pending |
| NSE/BSE | per-company API adapters are DB-integrated for all 30 registry companies; RSS is not | code/registry/live reconciliation |
| M2/M3 | normalizer and resolver are DB-integrated; 258 adapter-shaped rows reconciled cleanly at sample | explicitly read-only scoped query |
| Content | adapter artifacts/content registrations exist; canonical attempts, record links and text versions were empty | live counts + code review |
| Prices | yfinance adapter is DB-integrated; 30 series / 7,529 current bars at sample | read-only market audit; acceptance/provenance gaps remain |
| Google News/Screener | historical file pilots only; no canonical DB adapter | code/file inventory |
| M4–M10 | extraction/ranking/reaction/report paths are file prototypes/debugging views, not HLD stage completion | code/schema/live empty downstream tables |
| Tests | full DB suite is unsafe on the application DB; only offline/rollback-safe subsets were run | static test DB-selection/persistence audit |
| Counts | all numeric live values in this review are timestamped 2026-08-23 evidence, never evergreen status | explicitly read-only transactions, rolled back |
| Historical docs | pilot/build/evidence counts remain unchanged as dated history and carry supersession/status labels | documentation reconciliation |

## 8. Live PostgreSQL and object-store baseline

All figures below are **facts from explicitly read-only transactions**. No bodies,
payloads, metadata values, URLs, hashes, or stored bytes were emitted.

### 8.1 Table counts (44 tables)

| Plane | Table counts |
|---|---|
| `core` | companies 111; aliases 141; instruments 30; listings 60; whitelist memberships 84; sources 8; record types 3 |
| `platform` | processing jobs 624; outbox events 2,330 |
| `ingestion` | partitions 236; fetch runs 325; artifacts 724; feed items 542; cursors 171; normalized records 618; raw-record links 508; identity resolutions 2,369; assignments 387; stage runs 131; stage errors 265 |
| `documents` | content objects 350; fetch attempts 0; record-content links 0; text versions 0 |
| `intelligence` | document kinds 14; event categories 9; event kinds 24; all extraction/event/insight/reaction data tables 0 |
| `market` | price series 30; price bars 7,529 |
| `delivery` | all four delivery tables 0 |

Catalog inventory: 44 tables, 156 indexes, 296 constraints, and 38 non-internal
triggers across the seven application schemas.

### 8.2 Queue/outbox and pipeline aggregates

- Jobs: 539 succeeded, 67 dead, 18 queued, 0 running. All 18 currently runnable
  rows are test/selftest-family fixtures. Of 624 jobs, 534 are test/selftest
  families and 15 are demo jobs.
- Outbox: all 2,330 events are pending; 2,264 were older than one hour at audit
  time. No published events exist.
- Fetch runs: 232 ok, 81 not-modified, 11 failed, 1 running. The running row was
  about 5.6 hours old; four cursors had nonzero consecutive failures.
- Stage runs: 96 ok and 35 partial.
- Identity: zero cache/assignment mismatches, zero assigned non-resolved
  decisions, and zero records with a company cache but no assignment.
- Uniqueness checks found zero duplicate artifact idempotency keys, duplicate
  `(partition,idempotency_key)` raw items, or duplicate content SHA rows.
- There are 114 test-looking partitions, 81 `Identity Test ...` companies, and
  at least 216 namespaced test normalized records. These are persistent fixture
  contamination, not production entities.
- 96 `api_response` raw artifacts lack a matching `content_objects` SHA. Static
  test inspection attributes this shape to synthetic normalizer fixtures that
  deliberately insert `/tmp/normtest` artifact metadata; no claim is made that
  every one is a test row without a destructive/body-level investigation.
- Aggregate secret-marker scan found zero token/password/API-key/signature-style
  markers in content metadata, artifact metadata, or raw content URL query
  patterns. This is a heuristic, not proof of absence.

### 8.3 Object reconciliation

`documents.content_objects` had 350 rows and the filesystem had 350 files. Full
size and SHA-256 verification reported: 0 missing, 0 size mismatch, 0 hash
mismatch, 0 stale temp, and 0 orphan objects. Registry paths were all canonical
`objects/<prefix>/<sha>` paths. The physical default is
`data/objects/objects/...`; awkward naming aside, DB and code agree.

After focused tests and all audit work, a second read-only snapshot found **no row
count changes in any application table** and object verification remained clean.

### 8.4 DB-integrated NSE/BSE → normalization → identity reconciliation

A second audit used registry canonical names (rather than trusting partition
descriptions) to identify the 30 configured companies. Facts from that explicitly
read-only transaction:

- all 30 registry entries match one company, one active whitelist membership, one
  instrument and two current listings (30 companies / 30 instruments / 60 listings);
- all 30 have both an NSE API and BSE API partition and a cursor; no such cursor had
  `consecutive_failures > 0` at this sample;
- those company-associated partitions held 33 NSE and 31 BSE fetch runs, all in
  `ok`/`not_modified` state at the sample (0 failed/running), plus 217 NSE and 124
  BSE artifacts and 169 NSE / 90 BSE raw items;
- one of the 169 NSE rows is a synthetic `corr:*` fixture inserted into the real
  Adani Ports partition. It has a non-adapter idempotency key, a synthetic missing
  content registration and no normalized link. Excluding that known fixture, all
  168 adapter-shaped NSE rows and all 90 BSE rows are linked to exactly 258
  normalized records, all 258 assigned, with zero open production review rows;
- assignment/resolution/cache reconciliation for those 258 returned zero cache
  mismatches, zero selected non-`resolved` decisions, zero non-whitelisted
  assignments and zero cache-without-assignment rows;
- there are zero production revision groups (no `(dedup_scope, external_id)` with
  more than one normalized row), zero normalized records linked to
  `documents.record_contents`, and zero `documents.content_fetch_attempts` globally;
- 257 company-associated rows carry a content URL. The sole apparently unhandled
  NSE URL is the same synthetic `corr:*` fixture; adapter-shaped rows had no
  outstanding URL by the artifact-metadata heuristic. This proves metadata
  convergence only, not that every URL currently serves the intended PDF;
- one company-associated artifact-to-object mismatch is also that synthetic
  `/tmp/corrtest` fixture (4-byte declared artifact, no content-object row). It must
  not be misreported as production adapter loss. The earlier full object-store
  reconciliation remains clean for registered content objects.

These sampled counts materially contradicted the then-current prose totals in
`PROJECT_STATUS.md` (192 NSE / 138 BSE raw items, 177 / 109 valid PDFs and 28 BSE invalid
artifacts). The documentation pass removed those values as evergreen status while preserving
them in dated evidence logs and kept the architecture requirements explicit.

## 9. Integration-test safety assessment

A full suite was intentionally **not** run. Several modules point directly at the
configured application database and commit permanent rows; running them would
materially contaminate persistent state and can process real pending records.

| Test module | Database selection | Persistence / isolation result |
|---|---|---|
| `test_object_store.py` | Reads `DATABASE_URL` from repo `.env`; connectivity checked | Filesystem uses temporary directories. DB inserts use caller transactions and explicit rollback. Safe in observed run; 17 tests caused zero table-count delta. |
| `test_jobs.py` | Repo `.env`, then environment fallback; real application DB | Most tests commit namespaced append-only jobs/outbox rows and intentionally cannot clean them. Concurrency-safe naming, but not data-isolated. New final-lease regression is rollback-only. |
| `test_nse_capture.py` | `jobs.db_url_from_env()`; real application DB | Creates permanent partition/fetch/artifact/item/cursor/content rows and physical blobs. UUID names prevent key collision, not contamination. |
| `test_bse_capture.py` | Same as NSE | Creates permanent partition/fetch/artifact/item/cursor/content rows and physical blobs. |
| `test_normalize.py` | Decorator checks only that a URL string exists, then connects to it | Commits fixture rows and `run_normalize` processes **all** pending items, including real application rows. This is not an isolated integration test. |
| `test_resolve_identity.py` | Same weak URL-presence gate | Commits fixture companies, aliases, whitelist rows, records, resolutions and outbox; resolver also processes real dev rows. |
| `test_fetch_prices.py` | None | Offline pure tests; no DB/network. |

Concurrency risk: independently namespaced keys avoid most direct conflicts, but
parallel suites share global pending work, source master rows, queue sweeps, and the
same object store. Repeatability therefore depends on current application state.
A production worker with no `--queue` filter can also claim unsupported queued test
kinds and drive them toward dead-letter state.

**Required P0 test policy:** provision a dedicated disposable test database/object
root; require an explicit test marker in the database; fail closed if the target is
the application DB; wrap non-append-only cases in rollback; reserve committed
append-only soak tests for an explicitly approved namespace/database.

## 10. Persistence/control-plane audit

### 10.1 Enforced strengths

- Content writes use same-directory temp files, file `fsync`, atomic rename, and
  directory `fsync`; DB rows key content by unique SHA-256 and relative path.
- Capture callers pass an existing DB connection, so registry and lineage rows
  share their SQL transaction. A DB rollback can leave an orphan file, but never a
  DB row pointing to bytes that were not atomically renamed first; reconciliation
  detects the orphan.
- Queue claim is a short `FOR UPDATE SKIP LOCKED` transaction. Ownership is checked
  on heartbeat, completion, and failure updates. Attempts, backoff, terminal state,
  and payload identity have DDL checks/triggers.
- Completion/failure job transitions and their queued outbox events are atomic.
  Domain handlers use deterministic keys and are designed for at-least-once replay.
- SIGTERM/SIGINT in `run_forever` finish the current handler; hard death relies on
  lease recovery. Handlers are imported centrally so registered production kinds
  are visible to the CLI worker.
- `seed_core.py` validates registry shapes before connecting, and inserts the core
  graph in one transaction with natural-key existence checks.
- Evidence and snapshot tables have broad update/delete guards; deferred triggers
  enforce identity cache/pointer agreement, event evidence membership, and reaction
  bar membership. Current live identity consistency checks are clean.
- NSE and BSE use bounded 30-second API/session calls, 60-second attachment calls,
  one session-renew retry, 24-hour overlap, deterministic item keys and PDF magic
  checks. BSE filters rows whose `SCRIP_CD` differs from its partition and retains
  every HTTP-200 invalid attachment body it receives.
- Normalization is deterministic for the two implemented API shapes and quarantines
  malformed raw payloads. Identity uses exact matching only, limits automatic
  candidates to the active whitelist, and current production assignments reconcile
  cleanly across the history row, current pointer and cache.

### 10.2 Failure windows and limitations

- Filesystem and PostgreSQL cannot share one atomic commit. A crash after file
  rename but before DB commit leaves a detectable orphan. A DB backup without the
  matching object snapshot (or vice versa) can still be unrestorable.
- Handlers commonly commit domain work before the worker commits job completion and
  completion events. Retry convergence depends on every handler's idempotency; this
  is not the literal single producer transaction described in HLD §9.
- There is event insertion but no outbox claim/dispatch/publish implementation in
  tracked code. Consequently “transactional outbox” currently records durable
  events but does not perform durable hand-off.
- Heartbeat exceptions are swallowed without a metric/log. Exponential retry has no
  documented jitter despite the HLD requirement. There is no tracked scheduler,
  dispatcher, systemd unit, health command, or alert consumer.
- `seed_core.py` accepts an existing natural-key row without verifying that its
  company/instrument/listing relationships and provider config match the registry.
  Canonical company names also lack a DB-level case-insensitive uniqueness rule;
  concurrent seeders could race.
- The identity-assignment FK guarantees matching record/company IDs but does not
  constrain the pointed resolution's `outcome='resolved'`; application code
  currently supplies the missing rule and live data has no violation.
- Direct dependencies are version-pinned and the container image is digest-pinned,
  but there is no transitive lock/hash file or dependency-update policy.

### 10.3 M1–M3 code-path and test assessment

- **Exact-bytes ordering is not implemented as documented.** NSE calls
  `json.loads` before `_persist_bytes`; BSE parses and deduplicates every page in
  `_collect_pages` and persists pages only after the entire fetch/canary sequence.
  A process death or parser bug in that interval loses the response from canonical
  artifact lineage. In addition, ordinary SELECTs begin a psycopg transaction before
  the nested `conn.transaction()` blocks; with autocommit off those blocks are often
  savepoints, not the short durable commits claimed by comments. The object file may
  exist, but SHA registration and fetch lineage are not durably committed before
  parsing.
- **Fetch-event lineage is content-collapsed.** API artifact idempotency keys include
  partition/page and response SHA but not the fetch-run identity. Identical responses
  can reuse an earlier artifact row, so a later fetch run lacks its own exact-response
  event even though content dedup correctly belongs in `content_objects`.
- **BSE multi-page provenance is incorrect in mixed-reuse cases.** All raw rows point
  at page 1's `artifact_id`, while `first_seen_run` is the value left by the *last*
  page insertion. A page-1 artifact reused from an older run plus a changed later page
  can violate the composite FK; even the success case attributes page-2 rows to page
  1 rather than their containing bytes.
- **Retry convergence is asymmetric.** NSE retries retained invalid PDF content;
  BSE treats both valid and `filing_pdf_invalid` artifacts as permanently handled.
  A temporary BSE HTML block page can therefore suppress later retrieval of a valid
  filing. Network failures with no bytes are only counters/log lines; neither adapter
  writes the required `content_fetch_attempts` row.
- **Same-day keys implement at most daily fetch, not the documented six-hour rolling
  cadence.** Every later invocation that IST day skips the API. The next day's
  overlap should eventually recover ordinary announcements, but near-real-time
  latency and same-day reconciliation/backfill are defeated, and the source docs and
  status disagree on cadence.
- **Malformed/drift behavior still has fail-open paths.** NSE silently skips non-object
  rows, accepts missing IDs as `nse_ann:None`, and does not verify returned symbol
  against the partition. BSE runs the empty-result canary before foreign-scrip
  filtering, so a non-empty all-foreign response can become a successful empty target
  without a canary. Neither downloader bounds response size; NSE accepts any HTTP(S)
  attachment host supplied by the exchange response, creating an SSRF/disk-exhaustion
  boundary if that upstream payload is compromised.
- **Normalization's advertised version correction is not reachable from the normal
  same-partition capture path.** Raw capture uniqueness drops later payload changes
  with the same native ID before M2 sees them. M2 can append `@v2` only when a changed
  sighting arrives through another partition/manual fixture. Its revision lookup also
  interpolates the native ID into a PostgreSQL regex without escaping it.
- **M2/M3 are not concurrency-claimed.** Snapshot selection plus unlocked rechecks
  allows parallel workers to race. Normalized unique keys usually force a retry, but
  identity has no unique root-decision constraint: two workers can append competing
  initial resolutions and last-writer-wins the assignment pointer. Stage-run
  `ON CONFLICT ... DO UPDATE` also lets workers share and overwrite one audit row.
- **Identity tier B is not the HLD's cross-exchange byte/record anchor.** It checks an
  NSE symbol against BSE `exchange_symbol`, but seeded BSE listings carry only
  `exchange_code`; the tier is therefore dead for current master data. ISIN/same-venue
  symbol tiers currently resolve NSE rows, and the live 258-record result is clean,
  but cross-source anchoring/content dedup is not implemented.
- **Manual identity is less guarded/observable than automatic identity.** The CLI
  verifies only that a company exists, not that it is actively whitelisted, and
  `apply_manual_resolution` emits no correction outbox event. This contradicts the
  module docstring's “only whitelisted companies are ever assigned” and can leave
  downstream consumers unaware of a correction.
- **Content capture is not joined to canonical records.** Live
  `record_contents=0` and `content_fetch_attempts=0`; URLs/IDs exist only in raw rows
  and artifact metadata. Calling the API adapters “full C1/C3 lineage” overstates the
  implemented M4 boundary and leaves downstream text processing without a canonical
  record→content edge.
- **Tests have useful pure assertions but unsafe/misleading integration coverage.**
  Helper tests cover timestamp conversion, magic bytes, basic pagination, mapping and
  exact-match ambiguity deterministically. They do not exercise real TLS/session
  behavior, redirects, response-size bounds, hard process death, concurrent M2/M3
  workers, mixed-page artifact reuse, cursor regression, all-foreign NSE/BSE bodies,
  or exact-byte-before-parse ordering. “Crash” attachment tests simulate two returned
  HTTP failures, not `os._exit` at transaction boundaries. DB classes persist rows in
  the application DB, process global pending/review work, and resolver methods depend
  on shared class state/test order; UUID names prevent collisions but do not provide
  isolation. The synthetic row in a real company partition demonstrates that this is
  operational contamination, not a theoretical concern.

### 10.4 Remaining source integrations, pilots, dependencies and data quality

#### Runtime classification and enforced strengths

The pass read every scoped file completely. The implementation boundary is materially
mixed and must be explicit:

- **DB-integrated but not a complete downstream service:** `fetch_prices.py` writes
  immutable market observations and C1-style CSV artifacts; `download_filings.py`
  writes content objects/raw artifacts as a recovery path. Neither creates the full
  canonical M4/M8 products promised by the HLD. In particular, filing content still
  has no `record_contents`/attempt edge and no reaction signal exists.
- **Legacy file-based capture pilots:** `fetch_announcements.py` and
  `download_pdfs.py`. They overwrite `data/<slug>` files and overlap the DB adapters;
  they are empirical utilities, not restart-safe canonical ingestion.
- **File-based later-stage prototypes:** `extract_text.py`, `extract_facts.py`,
  `rank_insights.py`, `price_reaction.py`, and `make_report.py`. They demonstrate the
  pilot but do not implement HLD M4–M9 versioning, jobs, canonical DB rows or outbox.
- **File-based source pilots:** `resolve_news.py` and `fetch_fundamentals.py`.
  Google News article capture and Screener snapshot DB integration do not exist.
- **Dashboard utility:** `scripts/make_dashboard.py` was untracked at this pass's
  start and was reviewed as executable worktree source. During verification, a separate
  concurrent task committed it and `data/dashboard.html` in `9ea8a64`; this pass did not
  edit, stage or commit either file.

Useful implemented properties include bounded 30/60-second direct exchange/Screener/
Google calls, browser impersonation where empirically required, PDF magic checks in both
filing downloaders, deterministic SHA-256 document grouping, explicit UTF-8 in newer
text/report paths, deterministic price observation hashes, append-only revised price-bar
keys, current live OHLC sanity, and HTML escaping for most report fields. The Screener
raw snapshot reparsed byte-for-byte to the tracked JSON structure, all 200 RSS item dates
parsed, the 100 cached HDFC redirects were structurally valid HTTP(S) URLs with no URL
credentials or literal private IPs, and the two tracked price files had no non-finite or
negative close/volume values.

#### Correctness, reliability and trust-boundary critique

- **Filing recovery is not durable-attempt semantics.** `pending_urls()` considers
  invalid HTML evidence permanently handled, despite the module docstring saying the
  recovery path covers temporarily invalid NSE URLs. A job whose every download fails
  returns a failed fetch run but `handle_filings_download()` still emits a completed
  event and succeeds the durable job, so job backoff/dead-letter never applies. Per-URL
  blocks are savepoints under the caller's outer transaction, not independently durable
  commits; no-byte failures have no immutable attempt rows. Downloads are unbounded,
  URLs/errors are printed, and NSE accepts any upstream HTTP(S) attachment host. The
  docstring references `tests/test_download_filings.py`, but no such tracked or untracked
  test exists.
- **The legacy fetch chain is fail-open and crash-unsafe.** It parses before writing,
  overwrites final paths non-atomically, has no retries/backoff or response-size limits,
  and generally exits zero after partial source failure. BSE capture reads only page 1;
  Google RSS writes any status/body as success; the PDF utility swallows all exceptions
  and leaves invalid/stale files outside its manifest. Native IDs are interpolated into
  filenames without a path-component validation boundary. None preserves immutable
  fetch attempts or exact-response lineage.
- **Google resolver failures are cached forever.** A transient failure is written under
  the token and excluded from all future `todo` lists, contradicting R-GN-8's retryable
  backoff/dead-letter contract. Regex XML parsing and protobuf URL extraction are
  unbounded and permissive; decoded/batchexecute URLs have no canonicalization,
  credential/private-address/length policy for their future article-fetch consumer.
  Cache writes occur only at the end, so a crash loses the whole batch. Logs expose full
  resolved URLs and remote error text, which may include tracking/signature material.
- **Screener output is not the specified snapshot contract.** HTTP is bounded but has no
  spacing/backoff/content canary or title/company check. A 200 interstitial/wrong-company
  page can become a successful zero-section result. Duplicate section and row labels
  silently overwrite in dictionaries, and displayed unit/consolidated/provenance fields
  are not represented as explicit typed data. Raw HTML is written before parsing (good
  for this pilot) but neither write is atomic/versioned; parser output is an untyped
  convenience JSON, not `fundamentals_snapshots`.
- **Yahoo lineage is weaker than its docstring.** `frame_to_rows()` parses the provider
  frame before artifact persistence, and the artifact is a pandas CSV reserialization,
  not exact provider transport bytes. `ObjectStore.put_bytes()` is called without the
  caller connection, so content registration commits separately from artifact/bar
  lineage. The history call supplies no explicit timeout/retry policy. Artifact keys
  collapse identical observations across fetch runs, and the daily run key prevents a
  second normal fetch that day. These may be defensible contracts, but are not “exact
  bytes first” or one fetch-event artifact per run as currently claimed.
- **Price validation has material edge gaps.** `validate_frame()` accepts NaN/Infinity
  OHLC values (a characterization call returned zero hard errors for NaN), does not
  reject duplicate trade dates, and permits today's still-forming daily bar before the
  15:30 IST close. Weekday-only gap inference is a heuristic, not an exchange calendar;
  a date missing from 80% of series can hide correlated provider loss. Symbol changes,
  split/dividend adjustment validation, benchmark coverage and provider revisions have
  no integration fixture. Live data is clean today, so this is a prospective corruption
  risk rather than evidence of bad stored bars.
- **The reaction prototype violates M8's central rule.** It computes mean/std and rival
  anomalies over the entire file, including observations after the reaction. In a
  synthetic characterization, appending one future bar changed an unchanged event's
  z-score from 1.69 to 0.66 and created a rival. Publication timestamps are reduced to
  their source timezone's calendar date rather than converted to IST/session cutoffs,
  and every same-day filing is forced to the next session. “Unexplained” rivals are not
  joined to events at all, yet output says no substantive filing matched and can print
  “causal framing permitted,” contrary to the association-only invariant.
- **Evidence and event guarantees are demonstrably absent from the prototypes.** The
  extractor truncates at 12,000 characters without the promised stronger-model rerun,
  has no explicit request timeout/checkpoint, uses an undated model alias and hard-coded
  pricing, and performs only a naive substring quote check. `rank_insights.py` ships
  every extraction regardless of `quote_verified`, treats each document hash as an
  event, bakes wall-clock freshness into an overwritten file, and trusts incomplete
  JSON shapes. The tracked HDFC flags cannot be reproduced by current extractor code:
  only 3/38 quotes match naively while 37 are marked true; whitespace normalization
  reproduces 37. Across both samples, 26/66 ranked rows are explicitly marked unverified.
  This is pilot evidence, not a safe shipping path.
- **Report escaping is incomplete.** Most summaries, titles and URLs use
  `html.escape`, but source `published_at` cells and local `file:///` href values are
  interpolated without escaping. There is no malformed-input fixture. The report labels
  per-document rows “ranked events,” thereby overstating event clustering. The dashboard
  was changing concurrently: its initial untracked version queried nonexistent
  `normalized_records.title_or_desc`; a later revision fixed that expression but the
  latest observed worktree SQL placed `WHERE` before subsequent `JOIN` clauses. The
  reviewed versions therefore were not executable as written. Its DB connection also
  does not explicitly request read-only mode, and generated views include test fixtures.

#### Dependency and sample-data assessment

All third-party import roots are supplied by the environment: direct runtime pins are
`curl-cffi 0.16.1`, `pdfplumber 0.11.10`, `openai 3.3.1`, `psycopg 3.2.13`, and
`yfinance 1.6.0`; test pin `pytest 9.1.1` and transitive `pandas 2.3.3` are installed.
`pip check` is clean. However, runtime and dev dependencies share one file, pandas is a
test/runtime API used directly but only arrives transitively, there is no Python-version
contract, transitive lock/hash/SBOM, or reviewed update policy, and network/parser/LLM
behavior depends on large unpinned transitive trees. Direct pins alone do not make an
installation reproducible or supply-chain verified.

Tracked `data/` is 94 files / about 5.1 MiB: 138 normalized announcement rows,
136 downloaded-manifest rows collapsing to 66 hashes, 66 extraction manifests/texts/LLM
outputs/ranked rows, two 100-item RSS snapshots, two fundamentals snapshots, two daily
price CSVs, rendered pilot reports, the two gap summaries, and one generated DB dashboard. Structural checks found no duplicate exchange-native
IDs or document hashes at their intended levels, malformed JSON/XML, impossible tracked
close/volume values, or secret-pattern hits. The 30-series gap report is structurally
consistent and reports one genuine gap-day, but is a generated live snapshot, not a
stable fixture.

The corpus is not portable/reproducible: all 136 PDF paths and all 132 text/PDF paths in
manifests point outside the repository and are missing on this host; HDFC paths use a
foreign Windows layout and Tata paths use absolute Unix layout. Tata Elxsi was removed
from the current company registry, so seven pilot scripts whose default is `tataelxsi`
now fail at import/startup and the original sample cannot be regenerated through those
defaults. The tracked flags reflect code not present in the current extractor, and one
Tata price CSV row is silently skipped by the prototype loader. Raw PDFs are intentionally
untracked, while extracted public filing text and full third-party HTML are tracked without
a compact provenance/fixture manifest. The tracked dashboard is a stale whole-database
snapshot (111 companies including persistent test identities versus the 30-company registry)
and therefore is not a production-only view. It is structurally valid HTML with no script,
`file:`-scheme or absolute-home links (newer concurrent output adds relative object-store
links), but its aggregate counts have already drifted. No
credential signature matched; nevertheless, public filings can contain personal contact
details and full source bodies, so continued tracking/redistribution is an explicit owner
policy decision rather than a secret scan's conclusion.

#### Test quality

`tests/test_fetch_prices.py` is useful and fully offline for normal hashes, frame mapping,
basic OHLC, timezone-aware date conversion, pre-close expected-session gating and the 80%
gap heuristic. It does not test NaN/Infinity, duplicate dates, pre-close frame rejection,
DST/foreign timezone inputs, revisions/current-bar ordering, symbol/adjustment changes,
artifact transaction boundaries, empty/malformed frames, DB idempotency or job failure.
There are no tests at all for the other eleven reviewed scripts. Missing priority
fixtures include malformed exchange/publisher/Screener responses; URL host/private-address
policy; redirects and cache retry; parser/decompression/page/byte limits; normalized quote
verification and retry/suppression; deterministic multi-mention ranking/clustering;
pre-reaction-only baselines and IST session cutoffs; zero-variance reactions; and complete
HTML attribute/text escaping. Existing DB test isolation (`ST-P02`) must be solved before
adding persistent integration coverage.

## 11. Finding ledger

### `ST-P01` — Persistent database still documented/operated as a resettable baseline

- **Severity / priority:** High / P0
- **Fact:** the DB holds substantial persistent capture/market/pipeline data and
  append-only fixtures, while schema/project instructions said no database existed
  or tables were empty and advised direct baseline edits/re-apply. No migration
  directory exists.
- **Risk:** a well-meaning schema change/reset can destroy data or create
  unrepeatable live/schema drift.
- **Disposition:** **partially fixed.** Safety text in `AGENTS.md`,
  `PROJECT_STATUS.md`, `docs/database-schema.md`, and `SERVER_CONTEXT.md` now marks
  data persistent and the baseline frozen. Forward migration tooling/versioning is
  still required before any DDL change.

### `ST-P02` — Integration tests contaminate and act on the application database

- **Severity / priority:** High / P0
- **Fact:** five test modules commit permanent append-only rows/blobs to the only
  configured DB; normalizer/resolver tests can process real pending records. Live
  aggregates show dominant test fixture populations.
- **Risk:** test runs alter research data, queue behavior, metrics, disk usage, and
  reproducibility; concurrent runs can interact through global work.
- **Disposition:** **open.** Full suite withheld. Build a dedicated guarded test DB
  and object root; do not delete existing rows without an owner-approved data
  classification/cleanup plan.

### `ST-P03` — Outbox has no dispatcher and all events remain pending

- **Severity / priority:** High / P0
- **Fact:** tracked code inserts outbox events but contains no claim/publish/retry
  dispatcher. All 2,330 live events are pending.
- **Risk:** downstream hand-offs and operational dead-job alerts described by the
  HLD do not occur; queue/event-lag observability is absent.
- **Disposition:** **open.** Specify event-to-job routing, implement SKIP-LOCKED
  dispatch with lease/retry/idempotency, supervise it, and test crash windows in an
  isolated DB before claiming the outbox gate complete.

### `ST-P04` — Final-attempt worker crash stranded a job

- **Severity / priority:** Critical / P0
- **Fact:** lease recovery always requeued a job whose already-counted attempt could
  equal `max_attempts`; the next claim increment then violates
  `attempts <= max_attempts`, leaving the row unclaimable.
- **Disposition:** **fixed.** `requeue_expired_leases()` now atomically dead-letters
  an expired final attempt, sets `finished_at`, clears ownership, and emits
  per-attempt `job.failed` plus terminal `job.dead`. A rollback-only regression test
  passes.

### `ST-P05` — Same-size object corruption was accepted as an idempotent put

- **Severity / priority:** Critical / P0
- **Fact:** an existing digest path was checked only for size. Different bytes of
  equal length were treated as healthy and could then be registered/returned as the
  expected digest.
- **Disposition:** **fixed.** Re-put now verifies the existing content hash before
  registration and refuses corruption without overwriting forensic evidence. The
  focused regression and all 17 object-store tests pass; live full-store verify is
  clean.

### `ST-P06` — Secret-bearing `.env` was group/world readable

- **Severity / priority:** Critical / P0
- **Fact:** `.env` mode was `0664`; project docs establish it contains database/API
  secrets. Values were not read or printed.
- **Disposition:** **fixed operationally.** Mode is now `0600`; durable host context
  was updated. `.env.example` remains non-secret and `0664`.

### `ST-P07` — Application connects as the PostgreSQL bootstrap superuser

- **Severity / priority:** High / P0 before unattended workers
- **Fact:** the sole non-system login role/current user is superuser and can create
  roles. Compose is loopback-only and PostgreSQL's public schema does not grant
  `CREATE` to PUBLIC, which reduce remote exposure but not worker blast radius.
- **Risk:** parser, URL, dependency, or handler compromise has unrestricted DB
  control and can bypass intended schema boundaries.
- **Disposition:** **open.** Add separate owner/migration and least-privilege runtime
  roles/grants after mapping required writes; rotate/update connection credentials
  without broadening network exposure.

### `ST-P08` — PostgreSQL and object storage have no backup/restore coverage

- **Severity / priority:** High / P0
- **Fact:** both persistent stores reside on the host/root-disk workflow and are not
  included in existing homelab backups; no restore test exists.
- **Risk:** disk loss, schema mistake, or cross-store inconsistency is unrecoverable.
- **Disposition:** **open.** Design a coordinated PostgreSQL + immutable-object
  snapshot, retention and restore drill. Account for the 371 MiB current object
  store and growing append-only fixtures; do not improvise destructive mirroring.

### `ST-P09` — Stale/failed control rows have no operational surface

- **Severity / priority:** Medium / P1
- **Fact:** one fetch run was still `running` after ~5.6 hours, 11 fetch runs were
  failed, four cursors had consecutive failures, 67 jobs were dead, and no tracked
  dashboard/alert consumer exists.
- **Risk:** source silence or crashed runs can remain invisible to the single owner.
- **Disposition:** **open.** Define stale-run reconciliation and aggregate health
  output, then alert on stale fetches, cursor failures, dead jobs, runnable age,
  outbox age, and object reconciliation.

### `ST-P10` — Identity assignment does not enforce a resolved outcome in DDL

- **Severity / priority:** Medium / P1
- **Fact:** composite FKs enforce record/company agreement but a `rejected`
  resolution may legally carry a company and be selected. Live count is zero.
- **Risk:** future/manual code can publish a non-accepted identity decision while
  satisfying current constraints.
- **Disposition:** **open.** Add a forward migration/constraint design (not a direct
  baseline edit) and an isolated schema regression test.

### `ST-P11` — Seed idempotency can silently accept conflicting master data

- **Severity / priority:** Medium / P1
- **Fact:** seeding is one transaction and repeat-safe for the happy path, but
  existing ISIN/listing/name rows are accepted without relationship/config
  validation and canonical names are not DB-unique case-insensitively.
- **Risk:** drift or concurrent seeds can associate registry entities incorrectly
  while reporting “exists.”
- **Disposition:** **open.** Make existing-row checks assert all immutable
  relationships/config, add an advisory lock or DB natural-key constraint, and test
  mismatch failures in an isolated DB.

### `ST-P12` — Handler/outbox transaction boundary is weaker than the HLD wording

- **Severity / priority:** Medium / P1
- **Fact:** domain handlers generally commit domain rows/events in their own
  connection, then `Worker` commits job completion and completion events later.
- **Risk:** crash recovery is safe only where handler idempotency is complete; a new
  handler can introduce a dual-write omission despite the worker API's guarantee.
- **Disposition:** **open/design decision.** Either require handlers to execute
  canonical work + domain outbox + job completion on one connection, or document
  the current two-transaction convergence contract and enforce idempotency with
  contract tests.

### `ST-P13` — Retry/heartbeat observability and jitter are incomplete

- **Severity / priority:** Medium / P1
- **Fact:** heartbeat DB failures are silently ignored and retry delay is
  deterministic exponential backoff without jitter. No heartbeat-loss metric exists.
- **Risk:** correlated retry load and unnoticed lease loss can produce concurrent
  at-least-once executions.
- **Disposition:** **open.** Add structured aggregate logs/metrics and bounded jitter;
  preserve at-least-once semantics and never log secret payloads/URLs.

### `ST-P14` — Dependency/configuration reproducibility is partial

- **Severity / priority:** Medium / P2
- **Fact:** direct Python versions and PostgreSQL image digest are pinned, but
  transitive Python dependencies are not locked/hashed. Compose accepts an unset /
  empty password variable until image startup rather than failing during config
  interpolation.
- **Disposition:** **open.** Add a reviewed lock/hash workflow and fail-fast required
  variable syntax after testing compose behavior; do not commit generated secrets.

### `ST-P15` — Documentation status was materially stale

- **Severity / priority:** Medium / P1
- **Fact:** `ROADMAP.md` said object store, jobs/outbox and DB-writing fetchers were not
  started while other status prose claimed completion; Yahoo, persistent-state, module,
  cadence, phase, counts and next-action statements conflicted.
- **Disposition:** **fixed 2026-08-23.** `AGENTS.md`, `PROJECT_STATUS.md`, ROUTER/ROADMAP,
  the human hub, all module L1/L2 docs, architecture/schema explainers, product note, and
  historical pilot/build/source records were reconciled from the fact table in §7.1.
  Dated evidence counts remain append-only and are explicitly non-evergreen.

### `ST-C01` — Capture parses before exact artifact registration is durable

- **Severity / priority:** High / P0
- **Fact:** NSE parses JSON before `_persist_bytes`; BSE parses/collects all pages
  before persisting them. With psycopg autocommit disabled, preceding reads also make
  many advertised short `conn.transaction()` blocks savepoints inside one outer
  transaction rather than independent commits.
- **Risk:** parser failure or process death can leave no registered replay artifact;
  API/item/cursor and PDF crash boundaries differ from comments and tests.
- **Disposition:** **open.** Persist and commit each fetched response's object and
  fetch-event artifact before invoking a parser; then commit parsed items + cursor in
  a separate transaction. Add real kill-point tests in an isolated DB/object root.

### `ST-C02` — BSE pagination accepted an envelope without a trustworthy denominator

- **Severity / priority:** High / P0
- **Fact:** `parse_envelope` treated missing `Table1/ROWCNT` as valid, skipped rows
  missing `NEWSID`, and `_collect_pages` accepted a changing denominator. This
  contradicts R-BSE-2/7/8 and can silently truncate a successful run.
- **Disposition:** **fixed.** BSE now requires a non-negative `ROWCNT`, rejects
  `ROWCNT < len(Table)`, rejects every row without `NEWSID`, and fails if `ROWCNT`
  changes across pages. Five pagination and 12 helper tests pass offline. The unsafe
  DB-backed class was intentionally not run.

### `ST-C03` — Multi-page BSE rows can carry the wrong artifact/run lineage

- **Severity / priority:** High / P0
- **Fact:** all collected rows use page 1's `artifact_id`, while
  `first_seen_run_id` is taken from the final page artifact insertion. Reused page 1
  plus changed later pages can violate the composite FK; otherwise page-2 rows still
  point to bytes that do not contain them.
- **Disposition:** **open.** Preserve `(row, page artifact, artifact first run)`
  during collection and regression-test mixed reused/new pages.

### `ST-C04` — Attachment failure/convergence can strand canonical content

- **Severity / priority:** High / P0
- **Fact:** BSE considers retained `filing_pdf_invalid` evidence permanently handled;
  NSE retries it. Neither adapter records no-byte failures in
  `content_fetch_attempts`; no live canonical record has a `record_contents` link.
- **Risk:** a temporary HTML block can permanently suppress a later valid BSE PDF,
  and downstream M4 has no canonical record→content edge or attempt history.
- **Disposition:** **open.** Separate “attempt observed” from “valid content linked”;
  retry invalid content with a bounded schedule, create immutable attempt rows after
  normalization, and idempotently link valid content to every relevant record.

### `ST-C05` — Adapter validation still permits silent malformed/foreign outcomes

- **Severity / priority:** High / P0
- **Fact:** NSE skips non-object rows, permits missing IDs and does not enforce the
  partition symbol. BSE can accept an all-foreign non-empty response because its
  empty canary runs before scrip filtering. Download size is unbounded and NSE trusts
  any HTTP(S) attachment host from the payload.
- **Disposition:** **open.** Validate mandatory native IDs and partition identity
  before cursor success, run BSE's canary after target filtering, allowlist attachment
  hosts, cap bytes, and retain rejected responses as evidence.

### `ST-C06` — Capture/stage idempotency and correction semantics are incomplete

- **Severity / priority:** High / P0
- **Fact:** API artifact keys collapse identical network fetches across runs;
  same-day fetch keys suppress the documented six-hour cadence; raw-item uniqueness
  prevents a changed same-partition native ID reaching M2's advertised `@v2` path.
  M2 also embeds native IDs in an unescaped regex revision query.
- **Disposition:** **open/design required.** Keep one artifact event per fetch while
  deduping bytes only in `content_objects`; use scheduled-window/run keys with overlap;
  define source-correction identity/version semantics and replace regex lookup with an
  explicit base/revision model in a forward migration.

### `ST-C07` — M2/M3 concurrent workers can share audit rows or decisions

- **Severity / priority:** High / P0 before parallel workers
- **Fact:** pending snapshots and rechecks do not lock entities. Stage-run upsert lets
  workers overwrite one row; identity has no unique initial-decision guard, so two
  workers may append competing roots and race the assignment/cache pointer.
- **Disposition:** **open.** Claim records with `FOR UPDATE SKIP LOCKED` or advisory
  locks, make stage-run keys invocation-specific, constrain one unsuperseded decision,
  and add parallel isolated-DB tests.

### `ST-C08` — Identity's documented cross-exchange anchor is not implemented

- **Severity / priority:** Medium / P1
- **Fact:** tier B checks a symbol on the opposite venue, while seeded BSE listings
  have no `exchange_symbol`; the HLD instead specifies a byte/record anchor. Current
  production records resolve through scrip code, ISIN or same-venue symbol and all
  258 reconciled assignments are internally consistent.
- **Disposition:** **open.** Implement an evidence-backed record/content anchor or
  remove/rename the dead tier after owner/design alignment; do not lower fail-closed
  behavior.

### `ST-C09` — Manual identity correction bypasses two stated guarantees

- **Severity / priority:** Medium / P1
- **Fact:** manual assignment checks company existence but not active whitelist and
  emits no identity-correction outbox event. Automatic resolution does both gating
  and event emission.
- **Disposition:** **open/owner decision.** Decide whether manual assignment may
  intentionally expand beyond the whitelist; enforce and document the choice, and
  emit an idempotent versioned correction event atomically with pointer/cache update.

### `ST-C10` — M1–M3 tests are not isolated and omit critical crash/adversarial cases

- **Severity / priority:** High / P0
- **Fact:** DB tests commit permanent fixtures and process global pending/review work;
  a synthetic fixture exists in a real company partition. Attachment “crash” tests
  return HTTP failures rather than killing a process. No test covers exact-byte
  ordering, mixed BSE page lineage, concurrent M2/M3, all-foreign responses, hard
  kill points, size limits or host restrictions.
- **Disposition:** **open; overlaps `ST-P02`.** Guard a disposable test DB/object root
  before expanding integration tests. Keep pure/offline tests for envelope, mapping
  and ladder behavior; reserve persistent soak fixtures for an explicit namespace.

### `ST-D01` — Pilot reaction output uses future data and overstates attribution

- **Severity / priority:** High / P0 before any reaction is published
- **Fact:** `price_reaction.py` computes return/volume baselines and rival anomalies
  across the full CSV, including post-reaction sessions; a future synthetic bar changed
  an existing event z-score from 1.69 to 0.66. It does no event join for purportedly
  “unexplained” dates and prints that causal framing may be permitted.
- **Risk:** unstable look-ahead scores and unsupported causal copy can turn retrospective
  knowledge into a falsely strong market explanation.
- **Disposition:** **open; prototype must not ship.** Implement HLD M8 against exact
  pre-reaction bar memberships, IST exchange cutoffs/calendar, benchmarks and real
  event-exclusion evidence; association wording remains mandatory even when gates pass.

### `ST-D02` — Pilot evidence/ranking path ships unverified and non-reproducible quotes

- **Severity / priority:** High / P0 before Phase 1 shipping
- **Fact:** current extraction uses naive substring verification, does not retry/suppress,
  and ranking ignores the flag. The tracked HDFC `quote_verified` values require
  normalization absent from current code; 26/66 tracked ranked rows are marked unverified.
  Each content hash is also mislabeled as a business event.
- **Risk:** a fluent model omission/fabrication can be displayed as evidence, while
  duplicate documents are presented as independently ranked events.
- **Disposition:** **open; pilot-only.** Build versioned M5–M7 DB stages with normalized
  containment/location, retry then suppression, multi-mention event fixtures and
  deterministic rulepack tests. Do not patch the throwaway JSON chain into production.

### `ST-D03` — Filing recovery can permanently accept invalid content or silently succeed

- **Severity / priority:** High / P0
- **Fact:** `download_filings.py` excludes `filing_pdf_invalid` URLs from future work and
  reports its durable job complete even when all downloads fail. No-byte attempts are
  absent, valid contents are not linked to canonical records, downloads/hosts are
  unbounded, and its claimed regression test module does not exist.
- **Risk:** a temporary HTML block or transient network outage can leave filing evidence
  unavailable without durable retry/dead-letter signaling.
- **Disposition:** **open; extends `ST-C04`/`ST-C05`.** Model immutable attempts separately
  from valid-content completion, retry invalid/no-byte outcomes under bounded policy,
  enforce host/size controls, link content M:N, and fail the job when required work remains.

### `ST-D04` — Daily-bar acceptance and provenance are not production-safe

- **Severity / priority:** High / P0 before market reactions
- **Fact:** the Yahoo adapter accepts NaN/Infinity OHLC and still-forming pre-close daily
  frames, parses before persistence, and stores a pandas CSV serialization rather than
  exact transport. It has no explicit history timeout, authoritative holiday calendar,
  revision integration test or benchmark path. Live read-only checks found none of the
  invalid values and no revisions, so no current corruption is alleged.
- **Risk:** malformed/partial/provider-revised bars can become the current observation and
  silently change later calculations; a provider-wide gap may be mislabeled a holiday.
- **Disposition:** **open.** Add finite/duplicate/session-close checks and offline fixtures
  now; define transport/frame provenance explicitly, then prove append-only revisions,
  cursor and artifact crash behavior in the guarded test environment.

### `ST-D05` — Remaining source utilities lack bounded, retryable trust boundaries

- **Severity / priority:** Medium / P1 (High if promoted to unattended service)
- **Fact:** legacy exchange/news/PDF scripts overwrite outputs and can exit success after
  partial failure; BSE fetch is page-1 only. Resolver failures are cached permanently and
  future publisher URLs have no consumer trust policy. Screener lacks title/content/unit
  validation and silently overwrites duplicate labels. None caps response/parser growth.
- **Disposition:** **open.** Keep these explicitly pilot-only. Reuse empirical mechanics in
  new DB adapters with atomic exact artifacts, bounded requests/decompression/parsing,
  retryable state, URL allowlists/private-address denial, validation and visible failures.

### `ST-D06` — Tracked pilot products are stale, non-portable and weakly provenance-bound

- **Severity / priority:** Medium / P1
- **Fact:** every manifest PDF/text path is absent and absolute/foreign; Tata Elxsi is no
  longer in the registry although seven scripts default to it; output flags cannot be
  reproduced by current code; 26 ranked rows carry unverified quotes; generated reports
  call document rows events. The tracked dashboard includes test identities and stale live
  aggregates. Structural relationships and tracked numeric ranges otherwise passed, and no
  credential pattern matched.
- **Risk:** samples look executable/authoritative but cannot serve as deterministic fixtures;
  full public source bodies may also retain personal contact data without a tracking policy.
- **Disposition:** **owner decision required.** Choose between portable minimized fixtures
  with provenance/version manifests, clearly archival pilot evidence, or removal from Git
  history via a separately approved policy. Do not silently regenerate/refetch in this audit.

### `ST-D07` — Dependency reproducibility and runtime contract are incomplete

- **Severity / priority:** Medium / P2
- **Fact:** direct packages are pinned and `pip check` passes, but pandas is imported
  directly through a transitive dependency; runtime/dev packages are mixed; no supported
  Python declaration, transitive lock/hash/SBOM or update policy exists. LLM and parser
  behavior depends on unpinned transitives and an undated model alias.
- **Disposition:** **open; extends `ST-P14`.** Declare direct imports and supported Python,
  separate dev tooling, and adopt a reviewed hashed lock/update process with regression
  fixtures before upgrades. Record immutable model revision/prompt/schema versions per run.

### `ST-D08` — Report/dashboard output boundaries are incomplete

- **Severity / priority:** Medium / P1
- **Fact:** `make_report.py` leaves publication text and local-path href attributes
  unescaped and has no adversarial fixture. Dashboard source/output changed concurrently
  during this pass. Reviewed versions escaped displayed text but had fatal SQL drift
  (initially a nonexistent `title_or_desc` column; latest observed version put `WHERE`
  before later `JOIN`s), included test fixtures, and did not explicitly request a read-only
  DB transaction.
- **Disposition:** **open.** Escape every text/attribute context, remove local absolute
  paths from portable products, add malformed-input tests, and stabilize/test or discard
  the dashboard after owner alignment; this review did not alter the dashboard files.

### `ST-D09` — Source/dependency documentation overstated or understated status

- **Severity / priority:** Medium / P1
- **Fact:** Yahoo L1/L2 said no fetcher existed and used `yahoo` while live code/data use
  `yfinance`; project/product prose said prices or the database did not exist; Screener
  mixed superseded policy with status; pilot scripts could be mistaken for HLD completion.
- **Disposition:** **fixed 2026-08-23.** Module and root docs now distinguish requirement,
  DB-integrated, pilot-only, and gated status; use `yfinance` for canonical source/series;
  preserve the owner policy supersession; and label M4–M10/pilot products non-shipping.
  Filing-recovery defects remain open code findings (`ST-D03`), not documentation claims.

## 12. Verification record

Commands were run from the repository root without displaying secrets:

- `docker compose ps` — PostgreSQL container up/healthy, loopback port only.
- runtime version commands — versions recorded in §5.
- two psycopg audit scripts using `BEGIN TRANSACTION READ ONLY` — both reported
  `transaction_read_only=on`.
- pre/post all-table count comparison — `{}` delta across all 44 tables.
- `ObjectStore.verify(conn=read_only_conn)` before and after — 0 problems.
- `venv/bin/python -m unittest tests.test_object_store -v` — 17/17 passed; DB
  integration transactions rolled back.
- `venv/bin/python -m unittest tests.test_jobs.TestCrashRecovery.test_expired_final_attempt_is_dead_lettered_without_persistent_fixture -v`
  — 1/1 passed; fixture rolled back.
- `venv/bin/python -m py_compile scripts/object_store.py scripts/jobs.py tests/test_object_store.py tests/test_jobs.py`
  — passed.
- M1–M3 psycopg reconciliation script with `BEGIN TRANSACTION READ ONLY` /
  `SHOW transaction_read_only` — `on`; registry, capture, artifact, raw-item,
  normalized-record, assignment, review, cursor, whitelist and content-link counts
  recorded in §8.4; transaction rolled back.
- `venv/bin/python -m unittest tests.test_nse_capture.TestPureHelpers tests.test_bse_capture.TestPureHelpers tests.test_bse_capture.TestCollectPages tests.test_normalize.TestNormalizeItem tests.test_normalize.TestFingerprintAndKeys tests.test_resolve_identity.TestNormalizeName tests.test_resolve_identity.TestResolutionLadder -v`
  — 55/55 offline M1–M3 tests passed (no DB or network use).
- `venv/bin/python -m py_compile scripts/companies.py scripts/nse_capture.py scripts/bse_capture.py scripts/normalize.py scripts/resolve_identity.py tests/test_nse_capture.py tests/test_bse_capture.py tests/test_normalize.py tests/test_resolve_identity.py`
  — passed.
- `venv/bin/python -m pytest tests/test_fetch_prices.py -q` — all 16 offline price
  helper tests passed; no DB/network use.
- scoped `py_compile` over the eleven reviewed tracked scripts,
  `tests/test_fetch_prices.py`, and `make_dashboard.py` (untracked at command start,
  subsequently committed by a concurrent task) — passed.
- read-only market-data SQL (`BEGIN TRANSACTION READ ONLY`, confirmed `on`) — 30
  active series/partitions/cursors, 7,529 current bars (250–251 per series), 30
  price artifacts, no revisions, missing content registrations, NaN, weekend/future,
  invalid OHLC, missing fetch lineage, duplicate logical series, or cursor failures;
  date span 2025-08-21..2026-08-21; rolled back.
- tracked-data structural audit — all JSON/XML parsed; relationship/ID/hash/count,
  path portability, quote-gate, price and generated-report aggregates are recorded in
  §10.4 without emitting article/document bodies.
- Screener offline replay — tracked raw HDFC HTML reparsed identically to the tracked
  fundamentals JSON. Google resolver cache audit — 100/100 entries were structurally
  valid HTTP(S), with no URL credentials, literal private IPs or non-HTTP schemes.
- synthetic characterization (no files/DB) — Yahoo validation accepted NaN (zero hard
  errors); adding one future bar changed the reaction prototype's same-event z-score
  1.69→0.66 and rival count 0→1.
- `venv/bin/python -m pip check` — no broken requirements; installed direct versions
  matched `requirements.txt`; direct/transitive import comparison is in §10.4.
- secret-safe tracked-path scanner (locations/pattern names only, excluding `tmp/pi-*`)
  — two credentialed-URL pattern matches, manually confirmed as placeholders in
  `.env.example` and `seed_core.py`; no credential value match. No scan artifact written.
- documentation stale-claim searches — every match for “not started,” “no database,” “no
  fetcher,” old capture totals, six-hour cadence and superseded policy was adjudicated;
  historical evidence was labelled/appended rather than blindly replaced.
- non-writing project-document checker over 36 tracked root/`docs`/`.claude` Markdown/HTML
  files — YAML front matter closed/basic-key-valid, HTML parsed, and all 88 local relative
  links resolved. Raw captured third-party HTML under `data/` was intentionally excluded
  because its root-relative links target the source websites, not repository files.
- `git diff --check -- . ':(exclude)tmp/pi-*'` — passed after documentation reconciliation.

The full suite was **not run by design** due `ST-P02`; this is a safety result, not
an unverified claim that the full suite passes.

## 13. Prioritized recommendations and owner decisions

### P0 before unattended operation / more schema work

1. Establish coordinated DB + object-store backups and perform a non-destructive
   restore drill (`ST-P08`).
2. Freeze the baseline and choose a forward migration tool/process (`ST-P01`).
3. Isolate integration tests behind a hard test-database marker and separate object
   root (`ST-P02`). Decide whether existing fixture rows are retained as audit
   evidence, migrated to a test DB, or cleaned only after an approved backup.
4. Correct exact-bytes/commit ordering and per-page BSE provenance before relying
   on crash recovery (`ST-C01`, `ST-C03`).
5. Close attachment retry/linkage and malformed/foreign fail-open paths
   (`ST-C04`, `ST-C05`).
6. Define correction/run idempotency and make M2/M3 worker claims concurrency-safe
   (`ST-C06`, `ST-C07`).
7. Fix filing-recovery success/retry semantics and the valid record→content/attempt
   lifecycle; bound and validate URL/content retrieval (`ST-D03`, `ST-C04`, `ST-C05`).
8. Keep pilot extraction/reaction output non-shipping until verified evidence,
   event-level clustering and pre-reaction-only market fixtures pass (`ST-D01`,
   `ST-D02`, `ST-D04`).
9. Specify and implement the outbox dispatcher and alert path (`ST-P03`).
10. Introduce least-privilege runtime DB roles (`ST-P07`).

### P1 hardening

11. Add stale run/job/cursor/outbox/object health reporting (`ST-P09`, `ST-P13`).
12. Close the identity outcome constraint and seed mismatch gaps through migrations
    and isolated tests (`ST-P10`, `ST-P11`).
13. Resolve the dead cross-exchange tier and manual-correction policy/event gap
    (`ST-C08`, `ST-C09`).
14. Decide and document the canonical handler transaction contract (`ST-P12`).
15. Replace or explicitly archive legacy source utilities; add bounded parser/URL,
    escaping and malformed-response fixtures (`ST-D05`, `ST-D08`).
16. Choose a portable, provenance-bearing tracked sample policy (`ST-D06`).
17. Adopt the dependency/runtime lock and update policy (`ST-D07`, `ST-P14`).

Documentation/status reconciliation (`ST-P15`, `ST-D09`) completed in this review; keep it
current in the same change as future milestones.

### Owner alignment required

- Is this database now the durable application environment, or an intentionally
  disposable development environment? Current data volume and owner instructions
  require treating it as durable until explicitly decided otherwise.
- Should append-only integration fixtures remain permanently mixed with application
  research data? If not, what retention/migration policy is acceptable?
- Which backup target and retention are acceptable for PostgreSQL plus
  `data/objects/`, given root-disk constraints?
- Which migration tool and runtime-role split does the owner prefer?
- What events should the first outbox dispatcher route to jobs versus notifications,
  and what supervision mechanism should run scheduler/worker/dispatcher processes?
- Is NSE/BSE API capture intended every six hours, once daily, or only as a daily
  reconciliation denominator? The current day key enforces once daily.
- May a manual identity assignment target an existing non-whitelisted company, or
  must the automatic whitelist invariant also bind operator corrections?
- Should invalid BSE bodies retry until a valid PDF is obtained (recommended), and
  what retry/age budget is acceptable before an operator alert?
- Are `data/<slug>` products archival pilot evidence, maintained portable fixtures, or
  generated artifacts that should not be tracked? Is tracking full extracted public
  filings/Screener HTML (which may contain personal contact details) acceptable?
- Should Tata Elxsi remain a supported non-whitelist pilot registry entry, or should all
  pilot defaults/fixtures move to a current registry company?
- For Yahoo, is a deterministic returned-frame serialization accepted as the canonical
  provider artifact, or is transport interception required before reactions are trusted?
  Which authoritative NSE calendar and benchmark series should M8 use?
- Should `scripts/make_dashboard.py` be completed and tracked, or discarded as
  pre-existing scratch work?

## 14. Known limitations

- No full suite, network source call, worker drain, schema replay, restore, or
  destructive immutability test was run against the application DB.
- Legacy file-based/source and later-pipeline algorithms were statically reviewed, but
  were not rerun against remote sources and remain prototypes rather than endorsed paths.
- No live source call or hard-kill capture experiment was allowed, so source behavior
  is assessed from recorded docs/tests and current metadata, not independently
  reproduced network evidence.
- Aggregate pattern queries cannot prove every test-looking row's provenance.
- The review verified the live catalog/invariants but did not produce a full schema
  dump comparison because host `psql` is absent and a dump could expose metadata;
  migration/schema-drift tooling remains needed.
- Object-store verification proves current file/hash/registry agreement, not backup
  recoverability or underlying disk health.
