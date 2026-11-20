from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..db import (
    delete_document_chunks,
    delete_library_chunks_fts,
    insert_chunk,
    utc_now,
)
from ..logging_setup import get_logger
from .chunker import Chunker
from .parsers import ParseError, parse_file
from .walker import WalkResult, walk_source

logger = get_logger("indexer")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class LibraryExistsError(Exception):
    pass


class LibraryNotFoundError(Exception):
    pass


class IndexLockError(Exception):
    pass


@dataclass
class IndexStats:
    processed: int = 0
    changed: int = 0
    skipped: int = 0
    removed: int = 0
    errors: int = 0
    skipped_files: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"processed: {self.processed}, reindexed: {self.changed}, "
            f"skipped: {self.skipped}, removed: {self.removed}, "
            f"errors: {self.errors}"
        )


def library_id(name: str) -> str:
    return f"local/{name}"


_FAILURES_META_PREFIX = "index_failures:"


def _failure_key(lib_id: str) -> str:
    return f"{_FAILURES_META_PREFIX}{lib_id}"


def load_index_failures(conn: sqlite3.Connection, lib_id: str) -> set[str]:
    """Files whose last indexing attempt failed to read or parse."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (_failure_key(lib_id),)).fetchone()
    if row is None:
        return set()
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(item) for item in data}


def _store_index_failures(conn: sqlite3.Connection, lib_id: str, failures: set[str]) -> None:
    key = _failure_key(lib_id)
    if failures:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, json.dumps(sorted(failures))),
        )
    else:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))


def _drop_document(conn: sqlite3.Connection, document_id: int) -> None:
    delete_document_chunks(conn, document_id)
    conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))


def validate_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid library name '{name}': use lowercase latin letters, "
            "digits, hyphens and underscores"
        )


def _write_library(
    conn: sqlite3.Connection,
    source: Path,
    name: str,
    *,
    version: str | None = None,
    description: str | None = None,
    replace: bool = False,
) -> str:
    validate_name(name)
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"source directory not found: {source}")
    lib_id = library_id(name)
    row = conn.execute("SELECT id FROM libraries WHERE id = ?", (lib_id,)).fetchone()
    if row and not replace:
        raise LibraryExistsError(f"library '{lib_id}' already exists; use --replace to overwrite")
    now = utc_now()
    if row:
        conn.execute(
            """
            UPDATE libraries
            SET source_path = ?, description = ?, version = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(source), description, version, now, lib_id),
        )
    else:
        conn.execute(
            """
            INSERT INTO libraries
                (id, name, source_path, description, version, added_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (lib_id, name, str(source), description, version, now, now),
        )
    logger.info("registered library %s (%s)", lib_id, source)
    return lib_id


def add_library(
    conn: sqlite3.Connection,
    source: Path,
    name: str,
    *,
    version: str | None = None,
    description: str | None = None,
    replace: bool = False,
) -> str:
    lib_id = _write_library(
        conn,
        source,
        name,
        version=version,
        description=description,
        replace=replace,
    )
    conn.commit()
    return lib_id


def delete_library(conn: sqlite3.Connection, name: str) -> None:
    lib_id = name if name.startswith("local/") else library_id(name)
    delete_library_chunks_fts(conn, lib_id)
    conn.execute("DELETE FROM libraries WHERE id = ?", (lib_id,))
    conn.execute("DELETE FROM meta WHERE key = ?", (_failure_key(lib_id),))
    conn.commit()
    logger.info("deleted library %s", lib_id)


def get_library(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    lib_id = library_id(name) if not name.startswith("local/") else name
    row = conn.execute("SELECT * FROM libraries WHERE id = ?", (lib_id,)).fetchone()
    if row is None:
        raise LibraryNotFoundError(f"library not found: {lib_id}")
    return row


def list_libraries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT l.id, l.name, l.source_path, l.version, l.updated_at,
               (SELECT COUNT(*) FROM documents d WHERE d.library_id = l.id) AS doc_count,
               (SELECT COUNT(*) FROM chunks c WHERE c.library_id = l.id) AS chunk_count,
               (SELECT COALESCE(SUM(LENGTH(c.text)), 0) FROM chunks c
                WHERE c.library_id = l.id) AS index_bytes
        FROM libraries l ORDER BY l.id
        """
    ).fetchall()


def _begin_index(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise IndexLockError(
            "failed to acquire the index lock: another indexing run may be in progress"
        ) from exc


def _index_library(
    conn: sqlite3.Connection,
    lib_id: str,
    chunker: Chunker,
) -> IndexStats:
    stats = IndexStats()
    row = conn.execute("SELECT * FROM libraries WHERE id = ?", (lib_id,)).fetchone()
    if row is None:
        raise LibraryNotFoundError(f"library not found: {lib_id}")
    source = Path(row["source_path"])
    if not source.is_dir():
        raise ValueError(f"source directory unavailable: {source}")

    walked: WalkResult = walk_source(source)
    stats.skipped_files = walked.skipped_large
    for rel_path in walked.skipped_large:
        logger.warning("file over 5 MB, skipped: %s", rel_path)

    seen: set[str] = set()
    failures: set[str] = set()
    for walked_file in walked.files:
        stats.processed += 1
        rel_path = walked_file.rel_path
        seen.add(rel_path)
        doc_row = conn.execute(
            "SELECT id, content_hash FROM documents WHERE library_id = ? AND rel_path = ?",
            (lib_id, rel_path),
        ).fetchone()
        try:
            content = walked_file.path.read_bytes()
        except OSError as exc:
            stats.errors += 1
            failures.add(rel_path)
            logger.error("failed to read %s: %s", rel_path, exc)
            if doc_row:
                _drop_document(conn, doc_row["id"])
            continue
        digest = hashlib.sha1(content).hexdigest()
        if doc_row and doc_row["content_hash"] == digest:
            stats.skipped += 1
            continue
        try:
            text = content.decode("utf-8", errors="replace")
            parsed = parse_file(walked_file.path, text)
            chunks = chunker.chunk(parsed)
        except (ParseError, ValueError, UnicodeError) as exc:
            stats.errors += 1
            failures.add(rel_path)
            logger.error("parse error in %s: %s", rel_path, exc)
            if doc_row:
                # Never keep serving the previous revision of a file that
                # no longer parses; the failure is reported by doctor.
                _drop_document(conn, doc_row["id"])
            continue
        if doc_row:
            delete_document_chunks(conn, doc_row["id"])
        now = utc_now()
        cur = conn.execute(
            """
            INSERT INTO documents (library_id, rel_path, title, content_hash, indexed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (library_id, rel_path)
            DO UPDATE SET title = excluded.title, content_hash = excluded.content_hash,
                          indexed_at = excluded.indexed_at
            RETURNING id
            """,
            (lib_id, rel_path, parsed.title, digest, now),
        )
        document_id = int(cur.fetchone()[0])
        for chunk in chunks:
            insert_chunk(
                conn,
                document_id=document_id,
                library_id=lib_id,
                heading_path=chunk.heading_path,
                ord_=chunk.ord,
                text=chunk.text,
                token_est=chunk.token_est,
                code_ratio=chunk.code_ratio,
                lang=chunk.lang,
            )
        stats.changed += 1
        logger.debug("indexed %s: %d chunks", rel_path, len(chunks))

    stale = conn.execute(
        "SELECT id, rel_path FROM documents WHERE library_id = ?", (lib_id,)
    ).fetchall()
    for doc in stale:
        if doc["rel_path"] not in seen:
            _drop_document(conn, doc["id"])
            stats.removed += 1
            logger.debug("removed from index: %s", doc["rel_path"])

    failures &= seen
    _store_index_failures(conn, lib_id, failures)
    conn.execute("UPDATE libraries SET updated_at = ? WHERE id = ?", (utc_now(), lib_id))
    return stats


def index_library(
    conn: sqlite3.Connection,
    lib_id: str,
    chunker: Chunker,
) -> IndexStats:
    _begin_index(conn)
    try:
        stats = _index_library(conn, lib_id, chunker)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats


def add_and_index_library(
    conn: sqlite3.Connection,
    source: Path,
    name: str,
    *,
    chunker: Chunker,
    version: str | None = None,
    description: str | None = None,
    replace: bool = False,
) -> tuple[str, IndexStats]:
    _begin_index(conn)
    try:
        lib_id = _write_library(
            conn,
            source,
            name,
            version=version,
            description=description,
            replace=replace,
        )
        stats = _index_library(conn, lib_id, chunker)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return lib_id, stats


def _clear_library_index(conn: sqlite3.Connection, lib_id: str) -> None:
    delete_library_chunks_fts(conn, lib_id)
    conn.execute("DELETE FROM documents WHERE library_id = ?", (lib_id,))


def clear_library_index(conn: sqlite3.Connection, lib_id: str) -> None:
    """Drop every indexed document of a library, keeping registration and notes."""
    _clear_library_index(conn, lib_id)
    conn.commit()


def reindex_library(
    conn: sqlite3.Connection,
    lib_id: str,
    chunker: Chunker,
) -> IndexStats:
    """Full rebuild: re-chunk every document, applying current chunk.* settings."""
    row = conn.execute("SELECT id FROM libraries WHERE id = ?", (lib_id,)).fetchone()
    if row is None:
        raise LibraryNotFoundError(f"library not found: {lib_id}")
    _begin_index(conn)
    try:
        _clear_library_index(conn, lib_id)
        stats = _index_library(conn, lib_id, chunker)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats


def refresh_if_stale(
    conn: sqlite3.Connection,
    lib_id: str,
    chunker: Chunker,
) -> IndexStats | None:
    """Reindex the library when the source changed since the last indexing.

    Cheap heuristic (no network, no hashing): a difference in file count
    or a source file modified after the last indexed_at triggers a full
    incremental pass. Returns None when the index is fresh or the lock is
    held by another process — a serving server must not fail on this.
    """
    row = conn.execute("SELECT source_path FROM libraries WHERE id = ?", (lib_id,)).fetchone()
    if row is None:
        return None
    source = Path(row["source_path"])
    if not source.is_dir():
        return None
    indexed_at = conn.execute(
        "SELECT updated_at FROM libraries WHERE id = ?", (lib_id,)
    ).fetchone()[0]
    try:
        indexed_ts = datetime.fromisoformat(indexed_at).timestamp()
    except (TypeError, ValueError):
        indexed_ts = 0.0
    walked: WalkResult = walk_source(source)
    walked_paths = {walked_file.rel_path for walked_file in walked.files}
    failed_now = load_index_failures(conn, lib_id) & walked_paths
    document_count = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE library_id = ?", (lib_id,)
    ).fetchone()[0]
    if len(walked.files) != document_count + len(failed_now):
        return _safe_index(conn, lib_id, chunker)
    latest = max((f.path.stat().st_mtime for f in walked.files if f.path.exists()), default=0.0)
    if latest > indexed_ts:
        return _safe_index(conn, lib_id, chunker)
    return None


def _safe_index(conn: sqlite3.Connection, lib_id: str, chunker: Chunker) -> IndexStats | None:
    try:
        return index_library(conn, lib_id, chunker)
    except IndexLockError:
        logger.debug("stale refresh skipped for %s: index lock held", lib_id)
        return None


def update_all(conn: sqlite3.Connection, chunker: Chunker) -> dict[str, IndexStats]:
    results: dict[str, IndexStats] = {}
    rows = conn.execute("SELECT id FROM libraries ORDER BY id").fetchall()
    for row in rows:
        results[row["id"]] = index_library(conn, row["id"], chunker)
    return results
