# Stale-symbol evaluation

Measures how often an agent answers with a symbol that was renamed or removed
after it read the documentation, and whether `coverage` changes that.
Manual run, once per release; not part of CI.

## Corpus

`generate.py` creates two documentation trees and `tasks.jsonl`:

- 10 symbols renamed in `v2/`;
- 5 symbols removed in `v2/`;
- 5 control symbols unchanged.

Names are invented (`sessionBootstrap`, `legacyTokenIssuer`, ...) so the model
cannot know the v2 answer from training data. All 20 tasks ask the agent to
write a call; each task records the expected v2 symbol and the forbidden old
one.

## Conditions

| Condition | Index | Server |
|---|---|---|
| A — control | v1 | current |
| B — staleness without signals | v1, then v2 | build before v0.3.1 |
| C — staleness with signals | v1, then v2 | v0.3.1 |

In B and C the agent solves a warm-up task on v1 first, then the library is
reindexed from v2 and the same agent session continues. This reproduces
"memory from context" instead of a cold start.

## Run

```bash
python evals/stale_symbols/generate.py
python evals/stale_symbols/run.py --runs 3 --conditions a b c
```

`run.py` uses `claude -p` with an MCP config pointing at the eval data
directory. Override the agent binary with `VIRTUDOC_EVAL_AGENT`, the server
binary for condition B with `VIRTUDOC_BIN`, and the model with
`VIRTUDOC_EVAL_MODEL`. Logs go to `runs/<date>/`; `summary.json` holds the
metrics.

## Metrics

- **stale_rate** — answers containing a forbidden old symbol;
- **correct_rate** — answers containing the expected v2 symbol;
- **honest_rate** — for deleted symbols, answers stating the documentation
  does not cover them.

## Results

Fill in after the run. Do not publish invented numbers.

| Condition | stale_rate | correct_rate | honest_rate |
|---|---|---|---|
| A — control | — | — | — |
| B — without coverage | — | — | — |
| C — with coverage | — | — | — |

Model: —. Date: —.

Main README line after a run:

> stale symbols: X/20 without coverage → Y/20 with coverage
