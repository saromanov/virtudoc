from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 6

# Bumped when the shape of `library_symbols` changes, so an existing database
# is re-tokenized once after an upgrade.
SYMBOLS_VERSION = "2"

# RETURNING needs 3.35, the trigram tokenizer needs 3.34.
MIN_SQLITE_VERSION = (3, 35, 0)

_YO_MAP = str.maketrans({"ё": "е", "Ё": "Е"})


def fts_normalize(text: str) -> str:
    return text.translate(_YO_MAP)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def source_fingerprint(conn: sqlite3.Connection, library_id: str) -> str:
    """Fingerprint of the indexed source: the docs an agent reads right now.

    Built from the paths and content hashes of the indexed documents, so it
    works for non-git sources too and never depends on the working tree state.
    """
    rows = conn.execute(
        "SELECT rel_path, content_hash FROM documents WHERE library_id = ? ORDER BY rel_path",
        (library_id,),
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row["rel_path"].encode("utf-8", "surrogatepass"))
        digest.update(b"\x00")
        digest.update(row["content_hash"].encode("utf-8", "surrogatepass"))
        digest.update(b"\x00")
    return digest.hexdigest()[:12]


def refresh_source_fingerprint(conn: sqlite3.Connection, library_id: str) -> str:
    """Recompute and cache the fingerprint in `libraries.source_fingerprint`."""
    fingerprint = source_fingerprint(conn, library_id)
    conn.execute(
        "UPDATE libraries SET source_fingerprint = ? WHERE id = ?",
        (fingerprint, library_id),
    )
    return fingerprint


class UnsupportedSQLiteError(RuntimeError):
    pass


def require_sqlite(version_info: tuple[int, ...] | None = None) -> None:
    info = tuple(version_info or sqlite3.sqlite_version_info[:3])
    info = (*info, 0, 0, 0)[:3]
    if info < MIN_SQLITE_VERSION:
        found = ".".join(str(part) for part in info)
        raise UnsupportedSQLiteError(
            f"SQLite {MIN_SQLITE_VERSION[0]}.{MIN_SQLITE_VERSION[1]}+ is required "
            f"(found {found}); upgrade Python or link a newer SQLite library"
        )


def connect(path: Path | str) -> sqlite3.Connection:
    require_sqlite()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _symbols_current(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'library_symbols_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None and row[0] == SYMBOLS_VERSION


def _schema_ready(conn: sqlite3.Connection) -> bool:
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        return False
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
    }
    required = {
        "libraries",
        "documents",
        "chunks",
        "chunks_fts",
        "chunks_tri",
        "notes",
        "note_anchors",
        "note_symbols",
        "library_symbols",
        "project_links",
        "meta",
    }
    if not required <= names:
        return False
    note_symbol_columns = {row[1] for row in conn.execute("PRAGMA table_info(note_symbols)")}
    if "case_sensitive" not in note_symbol_columns:
        return False
    return _symbols_current(conn)


def _backfill_fts(conn: sqlite3.Connection, table: str) -> None:
    """Populate a recreated external-content FTS table with normalized text."""
    rows = conn.execute("SELECT id, text FROM chunks").fetchall()
    conn.executemany(
        f"INSERT INTO {table} (rowid, text) VALUES (?, ?)",
        [(rowid, fts_normalize(text)) for rowid, text in rows],
    )


def init_schema(conn: sqlite3.Connection) -> None:
    if _schema_ready(conn):
        # Serving opens the database on every tool call; do not write to it.
        return
    fts_existed = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks_fts'").fetchone()
    tri_existed = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks_tri'").fetchone()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS libraries (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            source_path TEXT NOT NULL,
            description TEXT,
            version TEXT,
            channel TEXT NOT NULL DEFAULT 'release',
            added_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source_fingerprint TEXT
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
            code_ratio REAL NOT NULL DEFAULT 0,
            lang TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_library ON chunks(library_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            text,
            content='chunks',
            content_rowid='id'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_tri USING fts5(
            text,
            tokenize='trigram',
            content='chunks',
            content_rowid='id'
        );

        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY,
            library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            source_rev TEXT NOT NULL DEFAULT '',
            library_name TEXT,
            origin_library_id TEXT,
            scope_version TEXT,
            pinned INTEGER NOT NULL DEFAULT 0,
            reviewed_at TEXT,
            anchor_kind TEXT NOT NULL DEFAULT 'library'
        );

        CREATE TABLE IF NOT EXISTS note_symbols (
            note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            case_sensitive INTEGER NOT NULL DEFAULT 1,
            anchored_at TEXT NOT NULL,
            PRIMARY KEY (note_id, symbol)
        );

        CREATE INDEX IF NOT EXISTS idx_note_symbols_symbol ON note_symbols(symbol);

        CREATE TABLE IF NOT EXISTS library_symbols (
            library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            symbol TEXT NOT NULL,
            PRIMARY KEY (library_id, symbol)
        );

        CREATE INDEX IF NOT EXISTS idx_library_symbols_symbol ON library_symbols(symbol);

        CREATE TABLE IF NOT EXISTS note_anchors (
            note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
            library_id TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            anchored_at TEXT NOT NULL,
            PRIMARY KEY (note_id, library_id, rel_path)
        );

        CREATE INDEX IF NOT EXISTS idx_note_anchors_lib ON note_anchors(library_id, rel_path);

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
        """
    )
    library_columns = {row[1] for row in conn.execute("PRAGMA table_info(libraries)")}
    if library_columns and "channel" not in library_columns:
        # Libraries indexed before v5 have no channel: every one of them is a
        # release. Working branches are opt-in and recorded explicitly.
        conn.execute("ALTER TABLE libraries ADD COLUMN channel TEXT NOT NULL DEFAULT 'release'")
    if library_columns and "source_fingerprint" not in library_columns:
        conn.execute("ALTER TABLE libraries ADD COLUMN source_fingerprint TEXT")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    if columns and "lang" not in columns:
        conn.execute("ALTER TABLE chunks ADD COLUMN lang TEXT NOT NULL DEFAULT ''")
    note_columns = {row[1] for row in conn.execute("PRAGMA table_info(notes)")}
    if note_columns and "source_rev" not in note_columns:
        # Notes written before v4 have no recorded revision; staleness is
        # unknown for them, so doctor leaves them alone.
        conn.execute("ALTER TABLE notes ADD COLUMN source_rev TEXT NOT NULL DEFAULT ''")
    symbol_columns = {row[1] for row in conn.execute("PRAGMA table_info(note_symbols)")}
    if symbol_columns and "case_sensitive" not in symbol_columns:
        # Pre-v0.3 notes tracked only the name. Treat their anchors as
        # case-sensitive: a note that used a code span is re-anchored on the
        # next review, and a false "stale" is safer than a false "current".
        conn.execute(
            "ALTER TABLE note_symbols ADD COLUMN case_sensitive INTEGER NOT NULL DEFAULT 1"
        )
    needs_note_scope = bool(note_columns) and "library_name" not in note_columns
    if needs_note_scope:
        # Notes written before v6 belong to the variant they were attached to.
        # The logical library name makes them visible to every variant.
        conn.execute("ALTER TABLE notes ADD COLUMN library_name TEXT")
        conn.execute("ALTER TABLE notes ADD COLUMN origin_library_id TEXT")
        conn.execute("ALTER TABLE notes ADD COLUMN scope_version TEXT")
        conn.execute("ALTER TABLE notes ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE notes ADD COLUMN reviewed_at TEXT")
        conn.execute("ALTER TABLE notes ADD COLUMN anchor_kind TEXT NOT NULL DEFAULT 'library'")
    elif note_columns and "anchor_kind" not in note_columns:
        conn.execute("ALTER TABLE notes ADD COLUMN anchor_kind TEXT NOT NULL DEFAULT 'library'")
    if needs_note_scope:
        # Local imports: notes and coverage import this module for primitives.
        from .coverage import rebuild_library_symbols
        from .notes import backfill_note_scope

        for library in conn.execute(
            "SELECT id FROM libraries WHERE EXISTS "
            "(SELECT 1 FROM documents d WHERE d.library_id = libraries.id)"
        ).fetchall():
            rebuild_library_symbols(conn, library["id"])
        backfill_note_scope(conn)
    if not _symbols_current(conn):
        # First run after an upgrade: the table predates dotted prefixes and
        # exact-case entries. Re-tokenize once, then record the shape.
        from .coverage import rebuild_library_symbols

        for library in conn.execute(
            "SELECT id FROM libraries WHERE EXISTS "
            "(SELECT 1 FROM documents d WHERE d.library_id = libraries.id)"
        ).fetchall():
            rebuild_library_symbols(conn, library["id"])
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('library_symbols_version', ?)",
            (SYMBOLS_VERSION,),
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_name ON notes(library_name)")
    if not fts_existed:
        # the FTS table was just recreated — restore pre-v3 rows
        _backfill_fts(conn, "chunks_fts")
    if not tri_existed:
        # the trigram table was just created — backfill it for pre-v3 rows
        _backfill_fts(conn, "chunks_tri")
    current = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if current is None or current[0] != str(SCHEMA_VERSION):
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def open_db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    init_schema(conn)
    return conn


def _delete_fts_rows(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> None:
    # External-content FTS5 removes the tokens of the supplied text, not the
    # text stored in the content table. The index stores normalized text, so
    # a plain DELETE would leave the "ё" → "е" form behind and a later rowid
    # reuse would surface it as a false match.
    normalized = [(row["id"], fts_normalize(row["text"])) for row in rows]
    conn.executemany(
        "INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', ?, ?)",
        normalized,
    )
    conn.executemany(
        "INSERT INTO chunks_tri (chunks_tri, rowid, text) VALUES ('delete', ?, ?)",
        normalized,
    )


def delete_document_chunks(conn: sqlite3.Connection, document_id: int) -> None:
    rows = conn.execute(
        "SELECT id, text FROM chunks WHERE document_id = ?", (document_id,)
    ).fetchall()
    _delete_fts_rows(conn, rows)
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))


def delete_library_chunks_fts(conn: sqlite3.Connection, library_id: str) -> None:
    rows = conn.execute(
        "SELECT id, text FROM chunks WHERE library_id = ?", (library_id,)
    ).fetchall()
    _delete_fts_rows(conn, rows)


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
    lang: str = "",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO chunks
            (document_id, library_id, heading_path, ord, text, token_est, code_ratio, lang)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (document_id, library_id, heading_path, ord_, text, token_est, code_ratio, lang),
    )
    chunk_id = int(cur.lastrowid)
    normalized = fts_normalize(text)
    conn.execute(
        "INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)",
        (chunk_id, normalized),
    )
    conn.execute(
        "INSERT INTO chunks_tri (rowid, text) VALUES (?, ?)",
        (chunk_id, normalized),
    )
    return chunk_id
