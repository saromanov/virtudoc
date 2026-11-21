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
virtudoc note list mylib
virtudoc note edit mylib 3 --text "Retries are capped at 5."
virtudoc note review mylib 3
virtudoc note review mylib --all --yes
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

Each note is anchored to the documents it relies on:

- explicit `--path` anchors (validated against the index, with closest-path
  hints on typos);
- otherwise the note text is searched in the variant and the top distinct
  documents become anchors (`notes.auto_anchor_k`, default 3; a document must
  score at least `notes.auto_anchor_ratio` = 0.5 of the best score);
- `--no-anchor` creates a note without anchors for conventions unrelated to
  the documentation.

`note list` shows the status of every note:

```
ID  STATUS   PIN  VERSION  REVIEWED          ANCHORS                 TEXT
7   stale    -    *        2026-11-20 14:02  guide/http.md (chg)     Use the retry helper…
9   current  ★    *        2026-11-21 09:15  api/client.md           All calls go through…
12  unknown  -    2.1.0    —                 —                       Do not use v1 auth…
```

- `current` — the anchored documents are unchanged.
- `stale` — an anchor changed or disappeared (`anchor_changed`,
  `anchor_deleted`), the library fingerprint changed for a library-level
  anchor (`library_changed`), or the note was checked against another
  variant (`version_differs`).
- `unknown` — the note has no anchors (pre-v4 notes and `--no-anchor`).
  Unknown notes never produce a warning.

Only documents the note actually relies on mark it stale: editing an
unrelated page does not.

## How notes reach agents

When `get-library-docs` (or `virtudoc search --name`) runs, pinned notes come
first, then the notes whose words match the query. Every note carries
`status`, `stale_reason`, `changed_sources` and `reviewed_at` in the same
response. If any note is stale, the payload adds `notes_warning`:

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
the warning. `--all` reviews every note of the library after showing their
statuses; add `--yes` for non-interactive runs.

`virtudoc doctor` reports stale notes with the reason and the exact command:

```
  WARN mylib: note #7 is stale (anchor_changed: guide/http.md)
         review:   virtudoc note review mylib 7
         reanchor: virtudoc note review mylib 7 --reanchor
```

`doctor` exits with code `1` while notes are stale, so CI can gate on it.
