from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import ClassVar

from markdown_it import MarkdownIt

SUPPORTED_EXTENSIONS = {".md", ".mdx", ".rst", ".txt", ".html", ".htm"}


@dataclass
class Block:
    kind: str  # "heading" | "para" | "code"
    text: str
    level: int = 0
    lang: str = ""


@dataclass
class ParsedDocument:
    blocks: list[Block] = field(default_factory=list)
    title: str | None = None


class ParseError(Exception):
    pass


_FRONT_MATTER = re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", re.S)
_HTML_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_FORMATTING_CHARS = re.compile(r"[*_`~]")


def _plain(text: str) -> str:
    return _FORMATTING_CHARS.sub("", text).strip()


_MD = MarkdownIt("commonmark")


def parse_markdown(text: str) -> ParsedDocument:
    text = _FRONT_MATTER.sub("", text)
    text = _HTML_BREAK.sub(" ", text)
    lines = text.split("\n")
    tokens = _MD.parse(text)

    headings: dict[int, tuple[int, str]] = {}
    code_spans: list[tuple[int, int]] = []
    for i, tok in enumerate(tokens):
        if tok.map is None:
            continue
        start, end = tok.map
        if tok.type == "heading_open":
            level = int(tok.tag[1])
            plain = ""
            if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                inline = tokens[i + 1]
                if inline.children:
                    plain = "".join(child.content for child in inline.children)
            headings[start] = (level, _plain(plain))
        elif tok.type in ("fence", "code_block"):
            code_spans.append((start, end))

    blocks: list[Block] = []
    title: str | None = None
    code_rows = set()
    for start, end in code_spans:
        code_rows.update(range(start, min(end, len(lines))))

    buffer: list[str] = []

    def flush_para() -> None:
        if buffer:
            para = "\n".join(buffer).strip("\n")
            if para.strip():
                blocks.append(Block("para", para))
            buffer.clear()

    i = 0
    while i < len(lines):
        if i in headings:
            flush_para()
            level, plain_text = headings[i]
            blocks.append(Block("heading", plain_text, level=level))
            if title is None and level == 1 and plain_text:
                title = plain_text
            i += 1
            continue
        if i in code_rows:
            flush_para()
            j = i
            segment: list[str] = []
            while j < len(lines) and j in code_rows:
                segment.append(lines[j])
                j += 1
            lang = ""
            first = segment[0].strip() if segment else ""
            if first.startswith(("```", "~~~")):
                lang = first.lstrip("`~").strip()
                segment = segment[1:]
                if segment and segment[-1].strip().startswith(("```", "~~~")):
                    segment = segment[:-1]
            blocks.append(Block("code", "\n".join(segment), lang=lang))
            i = j
            continue
        if not lines[i].strip():
            flush_para()
            i += 1
            continue
        buffer.append(lines[i])
        i += 1
    flush_para()
    return ParsedDocument(blocks=blocks, title=title)


_RST_ADORN = set("=-~^'\"")
_RST_DIRECTIVE = re.compile(r"\.\.\s+\w+")


def parse_rst(text: str) -> ParsedDocument:
    lines = text.split("\n")
    blocks: list[Block] = []
    title: str | None = None
    buffer: list[str] = []
    i = 0

    def flush_para() -> None:
        if buffer:
            para = "\n".join(buffer).strip("\n")
            if para.strip():
                literal = para.endswith("::")
                body = para[:-1].rstrip() if literal else para
                blocks.append(Block("para", body))
            buffer.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_para()
            i += 1
            continue
        if _RST_DIRECTIVE.match(stripped):
            flush_para()
            i += 1
            while i < len(lines) and lines[i].strip():
                i += 1
            continue
        if (
            i + 1 < len(lines)
            and len(lines[i + 1].strip()) >= max(2, len(stripped))
            and set(lines[i + 1].strip()) <= _RST_ADORN
            and len(set(lines[i + 1].strip())) == 1
        ):
            flush_para()
            adorn = lines[i + 1].strip()[0]
            level = {"=": 1, "-": 2, "~": 3, "^": 4}.get(adorn, 5)
            plain_text = _plain(stripped)
            blocks.append(Block("heading", plain_text, level=level))
            if title is None and level == 1:
                title = plain_text
            i += 2
            continue
        buffer.append(line)
        i += 1
    flush_para()
    return ParsedDocument(blocks=blocks, title=title)


def parse_text(text: str) -> ParsedDocument:
    blocks = [Block("para", para.strip()) for para in re.split(r"\n\s*\n", text) if para.strip()]
    return ParsedDocument(blocks=blocks)


@dataclass
class _HtmlNode:
    tag: str
    children: list[_HtmlNode | str] = field(default_factory=list)


class _HtmlTreeParser(HTMLParser):
    _VOID: ClassVar[set[str]] = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("")
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs) -> None:
        node = _HtmlNode(tag)
        self._stack[-1].children.append(node)
        if tag not in self._VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._stack[-1].children.append(_HtmlNode(tag))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


_HTML_HEADINGS = {f"h{level}": level for level in range(1, 7)}
_HTML_SKIP = {"head", "script", "style", "nav", "footer", "header"}
_HTML_CONTAINERS = {
    "html",
    "body",
    "main",
    "article",
    "section",
    "div",
    "ul",
    "ol",
    "dl",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "blockquote",
}
_HTML_TEXT_BLOCKS = {"p", "li", "dt", "dd", "tr", "figcaption"}


def _html_text(node: _HtmlNode, *, preserve: bool = False) -> str:
    parts: list[str] = []

    def collect(current: _HtmlNode) -> None:
        for child in current.children:
            if isinstance(child, str):
                parts.append(child)
            elif child.tag == "br":
                parts.append("\n" if preserve else " ")
            elif child.tag not in _HTML_SKIP:
                collect(child)

    collect(node)
    text = "".join(parts)
    if preserve:
        return text.strip("\r\n")
    return re.sub(r"\s+", " ", text).strip()


def _html_title(node: _HtmlNode) -> str | None:
    if node.tag == "title":
        return _html_text(node) or None
    for child in node.children:
        if isinstance(child, _HtmlNode):
            title = _html_title(child)
            if title:
                return title
    return None


def _html_row_text(row: _HtmlNode) -> str:
    cells = [
        child
        for child in row.children
        if isinstance(child, _HtmlNode) and child.tag in ("th", "td")
    ]
    if not cells:
        return _html_text(row)
    return " | ".join(filter(None, (_html_text(cell) for cell in cells)))


def parse_html(text: str) -> ParsedDocument:
    parser = _HtmlTreeParser()
    parser.feed(text)
    parser.close()
    blocks: list[Block] = []
    title: str | None = None

    def walk(node: _HtmlNode) -> None:
        nonlocal title
        inline: list[str] = []

        def flush_inline() -> None:
            text = re.sub(r"\s+", " ", "".join(inline)).strip()
            if text:
                blocks.append(Block("para", text))
            inline.clear()

        for child in node.children:
            if isinstance(child, str):
                inline.append(child)
                continue
            tag = child.tag
            if tag in _HTML_SKIP:
                continue
            if tag in _HTML_HEADINGS:
                flush_inline()
                heading = _html_text(child)
                if heading:
                    level = _HTML_HEADINGS[tag]
                    blocks.append(Block("heading", heading, level=level))
                    if level == 1 and title is None:
                        title = heading
            elif tag == "pre":
                flush_inline()
                code = _html_text(child, preserve=True)
                if code.strip():
                    blocks.append(Block("code", code))
            elif tag in _HTML_TEXT_BLOCKS:
                flush_inline()
                value = _html_row_text(child) if tag == "tr" else _html_text(child)
                if value:
                    blocks.append(Block("para", value))
            elif tag in _HTML_CONTAINERS:
                flush_inline()
                walk(child)
            elif tag == "br":
                inline.append(" ")
            else:
                inline.append(f" {_html_text(child)} ")
        flush_inline()

    walk(parser.root)
    return ParsedDocument(blocks=blocks, title=title or _html_title(parser.root))


def parse_file(path, text: str) -> ParsedDocument:
    ext = path.suffix.lower()
    if ext in (".md", ".mdx"):
        return parse_markdown(text)
    if ext == ".rst":
        return parse_rst(text)
    if ext in (".html", ".htm"):
        return parse_html(text)
    if ext == ".txt":
        return parse_text(text)
    raise ParseError(f"unsupported extension: {ext}")
