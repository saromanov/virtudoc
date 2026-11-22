"""Recommendation block that makes agents call the virtudoc MCP server."""

from __future__ import annotations

from pathlib import Path

AGENTS_FILE = "AGENTS.md"
CLAUDE_FILE = "CLAUDE.md"
MARKER = "virtudoc"

AGENTS_HINT = """## Project documentation

Project documentation lives in the virtudoc MCP server. Before writing code
against a library, call `find-library` and then `get-library-docs` with
keyword queries; search again when an API or snippet does not work.
"""


def hint_path(project_dir: Path) -> Path:
    """AGENTS.md in the project, or CLAUDE.md when only that file exists."""
    project_dir = Path(project_dir).expanduser()
    agents = project_dir / AGENTS_FILE
    claude = project_dir / CLAUDE_FILE
    if not agents.exists() and claude.is_file():
        return claude
    return agents


def ensure_agents_hint(project_dir: Path) -> tuple[Path, bool]:
    """Write the recommendation block unless the file already mentions virtudoc.

    Returns the target path and whether the file changed. Idempotent: a second
    run on an annotated file leaves it untouched.
    """
    path = hint_path(project_dir)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if MARKER in existing.lower():
        return path, False
    separator = ""
    if existing:
        separator = "\n" if existing.endswith("\n") else "\n\n"
    path.write_text(existing + separator + AGENTS_HINT, encoding="utf-8")
    return path, True
