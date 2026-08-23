---
tier: L1
role: test-safety-contract
updated: 2026-08-23
---

# Testing safety contract

Database-backed tests are destructive to their target in the operational sense: several
append immutable fixtures and worker subprocesses commit queue/outbox transitions. They must
never run against the application database or `data/objects/`.

## Required contract

All DB-backed test entry points use `scripts/test_environment.py`. The guard requires:

1. `TEST_DATABASE_URL` injected in the process environment (it is intentionally **not**
   loaded from `.env` and a database-name substring is not evidence);
2. the connected target contains exactly one marker row with identity
   `stocktracker-disposable-test-environment`, contract version `1`, and the connected
   database's current name and OID;
3. the target's PostgreSQL system identifier + database OID differs from the configured
   application target in `DATABASE_URL`; aliases therefore cannot bypass the comparison;
4. `TEST_OBJECT_ROOT` is an absolute, existing, resolved directory outside
   `data/objects/`, with the exact regular-file marker
   `.stocktracker-test-object-root` containing:

   ```text
   stocktracker-disposable-test-environment
   version=1
   ```

5. subprocesses receive `STOCKTRACKER_TEST_MODE=1`, `TEST_DATABASE_URL`, and
   `TEST_OBJECT_ROOT` through the harness. URL-less `jobs.connect()` and
   `ObjectStore.from_env()` then re-run the guard. Importing unittest/pytest alone never
   changes runtime routing.

Missing, partial, malformed, ambiguous, aliased-to-application, stale-marker, and
symlink-escaped configurations stop before a writable connection or application-root write.
Error messages do not include connection strings.

## One-time provisioning

Provision a **new dedicated database**, not a clone that will be confused with disposable
state and never the existing application database. Keep credentials in private environment
configuration; do not place them in shell history, docs, or Git.

For that new empty target only:

```bash
psql "$TEST_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/schema.sql
psql "$TEST_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/test_environment_marker.sql
```

`db/schema.sql` remains the frozen application baseline; the test-only marker is separate and
must never be added to it or applied to the application database. Re-provisioning/replacing a
disposable database is an explicit owner operation, not something the test harness performs.
The harness never creates or auto-marks a database based on its name.

Create a private disposable object directory outside the repository's production tree and
write its marker (example uses a fresh temporary directory):

```bash
export TEST_OBJECT_ROOT="$(mktemp -d /tmp/stocktracker-test-objects.XXXXXX)"
printf 'stocktracker-disposable-test-environment\nversion=1\n' > "$TEST_OBJECT_ROOT/.stocktracker-test-object-root"
chmod 700 "$TEST_OBJECT_ROOT"
```

Then privately inject the dedicated URL and run focused tests:

```bash
export TEST_DATABASE_URL='<dedicated target supplied privately>'
export STOCKTRACKER_TEST_MODE=1
venv/bin/python -m unittest tests.test_test_environment -v
```

Only run DB-backed modules (or full discovery) after the positive guard succeeds. Test rows
and blobs belong exclusively to these disposable targets. Do not clean or reset the existing
application database/object store to compensate for a mistaken invocation; stop and assess
first.

## Verified full-suite result

On 2026-08-23 the marked disposable database and separate `/tmp` object root ran the full
suite successfully: **109 tests passed**. SHA-256 inventories of every production object and
read-only row counts for all 44 application tables were captured immediately before and
after; both were identical. This verifies the current routing boundary, not permission to
omit the guard on future runs. Re-run the same before/after proof whenever connection,
subprocess, object-store, or test-harness code changes.
