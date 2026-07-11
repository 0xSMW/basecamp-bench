# Basecamp Bench

Basecamp Bench compares coding-agent harnesses on two realistic product tasks:
a frontend prototype and a production-shaped API. Implementation agents receive
only a concise product directive and the same seed/reference pack. Independent
evaluator agents inspect each immutable submission, decide how to run and test
it, and return evidence-backed dimension scores under a versioned contract.

The runner preserves raw attempts, computes scores and eligibility itself, and
generates a self-contained HTML quality-versus-cost report. FE and BE results
remain separate.

## Status

This is an alpha benchmark. Contracts are versioned, outputs are auditable, and
publication mode fails closed. Model APIs and vendor CLIs remain nondeterministic
external systems; results support only the exact inputs and provenance recorded
in their run manifest.

The source release includes no model scores or rankings. The credential-free
test suite validates adapter command shapes and the complete fake-harness
pipeline; paid live implementation/evaluation runs remain a separate release
gate before making production-reliability or performance claims.

## Requirements

- Python 3.11 or newer.
- A clone of this repository; run commands from its root.
- Any selected agent CLIs installed and authenticated: Codex, Claude Code,
  and/or Grok.
- A disposable VM for real publication runs.

No Python runtime dependencies are required.

## Install

```sh
python -m pip install -e .
basecamp-bench --help
```

Copy the annotated configuration and adjust models, evaluators, or executable
paths. `bench.toml` is intentionally ignored.

```sh
cp bench.example.toml bench.toml
basecamp-bench show-config
```

## Run

Local mode is intended for iteration and defaults to one repetition and one
evaluator:

```sh
basecamp-bench run --harness codex --track fe
```

Harness and track flags may be repeated or comma-separated. Workspace-only
execution is the default. A harness without an OS sandbox requires either an
explicit local acknowledgement or a confirmed disposable boundary:

```sh
basecamp-bench run --harness grok --track be \
  --allow-unsafe-host-execution
```

Publication mode requires at least three implementation repetitions, two valid
evaluator model IDs that do not exactly match the contestant model, exact or
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

Reports discover every matching leaderboard beneath the supplied directories,
so adding later model runs requires no hand-edited data:

```sh
basecamp-bench report runs --output model-performance.html
```

The output is one deterministic, offline HTML file containing separate
track/contract sections, Pareto frontier charts, expected implementation cost
per valid result, evaluator overhead, uncertainty, dimension profiles, raw
attempts, failures, eligibility reasons, and provenance hashes. Imported text
is escaped and the underlying tables remain usable without SVG.

## Re-evaluate immutable submissions

Re-evaluation verifies an earlier run and its declared snapshot hashes, then
creates a new run with fresh evaluator attempts. The prior run is never changed.

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
file. Workspaces and private logs are never exported.

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

Leaderboard aggregation is scoped by track, contract version, harness, and
model. Failed attempts remain visible and contribute to success rate. The
frontier's primary cost is median implementation cost per attempt divided by
success rate; evaluation cost is reported separately.

See [Methodology](docs/METHODOLOGY.md) for claim boundaries and detailed rules.

## Safety

Agent CLIs and generated applications are untrusted code. Directory separation,
prompt instructions, environment allowlists, hash checks, log caps, and process
group cleanup provide integrity and operational controls; they are not a host
security boundary. Never run real agents beside personal files, ambient cloud
credentials, a Docker socket, an SSH agent, a browser profile, or production
systems.

Use the [isolated execution guide](docs/ISOLATION.md) and the reference
[`containers/`](containers/) recipe. Publication mode requires an explicit
confirmation that the external isolation boundary exists.

## Add models and harnesses

A new model on an existing CLI is configuration only: update the corresponding
`[harnesses.<id>]` model and pricing override if necessary. A new harness needs
an adapter implementing command construction, environment allowlisting, working
directory, stdin, and output/usage parsing. Adapter tests must cover redaction,
malformed output, permissions, timeouts, and vendor output drift.

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
