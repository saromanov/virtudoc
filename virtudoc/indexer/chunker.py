from __future__ import annotations

import re
from dataclasses import dataclass

from ..tokens import estimate_tokens
from .parsers import Block, ParsedDocument

CONTINUATION_MARKER = "(continued)"


@dataclass
class Chunk:
    heading_path: str
    ord: int
    text: str
    token_est: int
    code_ratio: float
    lang: str = ""


def _split_oversized(text: str, max_tokens: int) -> list[str]:
    # A non-positive limit would make the slice empty and loop forever on a
    # misconfigured chunk.max_tokens.
    max_tokens = max(max_tokens, 1)
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if current and estimate_tokens(candidate) > max_tokens:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    final: list[str] = []
    for part in parts:
        while estimate_tokens(part) > max_tokens:
            budget = max(max_tokens * 3, 1)
            final.append(part[:budget])
            part = part[budget:]
        final.append(part)
    marked = [final[0]] + [f"{CONTINUATION_MARKER}\n\n{part}" for part in final[1:]]
    return [p for p in marked if p.strip()]


_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_FENCE_RUN_RE = re.compile(r"`+")


def _fence_closes(line: str, opening: str) -> bool:
    match = _FENCE_RE.match(line)
    if not match or match.group(1)[0] != opening[0]:
        return False
    return len(match.group(1)) >= len(opening)


def _render_block(block: Block) -> str:
    """Render a chunk block as Markdown, restoring fences around code."""
    if block.kind != "code":
        return block.text
    body = block.text.strip("\n")
    longest = max((len(run) for run in _FENCE_RUN_RE.findall(body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{block.lang.strip()}\n{body}\n{fence}"


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def _split_blocks(text: str) -> list[str]:
    """Split rendered chunk text into blank-line-separated blocks, fences intact."""
    blocks: list[str] = []
    current: list[str] = []
    opening: str | None = None
    for line in text.split("\n"):
        if opening is None:
            match = _FENCE_RE.match(line)
            if match:
                opening = match.group(1)
        elif _fence_closes(line, opening):
            opening = None
        if not line.strip() and opening is None:
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _tail_pieces(block: str) -> tuple[list[str], str]:
    if _FENCE_RE.match(block):
        return [block], "\n\n"
    for split, separator in (
        (_SENTENCE_SPLIT.split(block), " "),
        (block.split("\n"), "\n"),
        (block.split(" "), " "),
    ):
        pieces = [piece for piece in split if piece.strip()]
        if len(pieces) > 1:
            return pieces, separator
    return [block], "\n\n"


def _overlap_tail(text: str, overlap_tokens: int) -> str:
    if overlap_tokens <= 0 or not text:
        return ""
    blocks = _split_blocks(text)
    tail: list[str] = []
    used = 0
    while blocks:
        block = blocks.pop()
        est = estimate_tokens(block)
        if used and used + est > overlap_tokens:
            break
        if not used and est > overlap_tokens:
            pieces, separator = _tail_pieces(block)
            collected: list[str] = []
            for piece in reversed(pieces):
                piece_est = estimate_tokens(piece)
                if collected and used + piece_est > overlap_tokens:
                    break
                collected.insert(0, piece)
                used += piece_est
                if used >= overlap_tokens:
                    break
            if collected:
                tail.insert(0, separator.join(collected))
            break
        tail.insert(0, block)
        used += est
        if used >= overlap_tokens:
            break
    return "\n\n".join(tail).strip("\n")


class Chunker:
    def __init__(
        self,
        target_tokens: int = 600,
        max_tokens: int = 1200,
        overlap_tokens: int = 60,
    ) -> None:
        self.target = target_tokens
        self.max_tokens = max_tokens
        self.overlap = overlap_tokens

    def chunk(self, doc: ParsedDocument) -> list[Chunk]:
        chunks: list[Chunk] = []
        headings: dict[int, str] = {}
        current_blocks: list[Block] = []
        current_tokens = 0
        current_path = ""
        prev_text = ""

        def heading_path() -> str:
            return " → ".join(text for level, text in sorted(headings.items()) if text)

        def flush() -> None:
            nonlocal current_blocks, current_tokens, current_path, prev_text
            if not current_blocks:
                return
            rendered = [_render_block(block) for block in current_blocks]
            text = "\n\n".join(rendered).strip()
            if not text:
                current_blocks = []
                current_tokens = 0
                return
            code_chars = sum(
                len(rendered_text)
                for rendered_text, block in zip(rendered, current_blocks)
                if block.kind == "code"
            )
            total_chars = sum(len(rendered_text) for rendered_text in rendered)
            lang = next(
                (block.lang for block in current_blocks if block.kind == "code" and block.lang),
                "",
            )
            chunks.append(
                Chunk(
                    heading_path=current_path,
                    ord=len(chunks),
                    text=text,
                    token_est=estimate_tokens(text),
                    code_ratio=round(code_chars / total_chars, 4) if total_chars else 0.0,
                    lang=lang,
                )
            )
            prev_text = text
            current_blocks = []
            current_tokens = 0

        def start_new() -> None:
            nonlocal current_path, current_tokens
            flush()
            current_path = heading_path()
            tail = _overlap_tail(prev_text, self.overlap)
            if tail:
                current_blocks.append(Block("para", tail))
                current_tokens = estimate_tokens(tail)

        for block in doc.blocks:
            if block.kind == "heading":
                flush()
                headings[block.level] = block.text
                for level in list(headings):
                    if level > block.level:
                        del headings[level]
                current_path = heading_path()
                continue
            if block.kind == "para" and estimate_tokens(block.text) > self.max_tokens:
                part_budget = max(self.target, self.max_tokens - self.overlap)
                for part in _split_oversized(block.text, part_budget):
                    sub = Block("para", part)
                    if current_blocks and (current_tokens + estimate_tokens(part) > self.target):
                        start_new()
                    current_blocks.append(sub)
                    current_tokens += estimate_tokens(part)
                continue
            est = estimate_tokens(_render_block(block))
            if current_blocks and current_tokens + est > self.target:
                start_new()
            current_blocks.append(block)
            current_tokens += est
        flush()
        for index, chunk in enumerate(chunks):
            chunk.ord = index
        return chunks
