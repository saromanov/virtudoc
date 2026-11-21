"""Performance benchmark for note statuses and identifier coverage (TZ §10.5).

Run manually: ``pytest -m bench tests/bench -q -s``. The test writes
``bench-results.json`` for the CI artifact.

The corpus is cached on disk between runs: building 10 000 documents and 100
notes once is enough, and a fresh database would dominate every run. Delete
``$VIRTUDOC_BENCH_CACHE`` (or ``~/.cache/virtudoc-bench``) to regenerate it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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
CACHE_ENV = "VIRTUDOC_BENCH_CACHE"


@pytest.fixture(scope="session")
def bench_home() -> Path:
    home = Path(os.environ.get(CACHE_ENV, Path.home() / ".cache" / "virtudoc-bench"))
    ready = home / ".ready"
    if ready.is_file() and (home / "index.db").is_file():
        return home
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    previous = os.environ.get("VIRTUDOC_HOME")
    os.environ["VIRTUDOC_HOME"] = str(home)
    try:
        _build_library(home)
    finally:
        if previous is None:
            os.environ.pop("VIRTUDOC_HOME", None)
        else:
            os.environ["VIRTUDOC_HOME"] = previous
    ready.write_text("ok\n", encoding="utf-8")
    return home


def _build_library(home: Path) -> None:
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


def _query() -> str:
    found = " ".join(f"symbol{index}Name" for index in range(5))
    missing = " ".join(f"missing{index}Name" for index in range(5))
    return f"{found} {missing}"


def _baseline_query() -> str:
    # No identifiers: the baseline isolates the cost of coverage checks and
    # note statuses from identifier work.
    return "create token legacy flow"


def _percentile(values: list[int], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(fraction * len(ordered)))
    return ordered[index] / 1_000_000


async def _measure(
    with_notes: Config, baseline: Config
) -> tuple[dict[str, float], dict[str, float]]:
    """Measure both servers interleaved, so page-cache order cannot bias them."""
    servers = {
        "with_notes": build_server(with_notes),
        "baseline": build_server(baseline),
    }
    arguments = {
        "with_notes": {"libraryId": "local/perf", "query": _query()},
        "baseline": {"libraryId": "local/perf", "query": _baseline_query()},
    }
    for _ in range(WARMUP):
        for name, server in servers.items():
            await server.call_tool("get-library-docs", arguments[name])

    samples: dict[str, list[int]] = {name: [] for name in servers}
    for _ in range(RUNS):
        for name, server in servers.items():
            started = time.perf_counter_ns()
            await server.call_tool("get-library-docs", arguments[name])
            samples[name].append(time.perf_counter_ns() - started)

    def stats(values: list[int]) -> dict[str, float]:
        return {
            "p50": round(_percentile(values, 0.50), 3),
            "p95": round(_percentile(values, 0.95), 3),
            "p99": round(_percentile(values, 0.99), 3),
        }

    return stats(samples["with_notes"]), stats(samples["baseline"])


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@pytest.mark.anyio
async def test_notes_overhead_p95(bench_home, monkeypatch):
    monkeypatch.setenv("VIRTUDOC_HOME", str(bench_home))

    with_notes = Config()
    baseline = Config()
    baseline.values["notes.max_tokens"] = 0

    notes_stats, baseline_stats = await _measure(with_notes, baseline)
    delta = notes_stats["p95"] - baseline_stats["p95"]

    result = {
        "p50_with_notes_ms": notes_stats["p50"],
        "p95_with_notes_ms": notes_stats["p95"],
        "p99_with_notes_ms": notes_stats["p99"],
        "p50_baseline_ms": baseline_stats["p50"],
        "p95_baseline_ms": baseline_stats["p95"],
        "p99_baseline_ms": baseline_stats["p99"],
        "delta_ms": round(delta, 3),
        "limit_ms": LIMIT_MS,
        "documents": DOCS,
        "notes": NOTES,
        "runs": RUNS,
        "warmup": WARMUP,
        "commit": _commit(),
    }
    Path("bench-results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    assert delta <= LIMIT_MS, f"notes overhead {delta:.3f} ms exceeds {LIMIT_MS} ms"
