from __future__ import annotations

import pytest

from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.indexer import add_library, index_library
from virtudoc.notes import (
    add_note,
    edit_note,
    note_anchors,
    note_statuses,
    relevant_notes,
    review_note,
    select_notes,
    set_pinned,
    stale_notes,
)


@pytest.fixture
def lib(conn, docs_dir):
    lib_id = add_library(conn, docs_dir, "demo")
    index_library(conn, lib_id, Chunker())
    return lib_id


def _reindex(conn, lib_id):
    index_library(conn, lib_id, Chunker())


def test_explicit_anchor_is_current(conn, lib):
    note_id = add_note(conn, lib, "Use the retry helper.", paths=["api.md"])
    note = note_statuses(conn, lib)[0]
    assert note.id == note_id
    assert note.status == "current"
    assert note.stale_reason is None
    assert [a["rel_path"] for a in note_anchors(conn, note_id, lib)] == ["api.md"]


def test_changed_anchor_reports_reason_and_sources(conn, lib, docs_dir):
    add_note(conn, lib, "Use the retry helper.", paths=["api.md"])
    (docs_dir / "api.md").write_text("# API Reference\n\n## Retry\n\nMoved.\n")
    _reindex(conn, lib)

    note = note_statuses(conn, lib)[0]
    assert note.status == "stale"
    assert note.stale_reason == "anchor_changed"
    assert note.changed_sources == ["api.md"]


def test_deleted_anchor_reports_anchor_deleted(conn, lib, docs_dir):
    add_note(conn, lib, "Use the retry helper.", paths=["api.md"])
    (docs_dir / "api.md").unlink()
    _reindex(conn, lib)

    note = note_statuses(conn, lib)[0]
    assert note.status == "stale"
    assert note.stale_reason == "anchor_deleted"
    assert note.changed_sources == ["api.md"]


def test_auto_anchors_ignore_unrelated_document_changes(conn, lib, docs_dir):
    add_note(conn, lib, "middleware pipeline context handler")
    note = note_statuses(conn, lib)[0]
    assert note.anchor_kind == "documents"
    assert note.status == "current"

    (docs_dir / "queue.txt").write_text("Совсем другой текст про очереди.\n")
    _reindex(conn, lib)
    assert note_statuses(conn, lib)[0].status == "current"

    (docs_dir / "guide.md").write_text("# Руководство\n\nСовсем другая документация.\n")
    _reindex(conn, lib)
    stale = note_statuses(conn, lib)[0]
    assert stale.status == "stale"
    assert stale.stale_reason == "anchor_changed"


def test_note_without_matching_document_falls_back_to_library(conn, lib, docs_dir):
    add_note(conn, lib, "quantum chromodynamics conventions")
    note = note_statuses(conn, lib)[0]
    assert note.anchor_kind == "library"
    assert note.status == "current"

    (docs_dir / "queue.txt").write_text("Изменённый текст очереди.\n")
    _reindex(conn, lib)

    stale = note_statuses(conn, lib)[0]
    assert stale.status == "stale"
    assert stale.stale_reason == "library_changed"


def test_no_anchor_note_is_unknown_and_never_warns(conn, lib, docs_dir):
    add_note(conn, lib, "Middleware note unrelated to docs.", no_anchor=True)
    note = note_statuses(conn, lib)[0]
    assert note.anchor_kind == "none"
    assert note.status == "unknown"

    (docs_dir / "guide.md").write_text("# changed\n")
    _reindex(conn, lib)

    assert note_statuses(conn, lib)[0].status == "unknown"
    assert stale_notes(conn, lib) == []


def test_unknown_path_error_suggests_closest(conn, lib):
    with pytest.raises(ValueError) as excinfo:
        add_note(conn, lib, "Note.", paths=["guide.mdx"])
    message = str(excinfo.value)
    assert "guide.mdx" in message
    assert "closest: guide.md" in message


def test_review_clears_staleness(conn, lib, docs_dir):
    note_id = add_note(conn, lib, "Use the retry helper.", paths=["api.md"])
    (docs_dir / "api.md").write_text("# API Reference\n\n## Retry\n\nMoved.\n")
    _reindex(conn, lib)
    assert note_statuses(conn, lib)[0].status == "stale"

    assert review_note(conn, lib, note_id) is True
    note = note_statuses(conn, lib)[0]
    assert note.status == "current"
    assert note.reviewed_at is not None


def test_review_reanchor_picks_current_documents(conn, lib, docs_dir):
    note_id = add_note(conn, lib, "middleware pipeline context handler", paths=["api.md"])
    (docs_dir / "api.md").write_text("# API Reference\n\n## Retry\n\nMoved.\n")
    _reindex(conn, lib)
    assert note_statuses(conn, lib)[0].status == "stale"

    assert review_note(conn, lib, note_id, reanchor=True) is True
    anchors = [row["rel_path"] for row in note_anchors(conn, note_id, lib)]
    assert "guide.md" in anchors
    assert note_statuses(conn, lib)[0].status == "current"


def test_edit_updates_text_and_reviews(conn, lib):
    note_id = add_note(conn, lib, "Old convention.", no_anchor=True)
    assert edit_note(conn, lib, note_id, "New convention.") is True
    note = note_statuses(conn, lib)[0]
    assert note.text == "New convention."
    assert note.status in ("current", "unknown")


def test_pinned_notes_come_first_and_ignore_query(conn, lib):
    add_note(conn, lib, "Database migrations run on deploy.")
    pinned_id = add_note(conn, lib, "Never call the legacy client.", pinned=True)

    notes = relevant_notes(conn, lib, "middleware auth")
    assert [note.id for note in notes] == [pinned_id]


def test_pinned_budget_is_respected(conn, lib):
    for _ in range(4):
        add_note(conn, lib, "Pinned convention: " + "detail " * 100, pinned=True)

    selection = select_notes(conn, lib, "unrelated", budget_tokens=2000)
    assert selection.notes
    assert selection.omitted
    assert sum(len(note.text) // 4 for note in selection.notes) < 2000


def test_notes_belong_to_logical_library_across_versions(conn, docs_dir, tmp_path):
    canonical = add_library(conn, docs_dir, "demo")
    index_library(conn, canonical, Chunker())
    add_note(conn, canonical, "Use the retry helper.", paths=["api.md"])

    other = tmp_path / "docs2"
    other.mkdir()
    (other / "api.md").write_text("# API\n\n## Retry\n\nDifferent text.\n")
    variant = add_library(conn, other, "demo", version="2.0")
    index_library(conn, variant, Chunker())
    assert variant == "local/demo@2.0"

    note = note_statuses(conn, variant)[0]
    assert note.status == "stale"
    assert note.stale_reason == "version_differs"

    assert review_note(conn, variant, note.id, reanchor=True) is True
    assert note_statuses(conn, variant)[0].status == "current"
    assert note_statuses(conn, canonical)[0].status == "current"


def test_version_scoped_note_is_hidden_from_other_versions(conn, docs_dir, tmp_path):
    canonical = add_library(conn, docs_dir, "demo")
    index_library(conn, canonical, Chunker())
    add_note(conn, canonical, "Middleware rule for 2.0.", scope_version="2.0", no_anchor=True)

    other = tmp_path / "docs2"
    other.mkdir()
    (other / "api.md").write_text("# API\n")
    variant = add_library(conn, other, "demo", version="2.0")
    index_library(conn, variant, Chunker())

    assert select_notes(conn, canonical, "middleware").notes == []
    assert len(select_notes(conn, variant, "middleware").notes) == 1


def test_pin_and_unpin(conn, lib):
    note_id = add_note(conn, lib, "Convention.", no_anchor=True)
    assert set_pinned(conn, lib, note_id, True) is True
    assert note_statuses(conn, lib)[0].pinned is True
    assert set_pinned(conn, lib, note_id, False) is True
    assert note_statuses(conn, lib)[0].pinned is False


def test_symbols_only_note_when_autosearch_finds_nothing(conn, lib, docs_dir, monkeypatch):
    import virtudoc.notes as notes_module

    (docs_dir / "api.md").write_text(
        "# API\n\nvalidateSession() and issueLegacyToken are used here.\n"
    )
    _reindex(conn, lib)
    monkeypatch.setattr(notes_module, "auto_anchor_documents", lambda *args, **kwargs: [])

    add_note(conn, lib, "Call validateSession() before issueLegacyToken.")
    note = note_statuses(conn, lib)[0]
    assert note.anchor_kind == "symbols"
    assert note.symbols == ["issueLegacyToken", "validateSession"]
    assert note.status == "current"


def test_removed_symbol_makes_note_stale(conn, lib, docs_dir):
    (docs_dir / "api.md").write_text(
        "# API\n\nvalidateSession() and issueLegacyToken are used here.\n"
    )
    _reindex(conn, lib)
    add_note(conn, lib, "Call validateSession() before issueLegacyToken.", paths=["api.md"])

    (docs_dir / "api.md").write_text("# API\n\nvalidateSession() only.\n")
    _reindex(conn, lib)

    note = note_statuses(conn, lib)[0]
    assert note.status == "stale"
    assert note.stale_reason == "symbol_removed"
    assert note.missing_symbols == ["issueLegacyToken"]


def test_review_refuses_to_accept_removed_symbol(conn, lib, docs_dir):
    from virtudoc.notes import SymbolRemovedError

    (docs_dir / "api.md").write_text("# API\n\nissueLegacyToken here.\n")
    _reindex(conn, lib)
    note_id = add_note(conn, lib, "Use issueLegacyToken.", paths=["api.md"])
    (docs_dir / "api.md").write_text("# API\n\nSomething else.\n")
    _reindex(conn, lib)

    with pytest.raises(SymbolRemovedError) as excinfo:
        review_note(conn, lib, note_id)
    assert "issueLegacyToken" in str(excinfo.value)

    assert review_note(conn, lib, note_id, reanchor=True) is True
    note = note_statuses(conn, lib)[0]
    assert note.status != "stale"


def test_mentions_missing_marks_note_text(conn, lib):
    from virtudoc.notes import apply_mentions_missing

    add_note(conn, lib, "Prefer retryWithJitter for flaky calls.", no_anchor=True)
    notes = note_statuses(conn, lib)
    apply_mentions_missing(notes, ["retryWithJitter", "somethingElse"])
    assert notes[0].mentions_missing == ["retryWithJitter"]
    assert note_statuses(conn, lib)[0].status == "unknown"


def test_remove_variant_keeps_notes_until_the_last_one(conn, docs_dir, tmp_path):
    from virtudoc.indexer.indexer import add_library, delete_library

    canonical = add_library(conn, docs_dir, "demo")
    index_library(conn, canonical, Chunker())
    add_note(conn, canonical, "Convention.", paths=["api.md"])

    other = tmp_path / "docs2"
    other.mkdir()
    (other / "api.md").write_text("# API\n")
    variant = add_library(conn, other, "demo", version="2.0")
    index_library(conn, variant, Chunker())

    delete_library(conn, variant)
    assert [note.text for note in note_statuses(conn, canonical)] == ["Convention."]
    assert conn.execute("SELECT COUNT(*) FROM libraries").fetchone()[0] == 1

    delete_library(conn, canonical)
    assert note_statuses(conn, canonical) == []


def test_replace_keeps_anchors_and_reports_change(conn, lib, docs_dir):
    from virtudoc.indexer.indexer import add_library

    add_note(conn, lib, "Use the retry helper.", paths=["api.md"])
    (docs_dir / "api.md").write_text("# API Reference\n\nRetry moved.\n")
    add_library(conn, docs_dir, "demo", replace=True)
    _reindex(conn, lib)

    note = note_statuses(conn, lib)[0]
    assert note.status == "stale"
    assert note.stale_reason == "anchor_changed"
