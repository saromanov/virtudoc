# VirtuDoc Architecture

VirtuDoc is a local documentation server for AI agents. It indexes
documentation from local directories into SQLite with FTS5 full-text search
and serves relevant fragments to an agent through MCP tools (milestone M2).
Everything runs on the user's machine: there is no network access in any mode.

## Principles

- **A single data file.** SQLite in WAL mode: zero administration, atomic
  transactions, concurrent reads during indexing.
- **Incrementality.** Repeated indexing never redoes work: files are compared
  by content hash.
- **Pull, not push.** The agent requests exactly as much documentation as its
  token budget allows, instead of receiving whole files in context.
- **Minimal dependencies.** Runtime is the standard library + markdown-it-py.

## Components

```
virtudoc/
├── cli.py            # CLI: add, update, list, remove, search, serve (M2)
├── config.py         # TOML config ~/.virtudoc/config.toml + defaults
├── db.py             # SQLite connection (WAL), schema, chunk insert/delete
├── coverage.py       # identifier extraction, case rules, query coverage
├── notes.py          # note selection, anchors, staleness, payload fields
├── payload.py        # get-library-docs response assembly (stable key order)
├── tokens.py         # Empirical token estimate (chars/4 latin, /3 cyrillic)
├── indexer/
│   ├── walker.py     # Source traversal: hidden dirs, extension filter, 5 MB limit
│   ├── parsers.py    # md/mdx (markdown-it-py), rst, txt, html — best effort
│   ├── chunker.py    # Heading-aware chunking with token budgets
│   └── indexer.py    # Orchestration: hash → skip/reindex, locking
├── search/
│   └── engine.py     # FTS5/BM25, query cascade, boosts, budget assembly
└── (M2) mcp_server.py  # find-library and get-library-docs tools, stdio
```

## Data flows

Two contours, separated in time, converging on one database:

```
PREPARATION (human, CLI)               SERVING (agent, MCP — M2)
virtudoc add / update                     Claude Code / Cursor (stdio)
        │                                        │
        ▼                                        ▼
   walker → parser → chunker               MCP tool call
        │                                        │
        └──────► SQLite (WAL) ◄──────── search engine
                 ┌─────────────────────────────────┐
                 │ libraries  — library registry   │
                 │ documents  — indexed files      │
                 │              + content hash     │
                 │ chunks     — unit of retrieval  │
                 │ chunks_fts — FTS5 over texts    │
                 │ notes, project_links, meta      │
                 └─────────────────────────────────┘
```

### Indexing path

1. `walker` collects files (`.md`, `.mdx`, `.rst`, `.txt`, `.html`, `.htm`),
   skipping hidden directories and files over 5 MB.
2. `parsers` turn a file into a stream of blocks: headings (level + text),
   paragraphs, code blocks (with language). Markdown is parsed through
   markdown-it-py via `token.map`, preserving the original text verbatim;
   rst/txt/html use simplified line-based parsers.
3. `chunker` groups blocks by heading path ("API → Middleware") and budget:
   target 600 tokens, hard limit 1200, 60-token overlap between adjacent
   chunks. Code blocks are never split; an oversized paragraph is cut at
   line boundaries with a "(continued)" marker.
4. `indexer` compares the file hash against `documents`: unchanged — skip,
   changed — one transaction "delete old chunks, insert new ones", gone —
   remove from the index. Locking via `BEGIN IMMEDIATE` (a clear error on
   concurrent indexing). A parse error in one file never stops the rest.

### Search path

1. The query is normalized (ё → е, lowercased) and split into tokens.
2. A cascade of FTS5 queries until something hits:
   exact `AND` → prefix `AND` with ending stemming → `OR`.
   Prefix search covers Russian inflections that unicode61 does not fold
   (`пакетная` finds `пакетной`) — the answer to risk R-01 from the spec.
3. Candidates are ranked by BM25; chunks whose heading path or file name
   lexically matches query tokens receive a topic boost.
4. Output is assembled under a token budget (default 5000) and a result
   limit (20); exceeding the budget is not an error — output is cut at a
   chunk boundary with a 10% margin.

### Serving path: `get-library-docs`

```
refresh_if_stale
      ↓
search / library_outline
      ↓
coverage: extract identifiers → check the index → level/missing/unreturned
      ↓
notes: statuses, anchors, relevance, budget, mentions_missing
      ↓
payload: one key order for fragments, empty results and outline
```

`coverage.assess` owns the level rules; `notes.build_notes_payload` owns
note selection and warning text; `payload.doc_payload` only renders. The
transport module describes tools, parameters and errors, so the same
contract is used by every branch (fragments, empty result, outline).

### Version-aware ranking

`find-library` resolves the dependency version from the consuming project's
lockfile or manifest (lockfiles first) before ranking libraries. The
matching indexed release is boosted; other versions are not removed from
the candidate list, and an indexed working branch (`channel = 'branch'`)
never outranks a release. If the resolved version is not indexed, the
response lists `available_versions` and tells the agent to ask the user.
`get-library-docs` repeats the drift warning for the chosen id and suggests
the matching id when one exists.

## Storage schema

| Table | Purpose | Key fields |
|---|---|---|
| `libraries` | Library registry | `id` (`local/<name>` or `local/<name>@<version>`), `source_path`, `version`, `channel`, `updated_at` |
| `documents` | Indexed files | `library_id`, `rel_path`, `content_hash`, `indexed_at` |
| `chunks` | Unit of retrieval | `heading_path`, `ord`, `text`, `token_est`, `code_ratio`, `lang` |
| `chunks_fts` | Virtual FTS5 table | `text` (external content of `chunks`), linked by `rowid` |
| `notes` | Project notes (KF-03, M3) | `library_id`, `library_name`, `origin_library_id`, `version`, `text`, `created_at`, `source_rev`, `pinned`, `reviewed_at`, `anchor_kind` |
| `note_anchors` | Documents a note relies on | `note_id`, `library_id`, `rel_path`, `content_hash`, `anchored_at` |
| `note_symbols` | Identifiers a note relies on | `note_id`, `symbol`, `anchored_at` |
| `library_symbols` | Identifier lookup per variant | `library_id`, `symbol` (lower-cased plain, exact qualified) |
| `project_links` | Manifest links (KF-04, M3) | `manifest_path`, `dep_version` |
| `meta` | Schema version | `key`, `value` |

Notes belong to the logical library (`notes.library_name`), so each variant
sees them; `notes.scope_version` restricts a note to one variant.
`notes.anchor_kind` is `documents`, `symbols`, `library` or `none`;
`note_anchors` stores the content hashes a note was confirmed against and
`note_symbols` the identifiers found in the docs at anchor time. Symbol
anchors are checked against every served variant, which makes them the
cross-version signal (`symbol_removed`). `coverage.py` extracts identifiers
and answers whether the docs mention one; `library_symbols` is rebuilt by the
indexer and turns those checks into a primary-key lookup.
`notes.source_rev` fingerprints the whole variant and stays as the baseline
for library-level anchors. Agents get `current`/`stale`/`unknown` per note,
plus `notes_warning` when an anchor changed. `libraries.source_fingerprint`
caches the fingerprint, recomputed by the indexer after every pass.

The `heading_path` field is the single source of truth for the topic boost
and the outline mode (KF-05). `code_ratio` supports the code-share control
of output (FR-18). The `lang` field stores the fence language of the first
annotated code block in the chunk and feeds the `language` boost of
`get-library-docs`. The schema version is recorded in `meta` and
`PRAGMA user_version` — migrations build on it.

## Configuration

`~/.virtudoc/config.toml`, keys are overridden by CLI flags:

| Key | Default | Purpose |
|---|---|---|
| `db.path` | `~/.virtudoc/index.db` | Database location |
| `fetch.branch` | unset | Git branch for `fetch` (unset = repo default) |
| `chunk.target_tokens` / `max_tokens` / `overlap_tokens` | 600 / 1200 / 60 | Chunker budgets |
| `search.default_tokens` / `max_results` | 5000 / 20 | Output budget and limit |
| `notes.max_tokens` | 800 | Notes block budget (M3) |
| `notes.max_note_tokens` | 300 | Reject longer note text at `note add`/`edit` |
| `notes.auto_anchor_k` / `auto_anchor_ratio` | 3 / 0.5 | Documents auto-anchored from the note text |
| `notes.pinned_max_tokens` | 400 | Budget for pinned notes in a response |
| `notes.max_symbols` | 10 | Symbol anchors extracted from one note |
| `server.transport` / `http_host` / `http_port` | stdio / 127.0.0.1 / 8765 | MCP transport (M2) |
| `log.level` | INFO | Log at `~/.virtudoc/virtudoc.log` |

Changing `chunk.*` only affects new indexing; the existing index is not
rebuilt automatically.

## Boundaries and evolution

Out of scope for v1.0 (spec section 10): hybrid search with local embeddings
(sqlite-vec, optional), a filesystem watcher, Docusaurus/MkDocs converters,
library-set profiles, a team HTTP mode. The architecture does not preclude
them: the search engine is isolated behind a single interface, and the
database schema is versioned.

## Roadmap

- **M1 (done):** core — indexing, chunking, FTS5 search, CLI.
- **M2:** MCP server `serve` (stdio), `find-library` /
  `get-library-docs` tools, lazy refresh on request, Claude Code/Cursor integration.
- **M3:** retrieval quality — adjacent-chunk merging, pagination, code-share
  control, notes block, version-mismatch warnings, outline mode, a control
  query set.
- **M4:** packaging — doctor command, README, three-OS verification,
  acceptance scenarios.
