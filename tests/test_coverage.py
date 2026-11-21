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
