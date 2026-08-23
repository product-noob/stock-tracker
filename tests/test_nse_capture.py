"""Tests for the NSE capture adapter (scripts/nse_capture.py).

Run with:
    venv/bin/python -m unittest tests.test_nse_capture -v

Two layers:

* Offline unit tests for the pure helpers (an_dt parsing, idempotency keys,
  stale-session sniffing, magic-byte validation, cursor window math).
* A DB-backed end-to-end test against the live ``stocktracker_db`` with the
  HTTP layer and clock INJECTED (canned announcement JSON + PDF bytes; no
  network). It creates its own throwaway ``nse_api`` partition (symbol
  ``TEST<uuid>``) and drives the real ``run_partition`` across two simulated
  days, asserting:

  - day 1 first run: items persisted, response artifact + PDF content object
    registered, cursor advanced;
  - day 1 re-run: deterministic per-day run key → skipped entirely, zero new
    network calls, no duplicate rows (idempotency / at-least-once);
  - day 2 run (overlap window): same payload re-seen → zero new items,
    PDF not re-downloaded, no duplicate sha256 registrations.

Like ``tests/test_jobs.py``, these rows live in append-only tables (DB
triggers reject UPDATE/DELETE on ``ingestion.raw_feed_items`` /
``raw_artifacts`` / ``documents.content_objects``), so nothing is cleaned up:
every artifact is namespaced under a per-test-uuid symbol
(``symbol:TEST<uuid>``) and left behind as audit evidence.
"""
import hashlib
import json
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import nse_capture  # noqa: E402


def _database_url() -> str | None:
    import jobs
    try:
        return jobs.db_url_from_env()
    except jobs.JobsError:
        return None


# --------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------- #

class TestPureHelpers(unittest.TestCase):
    def test_parse_an_dt_ist_to_utc(self):
        dt = nse_capture.parse_an_dt("22-Aug-2026 14:26:26")
        self.assertEqual(dt, datetime(2026, 8, 22, 8, 56, 26,
                                      tzinfo=timezone.utc))

    def test_parse_an_dt_garbage_returns_none(self):
        self.assertIsNone(nse_capture.parse_an_dt("not a date"))
        self.assertIsNone(nse_capture.parse_an_dt(""))
        self.assertIsNone(nse_capture.parse_an_dt(None))
        self.assertIsNone(nse_capture.parse_an_dt(12345))

    def test_item_key_format(self):
        self.assertEqual(nse_capture.item_key(1063241), "nse_ann:1063241")

    def test_sniff_stale_session(self):
        self.assertFalse(nse_capture.sniff_stale_session(b'[{"seq_id": 1}]'))
        self.assertFalse(nse_capture.sniff_stale_session(b'\n\n [ ...'))
        self.assertTrue(nse_capture.sniff_stale_session(b"Missing index"))
        self.assertTrue(nse_capture.sniff_stale_session(b"<html>blocked</html>"))
        self.assertTrue(nse_capture.sniff_stale_session(b'{"not": "array"}'))
        self.assertTrue(nse_capture.sniff_stale_session(b""))

    def test_looks_like_pdf(self):
        self.assertTrue(nse_capture.looks_like_pdf(b"%PDF-1.7 ..."))
        self.assertFalse(nse_capture.looks_like_pdf(b"<html>Access Denied"))
        self.assertFalse(nse_capture.looks_like_pdf(b""))

    def test_window_first_run(self):
        now = datetime.now(timezone.utc)
        frm, to = nse_capture.window_from_cursor(None, now)
        self.assertAlmostEqual((to - frm).total_seconds(),
                               nse_capture.FIRST_RUN_WINDOW.total_seconds(),
                               delta=5)
        self.assertLessEqual(to, now)

    def test_window_overlap_after_watermark(self):
        now = datetime.now(timezone.utc)
        hw = now - timedelta(hours=3)
        cursor = {"high_watermark_at": hw, "overlap_from": None,
                  "last_success_at": hw, "checkpoint": {}}
        frm, to = nse_capture.window_from_cursor(cursor, now)
        self.assertAlmostEqual((frm - (hw - nse_capture.OVERLAP)).total_seconds(),
                               0, delta=5)
        self.assertLessEqual(to, now)

    def test_window_clock_skew_guard(self):
        now = datetime.now(timezone.utc)
        cursor = {"high_watermark_at": now + timedelta(days=2),  # skewed ahead
                  "overlap_from": None, "last_success_at": None, "checkpoint": {}}
        frm, to = nse_capture.window_from_cursor(cursor, now)
        self.assertLess(frm, to)


# --------------------------------------------------------------------- #
# DB-backed end-to-end idempotency test (injected HTTP + clock)
# --------------------------------------------------------------------- #

PDF_BYTES = b"%PDF-1.4 fake announcement pdf for tests " + b"x" * 512
PDF_SHA = hashlib.sha256(PDF_BYTES).hexdigest()

ANNOUNCEMENTS = [
    {
        "seq_id": 9900001,
        "symbol": "TESTFAKE",
        "sm_isin": "INE000TEST01",
        "sm_name": "Test Fake Industries Limited",
        "desc": "Analysts/Institutional Investor Meet/Con. Call Updates",
        "attchmntText": "Intimation of investor call on 22-Aug-2026.",
        "attchmntFile": "https://nsearchives.nseindia.com/corporate/testfake_1.pdf",
        "an_dt": "22-Aug-2026 11:15:00",
    },
    {
        "seq_id": 9900002,
        "symbol": "TESTFAKE",
        "sm_isin": "INE000TEST01",
        "sm_name": "Test Fake Industries Limited",
        "desc": "Disclosure under Reg. 30 of SEBI LODR",
        "attchmntText": "Press release regarding a new partnership.",
        "attchmntFile": "",  # no attachment
        "an_dt": "22-Aug-2026 09:02:00",
    },
]

API_BODY = ("[" + ",".join(
    json.dumps(a, separators=(",", ":")) for a in ANNOUNCEMENTS) + "]").encode()


class FakeHttp:
    """Canned announcements API + PDF responses; counts every call."""

    def __init__(self):
        self.api_calls = 0
        self.pdf_calls: list[str] = []
        self.pdf_fail = False  # simulate download failures (crash recovery)
        self.pdf_by_url: dict[str, bytes] = {
            "https://nsearchives.nseindia.com/corporate/testfake_1.pdf": PDF_BYTES,
            # an HTML block page masquerading as a download (invalid-content case)
            "https://nsearchives.nseindia.com/corporate/testfake_bad.pdf":
                b"<html>Access Denied</html>",
        }

    def fetch_api(self, symbol: str, frm: str, to: str, url: str):
        self.api_calls += 1
        return 200, API_BODY

    def fetch_pdf(self, session, url: str):
        self.pdf_calls.append(url)
        if self.pdf_fail:
            return None, 503, "HTTP 503"
        data = self.pdf_by_url.get(url)
        if data is None:
            return None, 404, "HTTP 404"
        return data, 200, ""


def _db_up() -> bool:
    url = _database_url()
    if not url:
        return False
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


@unittest.skipUnless(_db_up(), "live stocktracker_db not reachable")
class TestRunPartitionEndToEnd(unittest.TestCase):
    """Full run_partition cycle against the real DB, fake HTTP + fixed clock."""

    def setUp(self):
        import psycopg
        from object_store import ObjectStore
        self.psycopg = psycopg
        self.store = ObjectStore.from_env()
        self.uid = uuid.uuid4().hex[:12]
        self.symbol = f"TEST{self.uid.upper()[:8]}"
        self.partition_id = self._make_partition()
        self.http = FakeHttp()
        # T0 has no IST ambiguity worries: 05:00 UTC = 10:30 IST, mid-day
        self.t_day1 = datetime(2026, 8, 22, 5, 0, tzinfo=timezone.utc)
        self.t_day2 = self.t_day1 + timedelta(days=1)

    def _make_partition(self) -> int:
        with self.psycopg.connect(_database_url()) as conn:
            pid = conn.execute(
                """
                INSERT INTO ingestion.source_partitions
                    (source, partition_key, description, config)
                VALUES ('nse_api', %s,
                        'test partition (nse_capture tests)', '{}'::jsonb)
                RETURNING partition_id
                """,
                (f"symbol:{self.symbol}",),
            ).fetchone()[0]
            conn.commit()
        return pid

    def _run(self, now):
        return nse_capture.run_partition(
            self.partition_id,
            fetch_fn=self.http.fetch_api,
            pdf_fetch_fn=self.http.fetch_pdf,
            now=now,
            sleep_fn=lambda _s: None,
            quiet=True)

    def test_incremental_rerun_idempotent(self):
        pid = self.partition_id

        # ---- day 1, run 1: fresh partition, 7-day backfill ----
        s1 = self._run(self.t_day1)
        self.assertEqual(s1["status"], "ok")
        self.assertFalse(s1["skipped"])
        self.assertEqual(s1["items_seen"], 2)
        self.assertEqual(s1["items_new"], 2)
        self.assertEqual(s1["pdfs"]["downloaded"], 1)
        self.assertEqual(s1["http_status"], 200)
        api_calls_after_run1 = self.http.api_calls

        with self.psycopg.connect(_database_url()) as conn:
            # lineage shape: 2 raw items keyed per contract
            rows = conn.execute(
                "SELECT idempotency_key, external_id, title, content_url, "
                "published_at FROM ingestion.raw_feed_items "
                "WHERE partition_id=%s ORDER BY external_id",
                (pid,)).fetchall()
            self.assertEqual([r[0] for r in rows],
                             ["nse_ann:9900001", "nse_ann:9900002"])
            self.assertEqual(rows[0][1], "9900001")
            self.assertIn("Test Fake Industries", rows[0][2])
            self.assertEqual(rows[0][3],
                             "https://nsearchives.nseindia.com/corporate/testfake_1.pdf")
            self.assertIsNotNone(rows[0][4])
            # response artifact + PDF artifact on the run
            kinds = conn.execute(
                "SELECT artifact_kind, count(*) FROM ingestion.raw_artifacts "
                "WHERE fetch_run_id=%s GROUP BY 1",
                (s1["fetch_run_id"],)).fetchall()
            self.assertEqual(dict(kinds), {"api_response": 1, "filing_pdf": 1})
            # PDF registered as magic-valid content object
            pdf_sha = conn.execute(
                "SELECT sha256 FROM ingestion.raw_artifacts WHERE fetch_run_id=%s "
                "AND artifact_kind='filing_pdf'", (s1["fetch_run_id"],)
            ).fetchone()[0]
            co = conn.execute(
                "SELECT magic_valid, media_type FROM documents.content_objects "
                "WHERE sha256=%s", (pdf_sha,)).fetchone()
            self.assertEqual(co, (True, "application/pdf"))
            resp_sha = conn.execute(
                "SELECT sha256 FROM ingestion.raw_artifacts WHERE fetch_run_id=%s "
                "AND artifact_kind='api_response'", (s1["fetch_run_id"],)
            ).fetchone()[0]
            # raw_payload preserves identity fields verbatim (R-NSE-20, M3 input)
            payload = conn.execute(
                "SELECT raw_payload->>'sm_isin', raw_payload->>'sm_name' "
                "FROM ingestion.raw_feed_items "
                "WHERE partition_id=%s AND external_id='9900001'",
                (pid,)).fetchone()
            self.assertEqual(payload, ("INE000TEST01",
                                       "Test Fake Industries Limited"))
            # cursor advanced past the newest an_dt with exactly 24 h overlap
            cur = conn.execute(
                "SELECT high_watermark_at, overlap_from, last_fetch_run_id, "
                "consecutive_failures FROM ingestion.ingestion_cursors "
                "WHERE partition_id=%s", (pid,)).fetchone()
            self.assertEqual(cur[0], datetime(2026, 8, 22, 5, 45,
                                              tzinfo=timezone.utc))
            self.assertEqual(cur[0] - cur[1], timedelta(hours=24))
            self.assertEqual(cur[2], s1["fetch_run_id"])
            self.assertEqual(cur[3], 0)

        # ---- day 1, run 2 (same day): deterministic key → full skip ----
        s2 = self._run(self.t_day1)
        self.assertTrue(s2["skipped"])
        self.assertEqual(s2["fetch_run_id"], s1["fetch_run_id"])
        self.assertEqual(self.http.api_calls, api_calls_after_run1,
                         "same-day re-run must make zero network calls")

        # ---- day 2: overlap window re-sees both items → nothing new ----
        s3 = self._run(self.t_day2)
        self.assertEqual(s3["status"], "not_modified")
        self.assertFalse(s3["skipped"])
        self.assertEqual(s3["items_seen"], 2)
        self.assertEqual(s3["items_new"], 0)
        self.assertEqual(s3["pdfs"]["skipped"], 1,
                         "already-captured PDF must not be re-downloaded")
        self.assertEqual(len(self.http.pdf_calls), 1)

        with self.psycopg.connect(_database_url()) as conn:
            # still exactly 2 raw items — no duplicates
            n_items = conn.execute(
                "SELECT count(*) FROM ingestion.raw_feed_items "
                "WHERE partition_id=%s", (pid,)).fetchone()[0]
            self.assertEqual(n_items, 2)
            # exactly one registration per digest across all runs
            for sha in (resp_sha, pdf_sha):
                n_co = conn.execute(
                    "SELECT count(*) FROM documents.content_objects WHERE sha256=%s",
                    (sha,)).fetchone()[0]
                self.assertEqual(n_co, 1, f"duplicate content_object for {sha[:12]}…")
            # runs: day1 run + day2 run (the skip reused day1's row)
            n_runs = conn.execute(
                "SELECT count(*) FROM ingestion.fetch_runs WHERE partition_id=%s",
                (pid,)).fetchone()[0]
            self.assertEqual(n_runs, 2)
            # raw items still point at their original first_seen_run
            orphan = conn.execute(
                """
                SELECT count(*) FROM ingestion.raw_feed_items i
                JOIN ingestion.raw_artifacts a USING (artifact_id)
                WHERE i.partition_id=%s AND i.first_seen_run_id <> a.fetch_run_id
                """, (pid,)).fetchone()[0]
            self.assertEqual(orphan, 0)

    def test_crashed_pdf_download_recovers_without_duplicates(self):
        """Regression (attempt-2 defect): a run whose PDF download fails must
        be converged by the same-day skip path's recovery WITHOUT creating a
        duplicate artifact row for the same sha256."""
        pid = self.partition_id

        # ---- day 1 run with the PDF download failing (crash analogue) ----
        self.http.pdf_fail = True
        s1 = self._run(self.t_day1)
        self.assertEqual(s1["status"], "ok")
        self.assertEqual(s1["pdfs"]["failed"], 1,
                         "initial attempt + re-warm retry both fail → 1 failure")
        with self.psycopg.connect(_database_url()) as conn:
            n_pdf = conn.execute(
                "SELECT count(*) FROM ingestion.raw_artifacts "
                "WHERE fetch_run_id=%s AND artifact_kind='filing_pdf'",
                (s1["fetch_run_id"],)).fetchone()[0]
            self.assertEqual(n_pdf, 0)

        # ---- same-day re-run: skip path recovers the missing PDF ----
        self.http.pdf_fail = False
        s2 = self._run(self.t_day1)
        self.assertTrue(s2["skipped"])
        self.assertEqual(s2["fetch_run_id"], s1["fetch_run_id"])
        self.assertEqual(s2["pdfs"]["downloaded"], 1)

        # ---- third same-day re-run: fully converged, zero network calls ----
        api_calls = self.http.api_calls
        pdf_calls = len(self.http.pdf_calls)
        s3 = self._run(self.t_day1)
        self.assertTrue(s3["skipped"])
        self.assertEqual(s3["pdfs"]["downloaded"], 0)
        self.assertEqual(self.http.api_calls, api_calls)
        self.assertEqual(len(self.http.pdf_calls), pdf_calls)

        with self.psycopg.connect(_database_url()) as conn:
            rows = conn.execute(
                "SELECT sha256, count(*) FROM ingestion.raw_artifacts "
                "WHERE fetch_run_id=%s AND artifact_kind LIKE 'filing_pdf%%' "
                "GROUP BY 1",
                (s1["fetch_run_id"],)).fetchall()
            self.assertEqual(rows, [(PDF_SHA, 1)],
                             "recovery must not duplicate the PDF artifact row")
            co = conn.execute(
                "SELECT magic_valid, media_type FROM documents.content_objects "
                "WHERE sha256=%s", (PDF_SHA,)).fetchone()
            self.assertEqual(co, (True, "application/pdf"))


if __name__ == "__main__":
    unittest.main()
