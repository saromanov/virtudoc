import sqlite3

import pytest

from virtudoc.db import (
    connect,
    delete_document_chunks,
    delete_library_chunks_fts,
    init_schema,
    insert_chunk,
    open_db,
)


def test_schema_created(conn):
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        )
    }
    assert {
        "libraries",
        "documents",
        "chunks",
        "chunks_fts",
        "notes",
        "project_links",
        "meta",
    } <= tables
    version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    assert version == "6"


_V1_SCHEMA = """
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
"""


def test_migrates_v1_db_adds_lang_column(home, tmp_path):
    path = tmp_path / "index.db"
    old = sqlite3.connect(path)
    old.executescript(_V1_SCHEMA)
    old.execute("PRAGMA user_version=1")
    old.execute(
        "INSERT INTO libraries (id, name, source_path, added_at, updated_at)"
        " VALUES ('local/old', 'old', '/tmp', 't', 't')"
    )
    old.commit()
    old.close()

    migrated = open_db(path)
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(chunks)")}
    assert "lang" in columns
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == 6
    version = migrated.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    assert version == "6"
    migrated.close()

    fresh = open_db(path)
    fresh.close()


def test_migrates_v4_db_adds_library_channel(home, tmp_path):
    path = tmp_path / "index.db"
    old = sqlite3.connect(path)
    old.executescript(_V1_SCHEMA)
    old.execute("PRAGMA user_version=4")
    old.execute(
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, library_id TEXT NOT NULL,"
        " text TEXT NOT NULL, created_at TEXT NOT NULL, source_rev TEXT NOT NULL DEFAULT '')"
    )
    old.execute(
        "INSERT INTO libraries (id, name, source_path, added_at, updated_at)"
        " VALUES ('local/old', 'old', '/tmp', 't', 't')"
    )
    old.commit()
    old.close()

    migrated = open_db(path)
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(libraries)")}
    assert "channel" in columns
    assert migrated.execute("SELECT channel FROM libraries").fetchone()[0] == "release"
    migrated.close()


def test_migrates_v3_db_adds_notes_source_rev(home, tmp_path):
    path = tmp_path / "index.db"
    old = sqlite3.connect(path)
    old.executescript(_V1_SCHEMA)
    old.executescript(
        """
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY,
            library_id TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    old.execute("PRAGMA user_version=3")
    old.execute(
        "INSERT INTO libraries (id, name, source_path, added_at, updated_at)"
        " VALUES ('local/old', 'old', '/tmp', 't', 't')"
    )
    old.execute(
        "INSERT INTO notes (library_id, text, created_at) VALUES ('local/old', 'legacy', 't')"
    )
    old.commit()
    old.close()

    migrated = open_db(path)
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(notes)")}
    assert "source_rev" in columns
    row = migrated.execute("SELECT text, source_rev FROM notes").fetchone()
    assert row["text"] == "legacy"
    assert row["source_rev"] == "", "legacy notes record no revision"
    migrated.close()


def test_trigram_table_backfilled_for_pre_v3_dbs(home, tmp_path):
    path = tmp_path / "index.db"
    conn = open_db(path)
    conn.execute("DROP TABLE chunks_tri")
    conn.execute(
        "INSERT INTO libraries (id, name, source_path, added_at, updated_at)"
        " VALUES ('local/old', 'old', '/tmp', 't', 't')"
    )
    doc_id = conn.execute(
        "INSERT INTO documents (library_id, rel_path, title, content_hash, indexed_at)"
        " VALUES ('local/old', 'a.md', 'T', 'h', 't') RETURNING id"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO chunks"
        " (document_id, library_id, heading_path, ord, text, token_est, code_ratio)"
        " VALUES (?, 'local/old', '', 0, 'Служебный объём middlewareAuth', 5, 0.0)",
        (doc_id,),
    )
    conn.commit()
    conn.close()

    migrated = open_db(path)
    rows = migrated.execute(
        "SELECT rowid FROM chunks_tri WHERE chunks_tri MATCH '\"Auth\"'"
    ).fetchall()
    assert rows
    yo_rows = migrated.execute(
        "SELECT rowid FROM chunks_tri WHERE chunks_tri MATCH '\"объем\"'"
    ).fetchall()
    assert yo_rows, "the backfill must normalize ё like insert_chunk does"
    migrated.close()


def test_wal_mode(tmp_path):
    connection = connect(tmp_path / "x.db")
    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    connection.close()


def _insert_document(conn, lib="local/demo", rel="a.md"):
    cur = conn.execute(
        "INSERT INTO libraries (id, name, source_path, added_at, updated_at)"
        " VALUES (?, 'demo', '/tmp', 't', 't')",
        (lib,),
    )
    cur = conn.execute(
        "INSERT INTO documents (library_id, rel_path, title, content_hash, indexed_at)"
        " VALUES (?, ?, 'T', 'h', 't') RETURNING id",
        (lib, rel),
    )
    return cur.fetchone()[0]


def test_fts_insert_and_search(conn):
    doc_id = _insert_document(conn)
    insert_chunk(
        conn,
        document_id=doc_id,
        library_id="local/demo",
        heading_path="API",
        ord_=0,
        text="Middleware обрабатывает запросы конвейером",
        token_est=10,
        code_ratio=0.0,
    )
    conn.commit()
    rows = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"middleware\"'"
    ).fetchall()
    assert len(rows) == 1


def test_fts_yo_normalization(conn):
    doc_id = _insert_document(conn)
    insert_chunk(
        conn,
        document_id=doc_id,
        library_id="local/demo",
        heading_path="",
        ord_=0,
        text="Объём данных ограничен",
        token_est=5,
        code_ratio=0.0,
    )
    conn.commit()
    rows = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"объем\"'"
    ).fetchall()
    assert len(rows) == 1


def test_delete_document_removes_fts_rows(conn):
    doc_id = _insert_document(conn)
    chunk_id = insert_chunk(
        conn,
        document_id=doc_id,
        library_id="local/demo",
        heading_path="",
        ord_=0,
        text="уникальный термин xyzzy",
        token_est=5,
        code_ratio=0.0,
    )
    conn.commit()
    delete_document_chunks(conn, doc_id)
    conn.commit()
    rows = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"xyzzy\"'"
    ).fetchall()
    assert rows == []
    left = conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE rowid = ?", (chunk_id,)).fetchone()[
        0
    ]
    assert left == 0
    tri_left = conn.execute(
        "SELECT COUNT(*) FROM chunks_tri WHERE rowid = ?", (chunk_id,)
    ).fetchone()[0]
    assert tri_left == 0


def test_delete_document_removes_normalized_fts_terms(conn):
    doc_id = _insert_document(conn)
    insert_chunk(
        conn,
        document_id=doc_id,
        library_id="local/demo",
        heading_path="",
        ord_=0,
        text="Объём уникальный",
        token_est=5,
        code_ratio=0.0,
    )
    conn.commit()
    delete_document_chunks(conn, doc_id)
    conn.commit()
    for table in ("chunks_fts", "chunks_tri"):
        rows = conn.execute(f"SELECT rowid FROM {table} WHERE {table} MATCH '\"объем\"'").fetchall()
        assert rows == [], f"stale normalized term left in {table}"


def test_delete_library_fts_removes_normalized_terms(conn):
    doc_id = _insert_document(conn)
    insert_chunk(
        conn,
        document_id=doc_id,
        library_id="local/demo",
        heading_path="",
        ord_=0,
        text="Объём библиотеки",
        token_est=5,
        code_ratio=0.0,
    )
    conn.commit()
    delete_library_chunks_fts(conn, "local/demo")
    conn.commit()
    rows = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"объем\"'"
    ).fetchall()
    assert rows == []


def test_recreated_fts_table_is_backfilled_with_normalized_text(home, tmp_path):
    path = tmp_path / "recreated-fts.db"
    connection = open_db(path)
    doc_id = _insert_document(connection)
    insert_chunk(
        connection,
        document_id=doc_id,
        library_id="local/demo",
        heading_path="",
        ord_=0,
        text="Объём после пересоздания",
        token_est=5,
        code_ratio=0.0,
    )
    connection.commit()
    connection.execute("DROP TABLE chunks_fts")
    connection.commit()
    connection.close()

    reopened = open_db(path)
    rows = reopened.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH '\"объем\"'"
    ).fetchall()
    assert rows
    reopened.close()


def test_open_db_idempotent(tmp_path):
    path = tmp_path / "i.db"
    c1 = open_db(path)
    init_schema(c1)
    c1.close()
    c2 = open_db(path)
    c2.close()


def test_init_schema_does_not_write_when_schema_is_current(tmp_path):
    connection = open_db(tmp_path / "ro.db")
    connection.execute("PRAGMA query_only=ON")
    init_schema(connection)  # must not attempt any write
    row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row[0] == "6"


def test_init_schema_recreates_a_missing_table(tmp_path):
    connection = open_db(tmp_path / "missing-table.db")
    connection.execute("DROP TABLE notes")
    connection.commit()
    init_schema(connection)
    assert connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'notes'").fetchone()


def test_require_sqlite_rejects_old_versions():
    from virtudoc.db import UnsupportedSQLiteError, require_sqlite

    require_sqlite((3, 35, 0))
    require_sqlite((3, 35))
    require_sqlite((3, 46, 1))
    with pytest.raises(UnsupportedSQLiteError, match=r"SQLite 3\.35"):
        require_sqlite((3, 34, 9))


def test_migrates_v5_db_to_note_anchors(tmp_path):
    import hashlib
    import sqlite3

    from virtudoc.notes import note_statuses

    fingerprint = hashlib.sha256(b"api.md\x00hash-1\x00").hexdigest()[:12]
    path = tmp_path / "v5.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        f"""
        CREATE TABLE libraries (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, source_path TEXT NOT NULL,
            description TEXT, version TEXT, channel TEXT NOT NULL DEFAULT 'release',
            added_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, library_id TEXT NOT NULL, rel_path TEXT NOT NULL,
            title TEXT, content_hash TEXT NOT NULL, indexed_at TEXT NOT NULL,
            UNIQUE (library_id, rel_path)
        );
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY, library_id TEXT NOT NULL, text TEXT NOT NULL,
            created_at TEXT NOT NULL, source_rev TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO libraries VALUES
            ('local/demo', 'demo', '/tmp/docs', NULL, NULL, 'release', 'now', 'now');
        INSERT INTO documents VALUES (1, 'local/demo', 'api.md', 'API', 'hash-1', 'now');
        INSERT INTO notes (library_id, text, created_at, source_rev) VALUES
            ('local/demo', 'Fresh convention.', 'now', '{fingerprint}'),
            ('local/demo', 'Old convention.', 'now', 'deadbeef0000'),
            ('local/demo', 'Legacy convention.', 'now', '');
        """
    )
    raw.commit()
    raw.close()

    conn = open_db(path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"note_anchors", "note_symbols"} <= tables
    columns = {row[1] for row in conn.execute("PRAGMA table_info(notes)")}
    assert {
        "library_name",
        "origin_library_id",
        "scope_version",
        "pinned",
        "reviewed_at",
        "anchor_kind",
    } <= columns

    statuses = {note.text: note for note in note_statuses(conn, "local/demo")}
    assert statuses["Fresh convention."].status == "current"
    assert statuses["Fresh convention."].library_name == "demo"
    assert statuses["Old convention."].status == "stale"
    assert statuses["Old convention."].stale_reason == "library_changed"
    assert statuses["Legacy convention."].status == "unknown"
    assert statuses["Legacy convention."].anchor_kind == "none"

    fingerprint_row = conn.execute(
        "SELECT source_fingerprint FROM libraries WHERE id = 'local/demo'"
    ).fetchone()
    assert fingerprint_row[0] == fingerprint
    conn.close()
