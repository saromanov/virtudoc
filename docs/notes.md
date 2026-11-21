# Project Notes

Documentation answers "how does the API work". Project notes answer "how
do **we** do it": conventions, decisions, gotchas, forbidden modules,
workaround rationales. VirtuDoc attaches such notes to a logical library and
delivers them to agents together with the documentation.

## Managing notes

```bash
virtudoc note add mylib --text "Always use the retry helper for outbound calls."
virtudoc note add mylib --text "Retries are capped at 3." --path api/retry.md
virtudoc note add mylib --text "Never call the legacy client." --pin
virtudoc note add mylib --text "Auth v1 is gone in 2.0." --version 2.0
virtudoc note list mylib [--stale-only] [--json]
virtudoc note edit mylib 3 --text "Retries are capped at 5."
virtudoc note review mylib 3
virtudoc note review mylib --all --stale-only --yes
virtudoc note pin mylib 9
virtudoc note unpin mylib 9
virtudoc note remove mylib --id 3
```

- Notes belong to the **logical library** (`mylib`), so every indexed version
  and branch sees them. `--version V` limits a note to one variant.
- `--pin` sends a note with every response, regardless of the query.
- Notes survive reindexing and are removed together with the library.
- Note text over `notes.max_note_tokens` (default 300) is rejected: a long
  note must be split, because rules are never truncated in responses.

## Anchors and status

A note is anchored to the documents and the code identifiers it relies on:

- explicit `--path` anchors (validated against the index, with the three
  closest-path hints on typos);
- otherwise the note text is searched in the variant and the top distinct
  documents become anchors (`notes.auto_anchor_k`, default 3; a document must
  score at least `notes.auto_anchor_ratio` = 0.5 of the best score);
- identifiers are extracted from the note text with the same parser the
  coverage checks use (`notes.max_symbols`, default 10). Identifiers present
  in the docs become symbol anchors; the rest are reported as
  `not in docs (not tracked)` and never trigger warnings;
- `--no-anchor` creates a note without either kind of anchor for conventions
  unrelated to the documentation.

`note add` prints both kinds:

```
Note #7 added to auth.
  anchored documents: guide/http.md, api/retry.md
  anchored symbols:   issueLegacyToken, validateSession
  not in docs (not tracked): retryWithJitter
```

`note list` shows the status of every note:

```
ID  STATUS   REASON           PIN  SCOPE  REVIEWED          ANCHORS                     TEXT
7   stale    symbol_removed   -    *      2026-11-20 14:02  issueLegacyToken (gone)     Use issueLegacyToken…
9   current  —                ★    *      2026-11-21 09:15  api/client.md, retry()      All calls go through…
12  unknown  —                -    2.1.0  —                 —                           Do not use v1 auth…
```

`--json` prints the same note objects agents receive; `--stale-only` hides
current and unknown notes.

- `current` — the anchored documents are unchanged.
- `stale` — a symbol anchor is gone from the served variant
  (`symbol_removed`); a document anchor changed or disappeared
  (`anchor_changed`, `anchor_deleted`); the library fingerprint changed for a
  library-level anchor (`library_changed`); or the note was checked against
  another variant (`version_differs`). Symbol anchors win: they are the
  cross-version signal.
- `unknown` — the note has no anchors (pre-v4 notes and `--no-anchor`).
  Unknown notes never produce a warning.

Only documents the note actually relies on mark it stale: editing an
unrelated page does not.

## How notes reach agents

When `get-library-docs` (or `virtudoc search --name`) runs, pinned notes come
first, then the notes whose words match the query. Every note carries
`status`, `stale_reason`, `stale_details` and `reviewed_at` in the same
response. `stale_details` holds up to five items, chosen by the reason:

| `stale_reason` | `stale_details` |
|---|---|
| `symbol_removed` | removed symbol names |
| `anchor_deleted` / `anchor_changed` | changed `rel_path` values |
| `version_differs` | `origin_library_id` |
| `library_changed` | `[]` |

`changed_sources` is kept for one release as a deprecated alias of
`stale_details`; it will be removed in v0.4.0.

If a query mentions an identifier the docs never had, the payload adds
`missing_identifiers`, and notes that mention such an identifier carry
`mentions_missing` — the agent then knows the name came from a note, not
from the documentation. If any note is stale, the payload adds
`notes_warning`:

> N team note(s) may be outdated: the documentation they rely on changed
> after they were written or last reviewed. If a note contradicts the
> fragments, follow the fragments and tell the user the note may be outdated.

Notes are never truncated. A note that does not fit the remaining
`notes.max_tokens` budget is skipped and counted in `notes_omitted`. Pinned
notes have their own budget, `notes.pinned_max_tokens` (default 400).

## Confirming a note

After reviewing a note, confirm it against one variant:

```bash
virtudoc note review mylib 3
virtudoc note review mylib 3 --reanchor   # pick anchors from the current docs
```

`review` records the current document hashes, sets `reviewed_at` and clears
the warning. If a symbol anchor was removed, `review` refuses to confirm the
note:

```
error: note #7 references issueLegacyToken, which is no longer in the docs.
  edit the note:  virtudoc note edit auth 7 --text "..."
  or accept it:   virtudoc note review auth 7 --reanchor
```

Edit the text when the note is wrong, or pass `--reanchor` to recompute both
kinds of anchors and accept the removal. `note edit` always re-anchors.
`--all` reviews every note of the library after showing their statuses;
`--stale-only` limits it to stale notes, `--yes` skips the prompt.

`virtudoc doctor` reports stale notes with the reason and the exact command:

```
  WARN mylib: note #7 is stale (symbol_removed: issueLegacyToken)
         edit:     virtudoc note edit mylib 7 --text "..."
         reanchor: virtudoc note review mylib 7 --reanchor
  WARN mylib: note #11 is stale (anchor_changed: guide/http.md)
         review:   virtudoc note review mylib 11
         reanchor: virtudoc note review mylib 11 --reanchor
         mylib: note #3 is 410 tokens (limit 300): split this note
```

`doctor` exits with code `1` while notes are stale, so CI can gate on it.
