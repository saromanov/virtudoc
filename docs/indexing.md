# Indexing and Chunking

## Supported formats

| Format | Extensions | Notes |
|---|---|---|
| Markdown | `.md`, `.mdx` | Primary format; structure via markdown-it-py, original text preserved |
| reStructuredText | `.rst` | Simplified parser: headings, paragraphs, directives |
| Plain text | `.txt` | Paragraphs split on blank lines |
| HTML | `.html`, `.htm` | Best-effort extraction of headings, paragraphs, code blocks |

A parse error in one file never stops indexing: the file is logged, counted
in the error summary and the rest is processed.

## What is skipped

- Hidden files and directories (names starting with `.`), including `.git`
- Files larger than 5 MB (listed in the indexing summary)
- Unsupported extensions
- Paths matched by `.virtudocignore` at the source root

`.virtudocignore` uses a `.gitignore`-compatible subset: globs (`*`, `?`),
`**`, anchoring with a leading `/`, trailing-slash directory patterns and
`!` negation. The last matching pattern wins. Built-in rules (hidden files,
unsupported extensions) always apply and cannot be overridden.

## How documents are chunked

- Documents are split at H2/H3 headings. Each chunk carries its
  **heading path** (for example `API -> Middleware -> Errors`) and the
  ordinal within its document.
- Target size is 600 tokens; chunks approaching 1200 tokens are split at
  paragraph boundaries. Adjacent chunks overlap by roughly 60 tokens so
  context is not lost at boundaries.
- Fenced code blocks are never split. An oversized paragraph is cut at
  line boundaries with a `(continued)` marker.
- Each chunk stores an empirical token estimate
  (chars/4 for latin, chars/3 for cyrillic text) and the code-to-text
  ratio of its content.

## Incremental updates

Every file's content hash is stored in the index. On `update` or on server
start:

- unchanged files are skipped completely;
- changed files are re-parsed and their chunks replaced in one transaction;
- deleted files have their chunks removed.

Indexing throughput on a mid-range laptop is in the thousands of files per
minute, so even large documentation sets reindex quickly.

Parser and chunker improvements only affect newly indexed content —
unchanged files are skipped by hash. To rebuild existing libraries with the
current parser and `chunk.*` settings, re-run `virtudoc import` (it replaces
same-named libraries) or remove and add the library.

## The SQLite index

All data is one file, `~/.virtudoc/index.db`, in WAL mode:

- `libraries` — registered sources;
- `documents` — indexed files with content hashes;
- `chunks` — the unit of retrieval (heading path, ordinal, text, token
  estimate, code ratio);
- `chunks_fts` — FTS5 full-text index over chunk texts, linked by rowid;
- `notes` — project notes attached to libraries;
- `project_links` — connections to consuming projects' manifests;
- `meta` — schema version.

Deleting `~/.virtudoc` removes everything; nothing is written elsewhere.
