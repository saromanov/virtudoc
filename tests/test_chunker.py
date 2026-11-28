from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.parsers import parse_markdown
from virtudoc.tokens import estimate_tokens


def make_chunker(target=50, max_tokens=100, overlap=10):
    return Chunker(target_tokens=target, max_tokens=max_tokens, overlap_tokens=overlap)


def para(text):
    return text


def test_chunks_split_by_headings():
    text = "# Документ\n\n## Первый\n\n" + "слово " * 30 + "\n\n## Второй\n\n" + "другое " * 30
    chunks = make_chunker().chunk(parse_markdown(text))
    paths = [c.heading_path for c in chunks]
    assert paths[0] == "Документ → Первый"
    assert "Документ → Второй" in paths
    assert paths == sorted(paths, key=paths.index)


def test_heading_path_nesting():
    text = "# API\n\n## Middleware\n\n" + "текст " * 30 + "\n\n### Ошибки\n\n" + "ещё текст " * 20
    chunks = make_chunker().chunk(parse_markdown(text))
    assert chunks[0].heading_path == "API → Middleware"
    assert chunks[-1].heading_path == "API → Middleware → Ошибки"


def test_target_budget_creates_multiple_chunks():
    body = "\n\n".join(f"Параграф {i}: " + "а" * 120 for i in range(8))
    text = f"# T\n\n## Раздел\n\n{body}\n"
    chunks = make_chunker(target=100, max_tokens=200).chunk(parse_markdown(text))
    assert len(chunks) >= 3
    assert all(c.token_est <= 210 for c in chunks)
    assert [c.ord for c in chunks] == list(range(len(chunks)))


def test_overlap_between_adjacent_chunks():
    body = "\n\n".join(f"Абзац номер {i} " + "x" * 100 for i in range(8))
    text = f"# T\n\n## Раздел\n\n{body}\n"
    chunks = make_chunker(target=100, overlap=10).chunk(parse_markdown(text))
    assert len(chunks) >= 2
    second = chunks[1]
    assert second.text != chunks[0].text


def test_code_block_not_split():
    code = "\n".join(f"line_{i} = {i}" for i in range(40))
    text = f"# T\n\n## Код\n\n```python\n{code}\n```\n"
    chunks = make_chunker(target=30, max_tokens=200).chunk(parse_markdown(text))
    code_chunks = [c for c in chunks if "line_39" in c.text]
    assert len(code_chunks) == 1
    assert "line_0" in code_chunks[0].text


def test_oversized_paragraph_split_with_marker():
    big = "слово " * 400
    text = f"# T\n\n{big}\n"
    chunks = make_chunker(target=100, max_tokens=150).chunk(parse_markdown(text))
    assert len(chunks) >= 2
    assert all(c.token_est <= 160 for c in chunks)
    assert any("(continued)" in c.text for c in chunks[1:])


def test_non_positive_max_tokens_terminates():
    text = "# T\n\n" + "слово " * 20 + "\n"
    chunks = Chunker(target_tokens=10, max_tokens=0, overlap_tokens=0).chunk(parse_markdown(text))
    assert chunks


def test_negative_token_limits_terminate():
    text = "# T\n\n" + "слово " * 20 + "\n"
    chunks = Chunker(target_tokens=-10, max_tokens=-1, overlap_tokens=0).chunk(parse_markdown(text))
    assert chunks


def test_code_ratio():
    code = "\n".join(f"x = {i}" for i in range(30))
    text = f"# T\n\n## Раздел\n\nКороткий текст.\n\n```python\n{code}\n```\n"
    chunks = make_chunker().chunk(parse_markdown(text))
    assert any(c.code_ratio > 0.3 for c in chunks)


def test_token_est_matches_text():
    text = "# T\n\n## Раздел\n\n" + "текст " * 50 + "\n"
    chunks = make_chunker().chunk(parse_markdown(text))
    assert all(c.token_est == estimate_tokens(c.text) for c in chunks)


def test_split_oversized_multiline_text():
    from virtudoc.indexer.chunker import _split_oversized

    text = "а" * 120 + "\n" + "б" * 120
    parts = _split_oversized(text, 40)
    assert len(parts) == 2
    assert parts[0].startswith("а")
    assert parts[1].startswith("(continued)")


def test_overlap_disabled_and_empty_text():
    from virtudoc.indexer.chunker import _overlap_tail

    assert _overlap_tail("", 10) == ""
    assert _overlap_tail("текст", 0) == ""

    text = f"# T\n\n## Раздел\n\n{'слово ' * 60}\n\n{'ещё ' * 60}\n"
    chunks = make_chunker(overlap=0).chunk(parse_markdown(text))
    assert len(chunks) >= 2
    assert all(chunk.text for chunk in chunks)


def test_code_blocks_rendered_with_fences():
    text = "# T\n\n## Код\n\nПример.\n\n```python\nprint(1)\n```\n"
    chunks = make_chunker().chunk(parse_markdown(text))
    code_chunk = next(chunk for chunk in chunks if "print(1)" in chunk.text)
    assert "```python\nprint(1)\n```" in code_chunk.text


def test_rendered_code_fence_outlasts_inner_backticks():
    text = "# T\n\n## Код\n\n````markdown\n```python\nprint(1)\n```\n````\n"
    chunks = make_chunker().chunk(parse_markdown(text))
    code_chunk = next(chunk for chunk in chunks if "print(1)" in chunk.text)
    rendered = code_chunk.text
    assert rendered.startswith("````markdown\n")
    assert rendered.endswith("\n````")
    assert "```python\nprint(1)\n```" in rendered


def test_split_blocks_respects_long_outer_fence():
    from virtudoc.indexer.chunker import _split_blocks

    text = "````text\n```\ncode\n\nmore\n```\n````"
    assert _split_blocks(text) == [text]


def test_overlap_tail_uses_whole_blocks():
    from virtudoc.indexer.chunker import _overlap_tail

    text = "Первый блок.\n\nВторой блок целиком. Второе предложение.\n\nТретий блок."
    assert _overlap_tail(text, 1000) == text
    small = _overlap_tail(text, 5)
    assert text.endswith(small)
    assert small.startswith("Третий блок.")


def test_overlap_tail_keeps_whole_sentences():
    from virtudoc.indexer.chunker import _overlap_tail

    text = "Первый блок.\n\nОдно предложение. Второе предложение. Третье."
    tail = _overlap_tail(text, 8)
    assert text.endswith(tail)
    assert tail.endswith("Третье.")
    words = set(text.split())
    assert all(word in words for word in tail.split())


def test_overlap_tail_does_not_cut_tokens():
    from virtudoc.indexer.chunker import _overlap_tail

    text = " ".join(f"word{index}" for index in range(200))
    tail = _overlap_tail(text, 10)
    assert text.endswith(tail)
    assert all(word in set(text.split()) for word in tail.split())


def test_whitespace_only_document_produces_no_chunks():
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.parsers import Block, ParsedDocument

    doc = ParsedDocument(blocks=[Block("para", "   "), Block("para", "\n\n")])
    assert Chunker().chunk(doc) == []
