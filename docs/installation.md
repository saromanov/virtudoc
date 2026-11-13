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
