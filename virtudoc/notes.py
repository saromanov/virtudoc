from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .db import fts_normalize, utc_now
from .search.engine import _stem, tokenize
from .tokens import estimate_tokens, truncate_to_tokens


@dataclass
class Note:
    id: int
    text: str
    created_at: str
    score: int = 0


def add_note(conn: sqlite3.Connection, library_id: str, text: str) -> int:
    cur = conn.execute(
        "INSERT INTO notes (library_id, text, created_at) VALUES (?, ?, ?)",
        (library_id, text.strip(), utc_now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_notes(conn: sqlite3.Connection, library_id: str) -> list[Note]:
    rows = conn.execute(
        "SELECT id, text, created_at FROM notes WHERE library_id = ? ORDER BY id",
        (library_id,),
    ).fetchall()
    return [Note(id=row["id"], text=row["text"], created_at=row["created_at"]) for row in rows]


def remove_note(conn: sqlite3.Connection, library_id: str, note_id: int) -> bool:
    cur = conn.execute("DELETE FROM notes WHERE library_id = ? AND id = ?", (library_id, note_id))
    conn.commit()
    return cur.rowcount > 0


def relevant_notes(
    conn: sqlite3.Connection,
    library_id: str,
    query: str,
    *,
    budget_tokens: int = 800,
) -> list[Note]:
    tokens = tokenize(query)
    if not tokens:
        return []
    rows = conn.execute(
        "SELECT id, text, created_at FROM notes WHERE library_id = ?",
        (library_id,),
    ).fetchall()
    scored: list[tuple[int, sqlite3.Row]] = []
    for row in rows:
        haystack = fts_normalize(row["text"].lower())
        matched = sum(1 for tok in tokens if tok in haystack or _stem(tok) in haystack)
        if matched:
            scored.append((matched, row))
    scored.sort(key=lambda item: (-item[0], -item[1]["id"]))

    notes: list[Note] = []
    used = 0
    for matched, row in scored:
        text = row["text"]
        est = estimate_tokens(text)
        if used + est > budget_tokens and notes:
            continue
        if used + est > budget_tokens:
            text = truncate_to_tokens(text, max(budget_tokens - used, 50))
            est = estimate_tokens(text)
        notes.append(Note(id=row["id"], text=text, created_at=row["created_at"], score=matched))
        used += est
    return notes
