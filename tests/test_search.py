from virtudoc.search.engine import library_outline, search, tokenize, trim_to_window


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


def test_trigram_fallback_finds_subword(tmp_path, conn):
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library

    src = tmp_path / "src"
    src.mkdir()
    (src / "auth.md").write_text(
        "# Авторизация\n\nВнутри функции вызывается middlewareAuthWrapper для проверки.\n",
        encoding="utf-8",
    )
    lib_id = add_library(conn, src, "tri")
    index_library(conn, lib_id, Chunker())
    # "auth" is a prefix of the standalone token but a subword of middlewareAuthWrapper
    result = search(conn, "auth", library_id=lib_id)
    assert result.snippets
    assert result.snippets[0].source == "auth.md"


def test_prefix_matching_recalls_inflected_forms(indexed, conn):
    result = search(conn, "обработка конвейер")
    assert result.snippets
    assert result.snippets[0].heading_path == "Руководство → API → Middleware"


def test_search_no_results(indexed, conn):
    result = search(conn, "абракадабра зизигаура")
    assert result.snippets == []
    assert result.total_tokens == 0
    assert result.total_matches == 0
    assert result.has_more is False


def _paginated_library(tmp_path, conn, count=7):
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library

    src = tmp_path / "manydocs"
    src.mkdir()
    for index in range(count):
        (src / f"doc{index}.md").write_text(
            f"# Раздел {index}\n\npaginationmarker уникальный текст {index}.\n",
            encoding="utf-8",
        )
    lib_id = add_library(conn, src, "many")
    index_library(conn, lib_id, Chunker())
    return lib_id


def test_search_pagination_metadata(tmp_path, conn):
    lib_id = _paginated_library(tmp_path, conn)

    first = search(conn, "paginationmarker", library_id=lib_id, max_results=2)
    assert first.total_matches == 7
    assert first.page_chunks == 2
    assert first.has_more is True

    second = search(conn, "paginationmarker", library_id=lib_id, max_results=2, page=2)
    assert second.total_matches == 7
    assert second.page_chunks == 2
    assert second.has_more is True
    assert {s.text for s in first.snippets}.isdisjoint(s.text for s in second.snippets)

    last = search(conn, "paginationmarker", library_id=lib_id, max_results=2, page=4)
    assert last.total_matches == 7
    assert last.page_chunks == 1
    assert last.has_more is False

    beyond = search(conn, "paginationmarker", library_id=lib_id, max_results=2, page=5)
    assert beyond.total_matches == 7
    assert beyond.snippets == []
    assert beyond.has_more is False


def test_search_pagination_with_token_budget_does_not_skip_chunks(tmp_path, conn):
    lib_id = _paginated_library(tmp_path, conn, count=6)

    seen: set[str] = set()
    for page in range(1, 20):
        result = search(
            conn,
            "paginationmarker",
            library_id=lib_id,
            token_budget=20,
            max_results=20,
            page=page,
        )
        assert result.total_matches == 6
        assert seen.isdisjoint(s.text for s in result.snippets)
        seen.update(s.text for s in result.snippets)
        if not result.has_more:
            break
    assert len(seen) == 6


def test_topic_boost_prefers_matching_section(indexed, conn):
    result = search(conn, "middleware")
    paths = [s.heading_path for s in result.snippets]
    assert paths[0] == "Руководство → API → Middleware"


def test_search_max_results_zero_reports_no_more_pages(tmp_path, conn):
    lib_id = _paginated_library(tmp_path, conn)

    result = search(conn, "paginationmarker", library_id=lib_id, max_results=0)
    assert result.snippets == []
    assert result.total_matches == 7
    assert result.has_more is False


def test_search_in_cyrillic_text(indexed, conn):
    result = search(conn, "очередь подписчикам")
    assert result.snippets
    assert result.snippets[0].source == "queue.txt"


def test_trim_to_window_cuts_around_hits(tmp_path):
    filler = "Заполнитель без совпадений. " * 200
    needle = "конвейер обработки событий проходит через middleware слой."
    text = f"{filler}\n{needle}\n{filler}"
    cut = trim_to_window(text, ["конвейер", "middleware"], 50)
    assert "конвейер" in cut
    assert "middleware" in cut
    from virtudoc.tokens import estimate_tokens

    assert estimate_tokens(cut) < estimate_tokens(text) / 5
    assert cut.startswith("… ")
    assert cut.endswith(" …")


def test_trim_to_window_short_text_untouched():
    text = "короткий текст с middleware"
    assert trim_to_window(text, ["middleware"], 400) == text


def test_trim_to_window_disabled():
    text = "хвост " * 500
    assert trim_to_window(text, ["хвост"], 0) == text


def test_search_window_tokens_trims_long_chunks(conn, docs_dir):
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library

    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    full = search(conn, "middleware", library_id=lib_id, window_tokens=0)
    trimmed = search(conn, "middleware", library_id=lib_id, window_tokens=30)
    assert full.snippets and trimmed.snippets
    assert len(trimmed.snippets[0].text) <= len(full.snippets[0].text)
    assert "middleware" in trimmed.snippets[0].text.lower()


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


def test_search_section_filter_accepts_ascii_arrow(indexed, conn):
    result = search(conn, "исключения", section="Руководство -> API -> Ошибки")
    assert result.snippets
    assert all(s.heading_path.startswith("Руководство → API → Ошибки") for s in result.snippets)


def test_language_boost_prefers_code_language(indexed, conn, tmp_path):
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import add_library, index_library

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text(
        "# Очередь\n\nочередь задач планировщик\n\n```python\nworker.start(queue)\n```\n",
        encoding="utf-8",
    )
    (src / "b.md").write_text("# Очередь\n\nОчередь доставка подписчикам.\n", encoding="utf-8")
    lib_id = add_library(conn, src, "langs")
    index_library(conn, lib_id, Chunker())

    plain = search(conn, "очередь", library_id=lib_id)
    boosted = search(conn, "очередь", library_id=lib_id, language="python")
    plain_scores = {s.source: s.score for s in plain.snippets}
    boosted_scores = {s.source: s.score for s in boosted.snippets}
    assert boosted_scores["a.md"] > plain_scores["a.md"]
    assert boosted_scores["b.md"] == plain_scores["b.md"]


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


def test_library_outline_section_filter_accepts_ascii_arrow(indexed, conn):
    tree = library_outline(conn, "local/demo", section="Руководство -> API")
    assert [node.section for node in tree] == ["Руководство"]
    assert [child.section for child in tree[0].children] == ["API"]


def test_library_outline_dict_shape(indexed, conn):
    tree = library_outline(conn, "local/demo")
    payload = tree[0].to_dict()
    assert set(payload) == {"section", "path", "chunk_count", "token_est", "children"}
    assert payload["path"]
