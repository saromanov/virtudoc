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

## Tools

Tool names follow the Context7 convention, so existing agent rules that
mention `resolve-library-id` / `get-library-docs` keep working unchanged.

### resolve-library-id

```
resolve-library-id(libraryName: string, query: string)
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
  libraryId: string,      // from resolve-library-id, e.g. "local/mylib"
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

> When working with `<library>`, resolve its id with `resolve-library-id`
> and fetch current docs with `get-library-docs` before writing code. Use
> keyword queries; check `version_mismatch` and project notes in the
> response.
