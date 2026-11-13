# Installation

## Requirements

- Python 3.11 or newer (3.11–3.13 are covered by CI)
- Git — only for the `virtudoc fetch` command that clones remote docs
- Any OS: Linux, macOS (Intel and Apple Silicon), Windows 10/11

## From source

```bash
git clone https://github.com/saromanov/virtudoc.git
cd virtudoc
pip install -e .
```

This installs the `virtudoc` command. Runtime dependencies are just two
packages: `markdown-it-py` (Markdown parsing) and `mcp` (the MCP SDK).

For development (tests, lint):

```bash
pip install -e '.[dev]'
pytest
```

## What gets created

All data lives in `~/.virtudoc/`:

| Path | Contents |
|---|---|
| `~/.virtudoc/index.db` | SQLite database (WAL mode): libraries, chunks, FTS index |
| `~/.virtudoc/config.toml` | Optional configuration (created by you) |
| `~/.virtudoc/virtudoc.log` | Log file |
| `~/.virtudoc/fetch/<name>/` | Cache for sources downloaded with `fetch` |

Deleting `~/.virtudoc/` removes the system completely — no traces elsewhere.

## Custom data directory

Set the `VIRTUDOC_HOME` environment variable. Useful for experiments,
isolation and tests:

```bash
export VIRTUDOC_HOME=/tmp/my-virtudoc
virtudoc add ./docs --name mylib
```

Note: MCP clients usually spawn the server with a default environment, so
agents work with `~/.virtudoc` unless you add `env` to the server config.

## Team bootstrap: one file, one command

Keep the whole library set in a versioned setup file instead of registering
sources by hand on every machine.

**On the maintained machine** — export the current setup after any change
(new library, rename, version bump):

```bash
virtudoc export ~/dotfiles/virtudoc/libraries.toml
```

**On a new machine**:

```bash
# 1. install virtudoc (see above)
# 2. get the setup file (dotfiles clone, shared drive, scp — your choice)
# 3. register and index everything:
virtudoc import ~/dotfiles/virtudoc/libraries.toml
```

Import is idempotent: it replaces existing libraries with the same name and
indexes only what changed, so re-running it after edits is always safe.
Missing source directories are reported and skipped without aborting the
rest.

Then connect your agent once (see [MCP integration](mcp.md)) — done. From
here on, every agent session reindexes changed documentation automatically
when the server starts; the setup file is the only thing to keep in sync.

A typical update loop: `virtudoc add ...` (or `remove`) → `virtudoc export`
→ commit the setup file. Colleagues get the change with their next pull and
a single `virtudoc import`.
