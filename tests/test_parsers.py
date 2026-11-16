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


def test_html_preserves_divs_lists_and_title_fallback():
    doc = parse_html(
        "<html><head><title>Fallback &amp; Guide</title></head><body>"
        "<div>Intro <span>text</span></div>"
        "<ul><li>First</li><li>Second <strong>item</strong></li></ul>"
        "</body></html>"
    )

    assert doc.title == "Fallback & Guide"
    assert [block.text for block in doc.blocks] == [
        "Intro text",
        "First",
        "Second item",
    ]


def test_html_preserves_table_rows():
    doc = parse_html(
        "<table><tr><th>Name</th><th>Value</th></tr>"
        "<tr><td>timeout</td><td>30 seconds</td></tr></table>"
    )

    assert [block.text for block in doc.blocks] == [
        "Name | Value",
        "timeout | 30 seconds",
    ]


def test_html_sphinx_docusaurus_content_without_boilerplate_or_duplicates():
    doc = parse_html(
        "<html><head><title>Ignored when h1 exists</title>"
        "<style>.hidden { display: none; }</style></head><body>"
        "<nav>Navigation links</nav><main><div class='theme-doc-markdown markdown'>"
        "<h1>Guide</h1><div>Before<div><p>Nested paragraph</p></div>After</div>"
        "<pre><code>pip install demo\ndemo run</code></pre>"
        "<ul><li>Usage example</li></ul>"
        "</div></main><script>alert('ignored')</script></body></html>"
    )

    assert doc.title == "Guide"
    assert [(block.kind, block.text) for block in doc.blocks] == [
        ("heading", "Guide"),
        ("para", "Before"),
        ("para", "Nested paragraph"),
        ("para", "After"),
        ("code", "pip install demo\ndemo run"),
        ("para", "Usage example"),
    ]
