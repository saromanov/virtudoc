# MCP Integration and Tools

VirtuDoc speaks the Model Context Protocol, so any MCP-capable agent can
use it: Claude Code, Cursor, OpenCode, VS Code and others.

## Connecting

Agents spawn the server with their own environment, where `virtudoc` may
not be on `PATH` (typical for pipx/venv installs). If the tools never
appear in the agent, use the absolute path as `command` — find it with
`which virtudoc` — or add the server's `bin` directory to the MCP config's
`env.PATH`.

The fastest way to get a correct snippet is:

```bash
virtudoc mcp-config
```

It prints a ready `mcpServers` block with the absolute server path and,
when `VIRTUDOC_HOME` is set in your shell, an `env` block that pins the
data directory for the agent.

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

## Streamable HTTP and authentication

`virtudoc serve --http` listens on `127.0.0.1:8765` by default. Binding to a
non-loopback host requires an explicit bearer token:

```toml
# ~/.virtudoc/config.toml
[server]
http_host = "0.0.0.0"
http_token = "use-a-long-random-secret"
```

`VIRTUDOC_HTTP_TOKEN` overrides the config value. The server refuses to start
on a non-loopback host without either setting. HTTP clients must send
`Authorization: Bearer <token>` on every request; missing or invalid tokens
receive `401 Unauthorized`. If a token is configured on loopback, it is also
enforced there.

A bearer token over plain HTTP is sent in cleartext. It restricts access on a
trusted network only. Before exposing a non-loopback interface to an
untrusted network, put the server behind a TLS-terminating reverse proxy.

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

The tool names are unique to virtudoc, so an agent connected to other
documentation servers can always tell which one to call.

### find-library

```
find-library(libraryName: string, query: string, projectPath: string | null)
```

Finds the library for a question. Returns up to five candidates:

```json
{
  "libraries": [
    {
      "id": "local/mylib@2.2.0",
      "name": "mylib",
      "description": "...",
      "chunk_count": 120,
      "updated_at": "2026-11-13T10:00:00+00:00",
      "version": "2.2.0",
      "channel": "release",
      "version_status": "match"
    }
  ],
  "available_versions": [
    { "id": "local/mylib", "name": "mylib", "version": "2.1.0", "channel": "release" },
    { "id": "local/mylib@2.2.0", "name": "mylib", "version": "2.2.0", "channel": "release" }
  ],
  "project": {
    "path": "/path/to/project",
    "manifest": "/path/to/project/uv.lock",
    "dependency_name": "mylib",
    "dependency_version": "2.2.0"
  }
}
```

The version is resolved from the project's lockfile or dependency manifest
before ranking: lockfiles first, then direct manifests. `projectPath` is
optional and defaults to the server working directory. The release whose
version matches the project ranks first; working branches never outrank a
release. When the resolved version is not indexed, the response adds
`version_not_indexed: true` and a `message` telling the agent to ask the
user before using another version.

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
  section: string,        // restrict to a branch, e.g. "API -> Middleware"
  projectPath: string     // optional; manifest resolution (default: cwd)
)
```

Fragments mode returns:

```json
{
  "library_id": "local/mylib",
  "query": "authenticate request",
  "page": 1,
  "coverage": "full",
  "missing_identifiers": ["issueLegacyToken"],
  "notes_warning": "1 team note(s) may be outdated: ...",
  "notes": [
    {
      "id": 1,
      "text": "...",
      "status": "stale",
      "stale_reason": "symbol_removed",
      "missing_symbols": ["issueLegacyToken"]
    }
  ],
  "snippets": [
    {
      "heading_path": "API -> Authentication",
      "source": "api/auth.md",
      "text": "Markdown fragment...",
      "score": 2.41
    }
  ],
  "total_tokens": 420,
  "total_matches": 57,
  "has_more": true
}
```

- `coverage` is `full`, `partial` or `none`: how much of the query the
  returned fragments answer. `missing_identifiers` lists query identifiers
  the docs never mention; notes mentioning them carry `mentions_missing`.
- Team notes carry `status` (`current`/`stale`/`unknown`), `stale_reason`
  and anchors. `notes_warning` appears when any note is stale. See
  [Project notes](notes.md).

- `total_matches` is the number of ranked matches for the query and does
  not depend on the page. `has_more` tells whether `page + 1` has
  fragments; a page continues with the first fragment that did not fit
  the previous page's token budget, so budget filtering does not skip
  chunks. Requesting a page past the end returns empty `snippets` with
  the same `total_matches`.
- `score` is a BM25-based relevance value: larger means more relevant.
  The topic boost and the code-language boost multiply it. Scores are
  comparable only within one response — see
  [How search works](search.md#score-semantics).
- When docs and project versions diverge (see
  [Version drift](versions.md)), the output starts with a
  `version_mismatch` block naming both versions and the manifest. Without
  an explicit `virtudoc link`, the version is resolved from `projectPath`
  or the server working directory. The block contains `available_versions`
  and, when another indexed release matches, `suggested_library_id` — call
  `get-library-docs` with that id to read the right release. If no release
  matches, `version_not_indexed` is true and the message tells the agent to
  ask the user.
- When nothing matches, `snippets` is empty, `message` tells the agent not
  to answer from memory, and `sections` lists the library's top-level
  sections to help rephrase the query.
- Exceeding the budget is not an error: the page ends before the next
  fragment, and a fragment larger than the whole budget is truncated.

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
