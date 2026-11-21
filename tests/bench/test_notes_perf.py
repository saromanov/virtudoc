"""Performance benchmark for note statuses and identifier coverage (TZ §10.5).

Run manually: ``pytest -m bench tests/bench -q -s``. The test writes
``bench-results.json`` for the CI artifact.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from virtudoc.config import Config
from virtudoc.db import open_db
from virtudoc.indexer.chunker import Chunker
from virtudoc.indexer.indexer import add_library, index_library
from virtudoc.mcp_server import build_server
from virtudoc.notes import add_note

pytestmark = pytest.mark.bench

DOCS = 10_000
NOTES = 100
WARMUP = 20
RUNS = 200
LIMIT_MS = 5.0


def _build_library(home: Path) -> Path:
    docs = home / "synthetic"
    docs.mkdir(parents=True)
    for index in range(DOCS):
        (docs / f"doc_{index}.md").write_text(
            f"# Doc {index}\n\n## Overview\n\nsymbol{index}Name is defined here.\n\n"
            f"## Flow\n\ncreate token legacy flow for document {index}.\n"
        )
    with open_db(Config().db_path) as conn:
        chunker = Chunker(target_tokens=200, max_tokens=400, overlap_tokens=20)
        lib_id = add_library(conn, docs, "perf")
        index_library(conn, lib_id, chunker)
        for index in range(NOTES):
            add_note(
                conn,
                lib_id,
                f"Use symbol{index}Name and symbol{index + 1}Name.",
                paths=[f"doc_{index}.md", f"doc_{index + 1}.md"],
            )
    return docs


def _query() -> str:
    # Only identifiers: prose words would match every synthetic document and
    # make search noise dominate the note overhead this benchmark measures.
    found = " ".join(f"symbol{index}Name" for index in range(5))
    missing = " ".join(f"missing{index}Name" for index in range(5))
    return f"{found} {missing}"


async def _p95_pair(with_notes: Config, baseline: Config, query: str) -> tuple[float, float]:
    """Measure both servers interleaved, so page-cache order cannot bias them."""
    servers = {
        "with_notes": build_server(with_notes),
        "baseline": build_server(baseline),
    }
    arguments = {"libraryId": "local/perf", "query": query}
    for _ in range(WARMUP):
        for server in servers.values():
            await server.call_tool("get-library-docs", arguments)

    samples: dict[str, list[int]] = {name: [] for name in servers}
    for _ in range(RUNS):
        for name, server in servers.items():
            started = time.perf_counter_ns()
            await server.call_tool("get-library-docs", arguments)
            samples[name].append(time.perf_counter_ns() - started)

    def p95(values: list[int]) -> float:
        values = sorted(values)
        return values[min(len(values) - 1, int(0.95 * len(values)))] / 1_000_000

    return p95(samples["with_notes"]), p95(samples["baseline"])


@pytest.mark.anyio
async def test_notes_overhead_p95(home):
    _build_library(home)

    with_notes = Config()
    baseline = Config()
    baseline.values["notes.max_tokens"] = 0

    # Same query for both runs, measured interleaved: the TZ baseline drops
    # the identifiers, which changes search cost instead of measuring notes.
    p95_notes, p95_baseline = await _p95_pair(with_notes, baseline, _query())
    delta = p95_notes - p95_baseline

    result = {
        "p95_with_notes_ms": round(p95_notes, 3),
        "p95_baseline_ms": round(p95_baseline, 3),
        "delta_ms": round(delta, 3),
        "limit_ms": LIMIT_MS,
        "documents": DOCS,
        "notes": NOTES,
        "runs": RUNS,
        "warmup": WARMUP,
    }
    Path("bench-results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    assert delta <= LIMIT_MS, f"notes overhead {delta:.3f} ms exceeds {LIMIT_MS} ms"
