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
