from __future__ import annotations

import difflib
import sqlite3
from dataclasses import dataclass, field

from .coverage import (
    IdentifierChecker,
    extract_identifiers,
    library_has_identifier,
)
from .db import (
    fts_normalize,
    refresh_source_fingerprint,
    source_fingerprint,
    utc_now,
)
from .search.engine import _TOKEN_RE, MIN_TOPIC_PREFIX, _stem, search, tokenize
from .tokens import estimate_tokens

STATUS_CURRENT = "current"
STATUS_STALE = "stale"
STATUS_UNKNOWN = "unknown"

ANCHOR_DOCUMENTS = "documents"
ANCHOR_SYMBOLS = "symbols"
ANCHOR_LIBRARY = "library"
ANCHOR_NONE = "none"


class SymbolRemovedError(ValueError):
    """Review without --reanchor while symbol anchors are gone from the docs."""

    def __init__(self, note_id: int, symbols: list[str]) -> None:
        self.note_id = note_id
        self.symbols = symbols
        super().__init__(
            f"note #{note_id} references {', '.join(symbols)}, which is no longer in the docs"
        )


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
    library_name: str = ""
    origin_library_id: str = ""
    scope_version: str | None = None
    pinned: bool = False
    reviewed_at: str | None = None
    anchor_kind: str = ANCHOR_LIBRARY
    status: str = STATUS_UNKNOWN
    stale_reason: str | None = None
    changed_sources: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    missing_symbols: list[str] = field(default_factory=list)
    mentions_missing: list[str] = field(default_factory=list)

    @property
    def reviewed_or_created(self) -> str:
        return self.reviewed_at or self.created_at


@dataclass
class NotesSelection:
    """Notes for one response, plus what did not fit the budget."""

    notes: list[Note] = field(default_factory=list)
    omitted: int = 0

    @property
    def stale_count(self) -> int:
        return sum(1 for note in self.notes if note.status == STATUS_STALE)


@dataclass
class Anchor:
    rel_path: str
    content_hash: str


def source_revision(conn: sqlite3.Connection, library_id: str) -> str:
    """Fingerprint of the indexed source: the docs an agent reads right now.

    Reads the cached `libraries.source_fingerprint`; when it is NULL (old
    database, failed indexing run) it computes and stores it.
    """
    return _cached_fingerprint(conn, library_id)


def _library_row(conn: sqlite3.Connection, name_or_id: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM libraries WHERE id = ?", (name_or_id,)).fetchone()
    if row is not None:
        return row
    name = _logical_name(name_or_id)
    return conn.execute(
        "SELECT * FROM libraries WHERE name = ? ORDER BY id LIMIT 1", (name,)
    ).fetchone()


def _logical_name(name_or_id: str) -> str:
    return name_or_id.rsplit("/", 1)[-1].split("@", 1)[0]


def _variant_name(conn: sqlite3.Connection, library_id: str) -> str:
    row = conn.execute("SELECT name FROM libraries WHERE id = ?", (library_id,)).fetchone()
    return row["name"] if row is not None else _logical_name(library_id)


def _cached_fingerprint(conn: sqlite3.Connection, library_id: str) -> str:
    """Cached fingerprint; computes and stores it when the indexer left NULL."""
    row = conn.execute(
        "SELECT source_fingerprint FROM libraries WHERE id = ?", (library_id,)
    ).fetchone()
    if row is not None and row["source_fingerprint"]:
        return row["source_fingerprint"]
    fingerprint = source_fingerprint(conn, library_id)
    conn.execute(
        "UPDATE libraries SET source_fingerprint = ? WHERE id = ?",
        (fingerprint, library_id),
    )
    conn.commit()
    return fingerprint


def _document_hash(conn: sqlite3.Connection, library_id: str, rel_path: str) -> str | None:
    row = conn.execute(
        "SELECT content_hash FROM documents WHERE library_id = ? AND rel_path = ?",
        (library_id, rel_path),
    ).fetchone()
    return row["content_hash"] if row is not None else None


def document_hashes(conn: sqlite3.Connection, library_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT rel_path, content_hash FROM documents WHERE library_id = ?", (library_id,)
    ).fetchall()
    return {row["rel_path"]: row["content_hash"] for row in rows}


def nearest_paths(
    conn: sqlite3.Connection, library_id: str, rel_path: str, limit: int = 3
) -> list[str]:
    """Closest indexed paths, for an anchor path that does not exist."""
    known = [
        row["rel_path"]
        for row in conn.execute(
            "SELECT rel_path FROM documents WHERE library_id = ?", (library_id,)
        )
    ]
    return difflib.get_close_matches(rel_path, known, n=limit, cutoff=0.3)


def auto_anchor_documents(
    conn: sqlite3.Connection,
    library_id: str,
    text: str,
    *,
    k: int = 3,
    ratio: float = 0.5,
) -> list[str]:
    """Top-K distinct documents the note text matches, by search score.

    A document is anchored only when its best snippet scores at least
    ``ratio`` of the best score, so a weak tail does not create anchors that
    would later report false staleness.
    """
    if k <= 0 or not text.strip():
        return []
    result = search(conn, text, library_id=library_id, token_budget=5000)
    best_by_doc: dict[str, float] = {}
    order: list[str] = []
    for snippet in result.snippets:
        if snippet.source not in best_by_doc:
            order.append(snippet.source)
            best_by_doc[snippet.source] = snippet.score
        else:
            best_by_doc[snippet.source] = max(best_by_doc[snippet.source], snippet.score)
    if not order:
        return []
    threshold = best_by_doc[order[0]] * ratio
    return [source for source in order if best_by_doc[source] >= threshold][:k]


def _write_anchors(
    conn: sqlite3.Connection,
    note_id: int,
    library_id: str,
    rel_paths: list[str],
    *,
    replace: bool = True,
) -> list[Anchor]:
    if replace:
        conn.execute(
            "DELETE FROM note_anchors WHERE note_id = ? AND library_id = ?",
            (note_id, library_id),
        )
    now = utc_now()
    anchors: list[Anchor] = []
    for rel_path in rel_paths:
        content_hash = _document_hash(conn, library_id, rel_path)
        if content_hash is None:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO note_anchors
                (note_id, library_id, rel_path, content_hash, anchored_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (note_id, library_id, rel_path, content_hash, now),
        )
        anchors.append(Anchor(rel_path=rel_path, content_hash=content_hash))
    return anchors


def _write_symbols(
    conn: sqlite3.Connection,
    note_id: int,
    symbols: list[str],
    *,
    replace: bool = True,
) -> None:
    if replace:
        conn.execute("DELETE FROM note_symbols WHERE note_id = ?", (note_id,))
    now = utc_now()
    for symbol in dict.fromkeys(symbols):
        conn.execute(
            "INSERT OR REPLACE INTO note_symbols (note_id, symbol, anchored_at) VALUES (?, ?, ?)",
            (note_id, symbol, now),
        )


def add_note(
    conn: sqlite3.Connection,
    library_id: str,
    text: str,
    source_rev: str | None = None,
    *,
    paths: list[str] | None = None,
    scope_version: str | None = None,
    pinned: bool = False,
    no_anchor: bool = False,
    auto_anchor_k: int = 3,
    auto_anchor_ratio: float = 0.5,
    max_symbols: int = 10,
) -> int:
    """Attach a note to a logical library, anchored to the docs it relies on.

    Explicit ``paths`` are validated against the index of ``library_id``.
    Without them the note text is searched and the top-K distinct documents
    become anchors. When nothing matches, the note falls back to a
    library-level anchor; ``no_anchor`` records an unknown-status note.
    """
    library = _library_row(conn, library_id)
    if library is None:
        raise ValueError(f"library not found: {library_id}")
    variant_id = library["id"]
    variant_name = library["name"]
    if source_rev is None:
        source_rev = source_fingerprint(conn, variant_id)

    selected: list[str] = []
    symbols: list[str] = []
    if no_anchor:
        anchor_kind = ANCHOR_NONE
    else:
        identifiers = extract_identifiers(text, limit=max_symbols)
        symbols = [
            symbol for symbol in identifiers if library_has_identifier(conn, variant_id, symbol)
        ]
        if paths:
            missing = [path for path in paths if _document_hash(conn, variant_id, path) is None]
            if missing:
                hints = []
                for path in missing:
                    closest = nearest_paths(conn, variant_id, path)
                    hints.append(f"{path} (closest: {', '.join(closest) or 'none'})")
                raise ValueError("anchor paths not in the index: " + "; ".join(hints))
            selected = list(dict.fromkeys(paths))
            anchor_kind = ANCHOR_DOCUMENTS
        else:
            selected = auto_anchor_documents(
                conn, variant_id, text, k=auto_anchor_k, ratio=auto_anchor_ratio
            )
            if selected:
                anchor_kind = ANCHOR_DOCUMENTS
            elif symbols:
                anchor_kind = ANCHOR_SYMBOLS
            else:
                anchor_kind = ANCHOR_LIBRARY

    cur = conn.execute(
        """
        INSERT INTO notes
            (library_id, text, created_at, source_rev, library_name,
             origin_library_id, scope_version, pinned, reviewed_at, anchor_kind)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            variant_id,
            text.strip(),
            utc_now(),
            source_rev,
            variant_name,
            variant_id,
            scope_version,
            1 if pinned else 0,
            utc_now(),
            anchor_kind,
        ),
    )
    note_id = int(cur.lastrowid)
    if anchor_kind == ANCHOR_DOCUMENTS:
        _write_anchors(conn, note_id, variant_id, selected)
    if symbols:
        _write_symbols(conn, note_id, symbols)
    conn.commit()
    return note_id


def _note_from_row(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        text=row["text"],
        created_at=row["created_at"],
        source_rev=row["source_rev"] or "",
        library_name=row["library_name"] or "",
        origin_library_id=row["origin_library_id"] or row["library_id"],
        scope_version=row["scope_version"],
        pinned=bool(row["pinned"]),
        reviewed_at=row["reviewed_at"],
        anchor_kind=row["anchor_kind"] or ANCHOR_LIBRARY,
    )


_NOTE_COLUMNS = """
    n.id, n.text, n.created_at, n.source_rev, n.library_id, n.library_name,
    n.origin_library_id, n.scope_version, n.pinned, n.reviewed_at, n.anchor_kind
"""


def list_notes(conn: sqlite3.Connection, library_id: str) -> list[Note]:
    """All notes of the logical library, ordered by id."""
    name = _variant_name(conn, library_id)
    rows = conn.execute(
        f"SELECT {_NOTE_COLUMNS} FROM notes n WHERE n.library_name = ? ORDER BY n.id",
        (name,),
    ).fetchall()
    return [_note_from_row(row) for row in rows]


def note_anchors(
    conn: sqlite3.Connection, note_id: int, library_id: str | None = None
) -> list[sqlite3.Row]:
    if library_id is None:
        return conn.execute(
            "SELECT * FROM note_anchors WHERE note_id = ? ORDER BY library_id, rel_path",
            (note_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM note_anchors WHERE note_id = ? AND library_id = ? ORDER BY rel_path",
        (note_id, library_id),
    ).fetchall()


def note_symbols(conn: sqlite3.Connection, note_id: int) -> list[str]:
    return [
        row["symbol"]
        for row in conn.execute(
            "SELECT symbol FROM note_symbols WHERE note_id = ? ORDER BY symbol", (note_id,)
        )
    ]


def _describe_status(
    conn: sqlite3.Connection,
    note: Note,
    variant_id: str,
    anchors: list[dict],
    symbols: list[str],
    checker: IdentifierChecker,
) -> None:
    """Set status, stale_reason and changed_sources on the note in place."""
    note.symbols = symbols
    if note.anchor_kind == ANCHOR_NONE:
        note.status = STATUS_UNKNOWN
        return

    missing_symbols = [symbol for symbol in symbols if not checker.has(symbol)]
    if missing_symbols:
        note.status = STATUS_STALE
        note.stale_reason = "symbol_removed"
        note.missing_symbols = missing_symbols
        return

    variant_anchors = [a for a in anchors if a["library_id"] == variant_id]
    origin_anchors = [a for a in anchors if a["library_id"] != variant_id]
    if variant_anchors:
        changed: list[str] = []
        deleted = False
        for anchor in variant_anchors:
            current = anchor["current_hash"]
            if current is None:
                deleted = True
                changed.append(anchor["rel_path"])
            elif current != anchor["recorded_hash"]:
                changed.append(anchor["rel_path"])
        if changed:
            note.status = STATUS_STALE
            note.stale_reason = "anchor_deleted" if deleted else "anchor_changed"
            note.changed_sources = changed[:5]
        else:
            note.status = STATUS_CURRENT
        return

    if note.anchor_kind == ANCHOR_DOCUMENTS and origin_anchors:
        # The note was written against another variant: compare its anchors
        # with the docs of the variant being served.
        changed = []
        for anchor in origin_anchors:
            current = anchor["current_hash"]
            if current is None or current != anchor["recorded_hash"]:
                changed.append(anchor["rel_path"])
        if changed:
            note.status = STATUS_STALE
            note.stale_reason = "version_differs"
            note.changed_sources = changed[:5]
        else:
            note.status = STATUS_CURRENT
        return

    if note.anchor_kind == ANCHOR_SYMBOLS:
        # No document anchors: the symbols were found in the served variant.
        note.status = STATUS_CURRENT
        return

    if not note.source_rev:
        note.status = STATUS_UNKNOWN
        return
    current = _cached_fingerprint(conn, variant_id)
    if note.source_rev == current:
        note.status = STATUS_CURRENT
    else:
        note.status = STATUS_STALE
        note.stale_reason = "library_changed"


def note_statuses(
    conn: sqlite3.Connection,
    library_id: str,
    *,
    checker: IdentifierChecker | None = None,
) -> list[Note]:
    """Notes of the logical library with staleness relative to this variant.

    One query fetches the notes, their anchors (current and origin variant)
    and the matching document hashes; grouping happens in Python.
    """
    name = _variant_name(conn, library_id)
    variant_id = library_id
    rows = conn.execute(
        f"""
        SELECT {_NOTE_COLUMNS},
               a.library_id AS anchor_library_id,
               a.rel_path AS anchor_rel_path,
               a.content_hash AS anchor_hash,
               d.content_hash AS current_hash
        FROM notes n
        LEFT JOIN note_anchors a
               ON a.note_id = n.id
              AND (a.library_id = ? OR a.library_id = n.origin_library_id)
        LEFT JOIN documents d
               ON d.library_id = ? AND d.rel_path = a.rel_path
        WHERE n.library_name = ?
        ORDER BY n.id, a.rel_path
        """,
        (variant_id, variant_id, name),
    ).fetchall()

    notes: dict[int, Note] = {}
    anchors: dict[int, list[dict]] = {}
    for row in rows:
        note = notes.get(row["id"])
        if note is None:
            note = _note_from_row(row)
            notes[row["id"]] = note
            anchors[row["id"]] = []
        if row["anchor_rel_path"] is not None:
            anchors[row["id"]].append(
                {
                    "library_id": row["anchor_library_id"],
                    "rel_path": row["anchor_rel_path"],
                    "recorded_hash": row["anchor_hash"],
                    "current_hash": row["current_hash"],
                }
            )
    ordered = [notes[key] for key in sorted(notes)]
    symbol_rows: dict[int, list[str]] = {}
    if ordered:
        placeholders = ",".join("?" for _ in ordered)
        for row in conn.execute(
            f"SELECT note_id, symbol FROM note_symbols WHERE note_id IN ({placeholders}) "
            "ORDER BY symbol",
            tuple(note.id for note in ordered),
        ):
            symbol_rows.setdefault(row["note_id"], []).append(row["symbol"])
    checker = checker or IdentifierChecker(conn, variant_id)
    for note in ordered:
        _describe_status(
            conn,
            note,
            variant_id,
            anchors[note.id],
            symbol_rows.get(note.id, []),
            checker,
        )
    return ordered


def stale_notes(conn: sqlite3.Connection, library_id: str) -> list[Note]:
    return [note for note in note_statuses(conn, library_id) if note.status == STATUS_STALE]


def remove_note(conn: sqlite3.Connection, library_id: str, note_id: int) -> bool:
    # Anchors are deleted explicitly as well: servable databases may have been
    # created before foreign keys were enabled.
    conn.execute("DELETE FROM note_anchors WHERE note_id = ?", (note_id,))
    conn.execute("DELETE FROM note_symbols WHERE note_id = ?", (note_id,))
    cur = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.commit()
    return cur.rowcount > 0


def _version_matches(scope_version: str | None, variant_version: str | None) -> bool:
    if scope_version is None:
        return True
    return variant_version is not None and scope_version == variant_version


def select_notes(
    conn: sqlite3.Connection,
    library_id: str,
    query: str,
    *,
    budget_tokens: int = 800,
    pinned_budget_tokens: int = 400,
    checker: IdentifierChecker | None = None,
) -> NotesSelection:
    """Notes for an agent response: pinned first, then query matches.

    Pinned notes are always included within their own budget. Other notes are
    included only when the full text fits the remaining budget: a truncated
    rule could change its meaning, so overflow is reported as ``omitted``.
    """
    variant = _library_row(conn, library_id)
    variant_id = variant["id"] if variant is not None else library_id
    variant_version = variant["version"] if variant is not None else None
    notes = [
        note
        for note in note_statuses(conn, variant_id, checker=checker)
        if _version_matches(note.scope_version, variant_version)
    ]
    tokens = tokenize(query)
    for note in notes:
        haystack = fts_normalize(note.text.lower())
        words = _TOKEN_RE.findall(haystack)
        note.score = sum(1 for tok in tokens if _note_token_matches(tok, words)) if tokens else 0

    pinned = sorted((note for note in notes if note.pinned), key=lambda n: -n.id)
    rest = sorted(
        (note for note in notes if not note.pinned and note.score > 0),
        key=lambda n: (-n.score, -n.id),
    )

    selection = NotesSelection()
    used = 0
    pinned_used = 0
    for note in pinned:
        est = estimate_tokens(note.text)
        if pinned_used + est > pinned_budget_tokens or used + est > budget_tokens:
            selection.omitted += 1
            continue
        selection.notes.append(note)
        pinned_used += est
        used += est
    for note in rest:
        est = estimate_tokens(note.text)
        if used + est > budget_tokens:
            selection.omitted += 1
            continue
        selection.notes.append(note)
        used += est
    return selection


def relevant_notes(
    conn: sqlite3.Connection,
    library_id: str,
    query: str,
    *,
    budget_tokens: int = 800,
    pinned_budget_tokens: int = 400,
) -> list[Note]:
    return select_notes(
        conn,
        library_id,
        query,
        budget_tokens=budget_tokens,
        pinned_budget_tokens=pinned_budget_tokens,
    ).notes


def get_note(conn: sqlite3.Connection, library_id: str, note_id: int) -> Note | None:
    name = _variant_name(conn, library_id)
    row = conn.execute(
        f"SELECT {_NOTE_COLUMNS} FROM notes n WHERE n.id = ? AND n.library_name = ?",
        (note_id, name),
    ).fetchone()
    return _note_from_row(row) if row is not None else None


def review_note(
    conn: sqlite3.Connection,
    library_id: str,
    note_id: int,
    *,
    reanchor: bool = False,
    auto_anchor_k: int = 3,
    auto_anchor_ratio: float = 0.5,
    max_symbols: int = 10,
) -> bool:
    """Confirm that the note matches the documentation of this variant.

    A missing symbol anchor is an error unless ``reanchor`` is set: silently
    confirming a note that mentions a removed identifier would defeat the
    purpose of the check.
    """
    note = get_note(conn, library_id, note_id)
    if note is None:
        return False
    variant = _library_row(conn, library_id)
    variant_id = variant["id"] if variant is not None else library_id

    missing_symbols = [
        symbol
        for symbol in note_symbols(conn, note_id)
        if not library_has_identifier(conn, variant_id, symbol)
    ]
    if missing_symbols and not reanchor:
        raise SymbolRemovedError(note_id, missing_symbols)

    if reanchor:
        selected = auto_anchor_documents(
            conn,
            variant_id,
            note.text,
            k=auto_anchor_k,
            ratio=auto_anchor_ratio,
        )
        identifiers = extract_identifiers(note.text, limit=max_symbols)
        symbols = [
            symbol for symbol in identifiers if library_has_identifier(conn, variant_id, symbol)
        ]
        _write_symbols(conn, note_id, symbols)
        if selected:
            _write_anchors(conn, note_id, variant_id, selected)
            anchor_kind = ANCHOR_DOCUMENTS
        else:
            _write_anchors(conn, note_id, variant_id, [])
            anchor_kind = ANCHOR_SYMBOLS if symbols else ANCHOR_LIBRARY
    else:
        anchor_kind = note.anchor_kind
        if note.anchor_kind in (ANCHOR_DOCUMENTS, ANCHOR_SYMBOLS):
            rows = note_anchors(conn, note_id, variant_id)
            existing = [
                row["rel_path"] for row in rows if _document_hash(conn, variant_id, row["rel_path"])
            ]
            current_docs = document_hashes(conn, variant_id)
            now = utc_now()
            for rel_path in existing:
                conn.execute(
                    """
                    UPDATE note_anchors SET content_hash = ?, anchored_at = ?
                    WHERE note_id = ? AND library_id = ? AND rel_path = ?
                    """,
                    (current_docs[rel_path], now, note_id, variant_id, rel_path),
                )
            missing = [
                row["rel_path"]
                for row in rows
                if not _document_hash(conn, variant_id, row["rel_path"])
            ]
            for rel_path in missing:
                conn.execute(
                    "DELETE FROM note_anchors "
                    "WHERE note_id = ? AND library_id = ? AND rel_path = ?",
                    (note_id, variant_id, rel_path),
                )
            if note.anchor_kind == ANCHOR_DOCUMENTS and not existing:
                anchor_kind = ANCHOR_LIBRARY

    conn.execute(
        """
        UPDATE notes
        SET reviewed_at = ?, source_rev = ?, anchor_kind = ?
        WHERE id = ?
        """,
        (utc_now(), source_fingerprint(conn, variant_id), anchor_kind, note_id),
    )
    conn.commit()
    return True


def edit_note(
    conn: sqlite3.Connection,
    library_id: str,
    note_id: int,
    text: str,
    *,
    auto_anchor_k: int = 3,
    auto_anchor_ratio: float = 0.5,
    max_symbols: int = 10,
) -> bool:
    note = get_note(conn, library_id, note_id)
    if note is None:
        return False
    conn.execute("UPDATE notes SET text = ? WHERE id = ?", (text.strip(), note_id))
    conn.commit()
    return review_note(
        conn,
        library_id,
        note_id,
        reanchor=True,
        auto_anchor_k=auto_anchor_k,
        auto_anchor_ratio=auto_anchor_ratio,
        max_symbols=max_symbols,
    )


def set_pinned(conn: sqlite3.Connection, library_id: str, note_id: int, pinned: bool) -> bool:
    note = get_note(conn, library_id, note_id)
    if note is None:
        return False
    conn.execute("UPDATE notes SET pinned = ? WHERE id = ?", (1 if pinned else 0, note_id))
    conn.commit()
    return True


def pinned_tokens(conn: sqlite3.Connection, library_id: str) -> int:
    return sum(
        estimate_tokens(note.text) for note in note_statuses(conn, library_id) if note.pinned
    )


def apply_mentions_missing(notes: list[Note], missing_identifiers: list[str]) -> None:
    """Mark notes that mention identifiers absent from the docs.

    An identifier that is a symbol anchor already made the note stale
    (`symbol_removed`); it is not repeated in `mentions_missing`.
    """
    for note in notes:
        note.mentions_missing = [
            identifier
            for identifier in missing_identifiers
            if identifier not in note.missing_symbols and identifier in note.text
        ]


def note_payload(note: Note) -> dict:
    """The note object agents receive (`note list --json` uses it too)."""
    payload: dict = {
        "id": note.id,
        "text": note.text,
        "created_at": note.created_at,
        "status": note.status,
        "stale_reason": note.stale_reason,
        "changed_sources": note.changed_sources,
        "reviewed_at": note.reviewed_or_created,
        "pinned": note.pinned,
        "scope_version": note.scope_version,
    }
    if note.symbols:
        payload["symbols"] = note.symbols
    if note.missing_symbols:
        payload["missing_symbols"] = note.missing_symbols
    if note.mentions_missing:
        payload["mentions_missing"] = note.mentions_missing
    return payload


def backfill_note_scope(conn: sqlite3.Connection) -> None:
    """v5 → v6: give existing notes a logical library and anchors.

    Notes whose recorded revision still matches the index are anchored to the
    documents their text matches (or to the whole library when nothing does).
    Notes written against an older revision stay library-anchored and report
    staleness until someone reviews them. Notes without a revision (pre-v4)
    become `unknown`, so they never trigger a warning.
    """
    rows = conn.execute(
        f"SELECT {_NOTE_COLUMNS}, l.name AS lib_name FROM notes n "
        "LEFT JOIN libraries l ON l.id = n.library_id "
        "WHERE n.library_name IS NULL"
    ).fetchall()
    for row in rows:
        library_id = row["library_id"]
        name = row["lib_name"] or _logical_name(library_id)
        source_rev = row["source_rev"] or ""
        anchor_kind = ANCHOR_LIBRARY
        reviewed_at = row["reviewed_at"]
        selected: list[str] = []
        symbols: list[str] = []
        if not source_rev:
            anchor_kind = ANCHOR_NONE
        elif source_rev == source_fingerprint(conn, library_id):
            try:
                selected = auto_anchor_documents(conn, library_id, row["text"])
            except sqlite3.Error:
                selected = []
            symbols = [
                symbol
                for symbol in extract_identifiers(row["text"])
                if library_has_identifier(conn, library_id, symbol)
            ]
            if selected:
                anchor_kind = ANCHOR_DOCUMENTS
            elif symbols:
                anchor_kind = ANCHOR_SYMBOLS
            else:
                anchor_kind = ANCHOR_LIBRARY
            reviewed_at = utc_now()
        conn.execute(
            """
            UPDATE notes
            SET library_name = ?, origin_library_id = COALESCE(origin_library_id, ?),
                reviewed_at = ?, anchor_kind = ?
            WHERE id = ?
            """,
            (name, library_id, reviewed_at, anchor_kind, row["id"]),
        )
        if selected:
            _write_anchors(conn, row["id"], library_id, selected)
        if symbols:
            _write_symbols(conn, row["id"], symbols)
    for library in conn.execute("SELECT id FROM libraries").fetchall():
        refresh_source_fingerprint(conn, library["id"])
    if rows:
        conn.commit()


def refresh_library_fingerprints(conn: sqlite3.Connection) -> None:
    for row in conn.execute("SELECT id FROM libraries").fetchall():
        refresh_source_fingerprint(conn, row["id"])
    conn.commit()
