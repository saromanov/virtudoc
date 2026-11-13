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
                lang = first[3:].strip()
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


class _HtmlDocsParser(HTMLParser):
    _HEADINGS: ClassVar[dict[str, int]] = {
        "h1": 1,
        "h2": 2,
        "h3": 3,
        "h4": 4,
        "h5": 5,
        "h6": 6,
    }
    _SKIP: ClassVar[set[str]] = {"script", "style", "nav", "footer", "header"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.title: str | None = None
        self._stack: list[str] = []
        self._buf: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self._HEADINGS or tag in ("p", "pre"):
            self._stack.append(tag)
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth or not self._stack or self._stack[-1] != tag:
            return
        self._stack.pop()
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if not text:
            return
        if tag in self._HEADINGS:
            level = self._HEADINGS[tag]
            blocks_append = Block("heading", text, level=level)
            self.blocks.append(blocks_append)
            if level == 1 and self.title is None:
                self.title = text
        elif tag == "pre":
            self.blocks.append(Block("code", text))
        else:
            self.blocks.append(Block("para", text))

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and self._stack:
            self._buf.append(data)


def parse_html(text: str) -> ParsedDocument:
    parser = _HtmlDocsParser()
    parser.feed(text)
    return ParsedDocument(blocks=parser.blocks, title=parser.title)


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
