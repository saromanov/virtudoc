# How Search Works

Understanding the ranking helps formulate queries — both for humans in the
terminal and for agents calling `get-library-docs`.

## Query pipeline

1. **Normalization.** The query is lowercased; `ё` is folded to `е` so
   Russian text matches regardless of the letter's spelling.
2. **Tokenization** splits the query into words (latin and cyrillic).
3. **A cascade of FTS5 queries** runs until something hits:
   - exact `AND` — all query words must be present;
   - prefix `AND` — words are reduced to stems and matched as prefixes.
     Russian words lose common noun/adjective endings, English words lose
     only inflectional suffixes (`-ing`, `-ed`, `-es`, `-s`, `-ies`), so
     `пакетная` finds `пакетной`, `запросов` finds `запросы`,
     `configuring` finds `configured`. English words are never cut to an
     arbitrary length: `assert` does not become a prefix of `assessment`;
   - `OR` — any word is enough (ranked by relevance);
   - trigram `OR` — a second index over the same chunks matches query
     words as *substrings*, so subword hits inside compound identifiers
     (`auth` inside `middlewareAuth`) still surface.
4. **Ranking.** Results are ordered by BM25. Chunks whose heading path or
   file name matches a query word on **token boundaries** get a topic
   boost; chunks containing code blocks in the requested `language` are
   promoted. A query word inside a longer unrelated word (`api` inside
   `Partial`) does not count as a topic match.
5. **Assembly.** Long chunks are cut down to a window around the densest
   cluster of query-term occurrences (`search.snippet_window_tokens`,
   400 by default, 0 disables the trimming) so the agent pays for the
   relevant passage, not the whole page. Fragments are appended until the
   token budget (default 5000) or the result limit (default 20) is
   reached. A page ends before a fragment that does not fit the remaining
   budget; a fragment larger than the whole budget is truncated to fit.
   Every response reports
   `total_matches` (all ranked matches for the query, independent of the
   page) and `has_more`. `page=2` continues with the first fragment that
   did not fit the previous page, so token-budget filtering never drops
   chunks between pages.

## Score semantics

`score` in a fragment is a **relevance value, not a probability and not a
position**. It is the negation of SQLite's FTS5 `bm25()` rank for the
chunk — bm25 returns negative values where more negative is better, so
the exposed score grows with relevance. On top of that:

- a topic match (heading path or file name, on token boundaries)
  multiplies the score by `1.25`;
- a matching code `language` multiplies it by `1.15`.

Only scores inside one response are comparable. Absolute values depend
on the library size, chunk lengths and query terms, are not normalized
to a fixed range, and may change between versions. Two chunks with the
same text and the same query get the same score regardless of the order
in which the database returns them.

## Tips for good queries

- Use **keywords from the library's domain** rather than full sentences:
  `batch_query timeout` works better than `how do I set a timeout`.
- Names of classes, functions and error types are ideal tokens.
- When unsure about the structure, ask for `mode="outline"` first, then
  narrow with `section`.
- If the response has no snippets, do not answer from memory: it states
  that the index does not cover the query and lists the library's
  top-level sections — rephrase using their terminology.

## What is searched

Only the chunk texts and metadata of registered libraries — never the
network. Search, indexing (except `fetch`), listing and removal are fully
offline by design.
