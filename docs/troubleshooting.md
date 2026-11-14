# Troubleshooting

Symptom → cause → fix. If nothing here helps, run
`virtudoc doctor` and check the log at `~/.virtudoc/virtudoc.log`.

## The agent says the library does not exist, but I registered it

**Cause:** the agent spawned the server with a different data directory.
Agents start MCP servers with a default environment, so `serve` uses
`~/.virtudoc` unless told otherwise — while your CLI commands may have run
with `VIRTUDOC_HOME` pointing elsewhere.

**Fix:** check which directory the server actually uses: `virtudoc serve`
prints it to stderr on startup (`virtudoc serve: data directory ...`), and
MCP clients keep server stderr in their logs. Then either unset
`VIRTUDOC_HOME` in your shell or add it to the server's config:

```json
{ "mcpServers": { "virtudoc": {
    "command": "virtudoc", "args": ["serve"],
    "env": { "VIRTUDOC_HOME": "/your/data/dir" }
} } }
```

## The MCP tools never appear in the agent

**Cause:** the agent cannot find the `virtudoc` binary — pipx/venv
installs often leave it off the `PATH` that MCP clients construct.

**Fix:** find the binary (`which virtudoc` in the environment where
installing worked) and use its absolute path as `command` in the MCP
config, or extend `env.PATH` there. See [MCP integration](mcp.md).

## `fetch`: "fetch cache already exists and is not a git clone"

**Cause:** `~/.virtudoc/fetch/<name>` was left behind by an archive
download, and a repeated fetch of the same name now targets a git URL (or
vice versa).

**Fix:** the error message includes the exact command; it is
`rm -rf ~/.virtudoc/fetch/<name>`, then fetch again.

## `add`: "library ... already exists"

**Fix:** pass `--replace` to re-register and reindex the library, or pick
a different `--name`. Removal goes through `virtudoc remove <name>`.

## Some documents are missing from the index

**Causes:**
- Files over 5 MB are skipped — the paths are listed in the `add`/`fetch`
  output (stderr).
- Unsupported formats (anything but Markdown `.md`/`.mdx`,
  reStructuredText `.rst`, plain text).

**Fix:** split oversized pages, convert exotic formats to Markdown before
indexing.

## Changed `chunk.*` settings have no effect

**Cause:** chunk settings apply at indexing time; the existing index is
not rebuilt automatically (the CLI prints a note on `update`).

**Fix:** `virtudoc remove <name> --yes && virtudoc add <path> --name <name>`.

## `update` fails with `IndexLockError`

**Cause:** another indexing operation is running (a second `add`/`update`,
or `serve` is reindexing on startup).

**Fix:** wait for it to finish and retry; avoid running index commands
concurrently.

## `version_mismatch` in the agent's docs output

**Cause:** not an error — the indexed documentation version and the
version pinned in the project's dependency manifest have diverged.

**Fix:** re-fetch/re-add the docs for the pinned version, or re-link:
`virtudoc link <name> --project <path>`.

## Search returns nothing

**Cause:** full-sentence queries rank poorly in FTS5.

**Fix:** use keywords from the library's own terminology. When nothing
matches, the output (and the MCP response) lists the library's top-level
sections — or inspect the tree upfront with
`virtudoc search "x" --name <name> --outline` and target a branch with
`--section`.

## The agent does not see my latest edits

**Fix:** indexing happens when `serve` starts, so new agent sessions are
fresh automatically. For a session that is already running, execute
`virtudoc update` — the next tool call sees the new data.

## The indexed docs keep changing / differ between machines

**Cause:** the library's source is someone's git working tree: the index
follows whatever branch and uncommitted state are checked out there.

**Fix:** run `virtudoc doctor` — it warns about sources on a non-default
branch or with uncommitted changes. For a team baseline, index a pinned
clone of the default branch instead:
`virtudoc fetch --repo <url> --branch main --name <name>`; see the
`fetch` section in the [CLI reference](cli.md).

## Installation fails with a Python version error

VirtuDoc needs Python 3.11+ (`requires-python = ">=3.11"`). Check with
`python3 --version`; use a newer interpreter or a version manager
(pyenv, uv).

## Reset everything

Delete the data directory — the system leaves no traces elsewhere:

```bash
rm -rf ~/.virtudoc
```
