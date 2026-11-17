from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..db import fts_normalize
from ..tokens import (
    CYRILLIC_CHARS_PER_TOKEN,
    estimate_tokens,
    truncate_to_tokens,
)

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_LATIN_TOKEN_RE = re.compile(r"[a-z0-9]+")

TOPIC_BONUS = 0.25
LANGUAGE_BONUS = 0.15
# A topic prefix shorter than this matches too many unrelated headings.
MIN_TOPIC_PREFIX = 4


@dataclass
class Snippet:
    heading_path: str
    source: str
    text: str
    score: float
    tokens: int


@dataclass
class SearchResult:
    query: str
    page: int = 1
    snippets: list[Snippet] = field(default_factory=list)
    total_tokens: int = 0
    total_matches: int = 0
    has_more: bool = False

    @property
    def page_chunks(self) -> int:
        return len(self.snippets)


def tokenize(query: str) -> list[str]:
    raw = fts_normalize(query.lower())
    tokens = _TOKEN_RE.findall(raw)
    return [tok for tok in dict.fromkeys(tokens) if tok]


_ENDINGS = (
    "ого",
    "его",
    "ому",
    "ему",
    "ыми",
    "ими",
    "ами",
    "ями",
    "ой",
    "ей",
    "ый",
    "ий",
    "ая",
    "яя",
    "ое",
    "ее",
    "ые",
    "ие",
    "ов",
    "ев",
    "ам",
    "ям",
    "ах",
    "ях",
    "ую",
    "юю",
    "ом",
    "ем",
    "а",
    "я",
    "о",
    "е",
    "ы",
    "и",
    "у",
    "ю",
)


_LATIN_SUFFIXES = (("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", ""))


def _latin_stem(token: str) -> str:
    """Strip only inflectional English suffixes, never arbitrary characters.

    Cutting a fixed number of trailing letters turns unrelated words into
    prefixes of each other (`article` and `art`), so English tokens keep
    their stem unless a known suffix applies.
    """
    if len(token) < 5:
        return token
    for suffix, replacement in _LATIN_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)] + replacement
    return token


def _stem(token: str) -> str:
    """Return the prefix used by the prefix-search cascade stage."""
    if _LATIN_TOKEN_RE.fullmatch(token):
        return _latin_stem(token)
    for ending in _ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            return token[: -len(ending)]
    if len(token) >= 6:
        return token[:-2]
    if len(token) >= 5:
        return token[:-1]
    return token


def _and_expression(tokens: list[str], prefix: bool = False) -> str:
    if not prefix:
        return " AND ".join(f'"{tok}"' for tok in tokens)
    return " AND ".join(f"{_stem(tok)}*" for tok in tokens)


def _or_expression(tokens: list[str]) -> str:
    return " OR ".join(f'"{tok}" OR {_stem(tok)}*' for tok in tokens)


def _tri_expression(tokens: list[str]) -> str | None:
    parts = [f'"{tok}"' for tok in tokens if len(tok) >= 3]
    return " OR ".join(parts) or None


def _fetch_candidates(
    conn: sqlite3.Connection,
    match: str,
    library_id: str | None,
    limit: int | None = None,
    section: str | None = None,
    table: str = "chunks_fts",
) -> list[sqlite3.Row]:
    sql = f"""
        SELECT c.id, c.heading_path, c.text, c.token_est, c.lang, d.rel_path,
               bm25({table}) AS rank
        FROM {table}
        JOIN chunks c ON c.id = {table}.rowid
        JOIN documents d ON d.id = c.document_id
        WHERE {table} MATCH ?
    """
    params: list[object] = [match]
    if library_id:
        sql += " AND c.library_id = ?"
        params.append(library_id)
    if section:
        sql += " AND (c.heading_path = ? OR substr(c.heading_path, 1, length(?) + 3) = ? || ' → ')"
        params.extend([section, section, section])
    sql += " ORDER BY rank"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def _topic_tokens(row: sqlite3.Row) -> set[str]:
    """Word tokens of the chunk's heading path and file name.

    Substring containment is not used: a query token must coincide with a
    whole topic token (or be a long enough prefix of one).
    """
    text = f"{row['heading_path'] or ''} {row['rel_path'] or ''}".lower()
    return set(_TOKEN_RE.findall(fts_normalize(text)))


def _topic_token_matches(token: str, topic: str) -> bool:
    if token == topic:
        return True
    stem = _stem(token)
    if len(stem) >= MIN_TOPIC_PREFIX and topic.startswith(stem):
        return True
    return len(topic) >= MIN_TOPIC_PREFIX and token.startswith(topic)


def _topic_boost(row: sqlite3.Row, tokens: list[str]) -> bool:
    topic_tokens = _topic_tokens(row)
    return any(_topic_token_matches(token, topic) for token in tokens for topic in topic_tokens)


def _find_all(haystack: str, needle: str) -> list[int]:
    if not needle:
        return []
    positions = []
    i = haystack.find(needle)
    while i != -1:
        positions.append(i)
        i = haystack.find(needle, i + 1)
    return positions


def _best_window(text: str, anchors: list[str], window_chars: int) -> tuple[int, int]:
    """Start/end offsets of the text window holding the densest anchor cluster."""
    lower = fts_normalize(text.lower())
    hits = sorted({pos for anchor in anchors for pos in _find_all(lower, anchor)})
    if not hits:
        return 0, min(len(text), window_chars)
    best_index, best_count = 0, 0
    j = 0
    for i in range(len(hits)):
        while j < len(hits) and hits[j] - hits[i] < window_chars:
            j += 1
        if j - i > best_count:
            best_index, best_count = i, j - i
    start = max(0, hits[best_index] - window_chars // 4)
    end = min(len(text), start + window_chars)
    if start > 0:
        cut = text.find("\n", start, min(start + 60, end))
        if cut != -1:
            start = cut + 1
    if end < len(text):
        cut = text.rfind("\n", max(end - 60, start), end)
        if cut > start:
            end = cut
    return start, end


def trim_to_window(text: str, tokens: list[str], window_tokens: int) -> str:
    """Cut a long chunk down to a window around the densest query-term cluster."""
    if window_tokens <= 0 or estimate_tokens(text) <= window_tokens:
        return text
    window_chars = window_tokens * CYRILLIC_CHARS_PER_TOKEN  # worst-case density
    anchors = sorted({form for tok in tokens for form in (tok, _stem(tok))})
    start, end = _best_window(text, anchors, window_chars)
    cut = text[start:end].strip("\n")
    prefix = "… " if start > 0 else ""
    suffix = " …" if end < len(text) else ""
    return f"{prefix}{cut}{suffix}"


def _page_snippets(
    scored: list[tuple[float, sqlite3.Row]],
    tokens: list[str],
    *,
    page: int,
    token_budget: int,
    max_results: int,
    window_tokens: int,
) -> tuple[list[Snippet], bool]:
    """Assemble the requested page and report whether ranked matches remain.

    Pages are continuations of the ranking: a candidate that does not fit the
    remaining budget ends the current page and starts the next one with a
    fresh budget, so budget filtering never silently drops chunks between
    pages.
    """
    if max_results <= 0:
        return [], bool(scored)
    cursor = 0
    snippets: list[Snippet] = []
    for current_page in range(1, page + 1):
        if current_page > 1 and cursor >= len(scored):
            snippets = []
            break
        snippets = []
        remaining = token_budget
        while cursor < len(scored) and len(snippets) < max_results:
            score, row = scored[cursor]
            text = row["text"]
            if window_tokens and row["token_est"] > window_tokens:
                text = trim_to_window(text, tokens, window_tokens)
            est = estimate_tokens(text)
            if est > remaining:
                if snippets:
                    break
                text = truncate_to_tokens(text, remaining)
                est = estimate_tokens(text)
            snippets.append(
                Snippet(
                    heading_path=row["heading_path"],
                    source=row["rel_path"],
                    text=text,
                    score=round(score, 4),
                    tokens=est,
                )
            )
            cursor += 1
            remaining -= est
            if remaining <= 0:
                break
    return snippets, cursor < len(scored)


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    library_id: str | None = None,
    token_budget: int = 5000,
    max_results: int = 20,
    page: int = 1,
    language: str | None = None,
    section: str | None = None,
    window_tokens: int = 0,
) -> SearchResult:
    page = max(page, 1)
    tokens = tokenize(query)
    result = SearchResult(query=query, page=page)
    if not tokens:
        return result

    candidates = _fetch_candidates(conn, _and_expression(tokens), library_id, section=section)
    if not candidates:
        candidates = _fetch_candidates(
            conn, _and_expression(tokens, prefix=True), library_id, section=section
        )
    if not candidates:
        candidates = _fetch_candidates(conn, _or_expression(tokens), library_id, section=section)
    if not candidates:
        trigram_match = _tri_expression(tokens)
        if trigram_match:
            candidates = _fetch_candidates(
                conn,
                trigram_match,
                library_id,
                section=section,
                table="chunks_tri",
            )

    scored: list[tuple[float, sqlite3.Row]] = []
    for row in candidates:
        # FTS5 bm25() returns a negative rank (more negative is better), so
        # its negation is a relevance score: larger means more relevant.
        score = max(0.0, -float(row["rank"]))
        if _topic_boost(row, tokens):
            score *= 1.0 + TOPIC_BONUS
        if language and (row["lang"] or "").lower() == language.lower():
            score *= 1.0 + LANGUAGE_BONUS
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)

    result.total_matches = len(scored)
    result.snippets, result.has_more = _page_snippets(
        scored,
        tokens,
        page=page,
        token_budget=token_budget,
        max_results=max_results,
        window_tokens=window_tokens,
    )
    result.total_tokens = sum(s.tokens for s in result.snippets)
    return result


@dataclass
class LibraryMatch:
    id: str
    name: str
    description: str | None
    chunk_count: int
    updated_at: str
    score: float


def resolve_libraries(
    conn: sqlite3.Connection,
    library_name: str,
    query: str,
    *,
    limit: int = 5,
) -> list[LibraryMatch]:
    rows = conn.execute(
        """
        SELECT l.id, l.name, l.description, l.updated_at,
               (SELECT COUNT(*) FROM chunks c WHERE c.library_id = l.id) AS chunk_count
        FROM libraries l
        """
    ).fetchall()
    if not rows:
        return []

    hits: dict[str, int] = {}
    tokens = tokenize(query)
    if tokens:
        for row in conn.execute(
            """
            SELECT c.library_id, COUNT(*) FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            WHERE chunks_fts MATCH ?
            GROUP BY c.library_id
            """,
            (_or_expression(tokens),),
        ):
            hits[row[0]] = row[1]

    needle = library_name.strip().lower()
    scored: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        name = row["name"].lower()
        if needle and needle == name:
            score = 3.0
        elif needle and (needle in name or name in needle):
            score = 2.0
        else:
            score = difflib.SequenceMatcher(None, needle, name).ratio() if needle else 0.0
        if row["id"] in hits:
            score += min(2.0, hits[row["id"]] / 10)
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)

    matches = [(score, row) for score, row in scored if score >= 0.5 or row["id"] in hits]
    return [
        LibraryMatch(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            chunk_count=row["chunk_count"],
            updated_at=row["updated_at"],
            score=round(score, 4),
        )
        for score, row in matches[:limit]
    ]


@dataclass
class SectionHint:
    section: str
    chunk_count: int
    token_est: int


def top_level_sections(
    conn: sqlite3.Connection, library_id: str, *, limit: int = 20
) -> list[SectionHint]:
    roots: dict[str, SectionHint] = {}
    for row in conn.execute(
        """
        SELECT heading_path, COUNT(*), SUM(token_est) FROM chunks
        WHERE library_id = ? AND heading_path != ''
        GROUP BY heading_path
        """,
        (library_id,),
    ):
        root = row[0].split(" → ")[0].strip()
        hint = roots.setdefault(root, SectionHint(root, 0, 0))
        hint.chunk_count += row[1]
        hint.token_est += row[2] or 0
    return sorted(roots.values(), key=lambda hint: hint.chunk_count, reverse=True)[:limit]


@dataclass
class OutlineNode:
    section: str
    path: str
    chunk_count: int = 0
    token_est: int = 0
    children: list[OutlineNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "path": self.path,
            "chunk_count": self.chunk_count,
            "token_est": self.token_est,
            "children": [child.to_dict() for child in self.children],
        }


def library_outline(
    conn: sqlite3.Connection,
    library_id: str,
    *,
    section: str | None = None,
) -> list[OutlineNode]:
    rows = conn.execute(
        """
        SELECT heading_path, COUNT(*), COALESCE(SUM(token_est), 0)
        FROM chunks WHERE library_id = ?
        GROUP BY heading_path ORDER BY heading_path
        """,
        (library_id,),
    ).fetchall()

    nodes: dict[str, OutlineNode] = {}
    top: list[OutlineNode] = []
    for path, count, tokens in rows:
        if section and path != section and not path.startswith(section + " → "):
            continue
        parts = path.split(" → ") if path else ["(no section)"]
        for index, part in enumerate(parts):
            prefix = " → ".join(parts[: index + 1])
            node = nodes.get(prefix)
            if node is None:
                node = OutlineNode(section=part, path=prefix)
                nodes[prefix] = node
                parent_path = " → ".join(parts[:index])
                parent = nodes.get(parent_path)
                (parent.children if parent else top).append(node)
        node.chunk_count += count
        node.token_est += tokens

    def aggregate(node: OutlineNode) -> tuple[int, int]:
        child_chunks, child_tokens = 0, 0
        for child in node.children:
            chunks, tokens = aggregate(child)
            child_chunks += chunks
            child_tokens += tokens
        node.chunk_count += child_chunks
        node.token_est += child_tokens
        return node.chunk_count, node.token_est

    for node in top:
        aggregate(node)
    return top
