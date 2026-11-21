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

from .db import fts_normalize
from .search.engine import _TOKEN_RE, MIN_TOPIC_PREFIX, _stem, tokenize

_CANDIDATE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_CODE_SPAN_RE = re.compile(r"`+([^`]+)`+")
_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)

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

    def prefetch(self, identifiers: list[str]) -> None:
        """Resolve many identifiers at once from the symbols table.

        One indexed query per 500 identifiers; databases indexed before the
        table existed fall back to per-identifier checks.
        """
        pending = [
            identifier for identifier in dict.fromkeys(identifiers) if identifier not in self.cache
        ]
        if not pending:
            return
        built = self.conn.execute(
            "SELECT 1 FROM library_symbols WHERE library_id = ? LIMIT 1",
            (self.library_id,),
        ).fetchone()
        if built is None:
            for identifier in pending:
                self.has(identifier)
            return
        for start in range(0, len(pending), 500):
            batch = pending[start : start + 500]
            keys = [identifier if "." in identifier else identifier.lower() for identifier in batch]
            placeholders = ",".join("?" for _ in keys)
            rows = self.conn.execute(
                f"SELECT symbol FROM library_symbols "
                f"WHERE library_id = ? AND symbol IN ({placeholders})",
                (self.library_id, *keys),
            ).fetchall()
            present = {row["symbol"] for row in rows}
            for identifier, key in zip(batch, keys, strict=True):
                self.cache[identifier] = key in present


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


def _code_ranges(text: str) -> list[tuple[int, int]]:
    ranges = [(match.start(1), match.end(1)) for match in _CODE_SPAN_RE.finditer(text)]
    ranges.extend((match.start(1), match.end(1)) for match in _FENCE_RE.finditer(text))
    return ranges


def extract_identifiers(text: str, *, limit: int | None = None) -> list[str]:
    """Identifiers mentioned in a note or a query, in order of appearance.

    Text inside backticks and fenced code blocks is trusted as code, so a
    single capitalised word such as `Session` is kept there (R1).
    """
    code_ranges = _code_ranges(text)
    seen: set[str] = set()
    found: list[str] = []
    for match in _CANDIDATE_RE.finditer(text):
        candidate = match.group(0)
        in_code = any(start <= match.start() < end for start, end in code_ranges)
        tail = text[match.end() : match.end() + 2]
        called = tail.startswith("(")
        if in_code:
            if candidate.lower() in _KEYWORDS:
                continue
        elif not _looks_like_identifier(candidate, called=called):
            continue
        symbol = candidate.rstrip(".")
        if symbol in seen:
            continue
        seen.add(symbol)
        found.append(symbol)
        if limit is not None and len(found) >= limit:
            break
    return found


def symbols_in_text(text: str) -> set[str]:
    """Every identifier-ish token of a document, for the fast lookup table.

    Lower-cased forms serve case-insensitive plain identifiers; qualified
    names are also stored exactly for the case-sensitive member check.
    """
    found: set[str] = set()
    for match in _CANDIDATE_RE.finditer(text):
        token = match.group(0).rstrip(".")
        found.add(token.lower())
        if "." in token:
            found.add(token)
    return found


def rebuild_library_symbols(conn: sqlite3.Connection, library_id: str) -> None:
    """Rebuild `library_symbols` for one variant (called by the indexer)."""
    conn.execute("DELETE FROM library_symbols WHERE library_id = ?", (library_id,))
    symbols: set[str] = set()
    for row in conn.execute("SELECT text FROM chunks WHERE library_id = ?", (library_id,)):
        symbols |= symbols_in_text(row["text"])
    if symbols:
        conn.executemany(
            "INSERT OR IGNORE INTO library_symbols (library_id, symbol) VALUES (?, ?)",
            [(library_id, symbol) for symbol in symbols],
        )


def _scan_has_identifier(conn: sqlite3.Connection, library_id: str, identifier: str) -> bool:
    """Fallback for databases indexed before the symbols table existed."""
    qualified = "." in identifier
    if len(identifier) >= 3:
        try:
            sql = """
                SELECT 1
                FROM chunks_tri
                JOIN chunks c ON c.id = chunks_tri.rowid
                WHERE chunks_tri MATCH ? AND c.library_id = ?
            """
            params: tuple = (
                f'"{identifier.replace(chr(34), chr(34) * 2)}"',
                library_id,
            )
            if qualified:
                sql += " AND instr(c.text, ?) > 0"
                params += (identifier,)
            sql += " LIMIT 1"
            return conn.execute(sql, params).fetchone() is not None
        except sqlite3.OperationalError:
            pass
    sql = "SELECT 1 FROM chunks WHERE library_id = ? AND "
    params = (library_id,)
    if qualified:
        sql += "instr(text, ?) > 0"
        params += (identifier,)
    else:
        sql += "text LIKE ?"
        params += (f"%{identifier}%",)
    sql += " LIMIT 1"
    return conn.execute(sql, params).fetchone() is not None


def library_has_identifier(conn: sqlite3.Connection, library_id: str, identifier: str) -> bool:
    """True when the indexed chunks of this variant mention the identifier.

    Plain identifiers match case-insensitively (R1); qualified names with a
    dot are member references and match case-sensitively (R5). The
    `library_symbols` table makes this a primary-key lookup.
    """
    if not identifier:
        return False
    key = identifier if "." in identifier else identifier.lower()
    row = conn.execute(
        "SELECT 1 FROM library_symbols WHERE library_id = ? AND symbol = ?",
        (library_id, key),
    ).fetchone()
    if row is not None:
        return True
    built = conn.execute(
        "SELECT 1 FROM library_symbols WHERE library_id = ? LIMIT 1", (library_id,)
    ).fetchone()
    if built is not None:
        return False
    return _scan_has_identifier(conn, library_id, identifier)


def anchorable_identifier(conn: sqlite3.Connection, library_id: str, identifier: str) -> bool:
    """Whether a note may track this identifier as an anchor.

    A qualified name is tracked when its root is in the docs: the member is
    compared exactly at status time, so `Session.open` gets `symbol_removed`
    when the docs only ever had `session` (R5).
    """
    if "." in identifier:
        root = identifier.split(".", 1)[0]
        return library_has_identifier(conn, library_id, root)
    return library_has_identifier(conn, library_id, identifier)


def query_coverage(query: str, texts: list[str]) -> str:
    """How much of the query the returned text covers: full, partial or none.

    Coverage is word-level: identifiers alone may be absent while the prose
    part of the query is answered, which is exactly the `partial` case an
    agent must not present as a complete answer.
    """
    tokens = tokenize(query)
    if not tokens:
        return "none"
    words = set(_TOKEN_RE.findall(fts_normalize(" ".join(texts).lower())))
    covered = 0
    for token in tokens:
        stem = _stem(token)
        if any(
            word == token
            or word == stem
            or (len(stem) >= MIN_TOPIC_PREFIX and word.startswith(stem))
            for word in words
        ):
            covered += 1
    if covered == 0:
        return "none"
    return "full" if covered == len(tokens) else "partial"


def missing_identifiers(
    conn: sqlite3.Connection, library_id: str, identifiers: list[str]
) -> list[str]:
    """Identifiers the docs do not mention, preserving order and deduplicating."""
    checker = IdentifierChecker(conn, library_id)
    return checker.missing(list(dict.fromkeys(identifiers)))
