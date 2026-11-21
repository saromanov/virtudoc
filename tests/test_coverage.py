from __future__ import annotations

import pytest

from virtudoc.coverage import (
    IdentifierChecker,
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
    assert checker.cache["validateSession"] is True
    assert checker.cache["nope"] is False


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
    assert checker.cache["validateSession"] is True
    assert checker.cache["missing599Symbol"] is False
