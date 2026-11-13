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
   `package.json`, `requirements.txt` or `go.mod` (in this order).
2. Locates the dependency by the library name (matching is
   case-insensitive; underscores and hyphens are treated as equivalent).
3. Records the expected version in the index.

A library can have one link; linking again replaces it.
`virtudoc unlink mylib` removes the connection.

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
