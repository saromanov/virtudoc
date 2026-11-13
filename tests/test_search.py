from virtudoc.search.engine import library_outline, search, tokenize


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


def test_search_section_filter(indexed, conn):
    result = search(conn, "исключения", section="Руководство → API → Ошибки")
    assert result.snippets
    assert all(
        s.heading_path == "Руководство → API → Ошибки"
        or s.heading_path.startswith("Руководство → API → Ошибки →")
        for s in result.snippets
    )
    unfiltered = search(conn, "исключения")
    assert any(s.heading_path.startswith("Руководство → API") for s in unfiltered.snippets)


def test_library_outline_tree(indexed, conn):
    tree = library_outline(conn, "local/demo")
    roots = {node.section: node for node in tree}
    assert "Руководство" in roots
    guide = roots["Руководство"]
    api = next(n for n in guide.children if n.section == "API")
    middleware = next(n for n in api.children if n.section == "Middleware")
    assert middleware.chunk_count >= 1
    assert middleware.token_est > 0
    # parent aggregates children
    assert api.chunk_count >= middleware.chunk_count
    assert guide.token_est >= api.token_est


def test_library_outline_section_filter(indexed, conn):
    tree = library_outline(conn, "local/demo", section="Руководство → API")
    assert len(tree) == 1
    assert tree[0].section == "Руководство"
    assert len(tree[0].children) == 1
    assert tree[0].children[0].section == "API"


def test_library_outline_dict_shape(indexed, conn):
    tree = library_outline(conn, "local/demo")
    payload = tree[0].to_dict()
    assert set(payload) == {"section", "path", "chunk_count", "token_est", "children"}
    assert payload["path"]
