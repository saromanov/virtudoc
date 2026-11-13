# How Search Works

Understanding the ranking helps formulate queries — both for humans in the
terminal and for agents calling `get-library-docs`.

## Query pipeline

1. **Normalization.** The query is lowercased; `ё` is folded to `е` so
   Russian text matches regardless of the letter's spelling.
2. **Tokenization** splits the query into words (latin and cyrillic).
3. **A cascade of FTS5 queries** runs until something hits:
   - exact `AND` — all query words must be present;
   - prefix `AND` — words are stemmed by stripping common endings and
     matched as prefixes, so Russian inflections resolve: `пакетная`
     finds `пакетной`, `запросов` finds `запросы`;
   - `OR` — any word is enough (ranked by relevance).
4. **Ranking.** Results are ordered by BM25. Chunks whose heading path or
   file name lexically matches query words get a **topic boost**; chunks
   containing code blocks in the requested `language` are promoted.
5. **Assembly.** Fragments are appended until the token budget (default
   5000) or the result limit (default 20) is reached. Overspending the
   budget is not an error — the output is cut at a chunk boundary with a
   10% margin. `page=2` returns the next slice of the same ranking.

## Tips for good queries

- Use **keywords from the library's domain** rather than full sentences:
  `batch_query timeout` works better than `how do I set a timeout`.
- Names of classes, functions and error types are ideal tokens.
- When unsure about the structure, ask for `mode="outline"` first, then
  narrow with `section`.
- If the response has no snippets, it contains the library's top-level
  sections — rephrase using their terminology.

## What is searched

Only the chunk texts and metadata of registered libraries — never the
network. Search, indexing (except `fetch`), listing and removal are fully
offline by design.
