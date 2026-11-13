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

Notes age. Use `note list` to review them periodically, `note remove` to
delete stale ones, and `virtudoc doctor` to spot libraries accumulating
many notes.
