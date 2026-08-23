"""Offline regression tests for the integration-test safety boundary."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import jobs  # noqa: E402
from object_store import ObjectStore, ObjectStoreError  # noqa: E402
from test_environment import (  # noqa: E402
    MARKER_NAME,
    MARKER_VERSION,
    OBJECT_MARKER_CONTENT,
    OBJECT_MARKER_FILE,
    PRODUCTION_OBJECT_ROOT,
    TEST_DATABASE_URL_ENV,
    TEST_MODE_ENV,
    TEST_OBJECT_ROOT_ENV,
    TestEnvironmentError,
    guarded_test_environment,
)


class FakeResult:
    def __init__(self, *, one=None, all_rows=None):
        self._one = one
        self._all = [] if all_rows is None else all_rows

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeConnection:
    def __init__(self, identity, marker_rows=None):
        self.identity = identity
        self.marker_rows = [] if marker_rows is None else marker_rows
        self.closed = False
        self.rolled_back = False

    def execute(self, sql, params=None):
        if sql.startswith("BEGIN"):
            return FakeResult()
        if sql.startswith("SHOW transaction_read_only"):
            return FakeResult(one=("on",))
        if "pg_control_system" in sql:
            return FakeResult(one=self.identity)
        if "stocktracker_test_guard.environment" in sql:
            return FakeResult(all_rows=self.marker_rows)
        raise AssertionError("unexpected safety-boundary query")

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakeConnector:
    def __init__(self, targets):
        self.targets = targets
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        identity, marker_rows = self.targets[url]
        return FakeConnection(identity, marker_rows)


class TestEnvironmentGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.object_root = self.base / "objects"
        self.object_root.mkdir()
        (self.object_root / OBJECT_MARKER_FILE).write_text(
            OBJECT_MARKER_CONTENT, encoding="utf-8"
        )
        self.env_file = self.base / "app.env"
        self.env_file.write_text("DATABASE_URL=application-target\n", encoding="utf-8")
        self.test_identity = ("server-a", 200, "any_disposable_name")
        self.app_identity = ("server-a", 100, "stocktracker")

    def environ(self, **overrides):
        result = {
            TEST_DATABASE_URL_ENV: "test-target",
            TEST_OBJECT_ROOT_ENV: str(self.object_root),
        }
        result.update(overrides)
        return result

    def marker(self, identity=None, *, name=MARKER_NAME, version=MARKER_VERSION):
        identity = self.test_identity if identity is None else identity
        return [(name, version, identity[2], identity[1])]

    def connector(self, *, test_identity=None, marker_rows=None, app_identity=None):
        test_identity = self.test_identity if test_identity is None else test_identity
        app_identity = self.app_identity if app_identity is None else app_identity
        if marker_rows is None:
            marker_rows = self.marker(test_identity)
        return FakeConnector({
            "test-target": (test_identity, marker_rows),
            "application-target": (app_identity, []),
        })

    def guard(self, *, environ=None, connector=None):
        return guarded_test_environment(
            environ=self.environ() if environ is None else environ,
            env_path=self.env_file,
            connect_fn=self.connector() if connector is None else connector,
        )

    def test_accepts_only_positive_marker_and_separate_root(self):
        environment = self.guard()
        self.assertEqual(environment.identity.database_oid, self.test_identity[1])
        self.assertEqual(environment.object_root, self.object_root.resolve())
        self.assertNotEqual(environment.identity.database_oid, self.app_identity[1])

    def test_rejects_application_url_without_connecting(self):
        connector = self.connector()
        with self.assertRaises(TestEnvironmentError):
            self.guard(
                environ=self.environ(TEST_DATABASE_URL="application-target"),
                connector=connector,
            )
        self.assertEqual(connector.calls, [])

    def test_rejects_alias_of_application_database_by_connected_identity(self):
        same = self.app_identity
        connector = self.connector(
            test_identity=same,
            marker_rows=self.marker(same),
            app_identity=same,
        )
        with self.assertRaises(TestEnvironmentError):
            self.guard(connector=connector)

    def test_lookalike_database_name_without_marker_is_rejected(self):
        lookalike = ("server-a", 201, "stocktracker_test_lookalike")
        connector = self.connector(test_identity=lookalike, marker_rows=[])
        with self.assertRaises(TestEnvironmentError):
            self.guard(connector=connector)

    def test_absent_or_wrong_marker_is_rejected(self):
        for rows in (
            [],
            [(MARKER_NAME, MARKER_VERSION + 1,
              self.test_identity[2], self.test_identity[1])],
            [(MARKER_NAME, MARKER_VERSION,
              self.test_identity[2], self.test_identity[1] + 1)],
            self.marker() + self.marker(),
        ):
            with self.subTest(rows=rows):
                with self.assertRaises(TestEnvironmentError):
                    self.guard(connector=self.connector(marker_rows=rows))

    def test_missing_test_url_is_rejected(self):
        with self.assertRaises(TestEnvironmentError):
            self.guard(environ={TEST_OBJECT_ROOT_ENV: str(self.object_root)})

    def test_application_object_root_is_rejected(self):
        with self.assertRaises(TestEnvironmentError):
            self.guard(environ=self.environ(
                TEST_OBJECT_ROOT=str(PRODUCTION_OBJECT_ROOT.resolve())
            ))
        with mock.patch.dict(os.environ, {TEST_MODE_ENV: "1"}, clear=True):
            with self.assertRaises(ObjectStoreError):
                ObjectStore(root=PRODUCTION_OBJECT_ROOT)

    def test_symlink_alias_into_application_object_tree_is_rejected(self):
        alias = self.base / "object-alias"
        alias.symlink_to(PRODUCTION_OBJECT_ROOT.resolve(), target_is_directory=True)
        with self.assertRaises(TestEnvironmentError):
            self.guard(environ=self.environ(TEST_OBJECT_ROOT=str(alias)))

    def test_object_root_requires_exact_positive_marker(self):
        (self.object_root / OBJECT_MARKER_FILE).write_text(
            f"{MARKER_NAME}\nversion=999\n", encoding="utf-8"
        )
        with self.assertRaises(TestEnvironmentError):
            self.guard()

    def test_subprocess_environment_propagates_exact_guarded_settings(self):
        environment = self.guard()
        child = environment.subprocess_env({"DATABASE_URL": "wrong", "KEEP": "yes"})
        self.assertNotIn("DATABASE_URL", child)
        self.assertEqual(child[TEST_MODE_ENV], "1")
        self.assertEqual(child[TEST_DATABASE_URL_ENV], "test-target")
        self.assertEqual(child[TEST_OBJECT_ROOT_ENV], str(self.object_root.resolve()))
        self.assertEqual(child["KEEP"], "yes")

    def test_lost_subprocess_environment_cannot_fall_back_to_application(self):
        with mock.patch.dict(os.environ, {TEST_MODE_ENV: "1"}, clear=True):
            with self.assertRaises(jobs.JobsError):
                jobs.connect()

    def test_test_framework_import_without_explicit_mode_does_not_reroute(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            jobs, "db_url_from_env", return_value="application-target"
        ), mock.patch.object(jobs.psycopg, "connect", return_value="application-conn"):
            self.assertEqual(jobs.connect(), "application-conn")


if __name__ == "__main__":
    unittest.main()
