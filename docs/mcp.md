# MCP Integration and Tools

VirtuDoc speaks the Model Context Protocol, so any MCP-capable agent can
use it: Claude Code, Cursor, OpenCode, VS Code and others.

## Connecting

**Claude Code** — `.mcp.json` in the project root, or:

```bash
claude mcp add virtudoc --scope user -- virtudoc serve
```

```json
{
  "mcpServers": {
    "virtudoc": { "command": "virtudoc", "args": ["serve"] }
  }
}
```

**Cursor** — `~/.cursor/mcp.json` or `.cursor/mcp.json` in the project:

```json
{
  "mcpServers": {
    "virtudoc": { "command": "virtudoc", "args": ["serve"] }
  }
}
```

**OpenCode** — `opencode.json` (project root or `~/.config/opencode/`):

```json
{
  "mcp": {
    "virtudoc": {
      "type": "local",
      "command": ["virtudoc", "serve"],
      "enabled": true
    }
  }
}
```

Agents spawn the server with a default environment, so the data directory
is `~/.virtudoc`. Add an `env` block with `VIRTUDOC_HOME` to override.

## CLI + skill mode (no MCP setup)

If the agent can run shell commands, skip the MCP configuration entirely.
Install the bundled skill and let the agent drive the CLI:

```bash
cp -r skills/virtudoc-docs ~/.kimi-code/skills/     # Kimi Code, user-wide
# or project-wide: .kimi-code/skills/ (Kimi Code), .agents/skills/ (generic)
```

The skill teaches the `virtudoc list` → `virtudoc search --name ...`
workflow. For agents without a skill mechanism, add the CLI variant of the
rule below to the project's `AGENTS.md` / `CLAUDE.md` instead.

## Tools

The tool names are unique to virtudoc: they do not collide with
[Context7](https://github.com/upstash/context7) tools, so an agent
connected to both servers can always tell which one to call.

### find-library

```
find-library(libraryName: string, query: string)
```

Finds the library for a question. Returns up to five candidates:

```json
{
  "libraries": [
    {
      "id": "local/mylib",
      "name": "mylib",
      "description": "...",
      "chunk_count": 120,
      "updated_at": "2026-11-13T10:00:00+00:00"
    }
  ]
}
```

When nothing matches, `libraries` is empty and `message` explains how to
register a source with `virtudoc add`.

### get-library-docs

```
get-library-docs(
  libraryId: string,      // from find-library, e.g. "local/mylib"
  query: string,          // keyword query
  tokens: int = 5000,     // output budget, max 20000
  language: string,       // preferred code-example language (optional)
  page: int = 1,          // continuation
  mode: string = "fragments",   // or "outline"
  section: string         // restrict to a branch, e.g. "API -> Middleware"
)
```

Fragments mode returns:

```json
{
  "library_id": "local/mylib",
  "query": "authenticate request",
  "page": 1,
  "snippets": [
    {
      "heading_path": "API -> Authentication",
      "source": "api/auth.md",
      "text": "Markdown fragment...",
      "score": 1.25
    }
  ],
  "notes": [{ "id": 1, "text": "...", "created_at": "..." }],
  "total_tokens": 420
}
```

- When docs and project versions diverge (see
  [Version drift](versions.md)), the output starts with a
  `version_mismatch` block naming both versions and the manifest.
- When nothing matches, `snippets` is empty and `sections` lists the
  library's top-level sections to help rephrase the query.
- Exceeding the budget is not an error: output is cut at a chunk boundary.

Outline mode (`mode: "outline"`) returns the section tree with chunk counts
and token estimates instead of fragments — the agent can plan what to load
before spending tokens. `section` works in both modes.

## Suggested agent rules

Add a line to your project's `AGENTS.md` / `CLAUDE.md` to make usage
systematic:

> When working with `<library>`, resolve its id with `find-library`
> and fetch current docs with `get-library-docs` before writing code. Use
> keyword queries; check `version_mismatch` and project notes in the
> response.

CLI-only variant (no MCP server configured):

> When a question concerns `<library>`, check `virtudoc list` and answer
> from `virtudoc search --name <library> "<keywords>"` before relying on
> training data.
