# Project Notes

Documentation answers "how does the API work". Project notes answer "how
do **we** do it": conventions, decisions, gotchas, forbidden modules,
workaround rationales. VirtuDoc attaches such notes to a library and
delivers them to agents together with the documentation.

## Managing notes

```bash
virtudoc note add mylib --text "Always use the retry helper for outbound calls."
virtudoc note list mylib
virtudoc note remove mylib --id 3
```

- Notes are attached to a library and survive reindexing.
- They are removed together with the library.
- Each note shows its creation date in the output.

## How notes reach agents

When `get-library-docs` (or `virtudoc search --name`) runs, notes are
scored for relevance to the query using the same normalization and
stemming as full-text search. Relevant notes are included in a dedicated
`notes` block **before** the documentation fragments, within the
`notes.max_tokens` budget (default 800 tokens). Unrelated notes stay out of
the response.

Example: an agent asking about request timeouts receives both the
library's `query()` signature and your team's "timeout=30s by default"
convention — in one response, before any code is written.

## Keeping notes healthy

Notes age. Each note records the revision of the indexed source it was
written against — a short fingerprint of the indexed files and their content
hashes, so it works for non-git sources too. `note list` prints it next to
the note.

When the documentation changes, the note no longer describes the same
source. `virtudoc doctor` reports it:

```
  WARN local/mylib: note #3 is stale — the source changed since it was
       written (note rev 4f2a91c8d0ab, current 9b7e3d1f5c22);
       review it with 'virtudoc note list mylib'
```

Review the note, then either remove it or re-add it: a new note captures
the current revision and is fresh again. Notes written before revision
tracking have no revision recorded and are not reported. `doctor` exits
with code `1` while stale notes remain, so CI can gate on it.
