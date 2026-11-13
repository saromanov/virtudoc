from virtudoc.indexer.parsers import (
    parse_html,
    parse_markdown,
    parse_rst,
    parse_text,
)


def test_markdown_structure():
    doc = parse_markdown(
        "# Title\n\nПараграф один.\n\n## Раздел\n\nПараграф два.\n\n```python\nprint(1)\n```\n"
    )
    kinds = [block.kind for block in doc.blocks]
    assert kinds == ["heading", "para", "heading", "para", "code"]
    assert doc.blocks[0].level == 1
    assert doc.blocks[2].level == 2
    assert doc.title == "Title"
    assert doc.blocks[4].lang == "python"
    assert "print(1)" in doc.blocks[4].text


def test_markdown_front_matter_stripped():
    doc = parse_markdown("---\ntitle: X\n---\n# Заголовок\n\nТекст.\n")
    assert doc.blocks[0].kind == "heading"
    assert "title: X" not in doc.blocks[0].text


def test_markdown_html_breaks_cleaned():
    doc = parse_markdown("# T\n\n| A | B |\n|---|---|\n| x<br/>y | z |\n")
    assert "<br" not in doc.blocks[1].text
    assert "x y" in doc.blocks[1].text


def test_markdown_preserves_original_text():
    body = "текст с *выделением* и `кодом`"
    doc = parse_markdown(f"# T\n\n{body}\n")
    assert doc.blocks[1].text == body


def test_rst_headings():
    doc = parse_rst("Заголовок\n=========\n\nТекст.\n\nРаздел\n------\n\nЕщё текст.\n")
    headings = [b for b in doc.blocks if b.kind == "heading"]
    assert len(headings) == 2
    assert headings[0].level == 1
    assert headings[1].level == 2
    assert doc.title == "Заголовок"


def test_text_paragraphs():
    doc = parse_text("Первый абзац.\n\nВторой абзац.\n")
    assert len(doc.blocks) == 2
    assert all(block.kind == "para" for block in doc.blocks)


def test_html_headings_and_code():
    doc = parse_html("<h1>Документация</h1><p>Описание</p><pre>код</pre><script>var x=1;</script>")
    kinds = [block.kind for block in doc.blocks]
    assert kinds == ["heading", "para", "code"]
    assert doc.blocks[2].text == "код"
    assert doc.title == "Документация"
