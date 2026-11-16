# Configuration

Configuration lives in `~/.virtudoc/config.toml` and is optional — every
key has a default. CLI flags override config values where a flag exists.
Unknown keys produce a warning (typo protection) but do not stop the
program.

```toml
[db]
path = "~/.virtudoc/index.db"      # database location

[fetch]
branch = "main"                    # git branch for `fetch` (unset = repo default)
max_extract_bytes = 2147483648      # expanded archive size limit (2 GiB)
max_extract_files = 100000         # maximum number of files per archive

[chunk]
target_tokens = 600                # target chunk size
max_tokens = 1200                  # hard limit for a chunk
overlap_tokens = 60                # overlap between adjacent chunks

[index]
auto_refresh = true                # reindex a library when the source is newer

[search]
default_tokens = 5000              # output budget for search/get-library-docs
max_results = 20                   # max fragments per response
snippet_window_tokens = 400        # trim long chunks around query hits (0 = full chunks)

[notes]
max_tokens = 800                   # budget for the project notes block

[server]
transport = "stdio"                # MCP transport: stdio or http
http_host = "127.0.0.1"
http_port = 8765

[log]
level = "INFO"                     # DEBUG, INFO, WARNING, ERROR
```

## Chunk settings only affect new indexing

Changing `chunk.*` does not rebuild the existing index. To apply new chunk
settings to a library, run a full rebuild:

```bash
virtudoc reindex mylib
```

## Auto-refresh on tool calls

With `index.auto_refresh = true` (default), every `get-library-docs` call
first checks whether the source changed since the last indexing — a file
count difference or any source file newer than `indexed_at` triggers a
cheap incremental reindex before the search. Deleted files are caught by
the count check. Set it to `false` on huge documentation trees or when
another indexing process shares the data directory.

## Environment variables

- `VIRTUDOC_HOME` — data directory override (default `~/.virtudoc`).
  Handy for experiments; remember that agents spawn `serve` with a default
  environment, so they use the default directory unless you set `env` in
  the MCP server config.
