import pytest

from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.indexer import add_library, index_library
from virtudoc.notes import add_note, list_notes, relevant_notes, remove_note


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
