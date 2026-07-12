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
