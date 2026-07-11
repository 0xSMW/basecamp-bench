# Official baseline

This directory contains the repository's verified exploratory baseline as three
portable run exports plus a combined self-contained report. Each run directory
retains its manifest, raw attempts, immutable model snapshots, evaluator reports
and results, leaderboards, and individual HTML report.

The baseline compares Sol 5.6, Fable 5, and Grok 4.5 on the FE and BE tracks
with Sol 5.6 evaluating every submission, including Sol. It uses local
mode with one attempt per model and track, so the observed quality/cost points
remain outside publication eligibility and official Pareto-frontier claims.

Verify every evidence bundle and deterministically regenerate the combined
report:

```sh
for run in baseline/runs/*; do basecamp-bench verify-run "$run"; done
basecamp-bench report baseline/runs --output /tmp/basecamp-bench-report.html
cmp baseline/report.html /tmp/basecamp-bench-report.html
```

Private logs, prompts, credentials, execution workspaces, and undeclared files
must never appear here. Baseline evidence is repository-only and intentionally
excluded from the Python package.
