"""Recommendation block that makes agents call the virtudoc MCP server."""

from __future__ import annotations

from pathlib import Path

AGENTS_FILE = "AGENTS.md"
CLAUDE_FILE = "CLAUDE.md"
# A unique marker instead of the bare product name: a file that merely
# mentions virtudoc is not an annotated file yet.
MARKER = "<!-- virtudoc:agents-hint -->"

AGENTS_HINT = f"""## Project documentation

{MARKER}

Project documentation lives in the virtudoc MCP server. Before writing code
against a library, call `find-library` and then `get-library-docs` with
keyword queries; search again when an API or snippet does not work.
"""


def hint_path(project_dir: Path) -> Path:
    """AGENTS.md in the project, or CLAUDE.md when only that file exists.

    A non-regular-file AGENTS.md (for example a directory) is never replaced;
    ``ensure_agents_hint`` reports it as an error instead.
    """
    project_dir = Path(project_dir).expanduser()
    agents = project_dir / AGENTS_FILE
    claude = project_dir / CLAUDE_FILE
    if not agents.is_file() and claude.is_file():
        return claude
    return agents


def ensure_agents_hint(project_dir: Path) -> tuple[Path, bool]:
    """Write the recommendation block unless the file already carries it.

    Returns the target path and whether the file changed. Idempotent: a second
    run on an annotated file leaves it untouched. Raises ``ValueError`` when
    the target cannot be read or written (non-UTF-8 file, read-only directory,
    a directory in place of the file).
    """
    path = hint_path(project_dir)
    if path.exists() and not path.is_file():
        raise ValueError(f"cannot write agents hint: {path}: not a regular file")
    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        if MARKER in existing:
            return path, False
        separator = ""
        if existing:
            separator = "\n" if existing.endswith("\n") else "\n\n"
        path.write_text(existing + separator + AGENTS_HINT, encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot write agents hint: {path}: {exc}") from exc
    return path, True
