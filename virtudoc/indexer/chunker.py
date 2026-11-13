from __future__ import annotations

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


def _split_oversized(text: str, max_tokens: int) -> list[str]:
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
            budget = max_tokens * 3
            final.append(part[:budget])
            part = part[budget:]
        final.append(part)
    marked = [final[0]] + [f"{CONTINUATION_MARKER}\n\n{part}" for part in final[1:]]
    return [p for p in marked if p.strip()]


def _overlap_tail(text: str, overlap_tokens: int) -> str:
    if overlap_tokens <= 0 or not text:
        return ""
    candidate = text[-overlap_tokens * 4 :]
    while len(candidate) > 40 and estimate_tokens(candidate) > overlap_tokens:
        candidate = candidate[len(candidate) // 10 :]
    return candidate.lstrip()


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
            text = "\n\n".join(block.text for block in current_blocks).strip()
            if not text:
                current_blocks = []
                current_tokens = 0
                return
            code_chars = sum(len(block.text) for block in current_blocks if block.kind == "code")
            total_chars = sum(len(block.text) for block in current_blocks)
            chunks.append(
                Chunk(
                    heading_path=current_path,
                    ord=len(chunks),
                    text=text,
                    token_est=estimate_tokens(text),
                    code_ratio=round(code_chars / total_chars, 4) if total_chars else 0.0,
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
            est = estimate_tokens(block.text)
            if current_blocks and current_tokens + est > self.target:
                start_new()
            current_blocks.append(block)
            current_tokens += est
        flush()
        for index, chunk in enumerate(chunks):
            chunk.ord = index
        return chunks
