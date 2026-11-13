# VirtuDoc Documentation

VirtuDoc is a local documentation server for AI agents. It indexes
documentation from local directories and git repositories into a SQLite
database with FTS5 full-text search, and serves relevant fragments to an
agent through MCP tools. Everything runs on your machine: no API keys, no
cloud, no request limits.

Think of it as [Upstash Context7](https://github.com/upstash/context7) that
runs entirely on your machine — with one important difference: private and
internal documentation never leaves your computer.

## What it gives an agent

- Answers from **your** documentation: internal SDKs, proprietary
  integrations, team conventions — things no cloud service can know.
- Fresh results: the index is rebuilt incrementally every time the server
  starts.
- Context economy: the agent pulls exactly the fragments it needs under a
  token budget instead of receiving whole files.
- Team knowledge: project notes are delivered together with documentation.
- Version awareness: a warning appears when the indexed docs drift from the
  version pinned in your project's dependency manifest.

## How it works at a glance

1. You register documentation sources with one CLI command
   (`virtudoc add`). Files are parsed, split into chunks along headings and
   stored in a local SQLite database.
2. You register the MCP server in your agent's config once.
3. The agent calls `resolve-library-id` to find a library, then
   `get-library-docs` to fetch fragments, notes and section trees.

## Where to go next

- [Installation](installation.md)
- [Getting started](getting-started.md)
- [CLI reference](cli.md)
- [MCP integration and tools](mcp.md)
- [Configuration](configuration.md)
- [Indexing and chunking](indexing.md)
- [How search works](search.md)
- [Project notes](notes.md)
- [Version drift warnings](versions.md)
