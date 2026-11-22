import pytest

from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.indexer import add_library, index_library
from virtudoc.notes import (
    add_note,
    list_notes,
    relevant_notes,
    remove_note,
    source_revision,
    stale_notes,
)


@pytest.fixture
def lib(conn, docs_dir):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    return lib_id


def test_add_and_list_notes(conn, lib):
    note_id = add_note(conn, lib, "Prefer the middleware pipeline for auth checks.")
    notes = list_notes(conn, lib)
    assert len(notes) == 1
    assert notes[0].id == note_id
    assert "middleware" in notes[0].text


def test_remove_note(conn, lib):
    note_id = add_note(conn, lib, "Temporary note.")
    assert remove_note(conn, lib, note_id) is True
    assert list_notes(conn, lib) == []
    assert remove_note(conn, lib, note_id) is False


def test_relevant_notes_match_by_topic(conn, lib):
    add_note(conn, lib, "Always use the middleware pipeline for auth checks.")
    add_note(conn, lib, "Database migrations run automatically on deploy.")
    notes = relevant_notes(conn, lib, "how does auth middleware work")
    assert len(notes) == 1
    assert "middleware" in notes[0].text
    assert notes[0].score >= 2


def test_relevant_notes_empty_for_unrelated_query(conn, lib):
    add_note(conn, lib, "Database migrations run automatically on deploy.")
    assert relevant_notes(conn, lib, "quantum chromodynamics") == []


def test_relevant_notes_respect_budget(conn, lib):
    for i in range(10):
        add_note(conn, lib, f"Middleware convention number {i}: " + "detail " * 60)
    notes = relevant_notes(conn, lib, "middleware", budget_tokens=300)
    assert 1 <= len(notes) < 10
    total = sum(len(n.text) // 4 + 1 for n in notes)
    assert total <= 700


def test_notes_survive_reindex(conn, lib, docs_dir):
    add_note(conn, lib, "Team convention: use keyword queries.")
    index_library(conn, lib, Chunker())
    assert len(list_notes(conn, lib)) == 1


def test_notes_removed_with_library(conn, lib):
    add_note(conn, lib, "Some convention.")
    from virtudoc.indexer.indexer import delete_library

    delete_library(conn, "demo")
    assert list_notes(conn, lib) == []


def test_relevant_notes_empty_query(conn, lib):
    add_note(conn, lib, "Middleware note.")
    assert relevant_notes(conn, lib, " ,, ") == []


def test_relevant_notes_skips_oversized_first_note(conn, lib):
    from virtudoc.notes import select_notes

    add_note(conn, lib, "middleware " * 500)
    selection = select_notes(conn, lib, "middleware", budget_tokens=60)
    assert selection.notes == []
    assert selection.omitted == 1


def test_relevant_notes_match_whole_words_not_substrings(conn, lib):
    add_note(conn, lib, "Capital city office handling.")
    assert relevant_notes(conn, lib, "api") == []

    add_note(conn, lib, "Authentication helper for middleware.")
    notes = relevant_notes(conn, lib, "auth")
    assert len(notes) == 1
    assert "Authentication" in notes[0].text


def test_relevant_notes_never_exceed_budget(conn, lib):
    from virtudoc.tokens import estimate_tokens

    for index in range(5):
        add_note(conn, lib, f"middleware note {index}: " + "detail " * 80)
    notes = relevant_notes(conn, lib, "middleware", budget_tokens=300)
    assert notes
    assert sum(estimate_tokens(note.text) for note in notes) <= 300


def test_add_note_records_source_revision(conn, lib):
    add_note(conn, lib, "Team convention.")
    note = list_notes(conn, lib)[0]
    revision = source_revision(conn, lib)
    assert note.source_rev == revision
    assert len(revision) == 12


def test_source_revision_ignores_reindex_without_changes(conn, lib):
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import index_library

    before = source_revision(conn, lib)
    index_library(conn, lib, Chunker())
    assert source_revision(conn, lib) == before


def test_stale_notes_flagged_after_source_changes(conn, lib, docs_dir):
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import index_library

    add_note(conn, lib, "Use the retry helper for all outbound calls.", paths=["api.md"])
    assert stale_notes(conn, lib) == []

    (docs_dir / "api.md").write_text("# API Reference\n\n## Retry\n\nRetry moved to core.\n")
    index_library(conn, lib, Chunker())

    stale = stale_notes(conn, lib)
    assert [note.text for note in stale] == ["Use the retry helper for all outbound calls."]
    assert stale[0].stale_reason == "anchor_changed"
    assert stale[0].stale_details == ["api.md"]


def test_note_added_after_change_is_fresh(conn, lib, docs_dir):
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import index_library

    add_note(conn, lib, "Old convention.", paths=["api.md"])
    (docs_dir / "api.md").write_text("# API Reference\n\n## Retry\n\nRetry moved to core.\n")
    index_library(conn, lib, Chunker())
    add_note(conn, lib, "New convention.", paths=["api.md"])

    assert [note.text for note in stale_notes(conn, lib)] == ["Old convention."]


def test_stale_notes_skips_notes_without_revision(conn, lib):
    add_note(conn, lib, "Legacy note.", source_rev="")
    assert stale_notes(conn, lib) == []


def test_stale_details_match_the_reason(conn, lib, docs_dir):
    from virtudoc.indexer.chunker import Chunker
    from virtudoc.indexer.indexer import index_library
    from virtudoc.notes import note_payload, note_statuses

    add_note(conn, lib, "Use the retry helper.", paths=["api.md"])
    (docs_dir / "api.md").write_text("# API\n\n## Retry\n\nMoved.\n")
    index_library(conn, lib, Chunker())

    note = note_statuses(conn, lib)[0]
    payload = note_payload(note)
    assert note.stale_details == ["api.md"]
    assert payload["stale_details"] == ["api.md"]
    assert "changed_sources" not in payload


def test_remove_note_deletes_symbols_without_foreign_keys(conn, lib, docs_dir):
    from virtudoc.notes import note_symbols

    (docs_dir / "api.md").write_text("# API\n\nvalidateSession() here.\n")
    index_library(conn, lib, Chunker())
    note_id = add_note(conn, lib, "Use validateSession here.")
    assert note_symbols(conn, note_id)

    conn.execute("PRAGMA foreign_keys=OFF")
    assert remove_note(conn, lib, note_id) is True
    assert conn.execute("SELECT COUNT(*) FROM note_symbols").fetchone()[0] == 0


def test_library_removal_deletes_symbols(conn, lib, docs_dir):
    from virtudoc.indexer.indexer import delete_library

    (docs_dir / "api.md").write_text("# API\n\nvalidateSession() here.\n")
    index_library(conn, lib, Chunker())
    add_note(conn, lib, "Use validateSession here.")
    delete_library(conn, "demo")
    assert conn.execute("SELECT COUNT(*) FROM note_symbols").fetchone()[0] == 0


def test_relevant_notes_preserve_text_byte_for_byte(conn, lib):
    text = "middleware rule:\n  two  spaces\tand `validateSession`."
    add_note(conn, lib, text, no_anchor=True)
    assert [note.text for note in relevant_notes(conn, lib, "middleware")] == [text]
