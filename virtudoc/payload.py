"""Response assembly for `get-library-docs` (FR-9 key order)."""

from __future__ import annotations

from typing import Any

from .coverage import Coverage
from .notes import NotesPayload, note_payload

NO_COVERAGE_MESSAGE = (
    "no fragments found; the indexed documentation does not cover this query. "
    "Do not answer from memory: rephrase with the library's keywords or state "
    "that the documentation does not cover it. Top-level sections of this library:"
)


def partial_message(missing: list[str]) -> str:
    names = ", ".join(missing)
    return (
        f"the indexed documentation does not mention {names}. The fragments "
        "below are related matches, not documentation of these identifiers. "
        "Do not use them from memory: they may have been renamed or removed. "
        "Tell the user if the documentation does not cover them."
    )


def unreturned_message(unreturned: list[str], page: int) -> str:
    names = ", ".join(unreturned)
    return (
        f"{names} exist in the documentation but are not on this page; "
        f"query them directly or use page={page + 1}"
    )


def coverage_message(coverage: Coverage, *, page: int, has_fragments: bool) -> str | None:
    """Coverage text, then unreturned, then the page note (FR-1.5)."""
    messages: list[str] = []
    if coverage.level == "none":
        messages.append(NO_COVERAGE_MESSAGE)
    elif coverage.level == "partial":
        messages.append(partial_message(coverage.missing))
    if coverage.unreturned:
        messages.append(unreturned_message(coverage.unreturned, page))
    if not has_fragments and coverage.level != "none":
        messages.append(f"no fragments on page {page}")
    return " ".join(messages) or None


def doc_payload(
    warning: dict | None,
    *,
    library_id: str,
    query: str,
    context: dict[str, Any],
    coverage: Coverage,
    notes: NotesPayload,
    message: str | None,
    body_key: str,
    body: Any,
    trailing: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the response in the documented key order (FR-9).

    `version_mismatch` → `library_id` → `query` → mode/page/section →
    `coverage` → `missing_identifiers` → `unreturned_identifiers` →
    `message` → `notes_warning` → `notes` → `notes_omitted` →
    `snippets`/`sections` → totals.
    """
    payload: dict[str, Any] = {}
    if warning:
        payload["version_mismatch"] = warning
    payload["library_id"] = library_id
    payload["query"] = query
    payload.update(context)
    payload["coverage"] = coverage.level
    payload["missing_identifiers"] = coverage.missing
    payload["unreturned_identifiers"] = coverage.unreturned
    if message:
        payload["message"] = message
    if notes.warning:
        payload["notes_warning"] = notes.warning
    payload["notes"] = [note_payload(note) for note in notes.notes]
    if notes.omitted:
        payload["notes_omitted"] = notes.omitted
    payload[body_key] = body
    payload.update(trailing)
    return payload
