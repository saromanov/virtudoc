# Version Drift Warnings

An agent answering from version 1.x docs while the project depends on 2.x
produces confidently wrong code. VirtuDoc detects this drift and warns
before the documentation is consumed.

## Linking a library to a project

```bash
virtudoc link mylib --project /path/to/consuming/project
```

The command:

1. Finds the project's dependency manifest — `pyproject.toml`,
   `package.json`, `requirements.txt`, `go.mod`, `uv.lock`, `poetry.lock`
   or `Pipfile.lock`. Lockfiles are consulted first: they record the exact
   installed release, while a direct manifest may only declare a range.
   Poetry-style `[tool.poetry.dependencies]` in `pyproject.toml` is
   recognized too.
2. Locates the dependency by the library name (matching is
   case-insensitive; underscores and hyphens are treated as equivalent).
3. Records the expected version in the index.

Versions are compared numerically and trailing zero components are ignored:
`3.4` matches `3.4.0`. Unknown or unparseable versions yield `unknown` and
produce no warning.

A library can have one link; linking again replaces it.
`virtudoc unlink mylib` removes the connection.

## Indexing several versions

The first release of a library keeps the canonical id (`local/mylib`).
Further releases are stored next to it with their version in the id:

```bash
virtudoc add /docs/mylib-2.1 --name mylib --version 2.1.0  # local/mylib
virtudoc add /docs/mylib-2.2 --name mylib --version 2.2.0  # local/mylib@2.2.0
```

Versions never compete blindly. `find-library` resolves the version from
the consuming project's lockfile or manifest (optional `projectPath`,
defaults to the server working directory) and ranks the matching release
first. Command-line reference:

```bash
virtudoc add <path> --name mylib --version 2.2.0 [--channel release|branch]
virtudoc add <path> --name mylib --branch      # shorthand for --channel branch
```

A working branch is opt-in (`--channel branch` or `--branch`, and
`--channel branch` on `fetch`). It gets a `local/<name>@branch` id and
never outranks a release of the same library. Index releases first; add a
working tree only while you need it.

## When the project version is not indexed

`find-library` returns `available_versions` and `version_not_indexed: true`
when the resolved version has no indexed release. The message tells the
agent to ask the user before using another version. `get-library-docs`
repeats the drift warning and, when possible, points at the matching id:

```json
{
  "version_mismatch": {
    "library_id": "local/mylib",
    "documentation_version": "2.1.0",
    "project_version": "2.2.0",
    "manifest": "/path/to/project/uv.lock",
    "available_versions": [
      { "id": "local/mylib", "version": "2.1.0", "channel": "release" },
      { "id": "local/mylib@2.2.0", "version": "2.2.0", "channel": "release" }
    ],
    "suggested_library_id": "local/mylib@2.2.0",
    "version_not_indexed": false,
    "message": "version drift: indexed documentation is 2.1.0, but the project depends on 2.2.0 (uv.lock); indexed version 2.2.0 matches the project — use library id local/mylib@2.2.0"
  }
}
```

## Bulk onboarding with `virtudoc init`

```bash
virtudoc init /path/to/project        # report only
virtudoc init /path/to/project --link # also create links for the matches
```

`init` scans all manifests in the project at once, matches dependencies to
registered libraries and shows the version status for each. It is
non-interactive and read-only until `--link` is passed, so you can inspect
the report before anything is recorded. Matched libraries are linked to the
manifest the dependency was found in, which matters when the dependency is
declared only in a lockfile.

## Where the warning appears

When the linked dependency version diverges from the documentation
version recorded at `virtudoc add --version`, every `get-library-docs`
response starts with:

```json
{
  "version_mismatch": {
    "library_id": "local/mylib",
    "documentation_version": "1.0",
    "project_version": "2.0.0",
    "manifest": "/path/to/project/requirements.txt",
    "message": "version drift: indexed documentation is 1.0, but the project depends on 2.0.0 (requirements.txt)"
  }
}
```

The agent sees both versions and the manifest path, and can decide to ask
the user, check the changelog, or proceed with caution. Terminal search is
not affected.

## Drift summary

`virtudoc doctor` re-evaluates every link live: it re-reads the manifest,
re-extracts versions and reports which libraries drift, which match, and
which manifests or dependencies have gone missing. Exit code 1 on problems
makes it CI-friendly:

```bash
virtudoc doctor || echo "documentation needs attention"
```

## Common linking errors

- `no dependency manifest found` — none of the four manifest files exists
  in the given directory.
- `dependency '<name>' not found` — the library name does not match any
  package in the manifest; rename the library or adjust the manifest.
