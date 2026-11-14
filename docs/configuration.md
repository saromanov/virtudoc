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

[chunk]
target_tokens = 600                # target chunk size
max_tokens = 1200                  # hard limit for a chunk
overlap_tokens = 60                # overlap between adjacent chunks

[search]
default_tokens = 5000              # output budget for search/get-library-docs
max_results = 20                   # max fragments per response

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
settings to a library, remove and add it again:

```bash
virtudoc remove mylib --yes
virtudoc add /path/to/docs --name mylib
```

## Environment variables

- `VIRTUDOC_HOME` — data directory override (default `~/.virtudoc`).
  Handy for experiments; remember that agents spawn `serve` with a default
  environment, so they use the default directory unless you set `env` in
  the MCP server config.
