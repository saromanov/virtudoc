from __future__ import annotations

import json
from pathlib import Path

import pytest

from virtudoc.db import open_db
from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.indexer import add_library, index_library
from virtudoc.search.engine import TOPIC_BONUS, search

DATASET = json.loads(
    (Path(__file__).parent / "data" / "relevance.json").read_text(encoding="utf-8")
)
CASES = DATASET["cases"]
CASE_IDS = [case["name"] for case in CASES]
EQUAL_SCORE_CASES = [case for case in CASES if "equal_scores" in case]


@pytest.fixture(scope="module")
def relevance_index(tmp_path_factory):
    root = tmp_path_factory.mktemp("relevance")
    source = root / "docs"
    source.mkdir()
    for name, text in DATASET["documents"].items():
        (source / name).write_text(text, encoding="utf-8")
    conn = open_db(root / "index.db")
    library_id = add_library(conn, source, "relevance")
    index_library(conn, library_id, Chunker())
    yield conn, library_id
    conn.close()


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_relevance_dataset(relevance_index, case):
    conn, library_id = relevance_index
    result = search(conn, case["query"], library_id=library_id)

    if case.get("expected_empty"):
        assert result.snippets == []
        assert result.total_matches == 0
        return

    assert result.snippets, f"no results for {case['query']!r}"
    if "expected_top" in case:
        assert result.snippets[0].source == case["expected_top"]
    sources = {snippet.source for snippet in result.snippets}
    assert set(case.get("expected_sources", [])) <= sources


@pytest.mark.parametrize(
    "case", EQUAL_SCORE_CASES, ids=[case["name"] for case in EQUAL_SCORE_CASES]
)
def test_scores_follow_relevance_not_position(relevance_index, case):
    conn, library_id = relevance_index
    result = search(conn, case["query"], library_id=library_id)
    scores = {snippet.source: snippet.score for snippet in result.snippets}
    first, second = case["equal_scores"]
    assert scores[first] == scores[second]
    assert scores[first] > 0


def test_topic_boost_scales_the_bm25_score(relevance_index):
    conn, library_id = relevance_index
    result = search(conn, "middleware", library_id=library_id)
    scores = {snippet.source: snippet.score for snippet in result.snippets}

    assert result.snippets[0].source == "topic.md"
    assert scores["topic.md"] == pytest.approx(scores["hooks.md"] * (1 + TOPIC_BONUS), rel=1e-3)
    assert all(score > 0 for score in scores.values())
