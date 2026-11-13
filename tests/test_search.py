from virtudoc.search.engine import search, tokenize


def test_tokenize_normalizes_yo():
    assert tokenize("Объём файла") == ["объем", "файла"]


def test_search_finds_relevant_chunk(indexed, conn):
    result = search(conn, "middleware конвейер")
    assert result.snippets
    top = result.snippets[0]
    assert "middleware" in top.text.lower()
    assert top.source == "guide.md"
    assert top.heading_path == "Руководство → API → Middleware"


def test_search_budget_respected(indexed, conn):
    result = search(conn, "middleware", token_budget=30)
    assert result.snippets
    assert result.total_tokens <= 40
    assert len(result.snippets) == 1


def test_search_max_results(indexed, conn):
    result = search(conn, "библиотек", max_results=1)
    assert len(result.snippets) <= 1


def test_search_library_filter(indexed, conn, docs_dir, tmp_path):
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library

    other = tmp_path / "other"
    other.mkdir()
    (other / "x.md").write_text("# X\n\nСовсем другая тема.\n", encoding="utf-8")
    other_id = add_library(conn, other, "other")
    index_library(conn, other_id, Chunker())

    lib = conn.execute("SELECT id FROM libraries WHERE id = 'local/demo'").fetchone()
    result = search(conn, "middleware", library_id=lib["id"])
    assert result.snippets
    assert all(s.source != "x.md" for s in result.snippets)


def test_search_or_fallback(indexed, conn):
    result = search(conn, "middleware nonexistentword")
    assert result.snippets


def test_prefix_matching_recalls_inflected_forms(indexed, conn):
    result = search(conn, "обработка конвейер")
    assert result.snippets
    assert result.snippets[0].heading_path == "Руководство → API → Middleware"


def test_search_no_results(indexed, conn):
    result = search(conn, "абракадабра зизигаура")
    assert result.snippets == []
    assert result.total_tokens == 0


def test_topic_boost_prefers_matching_section(indexed, conn):
    result = search(conn, "middleware")
    paths = [s.heading_path for s in result.snippets]
    assert paths[0] == "Руководство → API → Middleware"


def test_search_in_cyrillic_text(indexed, conn):
    result = search(conn, "очередь подписчикам")
    assert result.snippets
    assert result.snippets[0].source == "queue.txt"
