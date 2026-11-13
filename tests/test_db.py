from virtudoc.db import (
    connect,
    delete_document_chunks,
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
    assert version == "1"


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


def test_open_db_idempotent(tmp_path):
    path = tmp_path / "i.db"
    c1 = open_db(path)
    init_schema(c1)
    c1.close()
    c2 = open_db(path)
    c2.close()
