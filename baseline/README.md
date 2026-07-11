# Official baseline

This directory contains the repository's verified exploratory baseline as an
exact unpacked portable run export. Each run directory retains its manifest,
raw attempts, immutable model snapshots, evaluator reports and results,
leaderboards, and self-contained HTML report.

The baseline compares Sol 5.6, Fable 5, GLM-5.2, and Grok 4.5 on the FE and BE
tracks with Sol 5.6 evaluating every submission, including Sol. It uses local
mode with one attempt per model and track, so the observed quality/cost points
remain outside publication eligibility and official Pareto-frontier claims.

After a baseline is added, verify and deterministically regenerate its report:

```sh
basecamp-bench verify-run baseline/<run-id>
basecamp-bench report baseline/<run-id>/leaderboards \
  --output /tmp/basecamp-bench-report.html
cmp baseline/<run-id>/report.html /tmp/basecamp-bench-report.html
```

Private logs, prompts, credentials, execution workspaces, and undeclared files
must never appear here. Baseline evidence is repository-only and intentionally
excluded from the Python package.
