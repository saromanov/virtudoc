from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1

_YO_MAP = str.maketrans({"ё": "е", "Ё": "Е"})


def fts_normalize(text: str) -> str:
    return text.translate(_YO_MAP)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS libraries (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            source_path TEXT NOT NULL,
            description TEXT,
            version TEXT,
            added_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            rel_path TEXT NOT NULL,
            title TEXT,
            content_hash TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            UNIQUE (library_id, rel_path)
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            library_id TEXT NOT NULL,
            heading_path TEXT NOT NULL DEFAULT '',
            ord INTEGER NOT NULL,
            text TEXT NOT NULL,
            token_est INTEGER NOT NULL,
            code_ratio REAL NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_library ON chunks(library_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            text,
            content='chunks',
            content_rowid='id'
        );

        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY,
            library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project_links (
            id INTEGER PRIMARY KEY,
            library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            manifest_path TEXT NOT NULL,
            dep_version TEXT,
            linked_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '1');
        """
    )
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def open_db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    init_schema(conn)
    return conn


def delete_document_chunks(conn: sqlite3.Connection, document_id: int) -> None:
    chunk_ids = [
        row[0]
        for row in conn.execute("SELECT id FROM chunks WHERE document_id = ?", (document_id,))
    ]
    for chunk_id in chunk_ids:
        conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))


def insert_chunk(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    library_id: str,
    heading_path: str,
    ord_: int,
    text: str,
    token_est: int,
    code_ratio: float,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO chunks (document_id, library_id, heading_path, ord, text, token_est, code_ratio)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (document_id, library_id, heading_path, ord_, text, token_est, code_ratio),
    )
    chunk_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)",
        (chunk_id, fts_normalize(text)),
    )
    return chunk_id
