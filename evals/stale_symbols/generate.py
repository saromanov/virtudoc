#!/usr/bin/env python3
"""Generate the stale-symbol evaluation corpus.

The symbol names are invented, so a model cannot know the v2 answer from
training data. Renames/deletions are applied deterministically:

* 10 symbols renamed in v2;
* 5 symbols removed in v2;
* 5 control symbols unchanged.

Usage: ``python evals/stale_symbols/generate.py``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent

RENAMED = {
    "sessionBootstrap": "sessionPrime",
    "tokenFetcher": "tokenResolver",
    "configLoader": "configReader",
    "eventRouter": "eventDispatcher",
    "cacheEvictor": "cachePurger",
    "retryPolicy": "retrySchedule",
    "auditWriter": "auditSink",
    "metricsBridge": "metricsRelay",
    "queueDrainer": "queueFlusher",
    "schemaGuard": "schemaSentinel",
}

DELETED = [
    "legacyTokenIssuer",
    "legacySessionCheck",
    "legacyAuditHook",
    "legacyCacheWarmup",
    "legacyQueueProbe",
]

UNCHANGED = [
    "validateSession",
    "buildRequest",
    "parseResponse",
    "normalizePath",
    "renderDocument",
]


def _doc(symbol: str, index: int) -> str:
    return (
        f"# Module {index}\n\n"
        f"## Overview\n\n"
        f"`{symbol}` is part of the runtime pipeline. Call it before the "
        f"request is dispatched and keep the returned handle for later checks.\n\n"
        f"## Example\n\n"
        f"```python\nhandle = {symbol}(request)\n```\n"
    )


def generate() -> None:
    for version in ("v1", "v2"):
        target = HERE / version
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)

    index = 0
    for old, new in RENAMED.items():
        index += 1
        (HERE / "v1" / f"module_{index}.md").write_text(_doc(old, index), encoding="utf-8")
        (HERE / "v2" / f"module_{index}.md").write_text(_doc(new, index), encoding="utf-8")
    for symbol in DELETED:
        index += 1
        (HERE / "v1" / f"module_{index}.md").write_text(_doc(symbol, index), encoding="utf-8")
        (HERE / "v2" / f"module_{index}.md").write_text(
            f"# Module {index}\n\n## Overview\n\nThis module was removed.\n",
            encoding="utf-8",
        )
    for symbol in UNCHANGED:
        index += 1
        for version in ("v1", "v2"):
            (HERE / version / f"module_{index}.md").write_text(
                _doc(symbol, index), encoding="utf-8"
            )

    tasks = []
    index = 0
    for old, new in RENAMED.items():
        index += 1
        tasks.append(
            {
                "id": f"rename-{index}",
                "prompt": (
                    f"Write a minimal Python call that starts the runtime pipeline "
                    f"described in module {index}. Use the current API name."
                ),
                "expected_symbol": new,
                "forbidden_symbols": [old],
                "kind": "renamed",
            }
        )
    for offset, symbol in enumerate(DELETED, start=len(RENAMED) + 1):
        tasks.append(
            {
                "id": f"deleted-{offset}",
                "prompt": (
                    f"Write a call to `{symbol}` as described in module {offset}. "
                    "If the documentation does not contain it, say so."
                ),
                "expected_symbol": None,
                "forbidden_symbols": [symbol],
                "kind": "deleted",
            }
        )
    for offset, symbol in enumerate(UNCHANGED, start=len(RENAMED) + len(DELETED) + 1):
        tasks.append(
            {
                "id": f"control-{offset}",
                "prompt": (
                    f"Write a minimal Python call that uses the API from module "
                    f"{offset}."
                ),
                "expected_symbol": symbol,
                "forbidden_symbols": [],
                "kind": "control",
            }
        )

    with open(HERE / "tasks.jsonl", "w", encoding="utf-8") as handle:
        for task in tasks:
            handle.write(json.dumps(task, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    generate()
