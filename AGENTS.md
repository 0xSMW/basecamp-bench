# Agent instructions

## Before pushing

After the final commit and before pushing to GitHub, run:

```sh
python -m ruff format --check basecamp_bench tests
python -m ruff check basecamp_bench tests
python -m mypy basecamp_bench
python -m pytest -q
```

Fix and commit any failures, then confirm the worktree is clean before pushing.

Across completed local runs, a successful implementation job takes a median of about 16 minutes and an evaluator job about 8 minutes, so quiet periods within those windows are normal.

Never run evaluators on failed implementation artifacts; treat them as invalid and diagnose or rerun the implementation instead of wasting paid evaluation calls.

## Benchmark reporting

- Use a Luna Max subagent to monitor active runs and report to the managing agent every 60 seconds. Inspect fresh raw Codex thread outputs, including worker threads, tool results, and artifacts; file sizes and buffered runner logs alone are insufficient. If internal messaging is unavailable, use a local status file that the managing agent polls. Never use external messaging tools for monitor reports.
- Present user-facing progress every 60 seconds as a table with columns: `Elapsed | Stage | Completed since last update | Evidence | Currently working on`. Include a row per active FE/BE stage, followed by 1–2 concise commentary lines.
- Report concrete completed work, observed results, current tasks, and evidenced blockers. Distinguish code written from checks passed. Do not speculate about activity or repeat stale work as new progress.
- Comment only on what the evidence presents. Do not narrate missing, pending, or hypothetical checks or events. Do not discuss browser verification during implementation; during evaluation, report browser activity only when it actually occurs.
- After evaluation, compare against all previous evaluated models, including those the user explicitly requested. Use `Model | Frontend | Backend | Total time | Total cost`, with three-decimal scores, clock-formatted time, and currency-formatted cost. Bold the current model's row and order rows by ranking; do not add reasoning, delta, or other columns unless requested. Verify historical values from saved results and keep failed or unevaluated entries unranked.
