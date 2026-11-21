# CLI Reference

All commands work offline; the only exception is `fetch`. Exit code is `0`
on success and non-zero on error, so the CLI is scriptable.

## add — register a source and index it

```bash
virtudoc add <path> --name <name> [--version V] [--description TEXT]
                    [--channel release|branch] [--branch] [--replace]
```

Registers the directory as a library and runs the first indexing. The first
release gets the canonical id `local/<name>`; another release of the same
name gets `local/<name>@<version>`, so several releases can be indexed side
by side. A working branch is opt-in: `--channel branch` (or the `--branch`
shorthand) registers it as `local/<name>@branch` and it never outranks a
release. Names must be lowercase latin with digits, hyphens and
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
virtudoc fetch --repo <url> --name <name> [--version V] [--branch B]
               [--channel release|branch] [--replace]
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
branch before serving (local-only checkout from the recorded
`origin/<branch>` ref — no network, and a dirty working tree is left
alone); `doctor` warns when the checkout and the pin disagree.

## list — index statistics

```bash
virtudoc list
```

One line per library: id, documentation version, channel (release or
branch), document count, chunk count, index size, last update, source path.
Commands that take a name (`update`, `reindex`, `remove`, `search`,
`note`, `link`) accept either the canonical name or a full versioned id
such as `local/mylib@2.2.0`.

## search — search from the terminal

```bash
virtudoc search <query> [--name <name>] [--tokens N] [--page N] [--section "Path -> Branch"] [--outline]
```

Prints the same fragments an agent receives via MCP. The summary line reports
`fragments` on the page, `total_matches` for the query, `has_more` and the
consumed `tokens`; when `has_more` is true, use `--page N+1` to continue.
`--outline` prints the section tree of a library (requires `--name`) with
chunk counts and token estimates; `--section` restricts output to one branch
in both modes.

## note — project notes

```bash
virtudoc note add <name> --text "Convention or gotcha"
virtudoc note list <name>
virtudoc note remove <name> --id N
```

Notes are attached to a library, survive reindexing and are delivered to
agents together with documentation. Each note records the source revision
it was written against; `note list` shows it and `doctor` flags notes left
behind by later documentation changes. See [Project notes](notes.md).

## init — project onboarding

```bash
virtudoc init [path] [--link]
```

Scans every supported dependency manifest in the project directory
(`pyproject.toml`, `package.json`, `requirements.txt`, `go.mod`, `uv.lock`,
`poetry.lock`, `Pipfile.lock`), merges the declared dependencies (lockfiles
contribute their resolved graph) and matches them against the registered
libraries by name. For each match it prints the documentation version, the
project version and the status: `match`, `mismatch` or `unknown` (one of the
versions is missing). Unmatched dependencies are listed with a hint to
register their documentation; long lists are truncated.

The command is read-only by default and never prompts, so it is safe in
scripts and CI. Pass `--link` to create the project links for the matched
libraries — the manifest where each dependency was found is recorded, so a
dependency present only in a lockfile still links correctly. `path` defaults
to the current directory.

Exit code is `0` when the scan succeeds (drift is reported, not treated as a
failure) and `1` when the directory is missing or holds no supported
manifest.

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
link drift (re-evaluated live), stale notes and index integrity (orphan
chunks/FTS rows). A note is stale when the indexed source changed since the
note was written — the recorded revision no longer matches. For git
checkout sources it also warns when the checkout is on a
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
`127.0.0.1:PORT` with `--http` (default port from config, 8765). Startup
does not index: the first `get-library-docs` call for a library refreshes
it when its source changed (`index.auto_refresh`). See
[MCP integration](mcp.md).

## export / import — move the setup between machines

```bash
virtudoc export [path]   # default: ~/.virtudoc/libraries.toml
virtudoc import [path]
```

`export` dumps the library list (names, paths, versions) to a TOML file.
`import` registers and indexes every entry, replacing same-named libraries —
safe to re-run. See [Team bootstrap](installation.md#team-bootstrap-one-file-one-command).
