"""DB-backed tests for the filing downloader's canonical content lifecycle
(review ST-C04 / ST-D03): immutable attempts, record→content links, bounded
retries, and fail-loud job semantics — against the positively marked
disposable DB + object root only."""
from __future__ import annotations

import hashlib
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import download_filings as df  # noqa: E402
from support import guarded_environment_or_skip  # noqa: E402

PDF_BYTES = b"%PDF-1.4 fake downloader pdf " + b"y" * 512
PDF_SHA = hashlib.sha256(PDF_BYTES).hexdigest()
HTML_BYTES = b"<html>Access Denied</html>"


class FakeFetch:
    """(source, url) → (data|None, status, err, resolved)."""

    def __init__(self):
        self.calls: list[str] = []
        self.response: bytes | None = PDF_BYTES
        self.http_status = 200

    def __call__(self, source: str, url: str):
        self.calls.append(url)
        if self.response is None:
            return None, self.http_status or 503, f"HTTP {self.http_status}", url
        return self.response, self.http_status, "", url


class FilingDownloaderTestBase(unittest.TestCase):
    def setUp(self):
        self.test_env = guarded_environment_or_skip()
        from object_store import ObjectStore
        self.store = ObjectStore(root=self.test_env.object_root,
                                 db_url=self.test_env.database_url)
        self.uid = uuid.uuid4().hex[:12]
        self.url = (f"https://nsearchives.nseindia.com/corporate/"
                    f"{self.uid}_filing.pdf")
        self.partition_id = self._make_partition()
        self.record_id = self._make_record_with_url()

    def _make_partition(self) -> int:
        with self.test_env.connect() as conn:
            pid = conn.execute(
                """
                INSERT INTO ingestion.source_partitions
                    (source, partition_key, description, config)
                VALUES ('nse_api', %s,
                        'test partition (download_filings tests)', '{}'::jsonb)
                RETURNING partition_id
                """,
                (f"symbol:DLS{self.uid.upper()[:8]}",),
            ).fetchone()[0]
            conn.commit()
        return pid

    def _make_record_with_url(self) -> int:
        with self.test_env.connect() as conn:
            run_id = conn.execute(
                """
                INSERT INTO ingestion.fetch_runs
                    (partition_id, idempotency_key, status, finished_at,
                     items_seen, items_new)
                VALUES (%s, %s, 'ok', now(), 1, 1)
                RETURNING fetch_run_id
                """,
                (self.partition_id, f"dltst-run:{self.uid}"),
            ).fetchone()[0]
            artifact_id = conn.execute(
                """
                INSERT INTO ingestion.raw_artifacts
                    (fetch_run_id, idempotency_key, artifact_kind,
                     content_type, sha256, storage_path, byte_size, metadata)
                VALUES (%s, %s, 'api_response', 'application/json', %s, %s, 2,
                        '{}'::jsonb)
                RETURNING artifact_id
                """,
                (run_id, f"dltst-art:{self.uid}",
                 "e" * 64, f"objects/dltst/{self.uid}"),
            ).fetchone()[0]
            item_id = conn.execute(
                """
                INSERT INTO ingestion.raw_feed_items
                    (partition_id, first_seen_run_id, artifact_id,
                     idempotency_key, external_id, content_url, title,
                     raw_payload)
                VALUES (%s, %s, %s, %s, %s, %s, 'dl test', '{}'::jsonb)
                RETURNING item_id
                """,
                (self.partition_id, run_id, artifact_id, f"dltst:{self.uid}",
                 f"DLTST{self.uid}", self.url),
            ).fetchone()[0]
            record_id = conn.execute(
                """
                INSERT INTO ingestion.normalized_records
                    (record_type, dedup_scope, dedup_key, exchange,
                     external_id, title)
                VALUES ('exchange_announcement', %s, %s, 'NSE', %s, 'dl test')
                RETURNING record_id
                """,
                (f"dltst:{self.uid}", f"DLKEY{self.uid}", f"DLTST{self.uid}"),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO ingestion.record_raw_items (item_id, record_id) "
                "VALUES (%s, %s)", (item_id, record_id))
            conn.commit()
        return record_id

    def _run(self, fetch: FakeFetch):
        with self.test_env.connect() as conn:
            stats = df.run_partition(
                self.partition_id, conn=conn, store=self.store,
                fetch_fn=fetch, sleep_fn=lambda _s: None, quiet=True)

        def query(sql, params=()):
            with self.test_env.connect() as c:
                return c.execute(sql, params).fetchall()
        return stats, query


class TestContentLifecycle(FilingDownloaderTestBase):
    def test_valid_download_writes_attempts_and_links(self):
        fetch = FakeFetch()
        stats, query = self._run(fetch)
        self.assertEqual(stats["status"], "ok")
        self.assertEqual(stats["downloaded"], 1)

        attempts = query(
            "SELECT status, content_id IS NOT NULL, http_status "
            "FROM documents.content_fetch_attempts "
            "WHERE record_id=%s AND requested_url=%s",
            (self.record_id, self.url))
        self.assertEqual(attempts, [("ok", True, 200)])

        links = query(
            "SELECT relation FROM documents.record_contents "
            "WHERE record_id=%s AND source_url=%s",
            (self.record_id, self.url))
        self.assertEqual(links, [("primary",)])

        # linked content is a magic-valid registered PDF
        row = query(
            """SELECT co.magic_valid, co.media_type
                 FROM documents.record_contents rc
                 JOIN documents.content_objects co USING (content_id)
                WHERE rc.record_id=%s AND rc.source_url=%s""",
            (self.record_id, self.url))
        self.assertEqual(row, [(True, "application/pdf")])

        # success excludes the URL from future work; re-run is a no-op
        calls_after_first = len(fetch.calls)
        stats2, _ = self._run(fetch)
        self.assertTrue(stats2["skipped"])
        self.assertEqual(len(fetch.calls), calls_after_first)
        attempts2 = query(
            "SELECT count(*) FROM documents.content_fetch_attempts "
            "WHERE record_id=%s AND requested_url=%s",
            (self.record_id, self.url))
        self.assertEqual(attempts2, [(1,)])

    def test_invalid_content_retries_then_converges(self):
        fetch = FakeFetch()
        fetch.response = HTML_BYTES
        stats, query = self._run(fetch)
        self.assertEqual(stats["invalid"], 1)
        attempts = query(
            "SELECT status FROM documents.content_fetch_attempts "
            "WHERE record_id=%s AND requested_url=%s",
            (self.record_id, self.url))
        self.assertEqual(attempts, [("invalid_content",)])
        # no record_contents link for invalid evidence
        links = query(
            "SELECT count(*) FROM documents.record_contents "
            "WHERE record_id=%s AND source_url=%s",
            (self.record_id, self.url))
        self.assertEqual(links, [(0,)])
        # invalid captures are NOT treated as handled — the URL stays pending
        self.assertFalse(stats["skipped"])

        # later the URL serves real bytes → converges to a valid link
        fetch.response = PDF_BYTES
        stats2, query = self._run(fetch)
        self.assertEqual(stats2["status"], "ok")
        latest = query(
            "SELECT status FROM documents.content_fetch_attempts "
            "WHERE record_id=%s AND requested_url=%s "
            "ORDER BY attempt_no DESC LIMIT 1",
            (self.record_id, self.url))
        self.assertEqual(latest, [("ok",)])
        links = query(
            "SELECT count(*) FROM documents.record_contents "
            "WHERE record_id=%s AND source_url=%s",
            (self.record_id, self.url))
        self.assertEqual(links, [(1,)])

    def test_failed_downloads_are_bounded_and_quarantined(self):
        fetch = FakeFetch()
        fetch.response = None
        fetch.http_status = 503
        for i in range(df.MAX_ATTEMPTS_PER_URL):
            stats, query = self._run(fetch)
            self.assertEqual(stats["status"], "failed", i)
        # budget exhausted: URL quarantined out of pending selection
        with self.test_env.connect() as conn:
            self.assertEqual(df.pending_urls(conn, self.partition_id), [])
            n = conn.execute(
                "SELECT count(*) FROM documents.content_fetch_attempts "
                "WHERE record_id=%s AND requested_url=%s",
                (self.record_id, self.url)).fetchone()[0]
        self.assertEqual(n, df.MAX_ATTEMPTS_PER_URL,
                         "each run must record exactly one durable attempt")

    def test_handler_fails_when_work_remains(self):
        fetch = FakeFetch()
        fetch.response = None
        fetch.http_status = 503
        with self.test_env.connect() as conn:
            stats = df.run_partition(
                self.partition_id, conn=conn, store=self.store,
                fetch_fn=fetch, sleep_fn=lambda _s: None, quiet=True)
        self.assertEqual(stats["status"], "failed")

        class FakeJob:
            payload = {"partition_id": self.partition_id}
            idempotency_key = f"dltst-{self.uid}"

        ctx = jobs_ctx(FakeJob())
        # The handler builds its own collaborators; inject the failing HTTP
        # layer so no test ever touches the real exchange endpoints.
        def failing_fetch(source, url):
            return None, 503, "HTTP 503", url

        with mock.patch.object(df, "_default_fetch_fn",
                               return_value=failing_fetch), \
             self.assertRaises(df.FilingDownloadError):
            df.handle_filings_download(ctx)


def jobs_ctx(job):
    import jobs
    return jobs.JobContext(
        job=job, worker_id="dltst-worker",
        db_url="postgresql://dltst.invalid/dltst")


if __name__ == "__main__":
    unittest.main()
