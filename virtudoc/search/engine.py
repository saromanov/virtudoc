from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..db import fts_normalize
from ..tokens import estimate_tokens, truncate_to_tokens

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

TOPIC_BONUS = 0.25


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

    @property
    def total_chunks(self) -> int:
        return len(self.snippets)


def tokenize(query: str) -> list[str]:
    raw = fts_normalize(query.lower())
    tokens = [tok.strip('"') for tok in _TOKEN_RE.findall(raw)]
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


def _stem(token: str) -> str:
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


def _fetch_candidates(
    conn: sqlite3.Connection,
    match: str,
    library_id: str | None,
    limit: int,
    section: str | None = None,
) -> list[sqlite3.Row]:
    sql = """
        SELECT c.id, c.heading_path, c.text, c.token_est, d.rel_path,
               bm25(chunks_fts) AS rank
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN documents d ON d.id = c.document_id
        WHERE chunks_fts MATCH ?
    """
    params: list[object] = [match]
    if library_id:
        sql += " AND c.library_id = ?"
        params.append(library_id)
    if section:
        sql += " AND (c.heading_path = ? OR substr(c.heading_path, 1, length(?) + 3) = ? || ' → ')"
        params.extend([section, section, section])
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def _topic_boost(row: sqlite3.Row, tokens: list[str]) -> bool:
    haystacks = (row["heading_path"] or "").lower(), (row["rel_path"] or "").lower()
    return any(token in haystack for token in tokens for haystack in haystacks)


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
) -> SearchResult:
    tokens = tokenize(query)
    result = SearchResult(query=query, page=page)
    if not tokens:
        return result

    candidates = _fetch_candidates(
        conn, _and_expression(tokens), library_id, max_results * 3 * page, section
    )
    if not candidates:
        candidates = _fetch_candidates(
            conn,
            _and_expression(tokens, prefix=True),
            library_id,
            max_results * 3 * page,
            section,
        )
    if not candidates:
        candidates = _fetch_candidates(
            conn, _or_expression(tokens), library_id, max_results * 3 * page, section
        )

    scored: list[tuple[float, sqlite3.Row]] = []
    for position, row in enumerate(candidates):
        base = 1.0 / (position + 1)
        score = base + (TOPIC_BONUS if _topic_boost(row, tokens) else 0.0)
        if language and f"```{language}" in row["text"]:
            score += 0.15
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)

    remaining = token_budget
    offset = (page - 1) * max_results
    for score, row in scored[offset:]:
        if len(result.snippets) >= max_results:
            break
        text = row["text"]
        if row["token_est"] > remaining and result.snippets:
            continue
        if row["token_est"] > remaining:
            text = truncate_to_tokens(text, remaining)
        snippet = Snippet(
            heading_path=row["heading_path"],
            source=row["rel_path"],
            text=text,
            score=round(score, 4),
            tokens=estimate_tokens(text),
        )
        result.snippets.append(snippet)
        remaining -= snippet.tokens
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
