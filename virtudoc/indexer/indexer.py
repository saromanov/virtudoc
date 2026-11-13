from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..db import delete_document_chunks, insert_chunk, utc_now
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


def _check_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid library name '{name}': use lowercase latin letters, "
            "digits, hyphens and underscores"
        )


def add_library(
    conn: sqlite3.Connection,
    source: Path,
    name: str,
    *,
    version: str | None = None,
    description: str | None = None,
    replace: bool = False,
) -> str:
    _check_name(name)
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"source directory not found: {source}")
    lib_id = library_id(name)
    row = conn.execute("SELECT id FROM libraries WHERE id = ?", (lib_id,)).fetchone()
    if row and not replace:
        raise LibraryExistsError(f"library '{lib_id}' already exists; use --replace to overwrite")
    now = utc_now()
    if row:
        delete_library(conn, name)
    conn.execute(
        """
        INSERT INTO libraries (id, name, source_path, description, version, added_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (lib_id, name, str(source), description, version, now, now),
    )
    conn.commit()
    logger.info("registered library %s (%s)", lib_id, source)
    return lib_id


def delete_library(conn: sqlite3.Connection, name: str) -> None:
    lib_id = library_id(name)
    for (chunk_id,) in conn.execute("SELECT id FROM chunks WHERE library_id = ?", (lib_id,)):
        conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
    conn.execute("DELETE FROM libraries WHERE id = ?", (lib_id,))
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


def index_library(
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

    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise IndexLockError(
            "failed to acquire the index lock: another indexing run may be in progress"
        ) from exc

    try:
        seen: set[str] = set()
        for walked_file in walked.files:
            stats.processed += 1
            rel_path = walked_file.rel_path
            seen.add(rel_path)
            try:
                content = walked_file.path.read_bytes()
            except OSError as exc:
                stats.errors += 1
                logger.error("failed to read %s: %s", rel_path, exc)
                continue
            digest = hashlib.sha1(content).hexdigest()
            doc_row = conn.execute(
                "SELECT id, content_hash FROM documents WHERE library_id = ? AND rel_path = ?",
                (lib_id, rel_path),
            ).fetchone()
            if doc_row and doc_row["content_hash"] == digest:
                stats.skipped += 1
                continue
            try:
                text = content.decode("utf-8", errors="replace")
                parsed = parse_file(walked_file.path, text)
                chunks = chunker.chunk(parsed)
            except (ParseError, ValueError, UnicodeError) as exc:
                stats.errors += 1
                logger.error("parse error in %s: %s", rel_path, exc)
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
                )
            stats.changed += 1
            logger.debug("indexed %s: %d chunks", rel_path, len(chunks))

        stale = conn.execute(
            "SELECT id, rel_path FROM documents WHERE library_id = ?", (lib_id,)
        ).fetchall()
        for doc in stale:
            if doc["rel_path"] not in seen:
                delete_document_chunks(conn, doc["id"])
                conn.execute("DELETE FROM documents WHERE id = ?", (doc["id"],))
                stats.removed += 1
                logger.debug("removed from index: %s", doc["rel_path"])

        conn.execute("UPDATE libraries SET updated_at = ? WHERE id = ?", (utc_now(), lib_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats


def update_all(conn: sqlite3.Connection, chunker: Chunker) -> dict[str, IndexStats]:
    results: dict[str, IndexStats] = {}
    rows = conn.execute("SELECT id FROM libraries ORDER BY id").fetchall()
    for row in rows:
        results[row["id"]] = index_library(conn, row["id"], chunker)
    return results
