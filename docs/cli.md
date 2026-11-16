# CLI Reference

All commands work offline; the only exception is `fetch`. Exit code is `0`
on success and non-zero on error, so the CLI is scriptable.

## add — register a source and index it

```bash
virtudoc add <path> --name <name> [--version V] [--description TEXT] [--replace]
```

Registers the directory as a library `local/<name>` and runs the first
indexing. Names must be lowercase latin with digits, hyphens and
underscores. On a name conflict the command fails without changes; pass
`--replace` to overwrite an existing library.

## update — incremental reindexing

```bash
virtudoc update [--name <name>]
```

Reindexes all registered libraries or one. Files whose content hash did not
change are skipped; changed files are re-chunked; deleted files are removed
from the index. Summary per library: processed / reindexed / skipped /
removed / errors.

## reindex — full rebuild of one library

```bash
virtudoc reindex <name>
```

Drops every indexed document of the library and re-chunks the whole source
from scratch with the current `chunk.*` settings. Use it after changing
`chunk.target_tokens` / `chunk.max_tokens` / `chunk.overlap_tokens` in the
config — incremental `update` only re-chunks changed files. Notes and the
library registration survive a rebuild.

## fetch — download remote documentation

```bash
virtudoc fetch --repo <url> --name <name> [--version V] [--branch B] [--replace]
```

The only command that uses the network. Git URLs are cloned with
`--depth 1` into `~/.virtudoc/fetch/<name>` (a repeated fetch pulls with
`--ff-only`); archive URLs (`.zip`, `.tar.gz`, `.tgz`, `.tar`) are
downloaded and extracted with path-traversal protection, rejection of links
and special files, and expanded-size/file-count limits. Extraction happens in
a staging directory; a failed refresh preserves the previous cache. Use `--branch`
(or the config key `fetch.branch`) to pin a git branch other than the
repo default; the flag overrides the config key.

The chosen branch is recorded in a `.virtudoc-fetch.json` sidecar file
inside the cache. Every `serve` start resets the clone to the pinned
branch before reindexing (local-only checkout from the recorded
`origin/<branch>` ref — no network, and a dirty working tree is left
alone); `doctor` warns when the checkout and the pin disagree.

## list — index statistics

```bash
virtudoc list
```

One line per library: id, document count, chunk count, index size, last
update, source path.

## search — search from the terminal

```bash
virtudoc search <query> [--name <name>] [--tokens N] [--section "Path -> Branch"] [--outline]
```

Prints the same fragments an agent receives via MCP. `--outline` prints the
section tree of a library (requires `--name`) with chunk counts and token
estimates; `--section` restricts output to one branch in both modes.

## note — project notes

```bash
virtudoc note add <name> --text "Convention or gotcha"
virtudoc note list <name>
virtudoc note remove <name> --id N
```

Notes are attached to a library, survive reindexing and are delivered to
agents together with documentation. See [Project notes](notes.md).

## link / unlink — version tracking

```bash
virtudoc link <name> --project <path>
virtudoc unlink <name>
```

Reads the dependency manifest of the consuming project and records the
expected version. See [Version drift warnings](versions.md).
## doctor — diagnostics

```bash
virtudoc doctor
```

Checks the data directory, schema version, every library's source path,
link drift (re-evaluated live) and index integrity (orphan chunks/FTS
rows). For git checkout sources it also warns when the checkout is on a
non-default branch or has uncommitted changes — the indexed docs then
depend on someone's work in progress. Prints a report and exits with code
`1` when problems are found — usable in CI.

## mcp-config — ready MCP client snippet

```bash
virtudoc mcp-config
```

Prints a ready `mcpServers` block for Claude Code / Cursor / OpenCode
configs: absolute path of the `virtudoc` binary as the `command` and,
when `VIRTUDOC_HOME` is set, an `env` block pinning the data directory.
Solves the two most common integration problems: the agent cannot find
the binary and the agent using a different data directory.

## remove — delete a library

```bash
virtudoc remove <name> [--yes]
```

Removes the library with all its chunks, notes and links. Asks for
confirmation unless `--yes` is passed.

## serve — MCP server

```bash
virtudoc serve [--http [PORT]]
```

Starts the MCP server: stdio by default, or streamable HTTP on
`127.0.0.1:PORT` with `--http` (default port from config, 8765). Before
announcing tools, all libraries are reindexed incrementally. See
[MCP integration](mcp.md).

## export / import — move the setup between machines

```bash
virtudoc export [path]   # default: ~/.virtudoc/libraries.toml
virtudoc import [path]
```

`export` dumps the library list (names, paths, versions) to a TOML file.
`import` registers and indexes every entry, replacing same-named libraries —
safe to re-run. See [Team bootstrap](installation.md#team-bootstrap-one-file-one-command).
