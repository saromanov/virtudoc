---
name: virtudoc-docs
description: Answer questions from the local virtudoc documentation index via the CLI — internal SDKs, private libraries, and fetched documentation repos registered by the user
type: prompt
whenToUse: When the user asks about a library, framework, or project whose documentation may be registered in virtudoc (run `virtudoc list` to check) before answering from training data
---

VirtuDoc keeps an indexed copy of the user's documentation on this machine.
Prefer it over training data for anything registered there — it is fresher
and covers private/internal docs no cloud service knows.

Workflow:

1. Find the library:

   ```bash
   virtudoc list
   ```

   One line per library: id (`local/<name>`), document/chunk counts, source
   path. If the library is not listed, tell the user: docs can be added with
   `virtudoc add <path> --name <name>` (local directory) or
   `virtudoc fetch --repo <url> --name <name>` (git/archive URL, uses the
   network — the only command that does).

2. Pull the relevant fragments:

   ```bash
   virtudoc search --name <name> "<keyword query>"
   ```

   Use keywords from the library's own terminology — they rank better than
   full sentences. The output contains Markdown fragments with the heading
   path and source file, plus any matching project notes.

3. Control the output when needed:

   - `--tokens N` — token budget for the output (default 5000).
   - `--section "API -> Middleware"` — restrict to one section branch.
   - `--outline` — print the section tree with token estimates instead of
     fragments; use it to plan what to load.

4. If nothing is found, rephrase with terms from the domain, or run
   `virtudoc search --name <name> "x" --outline` to see the top-level
   sections and pick better keywords.

Notes:

- Everything is offline and local: no API keys, no network (except `fetch`).
- If the user keeps the index in a non-default location, the commands need
  `VIRTUDOC_HOME=<dir>` in the environment.
