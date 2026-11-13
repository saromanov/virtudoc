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


def test_code_ratio():
    code = "\n".join(f"x = {i}" for i in range(30))
    text = f"# T\n\n## Раздел\n\nКороткий текст.\n\n```python\n{code}\n```\n"
    chunks = make_chunker().chunk(parse_markdown(text))
    assert any(c.code_ratio > 0.3 for c in chunks)


def test_token_est_matches_text():
    text = "# T\n\n## Раздел\n\n" + "текст " * 50 + "\n"
    chunks = make_chunker().chunk(parse_markdown(text))
    assert all(c.token_est == estimate_tokens(c.text) for c in chunks)
