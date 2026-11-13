# Getting Started

This walkthrough takes about five minutes and needs no network except the
initial clone.

## 1. Register a documentation source

```bash
virtudoc add /path/to/docs --name mylib --version 2.1.0
```

Output shows indexing statistics: how many files were processed, reindexed,
skipped, removed, and how many errors occurred.

## 2. Look at the index

```bash
virtudoc list
```

Shows every registered library: identifier, source path, document and chunk
counts, index size and last update time.

## 3. Search from the terminal

```bash
virtudoc search "how do I authenticate a request"
virtudoc search "middleware pipeline" --name mylib
virtudoc search "anything" --name mylib --outline
```

The terminal prints exactly the fragments an agent would receive: Markdown
text, the heading path and the source file path, plus project notes when
they match the query.

## 4. Attach a team note

```bash
virtudoc note add mylib --text "We always set timeout=30s on outbound calls."
```

From now on, agents asking about timeouts receive this convention together
with the documentation.

## 5. Warn about version drift

```bash
virtudoc link mylib --project /path/to/consuming/project
virtudoc doctor
```

`link` reads the project's dependency manifest (pyproject.toml,
package.json, requirements.txt or go.mod) and records the expected version.
If it diverges from the indexed docs version, `get-library-docs` output
starts with a `version_mismatch` warning, and `doctor` reports the drift.

## 6. Connect an agent

Add the MCP server to your agent (see [MCP integration](mcp.md) for
copy-paste configs for Claude Code, Cursor and OpenCode), then ask the
agent about `mylib`. It will resolve the library id and fetch the docs
itself.

## Keeping the index fresh

Editing a source file is enough: the next agent session reindexes changed
files automatically when the server starts. For a running session, run
`virtudoc update` — the agent's next tool call sees the new data.
