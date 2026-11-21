#!/usr/bin/env python3
"""Run the stale-symbol evaluation against a real agent (manual, not in CI).

Conditions:

* A — control: v1 index, current server.
* B — staleness without signals: v1 solved in a session, then v2 reindexed,
  same session continues; old server build.
* C — staleness with signals: same as B, v0.3.1 server (coverage fields).

The agent must be able to run the ``claude`` CLI in headless mode with MCP
(`claude -p ... --mcp-config ...`). Point ``VIRTUDOC_BIN`` at the server build
for condition B when it differs from the current checkout.

Usage:
    python evals/stale_symbols/run.py --runs 3 --conditions a b c
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

HONEST_MARKERS = (
    "not in the documentation",
    "documentation does not",
    "no longer",
    "removed",
    "not found",
    "does not contain",
)


def _load_tasks() -> list[dict]:
    with open(HERE / "tasks.jsonl", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _virtudoc(home: Path, *args: str) -> None:
    env = {"VIRTUDOC_HOME": str(home), "PATH": os.environ.get("PATH", "")}
    if os.environ.get("SYSTEMROOT"):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    subprocess.run(
        [sys.executable, "-m", "virtudoc", *args],
        env=env,
        cwd=str(REPO),
        check=True,
        capture_output=True,
        text=True,
    )


def _mcp_config(home: Path, server_bin: str | None) -> Path:
    entry: dict
    if server_bin:
        entry = {"command": server_bin, "args": ["serve"], "env": {"VIRTUDOC_HOME": str(home)}}
    else:
        entry = {
            "command": sys.executable,
            "args": ["-m", "virtudoc", "serve"],
            "env": {"VIRTUDOC_HOME": str(home)},
        }
    path = home / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"virtudoc": entry}}), encoding="utf-8")
    return path


def _call_agent(
    prompt: str,
    *,
    config: Path,
    session_id: str,
    resume: bool,
    model: str | None,
    timeout: int,
) -> str:
    command = [
        os.environ.get("VIRTUDOC_EVAL_AGENT", "claude"),
        "-p",
        prompt,
        "--mcp-config",
        str(config),
        "--allowedTools",
        "mcp__virtudoc__*",
        "--output-format",
        "json",
        "--session-id" if not resume else "--resume",
        session_id,
    ]
    if model:
        command.extend(["--model", model])
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        return result.stdout + "\n" + result.stderr
    try:
        payload = json.loads(result.stdout)
        return str(payload.get("result") or payload.get("text") or result.stdout)
    except json.JSONDecodeError:
        return result.stdout


def _score(task: dict, answer: str) -> dict:
    lowered = answer.lower()
    forbidden = [name for name in task["forbidden_symbols"] if name.lower() in lowered]
    expected = task["expected_symbol"]
    correct = bool(expected) and expected.lower() in lowered
    honest = not answer.strip() or any(marker in lowered for marker in HONEST_MARKERS)
    return {
        "answer": answer,
        "stale_symbol": forbidden[0] if forbidden else None,
        "correct": correct,
        "honest": honest,
    }


def _metrics(rows: list[dict]) -> dict:
    total = len(rows) or 1
    return {
        "stale_rate": round(sum(1 for row in rows if row["stale_symbol"]) / total, 3),
        "correct_rate": round(sum(1 for row in rows if row["correct"]) / total, 3),
        "honest_rate": round(
            sum(1 for row in rows if row["honest"] and row["kind"] == "deleted")
            / max(1, sum(1 for row in rows if row["kind"] == "deleted")),
            3,
        ),
    }


def _prepare_workspace(condition: str, docs_source: Path, tmp: Path) -> Path:
    home = tmp / f"home-{condition}"
    docs = tmp / f"docs-{condition}"
    shutil.rmtree(docs, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)
    shutil.copytree(docs_source, docs)
    _virtudoc(home, "add", str(docs), "--name", "eval")
    return home


def _reindex(home: Path, docs_source: Path, tmp: Path) -> None:
    docs = tmp / f"docs-{home.name.removeprefix('home-')}"
    shutil.rmtree(docs)
    shutil.copytree(docs_source, docs)
    _virtudoc(home, "reindex", "eval")


def run_condition(
    condition: str,
    tasks: list[dict],
    runs: int,
    *,
    server_bin: str | None,
    model: str | None,
    timeout: int,
) -> list[dict]:
    output = HERE / "runs" / date.today().isoformat() / condition
    output.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="virtudoc-eval-") as tmp_name:
        tmp = Path(tmp_name)
        for run_index in range(runs):
            home = _prepare_workspace(condition, HERE / "v1", tmp)
            config = _mcp_config(home, server_bin)
            session_id = str(uuid.uuid4())
            first_pass = condition in ("b", "c")
            if first_pass:
                # Warm the session's context on v1 before docs change.
                _call_agent(
                    "Read module 1 and remember its API name.",
                    config=config,
                    session_id=session_id,
                    resume=False,
                    model=model,
                    timeout=timeout,
                )
                _reindex(home, HERE / "v2", tmp)
            for position, task in enumerate(tasks):
                answer = _call_agent(
                    task["prompt"],
                    config=config,
                    session_id=session_id,
                    resume=first_pass or position > 0,
                    model=model,
                    timeout=timeout,
                )
                row = {
                    "task": task["id"],
                    "kind": task["kind"],
                    "run": run_index + 1,
                    **_score(task, answer),
                }
                rows.append(row)
                (output / f"{task['id']}-{run_index + 1}.json").write_text(
                    json.dumps(row, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--conditions", nargs="+", default=["a", "b", "c"])
    parser.add_argument("--model", default=os.environ.get("VIRTUDOC_EVAL_MODEL"))
    parser.add_argument("--server", default=os.environ.get("VIRTUDOC_BIN"))
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    tasks = _load_tasks()
    summary: dict[str, dict] = {}
    for condition in args.conditions:
        condition = condition.lower()
        rows = run_condition(
            condition,
            tasks,
            args.runs,
            server_bin=args.server,
            model=args.model,
            timeout=args.timeout,
        )
        summary[condition] = _metrics(rows)
        print(condition, json.dumps(summary[condition]))
    output = HERE / "runs" / date.today().isoformat() / "summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {"model": args.model, "date": date.today().isoformat(), "conditions": summary},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
