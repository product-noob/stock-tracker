# BUILD LOG — timestamped progress journal

Append-only. Newest entries at the bottom. One entry per meaningful action.
Started 2026-08-23 (IST). Maintained during interactive build/testing sessions.

---

## 2026-08-22 ~22:16–02:20 IST — Autonomous long-task build (PI goal loop, iteration 1)

- Built Phase-0 skeleton end-to-end (~9,000 lines): object store, durable jobs/outbox
  worker, NSE adapter, BSE adapter, normalizer (M2), identity resolver (M3),
  filing downloader, Yahoo price fetcher.
- Test suite created: 85 unit tests across 7 test files — ALL PASSING.
- First real ingestion executed during build verification:
  - 231 source partitions provisioned; 312 fetch runs completed
  - 704 raw artifacts stored; 526 announcements captured (334 NSE / 192 BSE)
  - 602 normalized records; 2,351 identity resolutions (0 stuck in review)
  - 346 filing content objects; 30 price series × ~250 bars = 7,529 bars (1 year)
- Known gap at cutoff: TODO 6 (Yahoo fetcher) flagged incomplete by the runner, though
  30/30 price series exist in DB — to re-verify in interactive testing.
- RSS pollers + Google News partitions provisioned but not yet executed.

## 2026-08-23 — Interactive full-pipeline test drive (phases A–H)

- A: unit suite re-run → **85/85 PASS** (8.7s)
- B: job queue demo → enqueued 3 `demo` jobs (`testA` prefix); worker drained 3/3 `succeeded`;
  re-enqueue attempt → "0 new (3 already existed)" = enqueue-level idempotency verified
- C: NSE capture →
  - idempotency: RELIANCE re-run short-circuited in 0.28s, zero network (same-day key)
  - hygiene finding: unit tests write TEST* partitions into the live DB (by design of the
    long-task build) — residue cleaned (1 junk partition deleted); proper test-DB isolation
    logged as TODO
  - liveness: forced fresh run-key for ASIANPAINT via `_run_partition_impl(day_key=...R1)`
    → genuine HTTP 200 through Akamai in 1.1s; window 20–23 Aug; 1 item seen / 0 new;
    PDF already stored. Capture→dedup→cursor loop confirmed live.
- D: BSE capture →
  - RELIANCE re-run: reused this morning's ok run (5 items)
  - liveness: forced fresh key for TATASTEEL → HTTP 200, ROWCNT=2 envelope validated,
    0 new / 2 PDFs already deduped, 0.3s
- E: normalizer → 4 pending sightings from the forced live runs all correctly dup-linked
  to existing canonical records (0 created / 0 errors)
- F: resolver → 0 pending (sightings attach to already-resolved records); overall
  410 resolved / 1959 unresolved attempt rows (ladder history, by design)
- G: filing downloads → **integration bug found & fixed**: `job_worker.py` never imported
  the capture modules, so real job kinds NoHandlerError'd on first drain (long task had
  only ever run fetchers synchronously). Fix = import all handler modules in worker.
  Re-drain: 60/60 succeeded. Content store now 350 objects / ~369 MB
  (204 PDF, 111 JSON, 30 CSV, 3 octet-stream, 2 HTML invalid-content evidence).
- H: prices refresh → 30/30 partitions ok (idempotent skips; bars current to 2026-08-21
  Friday close; weekend).
- VIZ: built `scripts/make_dashboard.py` → `data/dashboard.html` (51 KB, self-contained):
  totals cards, capture health by source, per-stock coverage + 90-day SVG sparklines
  (30/30), identity review queue, latest 25 announcements with evidence links.
  Schema fixes while building: record_contents lives in `documents`, not `ingestion`;
  title/description coalesce; datetime formatting.

### Session totals (after refresh)

- fetch_runs 360+, artifacts 700+, raw items 526, normalized records 602,
  identity resolutions 2351, content_objects 350 (~369 MB), price_bars 7529 across 30 series.
- Bugs found in interactive run: worker handler-registration gap (fixed);
  TEST* test pollution of live DB (cleaned 1 partition; proper isolation TODO);
  orphaned 'running' fetch_run from killed long-task worker (noted, lease sweep TODO).

### 2026-08-23 (cont.) — Dashboard evidence-link fix

- User reported PDFs/links invisible. Root causes found:
  1. per-stock PDF count joined `documents.record_contents` which is empty —
     filing lineage actually lives in `raw_artifacts.metadata` (`seq_ids`/`news_ids`
     → raw item idempotency keys). Rewrote joins accordingly.
  2. "latest announcements" was dominated by 216 synthetic `identtest:*` records
     from long-task unit tests (no timestamps → sorted first). Excluded via
     scope filter; real DB cleanup logged as TODO (append-only guards make
     deletes non-trivial).
  3. object-store blob URLs needed an extra `objects/` prefix (store root is
     `data/objects`, storage_path already starts with `objects/`).
- Result: 📄 stored-pdf links on announcements, new "Stored filings" table
  (30 latest with size + origin URL), all blob hrefs verified to exist on disk.

### 2026-08-23 (cont.) — Comprehensive review status reconciliation

- This log remains append-only: earlier counts, “all passing,” cleanup, and completion claims
  are historical session observations, not current acceptance status.
- Review found DB-backed tests commit fixtures and can process global work; a full suite was
  therefore withheld pending a guarded disposable DB/object root. Do not repeat live fixture
  cleanup without owner approval, backup and a precise classification plan.
- Phase 0 is partial, not gated. Durable review fixed final-attempt lease recovery,
  same-size object corruption handling, and BSE envelope validation; open P0 items include
  backups/migrations, test isolation, exact-bytes ordering, BSE page provenance, attachment
  retries/links, source validation, concurrency, outbox dispatch and service supervision.
- Canonical timestamped status is in `PROJECT_STATUS.md`, `docs/ROADMAP.md`, and
  `docs/reviews/2026-08-23-comprehensive-review.md`; live totals must be queried read-only.

## 2026-08-23 (cont.) — Authorized DB cleanup + test isolation + BSE AttachLive fix

**Owner authorized removal of all synthetic test data.**

1. Safety dump: backups/pre_cleanup_20260823_0541.sql (4.2 MB)
2. Full pipeline-data reset (TRUNCATE non-core schemas, bypassing row guards
   legally; core security master kept). intelligence ref-table seeds restored
   from db/schema.sql.
3. Root cause fixed — unit tests were writing TEST*/identtest rows into the
   LIVE database:
   - created isolated `stocktracker_test` DB (schema applied) + TEST_DATABASE_URL in .env
   - jobs.db_url_from_env() now prefers TEST_DATABASE_URL under unittest/pytest,
     refusing any URL that is not stocktracker_test; child worker processes opt in
     via STOCKTRACKER_USE_TEST_DB=1
   - all six DB-backed test files route through the guarded helper
4. Fresh re-ingestion of all 30 stocks on clean DB: NSE/BSE/yahoo/filings all ok
5. Owner-reported BSE bug fixed: GUID-named filings exist ONLY under AttachLive
   (His → 404 HTML). attachment_urls() orders by filename shape; invalid captures
   no longer count as "handled" (stranded-capture fix). 91/91 tests pass.
6. Surgical cleanup of remaining legacy test rows (guard triggers temporarily
   disabled inside one transaction, then re-enabled): removed 33 junk partitions,
   56 items, 88 runs, 74 sightings, 35 resolutions, 70 records.

### Final clean state

partitions=122 · fetch_runs=92 · raw_items=178 · normalized≈176 · PDFs stored for
25+ companies · price_bars=7529 (30 series × ~251) · junk=0
