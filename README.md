# Basecamp Bench

Basecamp Bench compares coding-agent harnesses on two realistic product tasks:
a frontend prototype and a production-shaped API. Implementation agents receive
only a concise product directive and the same seed/reference pack. Independent
evaluator agents inspect each immutable submission, decide how to run and test
it, and return evidence-backed dimension scores under a versioned contract.

The runner preserves raw attempts, computes scores and eligibility itself, and
generates a self-contained HTML quality-versus-cost report. FE and BE results
remain separate.

## Frontier, July 11, 2026

Basecamp Bench asks each agent to build two parts of a Basecamp 5 clone from a
fixed specification: a single-file frontend SPA and a production-shaped backend
API. An independent evaluator scores every submission across weighted quality
dimensions. In the July 11 baseline, Fable 5 leads both tracks with scores of
8.39 for the backend and 7.58 for the frontend. It also has the highest total
cost at $85.87. Grok 4.5 delivers the best value at $9.30 total, with a
competitive backend score of 7.28. Sonnet 5 costs $36.23, and GPT-5.6 Sol costs
$15.13. Both produce strong backends and weaker frontends. Every frontier model
scores higher on the backend than the frontend. Release-ready UI remains the
harder problem.

| Model | Frontend | Backend | Total time | Total cost |
| --- | ---: | ---: | ---: | ---: |
| Fable 5 | 7.578 | 8.392 | 2:06:40 | $85.87 |
| Sonnet 5 | 6.982 | 7.243 | 1:27:09 | $36.23 |
| Grok 4.5 | 6.384 | 7.278 | 36:48 | $9.30 |
| GPT-5.6 Sol | 5.765 | 7.310 | 59:48 | $15.13 |
| GPT-5.5 | 5.670 | 7.084 | 44:14 | $10.94 |

Full evidence is available in [baseline/](baseline/). Read the
[full report](https://basecamp-bench-report.smw-ai.chatgpt.site).

## Getting Started

Basecamp Bench runs locally with Python 3.11 or newer and no runtime
dependencies. Install and authenticate the agent CLIs you plan to compare:
Codex, Claude Code, Grok, Pi, and/or Google Antigravity (`agy`). External OS
isolation is optional for local runs and required for publication; the provided
container is one way to supply it.

1. Clone this repository and enter its root.
2. Install the runner and copy the annotated local configuration.

```sh
python -m pip install -e .
cp bench.example.toml bench.toml
```

3. Adjust the selected models or executable paths, inspect the effective
   configuration, and start a run.

```sh
basecamp-bench show-config
basecamp-bench run --harness codex --track fe
```

## Run

Local mode is intended for iteration and defaults to one repetition and one
evaluator:

```sh
basecamp-bench run --harness codex --track fe
```

Independent implementation attempts run concurrently. As soon as one
submission is snapshotted, its evaluator calls run concurrently while other
implementations continue. Live progress is written to stderr with attributable
`build`, `evaluate`, `aggregate`, `report`, and final run events; use `--quiet`
to suppress it. The completed run path remains the only stdout output. At most
32 paid agent processes run simultaneously by default; adjust the safety cap
with `--max-parallel-agents`.

Harness and track flags may be repeated or comma-separated. Local jobs use
isolated folders beneath `runs/<run-id>/workspaces`; `full_access = true`
requires `--allow-unsafe-host-execution` locally or
`--confirmed-isolated-environment` inside an external boundary.

Publication mode requires at least three implementation repetitions, two valid
evaluator model IDs, exact or
pinned pricing, a distributable reference pack, and confirmed isolation:

```sh
basecamp-bench run --mode publication --repetitions 3 \
  --confirmed-isolated-environment
```

Configure at least two enabled `[[evaluators]]` entries in `bench.toml` before
running that command. Paid model calls can be substantial; inspect the complete
fleet first with `basecamp-bench show-config` and enforce provider spending
limits outside the runner.

## Regenerate the report

Reports discover every matching attempt ledger (or legacy leaderboard JSON)
beneath the supplied directories. Each ledger stores comparison identity,
dimension profile, and raw attempts; statistics are derived at report time, so
adding later model runs requires no hand-edited aggregates. Canonical machine
artifacts are schema 2.0 JSON attempt ledgers (plus the HTML report). Normal
runs do not write leaderboard CSV or Markdown files.

```sh
basecamp-bench report runs --output model-performance.html
```

Optional display-name overrides apply only at render time and never rewrite
evidence:

```sh
basecamp-bench report runs --output model-performance.html \
  --rename model-id="Friendly Name"
```

The output is one deterministic, offline HTML decision surface: separate
track/contract sections, a plain-language verdict, a cost-versus-quality chart
with model labels, a comparison table (score, expected implementation cost per
valid result, success rate, tokens, duration, evaluator overhead, total
observed cost, and uncertainty when repetitions exist), dimension scores and
weights, failures and mixed-eligibility footnotes, and a complete embedded JSON
payload with provenance hashes, classifications, source IDs, and raw attempts.
Tables remain usable without SVG; imported text is escaped. Methodology prose
lives in docs/METHODOLOGY.md. Local reports combine matching benchmark evidence
across runner revisions for exploratory comparison; publication reports keep
those revisions separate.

## Optional CSV and Markdown projections

When a spreadsheet or static Markdown table is needed, project one leaderboard
JSON explicitly. Rows are derived only from the loaded attempts through the
same aggregator the report uses; CSV/Markdown are never a second source of
truth:

```sh
basecamp-bench export-tabular \
  runs/<run-id>/leaderboards/leaderboard_<track>_<version>_<sha>.json \
  --output-dir /tmp/tabular
```

The command writes two deterministic UTF-8 files named from the comparison
identity (`leaderboard_*.csv` and `leaderboard_*.md`) and refuses to overwrite
existing targets. Both schema 2.0 attempt ledgers and committed legacy schema
1.0 leaderboards are accepted as input.

## Re-evaluate immutable submissions

Re-evaluation verifies an earlier run and its declared snapshot hashes, then
creates a new run with fresh evaluator attempts. The prior run is never changed.
Verified prior snapshots and normal implementations share one evaluator and
attempt-finalization path; only the submission source differs.

```sh
basecamp-bench reevaluate runs/<run-id> --track fe
```

Current contracts and evaluator configuration apply to the new evaluation;
lineage records the prior run manifest and snapshot hashes.

## Verify and export

```sh
basecamp-bench verify-run runs/<run-id>
basecamp-bench export-run runs/<run-id> basecamp-bench-run.zip
```

Verification checks the strict manifest shape, identifiers, relative paths, and
every declared artifact hash. Export includes only the manifest and declared
public artifacts, scans them for likely credentials, rejects symlinks and path
escapes, and writes a deterministic archive without overwriting an existing
file. Workspaces and private logs are never exported. Local evaluation imports
only run provenance builders; publication verification and ZIP export load at
the verify/export (and reevaluation) boundary. Public APIs
`from basecamp_bench.manifest import verify_run, export_run` remain stable.

## Official baseline

The repository's [`baseline/`](baseline/) directory contains four verified,
shareability-scanned reference runs and one combined self-contained HTML report.
It preserves model snapshots, evaluator reports and results, raw attempts,
leaderboards, and provenance manifests while excluding private logs, prompts,
credentials, and execution workspaces.

```sh
for run in baseline/runs/*; do basecamp-bench verify-run "$run"; done
basecamp-bench report baseline/runs --output /tmp/basecamp-bench-report.html \
  --rename 'claude-fable-5=Fable 5' \
  --rename 'claude-sonnet-5=Sonnet 5' \
  --rename 'gpt-5.6-sol=GPT-5.6 Sol' \
  --commentary baseline/commentary.json
cmp baseline/report.html /tmp/basecamp-bench-report.html
```

New compatible model runs can be compared by regenerating a report from the
baseline and additional run directories together. The committed baseline is an
exploratory local run; its quality and cost points are auditable, while official
publication eligibility and Pareto-frontier claims require the stricter
repetition, evaluator, pricing, and isolation rules below.

## Method

Each track lives under `benchmarks/<track>/`:

- `prompt.md` is passed byte-for-byte to implementation agents. It contains the
  task only—no benchmark, evaluator, rubric, output filename, or runner
  instructions.
- `eval.md` gives evaluator agents the full assessment context and evidence
  standard without prescribing the submission's language, runtime, filenames,
  commands, or architecture.
- `contract.json` is the canonical dimension, anchor, weight, and score policy.

Evaluators receive disposable copies of the original seed and immutable
submission plus an exact JSON result schema. The runner rejects missing or
unknown dimensions, non-finite/out-of-range scores, identity or hash mismatch,
malformed evidence, failed evaluator processes, and any evidence mutation. It
then takes the median evaluator score per dimension and computes the weighted
overall score.

Sol 5.6 is the default evaluator model. Comparative evaluator trials with
Fable 5, Grok 4.5, and Sol 5.6 produced similarly fair judgments; Sol was the
most detailed and thorough. Changing the evaluator is therefore a methodology
and cost-routing choice that may affect the level of detail more than the
general direction of the judgment.

Leaderboard aggregation is scoped by track, contract version, harness, and
model. Failed attempts remain visible and contribute to success rate. The
frontier's primary cost is median implementation cost per attempt divided by
success rate; evaluation cost is reported separately.

See [Methodology](docs/METHODOLOGY.md) for claim boundaries and detailed rules.

## Safety

Agent CLIs and generated applications are untrusted code. Directory separation,
prompt instructions, environment allowlists, hash checks, log caps, and process
group cleanup provide integrity and operational controls; some harnesses do not
provide an OS security boundary. Use spend-limited credentials and keep
personal files, ambient cloud credentials, Docker sockets, SSH agents, browser
profiles, and production systems outside the run environment.

The [isolated execution guide](docs/ISOLATION.md) and reference
[`containers/`](containers/) recipe provide optional local hardening.
Publication mode requires an explicit external-isolation confirmation.

## Add models and harnesses

A new model on an existing CLI is configuration only: update the corresponding
`[harnesses.<id>]` model and pricing override if necessary. A new harness needs
an adapter implementing command construction, environment allowlisting, working
directory, stdin, and output/usage parsing. Adapter tests must cover redaction,
malformed output, permissions, timeouts, and vendor output drift.

The optional `pi` adapter exposes the safe benchmark model ID `glm-5.2` and
routes it to OpenRouter's `z-ai/glm-5.2`; set `OPENROUTER_API_KEY` and use the
commented example in `bench.example.toml`. Pi relies on the per-job workspace
boundary in local mode; the optional container recipe adds OS isolation.

The optional `agy` adapter supports `gemini-3.5-flash` at `low`, `medium`, or
`high` effort. It enables Antigravity's terminal sandbox and stages evaluator
evidence as disposable workspace copies, preserving the immutable originals.
See the commented `bench.example.toml` entry for setup.

Contract or evaluator-directive changes require a new contract version and
changelog entry. Published contract versions are immutable.

## Repository map

```text
basecamp_bench/     runner, adapters, validation, aggregation, reporting, CLI
benchmarks/         FE/BE directives, evaluator rubrics, contracts, asset manifest
Repo/               identical seed and reference material supplied to agents
schemas/            public artifact JSON Schemas
docs/               methodology, security, rights, and community documentation
containers/         disposable non-root runner reference
tests/              unit and credential-free fake-harness integration tests
```

## Rights and independence

Project code and original documentation are Apache-2.0. The vendored
`basecamp-sdk` reference material is MIT-licensed at its recorded upstream
commit. Personal-account screenshots are documented as `Fair use` in the
hash-matched reference-pack manifest. See [asset provenance](docs/ASSETS.md),
[third-party notices](docs/THIRD_PARTY_NOTICES.md), and
[trademarks](docs/TRADEMARKS.md).

This is an independent evaluation project and is not affiliated with or
endorsed by Basecamp.
