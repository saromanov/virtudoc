import pytest

from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.indexer import (
    LibraryExistsError,
    LibraryNotFoundError,
    add_library,
    delete_library,
    get_library,
    index_library,
    list_libraries,
    update_all,
)


def test_add_library_indexes_all_files(conn, docs_dir):
    lib_id = add_library(conn, docs_dir, "demo", version="1.0")
    stats = index_library(conn, lib_id, Chunker())
    assert lib_id == "local/demo"
    assert stats.errors == 0
    assert stats.changed == 3
    doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert doc_count == 3
    assert chunk_count > 3
    lib = get_library(conn, "demo")
    assert lib["version"] == "1.0"


def test_hidden_and_unsupported_files_skipped(conn, docs_dir):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    paths = [row[0] for row in conn.execute("SELECT rel_path FROM documents")]
    assert "style.css" not in paths
    assert not any(p.startswith(".git/") for p in paths)


def test_incremental_reindex_skips_unchanged(conn, docs_dir):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    stats = index_library(conn, lib_id, Chunker())
    assert stats.changed == 0
    assert stats.skipped == 3


def test_modified_file_reindexed(conn, docs_dir):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    (docs_dir / "api.md").write_text(
        "# API\n\n## Новый раздел\n\nСвежий контент для переиндексации.\n",
        encoding="utf-8",
    )
    stats = index_library(conn, lib_id, Chunker())
    assert stats.changed == 1
    assert stats.skipped == 2
    rows = conn.execute(
        "SELECT text FROM chunks WHERE heading_path LIKE '%Новый раздел%'"
    ).fetchall()
    assert rows


def test_deleted_file_removed_from_index(conn, docs_dir):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    (docs_dir / "api.md").unlink()
    stats = index_library(conn, lib_id, Chunker())
    assert stats.removed == 1
    paths = [row[0] for row in conn.execute("SELECT rel_path FROM documents")]
    assert "api.md" not in paths


def test_duplicate_name_rejected(conn, docs_dir):
    add_library(conn, docs_dir, "demo")
    with pytest.raises(LibraryExistsError):
        add_library(conn, docs_dir, "demo")


def test_replace_existing_library(conn, docs_dir):
    add_library(conn, docs_dir, "demo", version="1.0")
    lib_id = add_library(conn, docs_dir, "demo", version="2.0", replace=True)
    index_library(conn, lib_id, Chunker())
    lib = get_library(conn, "demo")
    assert lib["version"] == "2.0"
    assert len(list_libraries(conn)) == 1


def test_invalid_name_rejected(conn, docs_dir):
    with pytest.raises(ValueError):
        add_library(conn, docs_dir, "Плохое Имя")
    with pytest.raises(ValueError):
        add_library(conn, docs_dir, "-bad")


def test_missing_source_rejected(conn, tmp_path):
    with pytest.raises(ValueError):
        add_library(conn, tmp_path / "nope", "demo")


def test_parse_error_does_not_stop_indexing(conn, docs_dir, monkeypatch):
    from virtudoc.indexer import indexer as indexer_module
    from virtudoc.indexer.parsers import ParseError

    original = indexer_module.parse_file
    calls = {"n": 0}

    def flaky(path, text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ParseError("битый файл")
        return original(path, text)

    monkeypatch.setattr(indexer_module, "parse_file", flaky)
    lib_id = add_library(conn, docs_dir, "demo")
    stats = index_library(conn, lib_id, Chunker())
    assert stats.errors == 1
    assert stats.changed == 2


def test_update_all(conn, docs_dir, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "index.md").write_text("# Другое\n\nСодержимое второй библиотеки.\n", encoding="utf-8")
    add_library(conn, docs_dir, "demo")
    add_library(conn, other, "other")
    results = update_all(conn, Chunker())
    assert set(results) == {"local/demo", "local/other"}


def test_delete_library_removes_chunks(conn, docs_dir):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    delete_library(conn, "demo")
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0
    with pytest.raises(LibraryNotFoundError):
        get_library(conn, "demo")
