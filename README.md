# VirtuDoc

[![CI](https://github.com/saromanov/virtudoc/actions/workflows/ci.yml/badge.svg)](https://github.com/saromanov/virtudoc/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/saromanov/virtudoc/blob/main/LICENSE)

A local documentation server for AI agents. It indexes documentation from
local directories into SQLite with FTS5 full-text search and serves relevant
fragments to an agent through MCP tools (milestone M2). Fully offline: no
network access happens in any mode.

Think of it as [Upstash Context7](https://github.com/upstash/context7) running
entirely on your machine: private and internal docs never leave your computer,
there are no API keys, request limits, or cloud dependencies.

## Installation

```bash
pip install -e .
```

Requires Python 3.11+. The only runtime dependency is `markdown-it-py`.

## Quick start

```bash
# Register a documentation directory and index it
virtudoc add /path/to/docs --name mylib --version 2.1.0

# Index statistics
virtudoc list

# Search from the terminal (same results an agent will get via MCP)
virtudoc search "how to handle authentication errors"

# Incremental reindexing after the docs changed
virtudoc update              # all libraries
virtudoc update --name mylib

# Remove a library
virtudoc remove mylib
```

Supported formats: Markdown (`.md`, `.mdx`), reStructuredText (`.rst`),
plain text (`.txt`), HTML (`.html`, `.htm`, best-effort text extraction).

## How it works

- The **chunker** splits documents along H2/H3 headings (target chunk size
  600 tokens, hard limit 1200, 60-token overlap); code blocks are never split.
- **Search** is SQLite FTS5 with BM25 ranking. Queries cascade:
  exact `AND` → prefix `AND` with ending stemming → `OR`, so Russian
  inflections (`пакетная` finds `пакетной`) and fuzzy phrasing both resolve.
- Results are assembled under a token budget (default 5000) with a
  topic boost for chunks whose heading path matches the query.
- **Indexing is incremental**: unchanged files are skipped by content hash.
- All data lives in `~/.virtudoc/` (WAL-mode `index.db`, config, log).
  Deleting the directory removes the system completely.

## Configuration

`~/.virtudoc/config.toml` (all keys optional):

```toml
[chunk]
target_tokens = 600
max_tokens = 1200
overlap_tokens = 60

[search]
default_tokens = 5000
max_results = 20

[log]
level = "INFO"
```

Changing `chunk.*` only affects new indexing; the existing index is not
rebuilt automatically.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — components, data flows, storage schema

## Development

```bash
pip install -e '.[dev]'
pytest
ruff check virtudoc tests
ruff format virtudoc tests
```

## Status

Milestone M1 is done (indexing, chunking, FTS5 search, CLI). Next:
MCP server `serve` (M2), retrieval quality (M3), packaging (M4).

## License

MIT, see [LICENSE](LICENSE).
