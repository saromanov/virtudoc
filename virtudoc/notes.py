from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from .db import fts_normalize, utc_now
from .search.engine import _TOKEN_RE, MIN_TOPIC_PREFIX, _stem, tokenize
from .tokens import estimate_tokens, truncate_to_tokens


def _note_token_matches(token: str, words: list[str]) -> bool:
    """Whole-word match, allowing inflection only for meaningful stems."""
    stem = _stem(token)
    for word in words:
        if word == token or word == stem:
            return True
        if len(stem) >= MIN_TOPIC_PREFIX and word.startswith(stem):
            return True
    return False


@dataclass
class Note:
    id: int
    text: str
    created_at: str
    source_rev: str = ""
    score: int = 0


def source_revision(conn: sqlite3.Connection, library_id: str) -> str:
    """Fingerprint of the indexed source: the docs an agent reads right now.

    Built from the paths and content hashes of the indexed documents, so it
    works for non-git sources too and never depends on the working tree state.
    """
    rows = conn.execute(
        "SELECT rel_path, content_hash FROM documents WHERE library_id = ? ORDER BY rel_path",
        (library_id,),
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row["rel_path"].encode("utf-8", "surrogatepass"))
        digest.update(b"\x00")
        digest.update(row["content_hash"].encode("utf-8", "surrogatepass"))
        digest.update(b"\x00")
    return digest.hexdigest()[:12]


def add_note(
    conn: sqlite3.Connection, library_id: str, text: str, source_rev: str | None = None
) -> int:
    """Attach a note, recording the source revision it was written against."""
    if source_rev is None:
        source_rev = source_revision(conn, library_id)
    cur = conn.execute(
        "INSERT INTO notes (library_id, text, created_at, source_rev) VALUES (?, ?, ?, ?)",
        (library_id, text.strip(), utc_now(), source_rev),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_notes(conn: sqlite3.Connection, library_id: str) -> list[Note]:
    rows = conn.execute(
        "SELECT id, text, created_at, source_rev FROM notes WHERE library_id = ? ORDER BY id",
        (library_id,),
    ).fetchall()
    return [
        Note(
            id=row["id"],
            text=row["text"],
            created_at=row["created_at"],
            source_rev=row["source_rev"],
        )
        for row in rows
    ]


def stale_notes(conn: sqlite3.Connection, library_id: str) -> list[Note]:
    """Notes written against a different revision of the indexed source.

    Notes without a recorded revision (written before v4) are skipped: their
    staleness cannot be determined.
    """
    current = source_revision(conn, library_id)
    return [
        note
        for note in list_notes(conn, library_id)
        if note.source_rev and note.source_rev != current
    ]


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
        words = _TOKEN_RE.findall(haystack)
        matched = sum(1 for tok in tokens if _note_token_matches(tok, words))
        if matched:
            scored.append((matched, row))
    scored.sort(key=lambda item: (-item[0], -item[1]["id"]))

    notes: list[Note] = []
    used = 0
    for matched, row in scored:
        remaining = budget_tokens - used
        if remaining <= 0:
            break
        text = row["text"]
        est = estimate_tokens(text)
        if est > remaining:
            text = truncate_to_tokens(text, remaining)
            est = estimate_tokens(text)
        notes.append(Note(id=row["id"], text=text, created_at=row["created_at"], score=matched))
        used += est
    return notes
