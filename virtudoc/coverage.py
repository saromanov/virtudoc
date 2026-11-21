"""Identifier coverage: which identifiers a library's docs actually mention.

Extraction follows the documented rules (docs/mcp.md, FR-1):

R1. Anything inside backticks or a fenced code block is code; it is matched
    case-insensitively.
R2. camelCase (a lowercase letter before an uppercase one) — case-sensitive.
R3. PascalCase with at least two uppercase letters, not all caps — sensitive.
R4. snake_case: `_` between letters or digits — case-sensitive.
R5. Dotted member access, both parts start with a letter — case-sensitive.
R6. A call suffix `()` or `(…)` is stripped; the name is case-sensitive.

Numbers, versions, URLs, paths, short tokens and all-caps prose acronyms are
not identifiers. Duplicates are removed, the first occurrence wins, and the
caller may cap the result count.

The `library_symbols` table is a prefilter, not the source of truth: it is
looked up by exact key (respecting the case policy) and the result is still a
whole-token match because the table is built from the tokenizer.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

_CANDIDATE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_CODE_SPAN_RE = re.compile(r"`+([^`]+)`+")
_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_VERSION_RE = re.compile(r"v?\d+(?:\.\d+)+", re.IGNORECASE)
_CAMEL_RE = re.compile(r"[a-z][A-Z]")

_LANGUAGE_KEYWORDS = frozenset(
    {
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
    }
)

_PROSE_WORDS = frozenset(
    {
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

_PRESENCE_TEMPLATE = r"(?<![\w.]){}(?![\w])"


@dataclass(frozen=True)
class Identifier:
    """One extracted identifier and the case policy of its rule."""

    name: str
    case_sensitive: bool = True

    @property
    def cache_key(self) -> tuple[str, bool]:
        return self.name, self.case_sensitive


@dataclass
class IdentifierChecker:
    """Checks identifiers against one variant, at most once per identifier."""

    conn: sqlite3.Connection
    library_id: str
    cache: dict[tuple[str, bool], bool] = field(default_factory=dict)

    def has(self, identifier: str | Identifier, *, case_sensitive: bool | None = None) -> bool:
        if isinstance(identifier, Identifier):
            spec = identifier
        else:
            spec = Identifier(
                identifier,
                True if case_sensitive is None else case_sensitive,
            )
        cached = self.cache.get(spec.cache_key)
        if cached is not None:
            return cached
        found = library_has_identifier(
            self.conn,
            self.library_id,
            spec.name,
            case_sensitive=spec.case_sensitive,
        )
        self.cache[spec.cache_key] = found
        return found

    def missing(self, identifiers: list[str | Identifier]) -> list[str]:
        missing: list[str] = []
        for identifier in identifiers:
            if not self.has(identifier):
                name = identifier.name if isinstance(identifier, Identifier) else identifier
                missing.append(name)
        return missing

    def prefetch(self, identifiers: list[str | Identifier]) -> None:
        """Resolve many identifiers at once from the symbols table.

        One indexed query per 500 identifiers; databases indexed before the
        table existed fall back to per-identifier checks.
        """
        specs = [item if isinstance(item, Identifier) else Identifier(item) for item in identifiers]
        pending: list[Identifier] = []
        for spec in specs:
            if spec.cache_key in self.cache or spec in pending:
                continue
            pending.append(spec)
        if not pending:
            return
        built = self.conn.execute(
            "SELECT 1 FROM library_symbols WHERE library_id = ? LIMIT 1",
            (self.library_id,),
        ).fetchone()
        if built is None:
            for spec in pending:
                self.has(spec)
            return
        for start in range(0, len(pending), 500):
            batch = pending[start : start + 500]
            keys = [spec.name if spec.case_sensitive else spec.name.lower() for spec in batch]
            placeholders = ",".join("?" for _ in keys)
            rows = self.conn.execute(
                f"SELECT symbol FROM library_symbols "
                f"WHERE library_id = ? AND symbol IN ({placeholders})",
                (self.library_id, *keys),
            ).fetchall()
            present = {row["symbol"] for row in rows}
            for spec, key in zip(batch, keys, strict=True):
                self.cache[spec.cache_key] = key in present


def _code_ranges(text: str) -> list[tuple[int, int]]:
    ranges = [(match.start(1), match.end(1)) for match in _CODE_SPAN_RE.finditer(text)]
    ranges.extend((match.start(1), match.end(1)) for match in _FENCE_RE.finditer(text))
    return ranges


def _in_url_or_path(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if (before and before in "/\\") or (after and after in "/\\"):
        return True
    return text.startswith("://", end)


def _extract_specs(text: str, *, limit: int | None = None) -> list[Identifier]:
    code_ranges = _code_ranges(text)
    seen: set[str] = set()
    found: list[Identifier] = []
    for match in _CANDIDATE_RE.finditer(text):
        candidate = match.group(0).rstrip(".")
        lowered = candidate.lower()
        in_code = any(start <= match.start() < end for start, end in code_ranges)
        tail = text[match.end() : match.end() + 3]
        called = tail.startswith("(")

        if len(candidate) < 3 or _VERSION_RE.fullmatch(candidate):
            continue
        if not in_code:
            if lowered in _LANGUAGE_KEYWORDS or lowered in _PROSE_WORDS:
                continue
            if candidate.isupper():
                # A prose acronym, unless it is written as code.
                continue
            if _in_url_or_path(text, match.start(), match.end()):
                continue
        elif lowered in _LANGUAGE_KEYWORDS and not candidate.isupper():
            continue

        if called:
            case_sensitive = True
        elif "." in candidate:
            case_sensitive = True
        elif "_" in candidate:
            case_sensitive = True
        elif _CAMEL_RE.search(candidate):
            case_sensitive = True
        elif candidate[0].isupper() and sum(ch.isupper() for ch in candidate) >= 2:
            case_sensitive = not candidate.isupper()
        elif in_code:
            case_sensitive = False
        else:
            continue

        if candidate in seen:
            continue
        seen.add(candidate)
        found.append(Identifier(candidate, case_sensitive))
        if limit is not None and len(found) >= limit:
            break
    return found


def extract_identifier_specs(text: str, *, limit: int | None = None) -> list[Identifier]:
    """Identifiers with their case policy, in order of first appearance."""
    return _extract_specs(text, limit=limit)


def extract_identifiers(text: str, *, limit: int | None = None) -> list[str]:
    """Identifiers mentioned in a note or a query, in order of appearance."""
    return [spec.name for spec in _extract_specs(text, limit=limit)]


def symbols_in_text(text: str) -> set[str]:
    """Every identifier-ish token of a document, for the fast lookup table.

    Each token is stored twice: lower-cased (case-insensitive rules) and
    exactly (case-sensitive rules). Dotted prefixes are stored too, so a query
    for `Session` is found in `Session.open`.
    """
    found: set[str] = set()
    for match in _CANDIDATE_RE.finditer(text):
        token = match.group(0).rstrip(".")
        if not token:
            continue
        found.add(token)
        found.add(token.lower())
        if "." in token:
            parts = token.split(".")
            prefix: list[str] = []
            for part in parts[:-1]:
                prefix.append(part)
                joined = ".".join(prefix)
                found.add(joined)
                found.add(joined.lower())
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


def _regex_has_identifier(
    conn: sqlite3.Connection, library_id: str, identifier: str, *, case_sensitive: bool
) -> bool:
    pattern = re.compile(
        _PRESENCE_TEMPLATE.format(re.escape(identifier)),
        0 if case_sensitive else re.IGNORECASE,
    )
    for row in conn.execute("SELECT text FROM chunks WHERE library_id = ?", (library_id,)):
        if pattern.search(row["text"]):
            return True
    return False


def library_has_identifier(
    conn: sqlite3.Connection,
    library_id: str,
    identifier: str,
    *,
    case_sensitive: bool = True,
) -> bool:
    """True when the indexed chunks of this variant mention the identifier.

    The symbols table is a prefilter: the key is exact for case-sensitive
    rules and lower-cased for R1, and the table itself is tokenized, so only
    whole identifiers match (`client.send` never matches `client.sender`).
    """
    if not identifier:
        return False
    key = identifier if case_sensitive else identifier.lower()
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
    return _regex_has_identifier(conn, library_id, identifier, case_sensitive=case_sensitive)


def anchorable_identifier(
    conn: sqlite3.Connection, library_id: str, identifier: str | Identifier
) -> bool:
    """Whether a note may track this identifier as an anchor.

    A qualified name is tracked when its root is in the docs: the member is
    compared exactly at status time, so `Session.open` gets `symbol_removed`
    when the docs only ever had `session` (R5). The root lookup is
    case-insensitive because tracking is about presence, not identity.
    """
    spec = identifier if isinstance(identifier, Identifier) else Identifier(identifier)
    name = spec.name
    if "." in name:
        root = name.split(".", 1)[0]
        return library_has_identifier(conn, library_id, root, case_sensitive=False)
    return library_has_identifier(conn, library_id, name, case_sensitive=spec.case_sensitive)


def identifier_in_texts(identifier: str | Identifier, texts: list[str]) -> bool:
    """Whole-word presence of an identifier in the returned fragments."""
    spec = identifier if isinstance(identifier, Identifier) else Identifier(identifier)
    pattern = re.compile(
        _PRESENCE_TEMPLATE.format(re.escape(spec.name)),
        0 if spec.case_sensitive else re.IGNORECASE,
    )
    return any(pattern.search(text) for text in texts)


@dataclass
class Coverage:
    """How much of the query the indexed docs and the returned page cover."""

    level: str
    missing: list[str] = field(default_factory=list)
    unreturned: list[str] = field(default_factory=list)


def assess(
    conn: sqlite3.Connection,
    library_id: str,
    query: str,
    texts: list[str],
    *,
    total_matches: int,
    outline: bool = False,
    limit: int = 8,
    checker: IdentifierChecker | None = None,
) -> Coverage:
    """Assess identifier coverage for one response (FR-1.4).

    `matched` means every query identifier exists in the documentation, not
    that the documentation fully answers the question.
    """
    specs = extract_identifier_specs(query, limit=limit)
    checker = checker or IdentifierChecker(conn, library_id)
    checker.prefetch(specs)
    missing = [spec.name for spec in specs if not checker.has(spec)]
    if outline:
        # The outline is the whole tree: page-level "unreturned" is meaningless.
        return Coverage("partial" if missing else "matched", missing, [])
    unreturned = [
        spec.name
        for spec in specs
        if spec.name not in missing and not identifier_in_texts(spec, texts)
    ]
    if not texts and total_matches == 0:
        level = "none"
    elif missing:
        level = "partial"
    else:
        level = "matched"
    return Coverage(level, missing, unreturned)


def missing_identifiers(
    conn: sqlite3.Connection, library_id: str, identifiers: list[str]
) -> list[str]:
    """Identifiers the docs do not mention, preserving order and deduplicating."""
    checker = IdentifierChecker(conn, library_id)
    return checker.missing(list(dict.fromkeys(identifiers)))
