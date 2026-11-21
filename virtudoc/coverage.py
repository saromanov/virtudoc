"""Identifier coverage: which identifiers a library's docs actually mention.

The rules are deliberately conservative. A prose word must never become an
identifier, otherwise every note would report a false "symbol removed".

R1. A candidate starts with a letter or underscore and continues with
    letters, digits and underscores; dotted paths are kept whole.
R2. A candidate is an identifier only when it looks like code: it is
    followed by `(`, contains `_` or `.`, has an inner/leading capital
    (camelCase, PascalCase), or is an all-caps constant of two characters.
R3. A trailing call `()` is stripped: `retry()` and `retry` are one symbol.
R4. Language keywords and common prose acronyms are dropped.
R5. Duplicates are removed, the first occurrence wins.
R6. At most `limit` identifiers are returned (None = no limit).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

_CANDIDATE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")

_KEYWORDS = frozenset(
    {
        # Python / JavaScript / Go keywords
        "and",
        "as",
        "assert",
        "async",
        "await",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "def",
        "default",
        "del",
        "defer",
        "delete",
        "do",
        "elif",
        "else",
        "enum",
        "except",
        "export",
        "false",
        "finally",
        "for",
        "from",
        "func",
        "global",
        "go",
        "if",
        "import",
        "in",
        "instanceof",
        "interface",
        "is",
        "lambda",
        "let",
        "new",
        "none",
        "not",
        "null",
        "or",
        "package",
        "pass",
        "raise",
        "return",
        "select",
        "self",
        "static",
        "struct",
        "super",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "type",
        "typeof",
        "var",
        "void",
        "while",
        "with",
        "yield",
        # Prose words that can be capitalised
        "the",
        "a",
        "an",
        "all",
        "any",
        "use",
        "used",
        "using",
        "only",
        "never",
        "always",
        "must",
        "should",
        "note",
        "team",
        "doc",
        "docs",
        # Common acronyms that are not API symbols
        "api",
        "http",
        "https",
        "url",
        "uri",
        "json",
        "yaml",
        "toml",
        "id",
        "ids",
        "ok",
        "todo",
        "fixme",
        "tbd",
        "n/a",
    }
)


@dataclass
class IdentifierChecker:
    """Checks identifiers against one variant, at most once per identifier."""

    conn: sqlite3.Connection
    library_id: str
    cache: dict[str, bool] = field(default_factory=dict)

    def has(self, identifier: str) -> bool:
        cached = self.cache.get(identifier)
        if cached is not None:
            return cached
        found = library_has_identifier(self.conn, self.library_id, identifier)
        self.cache[identifier] = found
        return found

    def missing(self, identifiers: list[str]) -> list[str]:
        return [identifier for identifier in identifiers if not self.has(identifier)]


def _looks_like_identifier(candidate: str, *, called: bool) -> bool:
    lowered = candidate.lower()
    if lowered in _KEYWORDS or lowered.replace(".", "").replace("_", "") in _KEYWORDS:
        return False
    if called:
        return True
    if "_" in candidate or "." in candidate:
        return len(candidate) >= 3
    if candidate.isupper() and len(candidate) >= 2:
        return True
    if len(candidate) >= 4 and any(ch.isupper() for ch in candidate[1:]):
        return True
    return False


def extract_identifiers(text: str, *, limit: int | None = None) -> list[str]:
    """Identifiers mentioned in a note or a query, in order of appearance."""
    seen: set[str] = set()
    found: list[str] = []
    for match in _CANDIDATE_RE.finditer(text):
        candidate = match.group(0)
        tail = text[match.end() : match.end() + 2]
        called = tail.startswith("(")
        if not _looks_like_identifier(candidate, called=called):
            continue
        symbol = candidate.rstrip(".")
        if symbol in seen:
            continue
        seen.add(symbol)
        found.append(symbol)
        if limit is not None and len(found) >= limit:
            break
    return found


def library_has_identifier(conn: sqlite3.Connection, library_id: str, identifier: str) -> bool:
    """True when the indexed chunks of this variant mention the identifier.

    The trigram index answers substring queries without scanning every chunk.
    Identifiers shorter than one trigram fall back to LIKE.
    """
    if not identifier:
        return False
    if len(identifier) >= 3:
        try:
            row = conn.execute(
                """
                SELECT 1
                FROM chunks_tri
                JOIN chunks c ON c.id = chunks_tri.rowid
                WHERE chunks_tri MATCH ? AND c.library_id = ?
                LIMIT 1
                """,
                (f'"{identifier.replace(chr(34), chr(34) * 2)}"', library_id),
            ).fetchone()
            if row is not None:
                return True
            # An exact substring match is authoritative; do not fall through
            # to LIKE, which could match across token boundaries only.
            return False
        except sqlite3.OperationalError:
            pass
    row = conn.execute(
        "SELECT 1 FROM chunks WHERE library_id = ? AND text LIKE ? LIMIT 1",
        (library_id, f"%{identifier}%"),
    ).fetchone()
    return row is not None


def missing_identifiers(
    conn: sqlite3.Connection, library_id: str, identifiers: list[str]
) -> list[str]:
    """Identifiers the docs do not mention, preserving order and deduplicating."""
    checker = IdentifierChecker(conn, library_id)
    return checker.missing(list(dict.fromkeys(identifiers)))
