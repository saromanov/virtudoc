from __future__ import annotations

import re

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")

LATIN_CHARS_PER_TOKEN = 4
CYRILLIC_CHARS_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    cyrillic_chars = sum(len(m) for m in _CYRILLIC.findall(text))
    other_chars = len(text) - cyrillic_chars
    return -(-cyrillic_chars // CYRILLIC_CHARS_PER_TOKEN) + -(-other_chars // LATIN_CHARS_PER_TOKEN)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    budget = max_tokens * LATIN_CHARS_PER_TOKEN
    return text[:budget].rstrip() + "…"
