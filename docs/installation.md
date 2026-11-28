# Installation

## Requirements

- Python 3.11 or newer (3.11–3.14 are covered by CI)
- SQLite 3.35 or newer (bundled with official Python builds)
- Git — only for the `virtudoc fetch` command that clones remote docs
- Any OS: Linux, macOS (Intel and Apple Silicon), Windows 10/11

## From PyPI

```bash
pipx install virtudoc   # isolated CLI install, recommended
# or
pip install virtudoc
```

Publishing is automated: pushing a `v*` tag runs
`.github/workflows/release.yml`, which builds the distributions with
`python -m build` and uploads them to PyPI via OIDC trusted publishing. No
PyPI token is stored in the repository.

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

## Docker

The image ships the CLI and both MCP transports. All state stays in the
`/data` volume (`VIRTUDOC_HOME=/data`), and the process runs as an
unprivileged user (uid 10001), so nothing is written to the host outside the
volume.

```bash
docker build -t virtudoc .
docker volume create virtudoc-data

# Index host documentation through a read-only mount
docker run --rm -v virtudoc-data:/data -v "$PWD/docs:/docs:ro" \
  virtudoc add /docs --name mylib

# The rest of the CLI works unchanged
docker run --rm -v virtudoc-data:/data virtudoc list
docker run --rm -v virtudoc-data:/data virtudoc search "authentication errors"

# Indexing and search need no network at all
docker run --rm --network none -v virtudoc-data:/data virtudoc search "retry"
```

For an MCP client, run the container with stdin attached:

```json
{
  "mcpServers": {
    "virtudoc": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-v", "virtudoc-data:/data", "virtudoc", "serve"]
    }
  }
}
```

`virtudoc mcp-config` prints host paths (`/usr/local/bin/virtudoc`), so inside
the container use the snippet above instead of its output.

Streamable HTTP needs two things Docker makes non-obvious: a non-loopback
bind (a server on `127.0.0.1` inside the container is unreachable from the
host) and the bearer token that virtudoc requires for such a bind.
`docker/config.toml` sets `http_host = "0.0.0.0"`, and `docker-compose.yml`
publishes the port on the host loopback only:

```bash
export VIRTUDOC_HTTP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose run --rm virtudoc add /docs --name mylib
docker compose up -d
```

Then point the client at `http://127.0.0.1:8765/mcp` with
`Authorization: Bearer $VIRTUDOC_HTTP_TOKEN`. See
[MCP integration](mcp.md#streamable-http-and-authentication) for the
transport itself.

If you bind-mount a host directory at `/data` instead of using the named
volume, make it writable by uid 10001 (`chown -R 10001 /path/to/data`).

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

The setup file carries library names, absolute source paths, versions,
channels and descriptions, plus project notes with their text, version scope,
pin flag and document anchors. Project links are machine-specific and are not
exported: recreate them with `virtudoc init --link <project>` in each project
after importing. The exported file starts with a comment that says the same.

Then connect your agent once (see [MCP integration](mcp.md)) — done. From
here on, every agent session reindexes changed documentation automatically
on the first request for a library; the setup file is the only thing to keep in sync.

A typical update loop: `virtudoc add ...` (or `remove`) → `virtudoc export`
→ commit the setup file. Colleagues get the change with their next pull and
a single `virtudoc import`.
