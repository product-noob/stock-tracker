"""Unit tests for scripts/object_store.py — Phase 0 object store.

Run with:
    venv/bin/python -m unittest tests.test_object_store -v

Filesystem tests use temporary directories and always run. Registry tests
exercise the real `documents.content_objects` table inside a transaction that
is rolled back, so they leave no rows behind; they are skipped automatically
if DATABASE_URL is unavailable or the database is unreachable.
"""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import object_store  # noqa: E402
from object_store import (  # noqa: E402
    ContentMismatchError,
    ObjectStore,
    ObjectStoreError,
)


def _database_url() -> str | None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == "DATABASE_URL" and v.strip():
                return v.strip()
    return None


def _db_available(url: str) -> bool:
    import psycopg

    try:
        with psycopg.connect(url, connect_timeout=5):
            return True
    except Exception:
        return False


class ObjectStoreTestCase(unittest.TestCase):
    """Base: fresh store rooted in a temp dir.

    The registry insert is patched to a no-op so filesystem-only tests never
    write rows to the dev database (``put_bytes`` without ``conn`` would
    otherwise commit for real). :class:`TestRegistryIntegration` re-enables
    the real registry and wraps every test in a rolled-back transaction.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "objects"
        self.store = ObjectStore(root=self.root)
        self._reg_patcher = mock.patch.object(
            object_store.ObjectStore, "_register", lambda *a, **k: None
        )
        self._reg_patcher.start()
        self.addCleanup(self._reg_patcher.stop)


class TestRoundTrip(ObjectStoreTestCase):
    def test_put_then_get_returns_identical_bytes(self):
        data = b"hello \xe2\x82\xb9 unicode payload" * 100
        result = self.store.put_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        self.assertEqual(result.sha256, sha)
        self.assertEqual(self.store.get(sha), data)
        self.assertTrue(result.created)

    def test_layout_is_two_char_fanout(self):
        data = b"layout check"
        result = self.store.put_bytes(data)
        expected_rel = f"objects/{result.sha256[:2]}/{result.sha256}"
        self.assertEqual(result.storage_path, expected_rel)
        self.assertTrue((self.root / expected_rel).is_file())
        # no stray files next to the blob
        siblings = sorted(p.name for p in (self.root / "objects" / result.sha256[:2]).iterdir())
        self.assertEqual(siblings, [result.sha256])

    def test_path_for_rejects_bad_digests(self):
        for bad in ("", "xyz", "ABCDEF" * 10 + "abcd", "z" * 64):
            with self.assertRaises(ValueError):
                self.store.path_for(bad)


class TestIdempotency(ObjectStoreTestCase):
    def test_reput_identical_bytes_writes_once(self):
        data = b"idempotent payload"
        first = self.store.put_bytes(data)
        second = self.store.put_bytes(data)
        self.assertEqual(first.sha256, second.sha256)
        self.assertFalse(second.created)
        blobs = list(self.root.rglob("*"))
        files = [p for p in blobs if p.is_file()]
        self.assertEqual(len(files), 1)
        self.assertEqual(self.store.get(first.sha256), data)

    def test_metadata_change_on_reput_does_not_duplicate_row(self):
        """Registry insert uses ON CONFLICT DO NOTHING; re-put with different
        metadata must not create a second row."""
        url = _database_url()
        if not url or not _db_available(url):
            self.skipTest("DATABASE_URL unavailable; skipping registry test")
        import psycopg

        self._reg_patcher.stop()  # this test exercises the real registry

        data = b"metadata idempotency"
        with psycopg.connect(url) as conn:
            try:
                self.store.put_bytes(data, source="test_a", conn=conn)
                again = self.store.put_bytes(
                    data, metadata={"source": "test_b"}, conn=conn
                )
                count = conn.execute(
                    "SELECT count(*) FROM documents.content_objects WHERE sha256=%s",
                    (again.sha256,),
                ).fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                conn.rollback()


class TestAtomicity(ObjectStoreTestCase):
    def test_failure_during_rename_leaves_no_partial_blob_or_temp_files(self):
        data = b"atomic payload"
        sha = hashlib.sha256(data).hexdigest()

        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            raise OSError("simulated crash during rename")

        target = self.store.path_for(sha)
        with mock.patch.object(object_store.os, "replace", flaky_replace):
            with self.assertRaises(OSError):
                self.store.put_bytes(data)

        self.assertEqual(calls["n"], 1)
        self.assertFalse(target.exists(), "no partial file at final path")
        leftovers = [p.name for p in target.parent.glob(".tmp-*")]
        self.assertEqual(leftovers, [], "temp file cleaned up after failure")

        # store still usable afterwards
        with mock.patch.object(object_store.os, "replace", real_replace):
            self.store.put_bytes(data)
        self.assertEqual(self.store.get(sha), data)

    def test_failed_put_registers_nothing(self):
        """A put that fails mid-write leaves no committed registry row."""
        url = _database_url()
        if not url or not _db_available(url):
            self.skipTest("DATABASE_URL unavailable; skipping registry test")
        import psycopg

        data = b"never lands"

        def boom(*a, **k):
            raise OSError("simulated failure")

        with psycopg.connect(url) as conn:
            try:
                with mock.patch.object(
                    object_store.ObjectStore, "_atomic_write", boom
                ):
                    with self.assertRaises(OSError):
                        self.store.put_bytes(data, conn=conn)
                count = conn.execute(
                    "SELECT count(*) FROM documents.content_objects "
                    "WHERE sha256=%s",
                    (hashlib.sha256(data).hexdigest(),),
                ).fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                conn.rollback()


class TestCorruptionDetection(ObjectStoreTestCase):
    """Pure-filesystem tests (registry patched out by the base class)."""

    def test_get_detects_truncated_blob(self):
        data = b"a" * 4096
        sha = hashlib.sha256(data).hexdigest()
        self.store.put_bytes(data)
        blob = self.store.path_for(sha)
        blob.write_bytes(b"a" * 1024)  # simulate truncation
        with self.assertRaises(ContentMismatchError):
            self.store.get(sha)

    def test_get_detects_modified_blob(self):
        data = b"original bytes"
        sha = hashlib.sha256(data).hexdigest()
        self.store.put_bytes(data)
        blob = self.store.path_for(sha)
        blob.write_bytes(b"tampered bytes!")
        with self.assertRaises(ContentMismatchError):
            self.store.get(sha)
        # verify=False returns raw bytes (escape hatch for forensics)
        self.assertEqual(self.store.get(sha, verify=False), b"tampered bytes!")

    def test_put_detects_existing_blob_size_mismatch(self):
        """Re-put refuses to overwrite a corrupted blob at the digest path."""
        data = b"payload to corrupt"
        sha = hashlib.sha256(data).hexdigest()
        self.store.put_bytes(data)
        blob = self.store.path_for(sha)
        blob.write_bytes(b"short")  # different size, same path
        with self.assertRaises(ContentMismatchError):
            self.store.put_bytes(data)
        # corrupted content was NOT overwritten by the failed re-put
        self.assertEqual(blob.read_bytes(), b"short")

    def test_verify_flags_stale_temps_without_db(self):
        """skip_db mode consults no registry, so it can only flag stale temp
        files — orphan detection needs the DB and is deliberately skipped
        (otherwise every healthy blob would be a false orphan)."""
        self.store.put_bytes(b"registered-looking blob")  # just tree content
        orphan = hashlib.sha256(b"orphan").hexdigest()
        orphan_path = self.store.root / "objects" / orphan[:2] / orphan
        orphan_path.parent.mkdir(parents=True, exist_ok=True)
        orphan_path.write_bytes(b"orphan")
        stale_tmp = self.store.root / "objects" / ".tmp-abandoned"
        stale_tmp.write_bytes(b"")

        problems = self.store.verify(skip_db=True)
        joined = "\n".join(problems)
        self.assertIn(f"stale temp file: objects/{stale_tmp.name}", joined)
        # blobs are neither hash-checked against a registry nor flagged as
        # orphans in skip_db mode
        self.assertNotIn("orphan blob", joined)
        self.assertNotIn(f"objects/{orphan[:2]}/{orphan}", joined)

    def test_verify_clean_store_reports_nothing(self):
        self.store.put_bytes(b"only healthy content here")
        self.store.put_bytes(b"", media_type="text/plain")
        self.assertEqual(self.store.verify(skip_db=True), [])


class TestRegistryIntegration(ObjectStoreTestCase):
    """DB-backed tests; rolled back so the dev database stays untouched."""

    def setUp(self):
        super().setUp()
        self._reg_patcher.stop()  # real registry inserts, rolled back below
        url = _database_url()
        if not url or not _db_available(url):
            self.skipTest("DATABASE_URL unavailable; skipping DB tests")
        import psycopg

        self.conn = psycopg.connect(url)
        self.addCleanup(self.conn.close)

    def test_put_registers_row_with_expected_columns(self):
        data = b"registry row contents"
        media_type = "application/pdf"
        result = self.store.put_bytes(
            data, media_type=media_type, source="unittest",
            metadata={"url": "https://example.test/f.pdf"},
            conn=self.conn,
        )
        try:
            row = self.conn.execute(
                "SELECT storage_path, media_type, byte_size, metadata "
                "FROM documents.content_objects WHERE sha256=%s",
                (result.sha256,),
            ).fetchone()
            self.assertIsNotNone(row)
            storage_path, got_media, got_size, meta = row
            self.assertEqual(storage_path, result.storage_path)
            self.assertEqual(got_media, media_type)
            self.assertEqual(got_size, len(data))
            self.assertEqual(meta["source"], "unittest")
            self.assertEqual(meta["url"], "https://example.test/f.pdf")
        finally:
            self.conn.rollback()

    def test_verify_flags_size_hash_and_missing_blob(self):
        """Full verify against registry rows (uncommitted, rolled back):
        size mismatch, hash mismatch, and missing blob are all flagged."""
        good = self.store.put_bytes(b"good document bytes", conn=self.conn)
        corrupt = self.store.put_bytes(b"x" * 2048, conn=self.conn)
        try:
            # same size, different bytes -> hash mismatch
            self.store.path_for(corrupt.sha256).write_bytes(b"y" * 2048)
            # truncated -> size mismatch
            blob = self.store.path_for(good.sha256)
            blob.write_bytes(blob.read_bytes()[:-1])

            problems = self.store.verify(conn=self.conn)
            joined = "\n".join(problems)
            self.assertIn(f"size mismatch for {good.sha256}", joined)
            self.assertIn(f"hash mismatch for {corrupt.sha256}", joined)

            # missing blob
            blob.unlink()
            problems = self.store.verify(conn=self.conn)
            joined = "\n".join(problems)
            self.assertIn(f"missing blob for {good.sha256}", joined)
        finally:
            self.conn.rollback()

    def test_verify_flags_orphan_blob_with_db(self):
        """With registry access, a blob on disk with no registry row is
        flagged as an orphan (this needs the DB — skip_db mode cannot do it)."""
        result = self.store.put_bytes(b"registered blob", conn=self.conn)
        try:
            orphan = hashlib.sha256(b"unregistered").hexdigest()
            orphan_path = self.store.root / "objects" / orphan[:2] / orphan
            orphan_path.parent.mkdir(parents=True, exist_ok=True)
            orphan_path.write_bytes(b"unregistered")

            problems = self.store.verify(conn=self.conn)
            joined = "\n".join(problems)
            self.assertIn(
                f"orphan blob (not in registry): objects/{orphan[:2]}/{orphan}",
                joined,
            )
            self.assertNotIn(result.sha256, joined)  # registered blob is fine
        finally:
            self.conn.rollback()

    def test_manual_put_and_get_roundtrip_through_cli_semantics(self):
        """Simulates the manual acceptance check: put twice -> one row,
        identical bytes on get."""
        data = bytes(range(256)) * 17
        r1 = self.store.put_bytes(data, conn=self.conn)
        r2 = self.store.put_bytes(data, conn=self.conn)
        try:
            self.assertEqual(r1.sha256, r2.sha256)
            count = self.conn.execute(
                "SELECT count(*) FROM documents.content_objects WHERE sha256=%s",
                (r1.sha256,),
            ).fetchone()[0]
            self.assertEqual(count, 1, "re-put must not duplicate registry row")
            self.assertEqual(self.store.get(r1.sha256), data)
        finally:
            self.conn.rollback()


if __name__ == "__main__":
    unittest.main(verbosity=2)
