"""Local immutable object store with SHA-256 content registry (Phase 0, ROADMAP
next-action 1).

Storage layout
--------------
Content-addressed blobs live under the gitignored data root::

    <data-root>/objects/<sha256[:2]>/<sha256>

with ``<data-root>`` defaulting to ``<repo>/data``. The two-character prefix
fans directories out so no single directory grows unbounded. The relative path
``objects/<aa>/<sha256>`` is what gets registered in
``documents.content_objects.storage_path`` — never an absolute path, so the
store can be moved or mounted elsewhere without rewriting the registry.

Atomicity & immutability
------------------------
Writes are atomic: bytes go to a temp file (``.tmp-*``) in the *same* target
directory, are flushed + fsync'ed, then ``os.replace``'d onto the final name
(atomic on POSIX), and the containing directory is fsync'ed so the rename
survives a crash. A failure at any point leaves either the old content or no
file at all — never a truncated blob. Files are never rewritten in place;
identical bytes are simply re-registered (idempotent).

DB registry
-----------
Every blob is registered in ``documents.content_objects`` (sha256 UNIQUE,
storage_path, media_type, byte_size, metadata JSONB). Source lineage goes in
``metadata`` (e.g. ``{"source": "nse_rss", "url": ...}``) and document-level
links live in ``documents.record_contents``. The registry insert uses
``ON CONFLICT (sha256) DO NOTHING`` so at-least-once fetchers can re-put
freely.

Usage for fetchers
------------------
Compose the blob write and registry insert in ONE transaction with the rest of
the fetch-run lineage (HLD invariant: bytes persisted + registered before any
parser sees them)::

    from object_store import ObjectStore
    import psycopg

    store = ObjectStore.from_env()          # root=data/objects, DATABASE_URL
    with psycopg.connect(store.db_url) as conn:
        with conn.transaction():
            result = store.put_bytes(
                raw_bytes,
                media_type="application/pdf",
                metadata={"source": "nse_api", "url": url},
                conn=conn,                    # joins the outer transaction
            )
            # ... insert fetch_runs / raw_artifacts rows referencing
            # result.sha256 / result.storage_path in the same transaction ...
        # committed here; a crash anywhere rolls the registry row back and
        # leaves at worst an unreferenced (orphan) blob, which `verify` reports

Integrity check
---------------
``python scripts/object_store.py verify`` walks the registry and the directory
tree and flags: missing blobs, size mismatches, hash mismatches (corruption),
stale temp files, and orphan blobs with no registry row. Run it after any
manual touching of ``data/objects`` or disk incident.

CLI
---
    python scripts/object_store.py put FILE [--media-type T] [--source S]
    python scripts/object_store.py get SHA
    python scripts/object_store.py exists SHA
    python scripts/object_store.py verify [--skip-db]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import psycopg

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = ROOT / "data" / "objects"


class ObjectStoreError(Exception):
    """Base error for object-store failures."""


class ContentMismatchError(ObjectStoreError):
    """Stored bytes do not match their registered SHA-256/size (corruption,
    truncation, or a SHA-256 collision)."""


@dataclass(frozen=True)
class PutResult:
    sha256: str
    storage_path: str   # relative to the store root, as registered in the DB
    byte_size: int
    created: bool       # True if this call wrote the blob, False if it existed


class ObjectStore:
    """Content-addressed blob store backed by ``documents.content_objects``."""

    def __init__(self, root: Path | str = DEFAULT_ROOT, db_url: Optional[str] = None):
        self.root = Path(root)
        self.db_url = db_url

    # ------------------------------------------------------------------ #
    # construction helpers
    # ------------------------------------------------------------------ #

    @classmethod
    def from_env(cls, root: Path | str = DEFAULT_ROOT) -> "ObjectStore":
        """Build a store using DATABASE_URL from the repo ``.env`` (lazily, on
        first DB use)."""
        return cls(root=root, db_url=None)

    def _ensure_db_url(self) -> str:
        if self.db_url is None:
            env: dict[str, str] = {}
            env_file = ROOT / ".env"
            if env_file.exists():
                for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()
            self.db_url = env.get("DATABASE_URL")
            if not self.db_url:
                raise ObjectStoreError(
                    "no db_url given and DATABASE_URL missing from .env"
                )
        return self.db_url

    # ------------------------------------------------------------------ #
    # paths
    # ------------------------------------------------------------------ #

    def rel_path(self, sha256: str) -> str:
        """Registry-canonical relative path for a digest."""
        return f"objects/{sha256[:2]}/{sha256}"

    def path_for(self, sha256: str) -> Path:
        """Absolute filesystem path for a digest."""
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError(f"not a lowercase sha256 hex digest: {sha256!r}")
        return self.root / "objects" / sha256[:2] / sha256

    # ------------------------------------------------------------------ #
    # core API
    # ------------------------------------------------------------------ #

    def put_bytes(
        self,
        data: bytes,
        media_type: str = "application/octet-stream",
        metadata: Optional[dict[str, Any]] = None,
        source: Optional[str] = None,
        conn: Optional[psycopg.Connection] = None,
    ) -> PutResult:
        """Store ``data`` content-addressed and register it.

        Idempotent: re-putting identical bytes returns the same result with
        ``created=False`` and adds no second registry row. If a blob already
        exists at the digest path with a different size, raises
        :class:`ContentMismatchError` (corruption or hash collision) rather
        than overwriting immutable content.

        ``conn``: use the given psycopg connection and do NOT commit (the
        caller owns the transaction — recommended for fetchers so the registry
        row commits atomically with the rest of the run lineage). Without
        ``conn``, a short-lived connection is opened and committed.
        """
        sha256 = hashlib.sha256(data).hexdigest()
        path = self.path_for(sha256)
        created = False

        if path.exists():
            actual_size = path.stat().st_size
            if actual_size != len(data):
                raise ContentMismatchError(
                    f"blob {sha256} exists with size {actual_size} but re-put "
                    f"has {len(data)} bytes; refusing to overwrite immutable "
                    f"content ({path})"
                )
        else:
            self._atomic_write(path, data)
            created = True

        registered_path = self.rel_path(sha256)
        meta = dict(metadata or {})
        if source is not None:
            if "source" in meta and meta["source"] != source:
                raise ValueError("source conflicts with metadata['source']")
            meta["source"] = source

        self._register(sha256, registered_path, media_type, len(data), meta, conn)
        return PutResult(sha256, registered_path, len(data), created)

    def get(self, sha256: str, verify: bool = True) -> bytes:
        """Return blob bytes. With ``verify=True`` (default) the SHA-256 of the
        returned bytes is checked, raising :class:`ContentMismatchError` on
        corruption."""
        path = self.path_for(sha256)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise ObjectStoreError(f"blob {sha256} not found at {path}") from None
        if verify and hashlib.sha256(data).hexdigest() != sha256:
            raise ContentMismatchError(
                f"blob {sha256} is corrupt: content hash does not match ({path})"
            )
        return data

    def exists(self, sha256: str) -> bool:
        """True if the blob is present on disk."""
        return self.path_for(sha256).exists()

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """Temp file + fsync + atomic rename + directory fsync."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            raise
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _register(
        self,
        sha256: str,
        storage_path: str,
        media_type: str,
        byte_size: int,
        metadata: dict[str, Any],
        conn: Optional[psycopg.Connection],
    ) -> None:
        sql = """
            INSERT INTO documents.content_objects
                (sha256, storage_path, media_type, byte_size, metadata)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (sha256) DO NOTHING
        """
        params = (sha256, storage_path, media_type, byte_size, json.dumps(metadata))
        if conn is not None:
            conn.execute(sql, params)
            return
        with psycopg.connect(self._ensure_db_url()) as own:
            with own.transaction():
                own.execute(sql, params)

    # ------------------------------------------------------------------ #
    # integrity check
    # ------------------------------------------------------------------ #

    def verify(self, conn: Optional[psycopg.Connection] = None,
               check_orphans: bool = True,
               skip_db: bool = False) -> list[str]:
        """Full integrity check; returns a list of human-readable problems
        (empty list = healthy). Flags:

        * registry rows whose blob file is missing
        * blobs whose size differs from ``byte_size``
        * blobs whose content hash differs from ``sha256`` (corruption)
        * stale ``.tmp-*`` temp files
        * orphan blobs present on disk with no registry row (needs DB;
          skipped with ``skip_db=True``)
        """
        problems: list[str] = []

        rows: list[tuple[str, str, int]] = []
        if not skip_db:
            if conn is not None:
                rows = conn.execute(
                    "SELECT sha256, storage_path, byte_size FROM documents.content_objects"
                ).fetchall()
            else:
                with psycopg.connect(self._ensure_db_url()) as own:
                    rows = own.execute(
                        "SELECT sha256, storage_path, byte_size FROM documents.content_objects"
                    ).fetchall()

        registered_rel = set()
        for sha256, storage_path, byte_size in rows:
            registered_rel.add(storage_path)
            path = self.root / storage_path
            if not path.is_file():
                problems.append(f"missing blob for {sha256}: {path}")
                continue
            actual_size = path.stat().st_size
            if actual_size != byte_size:
                problems.append(
                    f"size mismatch for {sha256}: registered {byte_size}, "
                    f"on disk {actual_size}"
                )
                continue  # hash check on wrong bytes would just duplicate
            digest = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
            if digest.hexdigest() != sha256:
                problems.append(f"hash mismatch for {sha256}: content is corrupt")

        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            name = path.name
            if name.startswith(".tmp-"):
                problems.append(f"stale temp file: {rel}")
                continue
            if check_orphans and rel not in registered_rel:
                problems.append(f"orphan blob (not in registry): {rel}")

        return problems


# ---------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------- #

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Immutable object store client")
    parser.add_argument("--root", default=str(DEFAULT_ROOT),
                        help=f"store root (default: {DEFAULT_ROOT})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_put = sub.add_parser("put", help="store a file, print its sha256")
    p_put.add_argument("file")
    p_put.add_argument("--media-type", default="application/octet-stream")
    p_put.add_argument("--source", default=None)

    p_get = sub.add_parser("get", help="write a blob to stdout")
    p_get.add_argument("sha256")
    p_get.add_argument("--no-verify", action="store_true")

    p_exists = sub.add_parser("exists", help="exit 0 if blob exists")
    p_exists.add_argument("sha256")

    p_verify = sub.add_parser("verify", help="full integrity check")
    p_verify.add_argument("--skip-db", action="store_true",
                          help="only scan the directory tree (orphans/stale temps)")

    args = parser.parse_args(argv)
    store = ObjectStore(root=args.root)

    if args.cmd == "put":
        data = Path(args.file).read_bytes()
        result = store.put_bytes(data, media_type=args.media_type,
                                 source=args.source)
        print(json.dumps({"sha256": result.sha256,
                          "storage_path": result.storage_path,
                          "byte_size": result.byte_size,
                          "created": result.created}))
        return 0

    if args.cmd == "get":
        sys.stdout.buffer.write(store.get(args.sha256, verify=not args.no_verify))
        return 0

    if args.cmd == "exists":
        found = store.exists(args.sha256)
        print(found)
        return 0 if found else 1

    if args.cmd == "verify":
        if args.skip_db:
            problems = store.verify(skip_db=True)
        else:
            problems = store.verify()
        if problems:
            for p in problems:
                print(f"PROBLEM: {p}", file=sys.stderr)
            return 1
        print("ok: object store consistent")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
