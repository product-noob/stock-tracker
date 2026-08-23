"""Tests for the BSE capture adapter (scripts/bse_capture.py).

Run with:
    venv/bin/python -m unittest tests.test_bse_capture -v

Three layers:

* Offline unit tests for the pure helpers (envelope parsing, BOM handling,
  NEWS_DT parsing, idempotency keys, stale-session sniffing, magic-byte
  validation, download-path ordering, cursor window math).
* Offline pagination tests for ``_collect_pages`` (ROWCNT completeness,
  loud failure on shortfall) against a scripted fetcher — no DB, no network.
* A DB-backed end-to-end test against the live ``stocktracker_db`` with the
  HTTP layer and clock INJECTED (canned announcement JSON envelope + PDF /
  HTML-garbage attachment bytes; no network). It creates its own throwaway
  ``bse_api`` partition (``scrip:<fake>``) and drives the real
  ``run_partition`` across three simulated days, asserting:

  - day 1 first run: items persisted keyed ``bse_ann:{NEWSID}``, response
    artifact + PDF content object registered, invalid-content HTML retained
    as evidence, foreign-scrip rows dropped, cursor advanced with 24 h
    overlap;
  - day 1 re-run: deterministic per-day run key → skipped entirely, zero
    new network calls, no duplicate rows (idempotency / at-least-once);
  - day 2 run (overlap window): same payload re-seen → zero new items,
    attachments not re-downloaded, no duplicate sha256 registrations;
  - day 3 run with an empty-but-valid envelope: the R-BSE-9 control-scrip
    canary fires and "no data" is accepted only after the canary succeeds;
  - crashed-PDF-download convergence via the same-day skip path.

Like ``tests/test_jobs.py``, these rows live in append-only tables (DB
triggers reject UPDATE/DELETE on ``ingestion.raw_feed_items`` /
``raw_artifacts`` / ``documents.content_objects``), so nothing is cleaned
up: every artifact is namespaced under a per-test-uuid scrip and left
behind as audit evidence.
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

import bse_capture  # noqa: E402


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
    def test_strip_bom(self):
        bom = b"\xef\xbb\xbf"  # UTF-8 BOM (b"..." has no \u escape)
        self.assertEqual(bse_capture.strip_bom(bom + b"{}"), "{}")
        self.assertEqual(bse_capture.strip_bom(b'{"a":1}'), '{"a":1}')
        self.assertEqual(bse_capture.strip_bom(b""), "")

    def test_parse_envelope_ok(self):
        body = json.dumps(
            {"Table": [{"NEWSID": 1}], "Table1": [{"ROWCNT": 1}]}
        ).encode()
        rows, rowcnt = bse_capture.parse_envelope(body)
        self.assertEqual(rows, [{"NEWSID": 1}])
        self.assertEqual(rowcnt, 1)
        # UTF-8 BOM tolerated (R-BSE-3)
        rows, rowcnt = bse_capture.parse_envelope(b"\xef\xbb\xbf" + body)
        self.assertEqual(len(rows), 1)

    def test_parse_envelope_missing_table_fails_loudly(self):
        with self.assertRaises(bse_capture.EnvelopeError):
            bse_capture.parse_envelope(b"{}")
        with self.assertRaises(bse_capture.EnvelopeError):
            bse_capture.parse_envelope(b"<html>blocked</html>")
        with self.assertRaises(bse_capture.EnvelopeError):
            bse_capture.parse_envelope(b'{"Table": "not-a-list"}')
        with self.assertRaises(bse_capture.EnvelopeError):
            bse_capture.parse_envelope(
                b'{"Table": [], "Table1": [{"ROWCNT": "x"}]}')

    def test_sniff_stale_session(self):
        self.assertFalse(bse_capture.sniff_stale_session(b'{"Table": []}'))
        self.assertFalse(bse_capture.sniff_stale_session(
            b"\xef\xbb\xbf {\"Table\":[]}"))
        self.assertTrue(bse_capture.sniff_stale_session(b"<html>error</html>"))
        self.assertTrue(bse_capture.sniff_stale_session(b"Missing index"))
        self.assertTrue(bse_capture.sniff_stale_session(b""))

    def test_parse_news_dt(self):
        # ISO with fractional seconds, IST assumed → UTC
        dt = bse_capture.parse_news_dt("2026-08-22T14:23:53.217")
        self.assertEqual(dt, datetime(2026, 8, 22, 8, 53, 53, 217000,
                                      tzinfo=timezone.utc))
        dt = bse_capture.parse_news_dt("2026-08-22T14:23:53")
        self.assertEqual(dt, datetime(2026, 8, 22, 8, 53, 53,
                                      tzinfo=timezone.utc))
        # pilot-era fallback format
        dt = bse_capture.parse_news_dt("22-Aug-2026 14:23:53")
        self.assertEqual(dt, datetime(2026, 8, 22, 8, 53, 53,
                                      tzinfo=timezone.utc))
        self.assertIsNone(bse_capture.parse_news_dt("garbage"))
        self.assertIsNone(bse_capture.parse_news_dt(None))
        self.assertIsNone(bse_capture.parse_news_dt(123))

    def test_item_key_format(self):
        self.assertEqual(bse_capture.item_key(1063241), "bse_ann:1063241")

    def test_attachment_urls_his_first(self):
        urls = bse_capture.attachment_urls("ME_220826.PDF")
        self.assertEqual(urls, [
            bse_capture.ATTACH_HIS_BASE + "ME_220826.PDF",
            bse_capture.ATTACH_LIVE_BASE + "ME_220826.PDF",
        ])

    def test_looks_like_pdf(self):
        self.assertTrue(bse_capture.looks_like_pdf(b"%PDF-1.7 ..."))
        self.assertFalse(bse_capture.looks_like_pdf(b"<html>Access Denied"))
        self.assertFalse(bse_capture.looks_like_pdf(b""))

    def test_api_url_params(self):
        url = bse_capture.api_url("500325", "20260820", "20260822", 2)
        self.assertIn("https://api.bseindia.com/BseIndiaAPI/api/"
                      "AnnSubCategoryGetData/w?", url)
        self.assertIn("pageno=2", url)
        self.assertIn("strScrip=500325", url)
        self.assertIn("strPrevDate=20260820", url)
        self.assertIn("strToDate=20260822", url)

    def test_window_first_run(self):
        now = datetime.now(timezone.utc)
        frm, to = bse_capture.window_from_cursor(None, now)
        self.assertAlmostEqual((to - frm).total_seconds(),
                               bse_capture.FIRST_RUN_WINDOW.total_seconds(),
                               delta=5)

    def test_window_overlap_after_watermark(self):
        now = datetime.now(timezone.utc)
        hw = now - timedelta(hours=3)
        cursor = {"high_watermark_at": hw, "overlap_from": None,
                  "last_success_at": hw, "checkpoint": {}}
        frm, to = bse_capture.window_from_cursor(cursor, now)
        self.assertAlmostEqual(
            (frm - (hw - bse_capture.OVERLAP)).total_seconds(), 0, delta=5)

    def test_window_clock_skew_guard(self):
        now = datetime.now(timezone.utc)
        cursor = {"high_watermark_at": now + timedelta(days=2),
                  "overlap_from": None, "last_success_at": None,
                  "checkpoint": {}}
        frm, to = bse_capture.window_from_cursor(cursor, now)
        self.assertLess(frm, to)


# --------------------------------------------------------------------- #
# pagination (offline, scripted fetcher — R-BSE-8)
# --------------------------------------------------------------------- #

def _page(rows, rowcnt):
    payload: dict = {"Table": rows}
    if rowcnt is not None:  # envelope contract: ROWCNT must be an int or absent
        payload["Table1"] = [{"ROWCNT": rowcnt}]
    return 200, json.dumps(payload).encode()


class TestCollectPages(unittest.TestCase):
    def test_paginates_until_rowcnt(self):
        calls = []

        def fetch(scrip, frm, to, pageno, url):
            calls.append(pageno)
            if pageno == 1:
                return _page([{"NEWSID": i} for i in range(50)], 120)
            return _page([{"NEWSID": i} for i in range(50, 120)], 120)

        collected, rowcnt, meta = bse_capture._collect_pages(
            "500325", "20260820", "20260822", fetch, lambda m: None)
        self.assertEqual(rowcnt, 120)
        self.assertEqual(len(collected), 120)
        self.assertEqual(sorted(meta.keys()), [1, 2])
        self.assertIn(2, calls)

    def test_shortfall_raises(self):
        def fetch(scrip, frm, to, pageno, url):
            return _page([{"NEWSID": 1}], 100)

        with self.assertRaises(bse_capture.BseFetchError):
            bse_capture._collect_pages("500325", "20260820", "20260822",
                                       fetch, lambda m: None)

    def test_exhaustion_without_rowcnt_stops(self):
        def fetch(scrip, frm, to, pageno, url):
            if pageno <= 2:
                return _page([{"NEWSID": pageno}], None)
            return _page([], None)

        collected, rowcnt, meta = bse_capture._collect_pages(
            "500325", "20260820", "20260822", fetch, lambda m: None)
        self.assertEqual(len(collected), 2)
        self.assertIsNone(rowcnt)

    def test_row_without_newsid_skipped_defensively(self):
        def fetch(scrip, frm, to, pageno, url):
            return _page([{"SCRIP_CD": "500325"}, {"NEWSID": 7}], None)

        collected, _, _ = bse_capture._collect_pages(
            "500325", "20260820", "20260822", fetch, lambda m: None)
        self.assertEqual(list(collected.keys()), ["7"])


# --------------------------------------------------------------------- #
# DB-backed end-to-end idempotency test (injected HTTP + clock)
# --------------------------------------------------------------------- #

class FakeBseHttp:
    """Canned announcements-API + attachment responses; counts every call."""

    def __init__(self, news_id_base: int, scrip: str,
                 pdf_bytes: bytes, bad_pdf_bytes: bytes):
        self.api_calls = 0
        self.control_calls = 0
        self.pdf_calls: list[str] = []
        self.pdf_fail = False       # simulate network-level download failures
        self.empty_table = False    # simulate an empty (but valid) envelope
        self.scrip = scrip
        self.rows = [
            {
                "NEWSID": news_id_base + 1,
                "SCRIP_CD": scrip,
                "SLONGNAME": "Test Fake Industries Limited",
                "HEADLINE": "Analysts/Institutional Investor Meet/Con. "
                            "Call Updates",
                "CATEGORYNAME": "Company Update",
                "ATTACHMENTNAME": f"{news_id_base + 1}.pdf",
                "NEWS_DT": "2026-08-22T10:15:00.000",
            },
            {
                "NEWSID": news_id_base + 2,
                "SCRIP_CD": scrip,
                "SLONGNAME": "Test Fake Industries Limited",
                "HEADLINE": "Disclosure under Reg. 30 of SEBI LODR",
                "CATEGORYNAME": "Insider Trading / SAST",
                "ATTACHMENTNAME": "",  # no attachment
                "NEWS_DT": "2026-08-22T09:02:00.000",
            },
            {
                "NEWSID": news_id_base + 3,
                "SCRIP_CD": scrip,
                "SLONGNAME": "Test Fake Industries Limited",
                "HEADLINE": "Board Meeting Intimation",
                "CATEGORYNAME": "Board Meeting",
                "ATTACHMENTNAME": f"{news_id_base + 3}_bad.pdf",
                "NEWS_DT": "2026-08-21T18:30:00.000",
            },
            # foreign scrip row: must be dropped by the tier-1 identity
            # guard (R-BSE-5), never inserted under this partition
            {
                "NEWSID": news_id_base + 4,
                "SCRIP_CD": "000000",
                "SLONGNAME": "Some Other Company Ltd",
                "HEADLINE": "Not ours",
                "CATEGORYNAME": "Company Update",
                "ATTACHMENTNAME": "",
                "NEWS_DT": "2026-08-22T11:00:00.000",
            },
        ]
        self.pdf_by_url: dict[str, bytes] = {
            bse_capture.ATTACH_HIS_BASE + f"{news_id_base + 1}.pdf": pdf_bytes,
            bse_capture.ATTACH_LIVE_BASE + f"{news_id_base + 1}.pdf": pdf_bytes,
            # the AttachHis trap: HTTP 200 with an HTML error page on BOTH
            # paths → invalid-content evidence, never a valid PDF
            bse_capture.ATTACH_HIS_BASE + f"{news_id_base + 3}_bad.pdf":
                b"<html>Access Denied</html>",
            bse_capture.ATTACH_LIVE_BASE + f"{news_id_base + 3}_bad.pdf":
                b"<html>Not Found</html>",
        }

    def _envelope(self, rows):
        return json.dumps({"Table": rows,
                           "Table1": [{"ROWCNT": len(rows)}]}).encode()

    def fetch_api(self, scrip: str, frm: str, to: str, pageno: int, url: str):
        if scrip == bse_capture.CONTROL_SCRIP:
            self.control_calls += 1
            return _page([{"NEWSID": 1, "SCRIP_CD": scrip}], 1)
        self.api_calls += 1
        if self.empty_table:
            return _page([], 0)
        return _page(self.rows, len(self.rows))

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
        self.scrip = f"T{self.uid[:5]}"          # fake scrip code (unique)
        self.news_base = 99000000 + int(self.uid[:6], 16)
        self.partition_id = self._make_partition()
        # distinct bytes per test run → distinct sha256 registrations
        self.pdf_bytes = (b"%PDF-1.4 fake bse announcement pdf "
                          + self.uid.encode() + b" " + b"x" * 512)
        self.bad_pdf_bytes = (b"%PDF-1.4 fake bse live-path garbage "
                              + self.uid.encode())
        self.http = FakeBseHttp(self.news_base, self.scrip,
                                self.pdf_bytes, self.bad_pdf_bytes)
        # T0 = 05:00 UTC = 10:30 IST — mid-day, no date-boundary ambiguity
        self.t_day1 = datetime(2026, 8, 22, 5, 0, tzinfo=timezone.utc)
        self.t_day2 = self.t_day1 + timedelta(days=1)
        self.t_day3 = self.t_day1 + timedelta(days=2)

    def _make_partition(self) -> int:
        with self.psycopg.connect(_database_url()) as conn:
            pid = conn.execute(
                """
                INSERT INTO ingestion.source_partitions
                    (source, partition_key, description, config)
                VALUES ('bse_api', %s,
                        'test partition (bse_capture tests)', '{}'::jsonb)
                RETURNING partition_id
                """,
                (f"scrip:{self.scrip}",),
            ).fetchone()[0]
            conn.commit()
        return pid

    def _run(self, now):
        return bse_capture.run_partition(
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
        # 4 rows returned, 1 foreign-scrip row dropped → 3 seen
        self.assertEqual(s1["items_seen"], 3)
        self.assertEqual(s1["items_new"], 3)
        self.assertEqual(s1["http_status"], 200)
        self.assertEqual(s1["pdfs"]["downloaded"], 1)
        self.assertEqual(s1["pdfs"]["invalid"], 1)
        api_calls_after_run1 = self.http.api_calls

        with self.psycopg.connect(_database_url()) as conn:
            # lineage shape: 3 raw items keyed bse_ann:{NEWSID} per contract
            rows = conn.execute(
                "SELECT idempotency_key, external_id, title, description, "
                "content_url FROM ingestion.raw_feed_items "
                "WHERE partition_id=%s ORDER BY external_id",
                (pid,)).fetchall()
            self.assertEqual(
                [r[0] for r in rows],
                [f"bse_ann:{self.news_base + i}" for i in (1, 2, 3)])
            self.assertEqual(rows[0][2], "Test Fake Industries Limited")
            self.assertIn("Institutional Investor Meet", rows[0][3])
            self.assertEqual(
                rows[0][4],
                bse_capture.ATTACH_HIS_BASE + f"{self.news_base + 1}.pdf")
            # foreign-scrip row never landed
            n_foreign = conn.execute(
                "SELECT count(*) FROM ingestion.raw_feed_items "
                "WHERE partition_id=%s AND external_id=%s",
                (pid, str(self.news_base + 4))).fetchone()[0]
            self.assertEqual(n_foreign, 0)
            # raw_payload preserves identity fields verbatim (R-BSE-14/15:
            # no fabricated ISIN, SCRIP_CD/SLONGNAME preserved for M3)
            payload = conn.execute(
                "SELECT raw_payload->>'SCRIP_CD', raw_payload->>'SLONGNAME', "
                "raw_payload->>'NEWSID' FROM ingestion.raw_feed_items "
                "WHERE partition_id=%s AND external_id=%s",
                (pid, str(self.news_base + 1))).fetchone()
            self.assertEqual(payload, (self.scrip,
                                       "Test Fake Industries Limited",
                                       str(self.news_base + 1)))
            # artifacts: 1 API response + 1 valid PDF + invalid-content
            # evidence for BOTH path variants of the garbage attachment
            # (each variant's exact bytes retained per R-BSE-11/12)
            kinds = dict(conn.execute(
                "SELECT artifact_kind, count(*) FROM ingestion.raw_artifacts "
                "WHERE fetch_run_id=%s GROUP BY 1",
                (s1["fetch_run_id"],)).fetchall())
            self.assertEqual(kinds, {"api_response": 1, "filing_pdf": 1,
                                     "filing_pdf_invalid": 2})
            # PDF registered as magic-valid content object
            pdf_sha = conn.execute(
                "SELECT sha256 FROM ingestion.raw_artifacts "
                "WHERE fetch_run_id=%s AND artifact_kind='filing_pdf'",
                (s1["fetch_run_id"],)).fetchone()[0]
            co = conn.execute(
                "SELECT magic_valid, media_type, byte_size "
                "FROM documents.content_objects WHERE sha256=%s",
                (pdf_sha,)).fetchone()
            self.assertEqual(co[0], True)
            self.assertEqual(co[1], "application/pdf")
            self.assertEqual(co[2], len(self.pdf_bytes))
            # invalid HTML kept as evidence, magic_valid=false (the AttachHis
            # variant — the first candidate tried)
            inv_sha = conn.execute(
                "SELECT a.sha256 FROM ingestion.raw_artifacts a "
                "WHERE a.fetch_run_id=%s AND a.artifact_kind='filing_pdf_invalid' "
                "AND a.metadata->>'resolved_url' = %s",
                (s1["fetch_run_id"],
                 bse_capture.ATTACH_HIS_BASE + f"{self.news_base + 3}_bad.pdf")
            ).fetchone()[0]
            co = conn.execute(
                "SELECT magic_valid, media_type FROM documents.content_objects "
                "WHERE sha256=%s", (inv_sha,)).fetchone()
            self.assertEqual(co, (False, "text/html"))
            resp_sha = conn.execute(
                "SELECT sha256 FROM ingestion.raw_artifacts "
                "WHERE fetch_run_id=%s AND artifact_kind='api_response'",
                (s1["fetch_run_id"],)).fetchone()[0]
            # cursor advanced past the newest NEWS_DT (10:15 IST = 04:45 UTC)
            # with exactly 24 h overlap
            cur = conn.execute(
                "SELECT high_watermark_at, overlap_from, last_fetch_run_id, "
                "consecutive_failures FROM ingestion.ingestion_cursors "
                "WHERE partition_id=%s", (pid,)).fetchone()
            self.assertEqual(cur[0], datetime(2026, 8, 22, 4, 45,
                                              tzinfo=timezone.utc))
            self.assertEqual(cur[0] - cur[1], timedelta(hours=24))
            self.assertEqual(cur[2], s1["fetch_run_id"])
            self.assertEqual(cur[3], 0)

        # ---- day 1, run 2 (same day): deterministic key → full skip ----
        s2 = self._run(self.t_day1)
        self.assertTrue(s2["skipped"])
        self.assertEqual(s2["fetch_run_id"], s1["fetch_run_id"])
        self.assertEqual(self.http.api_calls, api_calls_after_run1,
                         "same-day re-run must make zero announcement calls")

        # ---- day 2: overlap window re-sees all items → nothing new ----
        s3 = self._run(self.t_day2)
        self.assertEqual(s3["status"], "not_modified")
        self.assertFalse(s3["skipped"])
        self.assertEqual(s3["items_seen"], 3)
        self.assertEqual(s3["items_new"], 0)
        self.assertEqual(s3["pdfs"]["downloaded"], 0)
        self.assertEqual(s3["pdfs"]["skipped"], 2,
                         "already-captured attachments must not re-download")
        self.assertEqual(len(self.http.pdf_calls), 3,
                         "day1: good URL hits valid His (1 attempt); garbage "
                         "URL tries His then Live (2 attempts)")

        with self.psycopg.connect(_database_url()) as conn:
            # still exactly 3 raw items — no duplicates
            n_items = conn.execute(
                "SELECT count(*) FROM ingestion.raw_feed_items "
                "WHERE partition_id=%s", (pid,)).fetchone()[0]
            self.assertEqual(n_items, 3)
            # exactly one registration per digest across all runs
            for sha in (resp_sha, pdf_sha, inv_sha):
                n_co = conn.execute(
                    "SELECT count(*) FROM documents.content_objects "
                    "WHERE sha256=%s", (sha,)).fetchone()[0]
                self.assertEqual(n_co, 1,
                                 f"duplicate content_object for {sha[:12]}…")
            n_runs = conn.execute(
                "SELECT count(*) FROM ingestion.fetch_runs "
                "WHERE partition_id=%s", (pid,)).fetchone()[0]
            self.assertEqual(n_runs, 2)

        # ---- day 3: valid-but-empty envelope → control-scrip canary ----
        self.http.empty_table = True
        s4 = self._run(self.t_day3)
        self.assertFalse(s4["skipped"])
        self.assertEqual(s4["status"], "not_modified")
        self.assertEqual(s4["items_new"], 0)
        self.assertEqual(self.http.control_calls, 1,
                         "empty Table must trigger the R-BSE-9 canary")
        with self.psycopg.connect(_database_url()) as conn:
            kinds = dict(conn.execute(
                "SELECT artifact_kind, count(*) FROM ingestion.raw_artifacts "
                "WHERE fetch_run_id=%s GROUP BY 1",
                (s4["fetch_run_id"],)).fetchall())
            self.assertEqual(kinds.get("api_response_control"), 1)
            # cursor still advanced (canary proved the endpoint healthy)
            cur = conn.execute(
                "SELECT consecutive_failures FROM ingestion.ingestion_cursors "
                "WHERE partition_id=%s", (pid,)).fetchone()
            self.assertEqual(cur[0], 0)

    def test_crashed_pdf_download_recovers_without_duplicates(self):
        """A run whose attachment downloads fail at network level must be
        converged by the same-day skip path WITHOUT duplicating lineage."""
        pid = self.partition_id

        # ---- day 1 run with all downloads failing (crash analogue) ----
        self.http.pdf_fail = True
        s1 = self._run(self.t_day1)
        self.assertEqual(s1["status"], "ok")
        self.assertEqual(s1["pdfs"]["failed"], 2)
        with self.psycopg.connect(_database_url()) as conn:
            n_pdf = conn.execute(
                "SELECT count(*) FROM ingestion.raw_artifacts "
                "WHERE fetch_run_id=%s AND artifact_kind LIKE 'filing_pdf%%'",
                (s1["fetch_run_id"],)).fetchone()[0]
            self.assertEqual(n_pdf, 0)

        # ---- same-day re-run: skip path recovers the missing downloads ----
        self.http.pdf_fail = False
        s2 = self._run(self.t_day1)
        self.assertTrue(s2["skipped"])
        self.assertEqual(s2["fetch_run_id"], s1["fetch_run_id"])
        self.assertEqual(s2["pdfs"]["downloaded"], 1)
        self.assertEqual(s2["pdfs"]["invalid"], 1)

        # ---- third same-day re-run: fully converged, zero network calls ----
        api_calls = self.http.api_calls
        pdf_calls = len(self.http.pdf_calls)
        s3 = self._run(self.t_day1)
        self.assertTrue(s3["skipped"])
        self.assertEqual(s3["pdfs"], {"downloaded": 0, "skipped": 0,
                                      "invalid": 0, "failed": 0})
        self.assertEqual(self.http.api_calls, api_calls)
        self.assertEqual(len(self.http.pdf_calls), pdf_calls)

        with self.psycopg.connect(_database_url()) as conn:
            rows = dict(conn.execute(
                "SELECT sha256, count(*) FROM ingestion.raw_artifacts "
                "WHERE fetch_run_id=%s AND artifact_kind IN "
                "('filing_pdf','filing_pdf_invalid') GROUP BY 1",
                (s1["fetch_run_id"],)).fetchall())
            pdf_sha = hashlib.sha256(self.pdf_bytes).hexdigest()
            his_sha = hashlib.sha256(
                b"<html>Access Denied</html>").hexdigest()
            live_sha = hashlib.sha256(b"<html>Not Found</html>").hexdigest()
            self.assertEqual(rows, {pdf_sha: 1, his_sha: 1, live_sha: 1},
                             "recovery must not duplicate artifact rows")
            co = conn.execute(
                "SELECT magic_valid FROM documents.content_objects "
                "WHERE sha256=%s", (pdf_sha,)).fetchone()
            self.assertEqual(co, (True,))


if __name__ == "__main__":
    unittest.main()
