from __future__ import annotations

import pytest

from virtudoc.coverage import (
    IdentifierChecker,
    extract_identifier_specs,
    extract_identifiers,
    library_has_identifier,
    missing_identifiers,
)
from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.indexer import add_library, index_library


@pytest.fixture
def lib(conn, docs_dir):
    (docs_dir / "api.md").write_text(
        "# API\n\n## Auth\n\nCall validateSession() before issueLegacyToken.\n"
    )
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    return lib_id


def test_extract_identifiers_keeps_code_like_tokens():
    text = (
        "Use issueLegacyToken before validateSession(); "
        "retry_with_jitter, config.timeout and HTTP 404. "
        "The middleware pipeline is prose."
    )
    assert extract_identifiers(text) == [
        "issueLegacyToken",
        "validateSession",
        "retry_with_jitter",
        "config.timeout",
    ]


def test_extract_identifiers_honours_limit():
    text = "alphaBeta gammaDelta epsilonZeta thetaIota"
    assert extract_identifiers(text, limit=2) == ["alphaBeta", "gammaDelta"]


def test_library_has_identifier(conn, lib):
    assert library_has_identifier(conn, lib, "validateSession") is True
    assert library_has_identifier(conn, lib, "issueLegacyToken") is True
    assert library_has_identifier(conn, lib, "retryWithJitter") is False


def test_checker_checks_each_identifier_once(conn, lib):
    checker = IdentifierChecker(conn, lib)
    assert checker.missing(["validateSession", "validateSession", "nope"]) == ["nope"]
    assert checker.cache[("validateSession", True)] is True
    assert checker.cache[("nope", True)] is False


def test_missing_identifiers_deduplicates(conn, lib):
    assert missing_identifiers(conn, lib, ["nope", "validateSession", "nope"]) == ["nope"]


def test_plain_symbol_matches_case_insensitively(conn, docs_dir, monkeypatch):
    import virtudoc.notes as notes_module
    from virtudoc.notes import add_note, note_statuses

    (docs_dir / "api.md").write_text("# API\n\nsession management lives here.\n")
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    monkeypatch.setattr(notes_module, "auto_anchor_documents", lambda *a, **k: [])

    add_note(conn, lib_id, "Use `Session` for auth.")
    note = note_statuses(conn, lib_id)[0]
    assert note.anchor_kind == "symbols"
    assert note.symbols == ["Session"]
    assert note.status == "current"


def test_qualified_symbol_matches_case_sensitively(conn, docs_dir, monkeypatch):
    import virtudoc.notes as notes_module
    from virtudoc.notes import add_note, note_statuses

    (docs_dir / "api.md").write_text("# API\n\nsession management lives here.\n")
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    monkeypatch.setattr(notes_module, "auto_anchor_documents", lambda *a, **k: [])

    add_note(conn, lib_id, "Call Session.open before the request.")
    note = note_statuses(conn, lib_id)[0]
    assert note.anchor_kind == "symbols"
    assert note.symbols == ["Session.open"]
    assert note.status == "stale"
    assert note.stale_reason == "symbol_removed"


def test_index_builds_symbol_table_and_drops_deleted_docs(conn, docs_dir):
    (docs_dir / "api.md").write_text("# API\n\nSession.open and validateSession.\n")
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())

    symbols = {
        row[0]
        for row in conn.execute(
            "SELECT symbol FROM library_symbols WHERE library_id = ?", (lib_id,)
        )
    }
    assert {"session.open", "Session.open", "validatesession"} <= symbols

    (docs_dir / "api.md").unlink()
    index_library(conn, lib_id, Chunker())
    left = {
        row[0]
        for row in conn.execute(
            "SELECT symbol FROM library_symbols WHERE library_id = ?", (lib_id,)
        )
    }
    assert "session.open" not in left
    assert library_has_identifier(conn, lib_id, "validateSession") is False


def test_prefetch_handles_more_than_one_batch(conn, lib):
    checker = IdentifierChecker(conn, lib)
    identifiers = [f"missing{index}Symbol" for index in range(600)] + ["validateSession"]
    checker.prefetch(identifiers)
    assert checker.cache[("validateSession", True)] is True
    assert checker.cache[("missing599Symbol", True)] is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Call `retry` first.", ["retry"]),
        ("Use issueLegacyToken now.", ["issueLegacyToken"]),
        ("Both HttpClient and HTTPClient work.", ["HttpClient", "HTTPClient"]),
        ("Set issue_legacy_token in config.", ["issue_legacy_token"]),
        ("Call client.send before the request.", ["client.send"]),
        ("Invoke connect() and read(x).", ["connect", "read"]),
    ],
)
def test_extract_identifiers_rule_table(text, expected):
    assert extract_identifiers(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Version 2.1.0 and v3.2 are numbers.", []),
        ("See https://example.com/api for details.", []),
        ("The path docs/guide.md holds it.", []),
        ("HTTP and API are prose acronyms.", []),
        ("id is too short.", []),
        ("`HTTP` in backticks is code.", ["HTTP"]),
    ],
)
def test_extract_identifiers_exclusions(text, expected):
    assert extract_identifiers(text) == expected


def test_extract_identifiers_code_span_is_case_insensitive():
    specs = extract_identifier_specs("Use `Session` and issueLegacyToken.")
    assert [(spec.name, spec.case_sensitive) for spec in specs] == [
        ("Session", False),
        ("issueLegacyToken", True),
    ]


def test_assess_reports_missing_and_levels(conn, lib):
    from virtudoc.coverage import assess

    none = assess(conn, lib, "issueMissingToken", [], total_matches=0)
    assert none.level == "none"
    assert none.missing == ["issueMissingToken"]

    partial = assess(conn, lib, "issueMissingToken create token legacy flow", [], total_matches=2)
    assert partial.level == "partial"
    assert partial.missing == ["issueMissingToken"]

    matched = assess(conn, lib, "validateSession", [], total_matches=1)
    assert matched.level == "matched"
    assert matched.missing == []
    assert matched.unreturned == ["validateSession"]


def test_assess_query_without_identifiers_is_matched(conn, lib):
    from virtudoc.coverage import assess

    coverage = assess(conn, lib, "how do we create a token", [], total_matches=1)
    assert coverage.level == "matched"
    assert coverage.missing == []
    assert coverage.unreturned == []


def test_assess_flags_identifier_on_another_page(conn, lib):
    from virtudoc.coverage import assess

    coverage = assess(conn, lib, "validateSession auth", ["Auth middleware text."], total_matches=4)
    assert coverage.level == "matched"
    assert coverage.unreturned == ["validateSession"]


def test_qualified_identifier_needs_word_boundary(conn, docs_dir):
    (docs_dir / "api.md").write_text("# API\n\nclient.sender is the target.\n")
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())

    assert library_has_identifier(conn, lib_id, "client.sender") is True
    assert library_has_identifier(conn, lib_id, "client.send") is False


def test_query_lowercase_of_pascal_symbol_is_not_an_identifier(conn, docs_dir):
    from virtudoc.coverage import assess

    (docs_dir / "api.md").write_text("# API\n\nHttpClient sends requests.\n")
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())

    coverage = assess(conn, lib_id, "httpclient", [], total_matches=1)
    assert coverage.level == "matched"
    assert coverage.missing == []
