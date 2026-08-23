"""Fail-closed safety boundary for database-backed tests.

This module is intentionally imported by production connection/object-store helpers only
when ``STOCKTRACKER_TEST_MODE=1`` is explicitly present.  Merely running under unittest or
pytest never changes production routing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping, Optional

import psycopg

ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_OBJECT_ROOT = ROOT / "data" / "objects"
TEST_MODE_ENV = "STOCKTRACKER_TEST_MODE"
TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"
TEST_OBJECT_ROOT_ENV = "TEST_OBJECT_ROOT"
MARKER_NAME = "stocktracker-disposable-test-environment"
MARKER_VERSION = 1
OBJECT_MARKER_FILE = ".stocktracker-test-object-root"
OBJECT_MARKER_CONTENT = f"{MARKER_NAME}\nversion={MARKER_VERSION}\n"


class TestEnvironmentError(RuntimeError):
    """Test configuration is absent, unsafe, ambiguous, or not positively marked."""


@dataclass(frozen=True)
class DatabaseIdentity:
    system_identifier: str
    database_oid: int
    database_name: str


@dataclass(frozen=True)
class GuardedTestEnvironment:
    database_url: str
    object_root: Path
    identity: DatabaseIdentity
    application_database_url: str

    def activate(self, environ: Optional[MutableMapping[str, str]] = None) -> None:
        """Opt this process into guarded URL-less helpers after validation."""
        target = os.environ if environ is None else environ
        target[TEST_MODE_ENV] = "1"
        target[TEST_DATABASE_URL_ENV] = self.database_url
        target[TEST_OBJECT_ROOT_ENV] = str(self.object_root)

    def subprocess_env(
        self, base: Optional[Mapping[str, str]] = None
    ) -> dict[str, str]:
        """Return an exact child environment for a test worker.

        An inherited ``DATABASE_URL`` is removed so it cannot conflict with the
        repository's authoritative application target.  The child still performs the
        same marker and identity checks before opening its writable connection.
        """
        child = dict(os.environ if base is None else base)
        child.pop("DATABASE_URL", None)
        child[TEST_MODE_ENV] = "1"
        child[TEST_DATABASE_URL_ENV] = self.database_url
        child[TEST_OBJECT_ROOT_ENV] = str(self.object_root)
        return child

    def connect(self, **kwargs) -> psycopg.Connection:
        """Re-verify the complete boundary and return its writable test connection."""
        current = guarded_test_environment()
        if current.identity != self.identity or current.object_root != self.object_root:
            raise TestEnvironmentError("guarded test target changed during the test run")
        return _open_guarded_connection(current, **kwargs)


def test_mode_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    source = os.environ if environ is None else environ
    return source.get(TEST_MODE_ENV) == "1"


def _env_file_value(path: Path, key: str) -> Optional[str]:
    if not path.exists():
        return None
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key and value.strip():
            return value.strip()
    return None


def _application_url(environ: Mapping[str, str], env_path: Path) -> str:
    file_url = _env_file_value(env_path, "DATABASE_URL")
    process_url = environ.get("DATABASE_URL") or None
    if file_url and process_url and file_url != process_url:
        raise TestEnvironmentError("application database configuration is ambiguous")
    url = file_url or process_url
    if not url:
        raise TestEnvironmentError("application database target is unavailable for comparison")
    return url


def _resolved_object_root(environ: Mapping[str, str]) -> Path:
    raw = environ.get(TEST_OBJECT_ROOT_ENV)
    if not raw:
        raise TestEnvironmentError("TEST_OBJECT_ROOT is required")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise TestEnvironmentError("TEST_OBJECT_ROOT must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
        production = PRODUCTION_OBJECT_ROOT.resolve(strict=True)
    except (OSError, RuntimeError):
        raise TestEnvironmentError("test object root cannot be resolved safely") from None
    if not resolved.is_dir():
        raise TestEnvironmentError("TEST_OBJECT_ROOT must name an existing directory")
    if resolved == production or production in resolved.parents:
        raise TestEnvironmentError("test object root must be outside the application object tree")

    marker = resolved / OBJECT_MARKER_FILE
    try:
        if marker.is_symlink() or not marker.is_file():
            raise TestEnvironmentError("test object root lacks its positive marker")
        content = marker.read_text(encoding="utf-8")
    except OSError:
        raise TestEnvironmentError("test object-root marker cannot be read safely") from None
    if content != OBJECT_MARKER_CONTENT:
        raise TestEnvironmentError("test object-root marker is malformed or has the wrong version")
    return resolved


def _identity(conn: psycopg.Connection) -> DatabaseIdentity:
    row = conn.execute(
        """
        SELECT (pg_control_system()).system_identifier::text,
               d.oid::bigint,
               current_database()::text
          FROM pg_database AS d
         WHERE d.datname = current_database()
        """
    ).fetchone()
    if row is None or len(row) != 3:
        raise TestEnvironmentError("database identity could not be established")
    return DatabaseIdentity(str(row[0]), int(row[1]), str(row[2]))


def _inspect_read_only(conn: psycopg.Connection, *, require_marker: bool) -> DatabaseIdentity:
    try:
        conn.execute("BEGIN TRANSACTION READ ONLY")
        mode = conn.execute("SHOW transaction_read_only").fetchone()
        if not mode or mode[0] != "on":
            raise TestEnvironmentError("database safety inspection is not read-only")
        identity = _identity(conn)
        if require_marker:
            rows = conn.execute(
                """
                SELECT marker_name, contract_version,
                       database_name::text, database_oid::bigint
                  FROM stocktracker_test_guard.environment
                """
            ).fetchall()
            expected = (
                MARKER_NAME,
                MARKER_VERSION,
                identity.database_name,
                identity.database_oid,
            )
            if len(rows) != 1 or tuple(rows[0]) != expected:
                raise TestEnvironmentError(
                    "test database marker is absent, ambiguous, stale, or the wrong version"
                )
        conn.rollback()
        return identity
    except TestEnvironmentError:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise TestEnvironmentError("database identity/marker verification failed") from None


def guarded_test_environment(
    *,
    environ: Optional[Mapping[str, str]] = None,
    env_path: Path | str = ROOT / ".env",
    connect_fn=None,
) -> GuardedTestEnvironment:
    """Validate and return the one permitted disposable test environment.

    ``TEST_DATABASE_URL`` is accepted only from the process environment.  The database
    name is never used as evidence.  The connected database must contain the versioned
    marker installed by ``db/test_environment_marker.sql``, and its server/database
    identity must differ from the configured application target.  No URL is included in
    an exception message.
    """
    source = os.environ if environ is None else environ
    test_url = source.get(TEST_DATABASE_URL_ENV)
    if not test_url:
        raise TestEnvironmentError("TEST_DATABASE_URL is required")
    app_url = _application_url(source, Path(env_path))
    if test_url == app_url:
        raise TestEnvironmentError("test target is the configured application database")
    object_root = _resolved_object_root(source)
    connector = psycopg.connect if connect_fn is None else connect_fn

    test_conn = None
    app_conn = None
    try:
        test_conn = connector(test_url, connect_timeout=5)
        test_identity = _inspect_read_only(test_conn, require_marker=True)
        app_conn = connector(app_url, connect_timeout=5)
        app_identity = _inspect_read_only(app_conn, require_marker=False)
        if test_identity == app_identity:
            raise TestEnvironmentError("test target is the configured application database")
    except TestEnvironmentError:
        raise
    except Exception:
        raise TestEnvironmentError("guarded database targets could not be verified") from None
    finally:
        if test_conn is not None:
            test_conn.close()
        if app_conn is not None:
            app_conn.close()

    return GuardedTestEnvironment(test_url, object_root, test_identity, app_url)


def _open_guarded_connection(
    environment: GuardedTestEnvironment, **kwargs
) -> psycopg.Connection:
    """Open and recheck one test connection, returning it only after rollback."""
    kwargs.setdefault("connect_timeout", 5)
    conn = psycopg.connect(environment.database_url, **kwargs)
    try:
        identity = _inspect_read_only(conn, require_marker=True)
        if identity != environment.identity:
            raise TestEnvironmentError("guarded test target identity changed")
        return conn
    except BaseException:
        conn.close()
        raise
