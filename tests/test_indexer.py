import os
import time

import pytest

from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.indexer import (
    LibraryExistsError,
    LibraryNotFoundError,
    add_and_index_library,
    add_library,
    delete_library,
    get_library,
    index_library,
    list_libraries,
    refresh_if_stale,
    reindex_library,
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


def test_replace_preserves_notes_and_project_link(conn, docs_dir, tmp_path):
    from virtudoc.notes import add_note, list_notes
    from virtudoc.versions import get_link, link_library

    lib_id, _ = add_and_index_library(conn, docs_dir, "demo", chunker=Chunker())
    add_note(conn, lib_id, "Keep this convention")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["demo==1.0"]\n', encoding="utf-8"
    )
    link_library(conn, lib_id, tmp_path)

    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "index.md").write_text("# Replacement\n", encoding="utf-8")
    add_and_index_library(
        conn,
        replacement,
        "demo",
        chunker=Chunker(),
        version="2.0",
        replace=True,
    )

    assert [note.text for note in list_notes(conn, lib_id)] == ["Keep this convention"]
    assert get_link(conn, lib_id)["dep_version"] == "1.0"
    assert get_library(conn, "demo")["version"] == "2.0"


def test_failed_replacement_rolls_back_registration_and_index(
    conn, docs_dir, tmp_path, monkeypatch
):
    from virtudoc.indexer import indexer as indexer_module

    lib_id, _ = add_and_index_library(conn, docs_dir, "demo", chunker=Chunker(), version="1.0")
    original_chunks = conn.execute(
        "SELECT text FROM chunks WHERE library_id = ? ORDER BY id", (lib_id,)
    ).fetchall()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "index.md").write_text(
        "# Must not be committed\n\nReplacement body.\n", encoding="utf-8"
    )

    def fail_insert(*args, **kwargs):
        raise RuntimeError("indexing failed")

    monkeypatch.setattr(indexer_module, "insert_chunk", fail_insert)
    with pytest.raises(RuntimeError, match="indexing failed"):
        add_and_index_library(
            conn,
            replacement,
            "demo",
            chunker=Chunker(),
            version="2.0",
            replace=True,
        )

    lib = get_library(conn, "demo")
    assert lib["version"] == "1.0"
    assert lib["source_path"] == str(docs_dir)
    chunks = conn.execute(
        "SELECT text FROM chunks WHERE library_id = ? ORDER BY id", (lib_id,)
    ).fetchall()
    assert chunks == original_chunks


def test_invalid_name_rejected(conn, docs_dir):
    with pytest.raises(ValueError):
        add_library(conn, docs_dir, "Плохое Имя")
    with pytest.raises(ValueError):
        add_library(conn, docs_dir, "-bad")
    with pytest.raises(ValueError):
        add_library(conn, docs_dir, "demo\n")


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


def test_update_all_continues_after_missing_source(conn, docs_dir, tmp_path):
    import shutil

    other = tmp_path / "other"
    other.mkdir()
    (other / "index.md").write_text("# Другое\n\nСодержимое.\n", encoding="utf-8")
    add_library(conn, docs_dir, "demo")
    add_library(conn, other, "late")
    index_library(conn, "local/demo", Chunker())
    shutil.rmtree(other)
    (docs_dir / "fresh.md").write_text("# Свежий\n\nНовый документ.\n", encoding="utf-8")
    results = update_all(conn, Chunker())
    assert set(results) == {"local/demo", "local/late"}
    assert results["local/demo"].changed == 1
    assert results["local/late"].errors == 1
    assert "source directory unavailable" in results["local/late"].error


def test_delete_library_removes_chunks(conn, docs_dir):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    delete_library(conn, "demo")
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == 0
    with pytest.raises(LibraryNotFoundError):
        get_library(conn, "demo")


def test_reindex_rebuilds_from_scratch(conn, tmp_path):
    big = tmp_path / "big"
    big.mkdir()
    long_text = ("Слово " * 400 + "\n\n") * 3
    (big / "a.md").write_text("# Раздел\n\n" + long_text, encoding="utf-8")
    (big / "b.md").write_text("# Ещё\n\n" + long_text, encoding="utf-8")
    lib_id = add_library(conn, big, "demo")
    index_library(conn, lib_id, Chunker())
    before = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    stats = reindex_library(conn, lib_id, Chunker(target_tokens=60, max_tokens=120))
    assert stats.changed == 2
    after = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert after > before  # smaller chunks -> more chunks
    doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    assert doc_count == 2


def test_reindex_unknown_library(conn):
    with pytest.raises(LibraryNotFoundError):
        reindex_library(conn, "local/nope", Chunker())


def test_failed_reindex_keeps_previous_index(conn, docs_dir, monkeypatch):
    from virtudoc.indexer import indexer as indexer_module

    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    before = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert before > 0

    def boom(*args, **kwargs):
        raise RuntimeError("indexing failed")

    monkeypatch.setattr(indexer_module, "_index_library", boom)
    with pytest.raises(RuntimeError, match="indexing failed"):
        reindex_library(conn, lib_id, Chunker())

    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == before


def _age_sources(docs_dir, seconds: float) -> None:
    """Set every source mtime into the past, simulating 'indexed after edit'."""
    old = time.time() - seconds
    for path in docs_dir.iterdir():
        if path.is_file():
            os.utime(path, (old, old))


def test_refresh_if_stale_detects_modified_file(conn, docs_dir):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    _age_sources(docs_dir, 60)
    assert refresh_if_stale(conn, lib_id, Chunker()) is None  # fresh

    (docs_dir / "guide.md").write_text(
        "# Изменено\n\nНовый контент после индексации.\n", encoding="utf-8"
    )
    stats = refresh_if_stale(conn, lib_id, Chunker())
    assert stats is not None and stats.changed == 1
    _age_sources(docs_dir, 60)
    assert refresh_if_stale(conn, lib_id, Chunker()) is None  # fresh again


def test_refresh_if_stale_detects_deleted_and_added_files(conn, docs_dir):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    _age_sources(docs_dir, 60)
    (docs_dir / "api.md").unlink()
    stats = refresh_if_stale(conn, lib_id, Chunker())
    assert stats is not None and stats.removed == 1
    _age_sources(docs_dir, 60)
    (docs_dir / "extra.md").write_text(
        "# Extra\n\nНовый файл для проверки счётчика.\n", encoding="utf-8"
    )
    stats = refresh_if_stale(conn, lib_id, Chunker())
    assert stats is not None and stats.changed == 1
    _age_sources(docs_dir, 60)
    assert refresh_if_stale(conn, lib_id, Chunker()) is None


def test_refresh_if_stale_unknown_library(conn):
    assert refresh_if_stale(conn, "local/nope", Chunker()) is None


def test_index_unknown_library(conn):
    with pytest.raises(LibraryNotFoundError):
        index_library(conn, "local/nope", Chunker())


def test_index_missing_source_raises(conn, docs_dir):
    import shutil

    lib_id = add_library(conn, docs_dir, "demo")
    shutil.rmtree(docs_dir)
    with pytest.raises(ValueError, match="source directory unavailable"):
        index_library(conn, lib_id, Chunker())


def test_index_reports_skipped_large_files(conn, tmp_path, monkeypatch):
    from virtudoc.indexer import walker

    monkeypatch.setattr(walker, "MAX_FILE_SIZE", 10)
    source = tmp_path / "skips"
    source.mkdir()
    (source / "big.md").write_text("# Big\n\n" + "x" * 100, encoding="utf-8")
    lib_id = add_library(conn, source, "skips")
    stats = index_library(conn, lib_id, Chunker())
    assert stats.skipped_files == ["big.md"]
    assert stats.errors == 0
    assert stats.processed == 0


def test_index_reports_unreadable_file(conn, tmp_path, monkeypatch):
    from virtudoc.indexer import indexer as indexer_module
    from virtudoc.indexer.walker import WalkedFile, WalkResult

    lib_id = add_library(conn, tmp_path, "demo")
    missing = tmp_path / "missing.md"
    monkeypatch.setattr(
        indexer_module,
        "walk_source",
        lambda source: WalkResult(files=[WalkedFile(path=missing, rel_path="missing.md")]),
    )
    stats = index_library(conn, lib_id, Chunker())
    assert stats.errors == 1
    assert stats.processed == 1
    assert stats.changed == 0


def test_index_rolls_back_on_insert_failure(conn, docs_dir, monkeypatch):
    from virtudoc.indexer import indexer as indexer_module

    def fail_insert(*args, **kwargs):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(indexer_module, "insert_chunk", fail_insert)
    lib_id = add_library(conn, docs_dir, "demo")
    with pytest.raises(RuntimeError, match="insert failed"):
        index_library(conn, lib_id, Chunker())
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_begin_index_lock_error_is_wrapped():
    import sqlite3
    import types

    from virtudoc.indexer.indexer import IndexLockError, _begin_index

    def locked(sql):
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(IndexLockError, match="failed to acquire the index lock"):
        _begin_index(types.SimpleNamespace(execute=locked))


def test_refresh_if_stale_missing_source(conn, docs_dir):
    import shutil

    add_library(conn, docs_dir, "demo")
    shutil.rmtree(docs_dir)
    assert refresh_if_stale(conn, "local/demo", Chunker()) is None


def test_refresh_if_stale_handles_invalid_timestamp(conn, docs_dir):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    conn.execute("UPDATE libraries SET updated_at = 'not-a-date' WHERE id = ?", (lib_id,))
    conn.commit()
    assert refresh_if_stale(conn, lib_id, Chunker()) is not None


def test_refresh_if_stale_skips_when_lock_is_held(conn, docs_dir, monkeypatch):
    from virtudoc.indexer import indexer as indexer_module

    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())

    def locked(*args, **kwargs):
        raise indexer_module.IndexLockError("locked")

    (docs_dir / "changed.md").write_text("# New\n\nFresh.\n", encoding="utf-8")
    monkeypatch.setattr(indexer_module, "index_library", locked)
    assert refresh_if_stale(conn, lib_id, Chunker()) is None
    assert indexer_module._safe_index(conn, lib_id, Chunker()) is None


def test_parse_failure_drops_stale_document_and_stops_refresh_loop(conn, docs_dir, monkeypatch):
    from virtudoc.indexer import indexer as indexer_module
    from virtudoc.indexer.indexer import load_index_failures
    from virtudoc.indexer.parsers import ParseError

    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    (docs_dir / "api.md").write_text("# Broken revision\n\nnow invalid\n", encoding="utf-8")

    def fail_parse(path, text):
        raise ParseError("broken")

    monkeypatch.setattr(indexer_module, "parse_file", fail_parse)
    stats = index_library(conn, lib_id, Chunker())

    assert stats.errors == 1
    assert load_index_failures(conn, lib_id) == {"api.md"}
    paths = {row[0] for row in conn.execute("SELECT rel_path FROM documents")}
    assert "api.md" not in paths, "stale content must not stay searchable"
    chunks = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE heading_path LIKE '%Broken revision%'"
    ).fetchone()[0]
    assert chunks == 0
    # 3 walked files == 2 documents + 1 recorded failure: no reindex loop.
    assert refresh_if_stale(conn, lib_id, Chunker()) is None


def test_successful_reindex_clears_failure_record(conn, docs_dir, monkeypatch):
    from virtudoc.indexer import indexer as indexer_module
    from virtudoc.indexer.indexer import load_index_failures
    from virtudoc.indexer.parsers import ParseError

    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    (docs_dir / "api.md").write_text("# Fixed later\n\nbody\n", encoding="utf-8")

    original = indexer_module.parse_file

    def fail_parse(path, text):
        raise ParseError("broken")

    monkeypatch.setattr(indexer_module, "parse_file", fail_parse)
    index_library(conn, lib_id, Chunker())
    assert load_index_failures(conn, lib_id) == {"api.md"}

    monkeypatch.setattr(indexer_module, "parse_file", original)
    index_library(conn, lib_id, Chunker())
    assert load_index_failures(conn, lib_id) == set()


def test_unreadable_file_is_recorded_as_failure(conn, tmp_path, monkeypatch):
    from virtudoc.indexer import indexer as indexer_module
    from virtudoc.indexer.indexer import load_index_failures
    from virtudoc.indexer.walker import WalkedFile, WalkResult

    lib_id = add_library(conn, tmp_path, "demo")
    missing = tmp_path / "missing.md"
    monkeypatch.setattr(
        indexer_module,
        "walk_source",
        lambda source: WalkResult(files=[WalkedFile(path=missing, rel_path="missing.md")]),
    )
    index_library(conn, lib_id, Chunker())
    assert load_index_failures(conn, lib_id) == {"missing.md"}
    assert refresh_if_stale(conn, lib_id, Chunker()) is None


def test_delete_library_removes_failure_record(conn, docs_dir, monkeypatch):
    from virtudoc.indexer import indexer as indexer_module
    from virtudoc.indexer.parsers import ParseError

    lib_id = add_library(conn, docs_dir, "demo")
    monkeypatch.setattr(
        indexer_module,
        "parse_file",
        lambda path, text: (_ for _ in ()).throw(ParseError("broken")),
    )
    index_library(conn, lib_id, Chunker())
    delete_library(conn, "demo")
    keys = conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'index_failures:%'").fetchone()[0]
    assert keys == 0


def test_load_index_failures_ignores_malformed_meta(conn):
    from virtudoc.indexer.indexer import load_index_failures

    conn.execute("INSERT INTO meta (key, value) VALUES ('index_failures:local/demo', 'not-json')")
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value)"
        " VALUES ('index_failures:local/other', '{\"a\": 1}')"
    )
    conn.commit()
    assert load_index_failures(conn, "local/demo") == set()
    assert load_index_failures(conn, "local/other") == set()


def test_read_failure_drops_previously_indexed_document(conn, docs_dir, monkeypatch):
    from virtudoc.indexer import indexer as indexer_module
    from virtudoc.indexer.walker import WalkedFile, WalkResult

    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    missing = docs_dir / "missing.md"
    monkeypatch.setattr(
        indexer_module,
        "walk_source",
        lambda source: WalkResult(files=[WalkedFile(path=missing, rel_path="guide.md")]),
    )
    stats = index_library(conn, lib_id, Chunker())
    assert stats.errors == 1
    paths = {row[0] for row in conn.execute("SELECT rel_path FROM documents")}
    assert "guide.md" not in paths


def test_reindex_rolls_back_on_failure(conn, docs_dir, monkeypatch):
    from virtudoc.indexer import indexer as indexer_module

    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    before = conn.execute(
        "SELECT id, document_id, text FROM chunks WHERE library_id = ? ORDER BY id", (lib_id,)
    ).fetchall()
    documents_before = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def fail_insert(*args, **kwargs):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(indexer_module, "insert_chunk", fail_insert)
    with pytest.raises(RuntimeError, match="insert failed"):
        reindex_library(conn, lib_id, Chunker())

    after = conn.execute(
        "SELECT id, document_id, text FROM chunks WHERE library_id = ? ORDER BY id", (lib_id,)
    ).fetchall()
    assert after == before, "a failed reindex must keep the previous index"
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == documents_before


def test_clear_library_index_commits_by_default(conn, docs_dir):
    from virtudoc.indexer.indexer import clear_library_index

    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    clear_library_index(conn, lib_id)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
