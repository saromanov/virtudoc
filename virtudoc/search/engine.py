from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

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
) -> SearchResult:
    tokens = tokenize(query)
    result = SearchResult(query=query)
    if not tokens:
        return result

    candidates = _fetch_candidates(conn, _and_expression(tokens), library_id, max_results * 3)
    if not candidates:
        candidates = _fetch_candidates(
            conn, _and_expression(tokens, prefix=True), library_id, max_results * 3
        )
    if not candidates:
        candidates = _fetch_candidates(conn, _or_expression(tokens), library_id, max_results * 3)

    scored: list[tuple[float, sqlite3.Row]] = []
    for position, row in enumerate(candidates):
        base = 1.0 / (position + 1)
        score = base + (TOPIC_BONUS if _topic_boost(row, tokens) else 0.0)
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)

    remaining = token_budget
    for score, row in scored:
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
