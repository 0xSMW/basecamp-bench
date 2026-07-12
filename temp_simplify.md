# Basecamp Bench Evaluation-System Simplification Plan

Status: implementation specification
Repository: `/Users/stephenwalker/Code/projects/basecamp/basecamp-bench`
Target worker: `grok-4.5`, high reasoning, single worker, no planning delegation, no subagents
Execution model: one work package at a time, in the order listed below

## 1. Objective

Reduce the amount of code and duplicated policy in the evaluation system while preserving its meaningful behavior:

- local and publication benchmark runs;
- implementation snapshots and evaluator evidence integrity;
- contract-bound, runner-owned scoring;
- multi-evaluator median aggregation and judge-spread reporting;
- raw attempts, failures, costs, timing, tokens, and eligibility reasons;
- compatible cross-run report generation and exact-attempt deduplication;
- immutable-submission reevaluation;
- publication verification and deterministic portable export;
- a self-contained HTML quality-versus-cost report;
- compatibility with the committed baseline evidence.

The simplification target is fewer representations, fewer validators, fewer parallel execution paths, and less presentation code. Security, evidence integrity, provenance, and fail-closed publication behavior are invariants.

## 2. Global worker contract

Every Grok assignment must use this execution envelope:

```text
Working directory: /Users/stephenwalker/Code/projects/basecamp/basecamp-bench
Model: grok-4.5
Reasoning effort: high
Permissions: read and edit only the paths explicitly listed in the work package
Agent behavior: --no-memory --no-plan --no-subagents
Git: read-only; do not stage, commit, switch branches, restore files, or rewrite history
Network: disabled; all verification must be local and credential-free
Deletion: never use rm; use apply-style edits for tracked source removal
```

Before editing, the worker must:

1. Run `git status --short` and `git diff -- <allowed paths>`.
2. Treat all existing changes as user-owned.
3. Stop and report a conflict if an existing edit overlaps the exact lines it needs and cannot be preserved safely.
4. Read the complete implementations and tests in its allowed path scope.
5. State the current source of truth and the behavior it will preserve.

After editing, the worker must return:

```text
Summary:
- concise description of the simplification

Behavior preserved:
- explicit invariant list with source/test evidence

Files changed:
- path: purpose

Verification:
- command: exact result

Net change:
- production lines added/deleted
- test lines added/deleted

Remaining concerns:
- concrete risks or "none"
```

Each package must reduce net production complexity. Moving the same logic between files without eliminating duplication fails the assignment.

## 3. Global acceptance gates

Run the narrow tests named by each package first. Before a package is accepted, the managing agent will inspect the diff and run the full ladder:

```sh
python -m ruff format --check basecamp_bench tests
python -m ruff check basecamp_bench tests
python -m mypy basecamp_bench
python -m pytest -q
```

For packages that affect generated evidence or reporting, also run:

```sh
for run in baseline/runs/*; do basecamp-bench verify-run "$run"; done
basecamp-bench report baseline/runs --output /tmp/basecamp-bench-simplified-report.html
```

The worker must never modify `baseline/` merely to make verification pass. A baseline incompatibility is a regression unless the work package explicitly authorizes a migration and includes backward compatibility.

## 4. Target architecture

The completed system should have one directional data flow:

```text
implementation/evaluator execution
        -> validated Attempt records
        -> canonical raw-attempt JSON ledger
        -> one aggregation implementation
        -> report payload
        -> HTML or optional tabular views
```

Trust boundaries remain explicit:

```text
contract JSON -> contract validator -> typed contract
judge JSON    -> judge validator    -> scored evaluator result
ledger JSON   -> ledger codec       -> typed attempts
manifest JSON -> publication verifier/exporter
```

Derived statistics must have one owner. Stored data must never contain a second authoritative copy of values that are deterministically derivable from raw attempts.

## 5. Work package 1 — Remove the dead repetition-aggregation API

### Goal

Remove `aggregate_repetitions`, which has no production caller and duplicates statistics already owned by leaderboard aggregation.

### Allowed paths

- `basecamp_bench/contracts.py`
- `tests/test_contracts.py`
- any direct documentation reference discovered with `rg`, after reporting it to the managing agent before editing

### Required work

1. Prove with `rg` that `aggregate_repetitions` is referenced only by its definition, export declaration, and dedicated tests.
2. Remove it from `__all__`, delete the function, and delete only its dedicated tests/import.
3. Remove helpers only if they become genuinely unused. Preserve `_population_stdev` if judge aggregation still uses it.
4. Do not change contract loading, judge validation, weighted scoring, or multi-judge aggregation.

### Acceptance criteria

- Importing `basecamp_bench.contracts` succeeds.
- All remaining contract tests pass.
- `rg -n "aggregate_repetitions" basecamp_bench tests` returns no matches.
- Production and test line counts decrease.

### Narrow verification

```sh
python -m pytest -q tests/test_contracts.py
python -m ruff check basecamp_bench/contracts.py tests/test_contracts.py
python -m mypy basecamp_bench/contracts.py
```

## 6. Work package 2 — Establish one canonical attempt codec and ledger

### Goal

Make raw attempts the sole persisted evaluation record and eliminate repeated conversion among `Attempt`, leaderboard entry dictionaries, `ReportPoint.raw_attempts`, and reconstructed `Attempt` objects.

### Allowed paths

- `basecamp_bench/leaderboard.py`
- `basecamp_bench/reporting.py`
- `basecamp_bench/reporting_model.py`
- `basecamp_bench/runner.py`, only call sites that write attempts or leaderboards
- `schemas/leaderboard.schema.json`
- `tests/test_leaderboard.py`
- `tests/test_reporting.py`
- `tests/test_runner.py`, only tests directly affected by the ledger shape
- `tests/test_schemas.py`, only leaderboard-schema cases
- `docs/METHODOLOGY.md`
- `README.md`, only the leaderboard artifact description

### Required design

Introduce one codec boundary, placed in `leaderboard.py` or a narrowly named replacement module if that produces a clear net reduction:

```python
def attempt_to_raw(attempt: Attempt) -> dict[str, object]: ...
def attempt_from_raw(raw: Mapping[str, object]) -> Attempt: ...
def load_attempt_ledger(path: Path) -> AttemptLedger: ...
def write_attempt_ledger(path: Path, ledger: AttemptLedger) -> Path: ...
```

Exact names may vary. The following rules may not vary:

- `Attempt` remains the sole typed representation of one attempt.
- The persisted canonical payload contains comparison identity/provenance, dimension profile, and raw attempts.
- Median, mean, standard deviation, min/max/range, success rate, expected cost, eligibility, and model aggregates are derived after loading.
- Exact attempt identity is `(run_id, submission_id, repetition)` within its comparison section.
- Byte-equivalent duplicate attempts across compatible inputs are deduplicated.
- Conflicting attempts with the same identity fail closed.
- FE and BE never combine.
- Distinct contract versions or hashes never combine.
- Publication compatibility continues to include the full comparison provenance currently enforced by reporting.
- Local reports may continue to combine compatible evidence across runner revisions according to the current methodology.
- Existing committed leaderboard JSON must remain readable through a legacy decoder. New output should use the simplified canonical shape.

### Implementation steps

1. Map every field in `_ROOT_KEYS`, `_ENTRY_KEYS`, `_RAW_ATTEMPT_KEYS`, `Attempt`, and `ReportPoint` as stored, derived, presentation-only, or provenance.
2. Add the new ledger model/codec and tests before changing runner output.
3. Route new runner output through the ledger writer.
4. Teach report loading to accept both the legacy leaderboard shape and the new ledger shape, normalizing both to `Attempt` records.
5. Replace `_parse_raw_attempt`, `_parse_entry`, and report-side reconstruction with the canonical codec wherever possible.
6. Retain one aggregation function and have both runner finalization and report generation call it.
7. Remove aggregate fields from newly persisted JSON.
8. Update the public leaderboard schema to describe the new form while retaining a legacy schema branch only if baseline validation requires it.
9. Update methodology prose to identify raw attempts as canonical and all statistics as derived.

### Prohibited outcomes

- Do not weaken malformed-input rejection.
- Do not accept NaN, infinity, booleans as numbers, negative costs, unsafe identifiers, or missing successful-attempt dimensions.
- Do not silently choose between conflicting duplicates.
- Do not alter score, cost, eligibility, or frontier formulas.
- Do not delete compatibility with committed baseline files.
- Do not keep old aggregate validators after aggregate fields cease to be persisted.

### Acceptance criteria

- New ledgers contain no derived model statistics.
- A report generated from a new ledger has the same semantic model metrics as the current implementation for the same raw attempts.
- Reports generated from committed baseline leaderboards remain valid.
- Legacy and new inputs can be combined when their comparison identities are compatible.
- Duplicate and conflicting-attempt behavior remains covered.
- The combined production line count of `leaderboard.py`, `reporting.py`, and `reporting_model.py` decreases materially; target at least 400 lines.

### Narrow verification

```sh
python -m pytest -q tests/test_leaderboard.py tests/test_reporting.py tests/test_schemas.py
python -m pytest -q tests/test_runner.py -k 'leaderboard or report or attempt'
python -m ruff check basecamp_bench/leaderboard.py basecamp_bench/reporting.py basecamp_bench/reporting_model.py
python -m mypy basecamp_bench/leaderboard.py basecamp_bench/reporting.py basecamp_bench/reporting_model.py
```

## 7. Work package 3 — Consolidate contract and judge-result validation

### Goal

Give each contract and judge-result rule one implementation while retaining public JSON Schemas and descriptive validation errors.

### Allowed paths

- `basecamp_bench/contracts.py`
- `basecamp_bench/validation.py`
- `basecamp_bench/prompts.py`
- `schemas/evaluation-contract.schema.json`
- `schemas/judge-result.schema.json`
- `tests/test_contracts.py`
- `tests/test_validation.py`
- `tests/test_prompts.py`
- `tests/test_schemas.py`

### Required design

Choose one of these approaches after measuring the smaller implementation:

1. A small internal declarative rule set that generates the public schemas and powers runtime checks; or
2. JSON Schema validation at the untrusted JSON boundary followed by concise semantic checks for dynamic dimension identity, exact expected hashes/IDs, weight sum, and cross-field rules.

The repository currently lists `jsonschema` as a development dependency. Do not add a mandatory runtime dependency unless the package metadata, installation model, and no-dependency product claim are deliberately updated and the total system is still simpler. Prefer a standard-library solution if runtime dependency changes would broaden the task.

### Required work

1. Inventory rules duplicated among the two schemas, `validate_contract_data`, `validate_judge_result`, `_extract_judge_scores`, and `compute_weighted_score`.
2. Preserve schema-level errors and semantic identity checks:
   - exact root and dimension keys;
   - schema and track values;
   - safe identifiers;
   - exact submission, judge, and contract-hash identity;
   - finite scores in `0..10` with booleans rejected;
   - nonempty notes and evidence;
   - complete and exact dimension set;
   - positive weights summing to `1.0` within the current tolerance;
   - supported weighted-sum/missing/precision policy.
3. Make judge aggregation consume already validated normalized scores. Remove the second full shape-validation pass from `_extract_judge_scores`.
4. Ensure the evaluator prompt still embeds the exact result contract and expected identities.
5. Keep error ordering deterministic.

### Acceptance criteria

- Every currently rejected malformed fixture remains rejected.
- Valid current FE and BE contracts load unchanged.
- Public schemas remain Draft 2020-12 valid.
- Aggregation cannot be called accidentally with unvalidated malformed results; enforce this with a private normalized type or a single validating entry point.
- Contract/validation production code decreases by at least 100 lines.

### Narrow verification

```sh
python -m pytest -q tests/test_contracts.py tests/test_validation.py tests/test_prompts.py tests/test_schemas.py
python -m ruff check basecamp_bench/contracts.py basecamp_bench/validation.py basecamp_bench/prompts.py
python -m mypy basecamp_bench/contracts.py basecamp_bench/validation.py basecamp_bench/prompts.py
```

## 8. Work package 4 — Unify normal runs and reevaluation with a submission source

### Goal

Preserve the `reevaluate` CLI and its security guarantees while eliminating its parallel attempt/evaluation lifecycle.

### Allowed paths

- `basecamp_bench/runner.py`
- `basecamp_bench/execution.py`, only if a shared source abstraction belongs there
- `basecamp_bench/cli.py`, only reevaluation wiring if required
- `tests/test_runner.py`
- `tests/test_cli.py`, only reevaluation cases
- `tests/test_e2e.py`, only reevaluation cases
- `README.md`, only reevaluation implementation wording

### Required design

Create an internal submission-source abstraction with two implementations:

```text
BuildSubmissionSource
  - materializes seed
  - runs implementation agent
  - snapshots successful workspace
  - returns implementation provenance

VerifiedSnapshotSource
  - verifies prior run and selected attempt
  - copies the declared immutable snapshot
  - returns attributed historical implementation provenance
  - records zero newly incurred implementation spend
```

Both sources must feed one shared function that:

- runs evaluators;
- aggregates valid judge results;
- creates `Attempt`;
- writes the attempt artifact;
- checkpoints jobs/artifacts;
- emits progress;
- applies current contract, evaluator, pricing, and eligibility policy.

### Required work

1. Preserve prior-run verification and all publication reevaluation gates.
2. Keep reusable-submission selection deterministic and explicit.
3. Extract prior snapshot loading/verification from orchestration.
4. Replace `_reeval_submission` and the duplicate lower half of `_run_repetition` with the shared evaluation/finalization path.
5. Retain lineage hashes and prior run identity.
6. Preserve historical implementation duration, usage, and attributed cost on the attempt while excluding reused implementation cost from newly incurred run spend.
7. Preserve cancellation, checkpointing, and failure recording.

### Prohibited outcomes

- Do not weaken snapshot hash verification.
- Do not permit publication reevaluation from a local, incomplete, or ineligible prior run.
- Do not allow changed seed, reference pack, or implementation prompt hashes.
- Do not mutate the prior run.
- Do not double-count historical implementation cost as newly incurred spend.
- Do not change evaluator concurrency semantics.

### Acceptance criteria

- Existing reevaluation behavior and CLI remain available.
- Normal and reevaluation attempts pass through one shared evaluator/attempt-finalization implementation.
- `_reeval_submission` is removed or reduced to source preparation with no scoring/persistence duplication.
- `runner.py` decreases by at least 200 lines.
- Tests cover local reevaluation, publication gates, selected submissions, lineage, cost accounting, hash mismatch, and prior-run immutability.

### Narrow verification

```sh
python -m pytest -q tests/test_runner.py -k 'reeval or repetition or evaluator or cost'
python -m pytest -q tests/test_cli.py -k reevaluate
python -m pytest -q tests/test_e2e.py -k reevaluate
python -m ruff check basecamp_bench/runner.py basecamp_bench/execution.py basecamp_bench/cli.py
python -m mypy basecamp_bench/runner.py basecamp_bench/execution.py basecamp_bench/cli.py
```

## 9. Work package 5 — Make JSON canonical and tabular formats optional views

### Goal

Remove CSV and Markdown generation from the benchmark finalization path while preserving access to those views for callers that need them.

### Allowed paths

- `basecamp_bench/leaderboard.py`
- `basecamp_bench/cli.py`
- `basecamp_bench/runner.py`, only final artifact wiring
- `basecamp_bench/manifest.py`, only artifact expectations if required
- `tests/test_leaderboard.py`
- `tests/test_cli.py`
- `tests/test_runner.py`, only artifact-list cases
- `README.md`
- `docs/METHODOLOGY.md`

### Required design

- JSON/ledger output is the canonical machine artifact.
- Normal benchmark completion writes only canonical JSON plus the HTML report.
- CSV and Markdown become pure projections generated explicitly from canonical JSON.
- Preserve an importable compatibility API or CLI command for generating both views, unless a repository-wide usage search proves there are no non-test/documentation callers and the managing agent approves deletion.

### Required work

1. Prove all internal consumers of CSV/Markdown with `rg`.
2. Remove tabular writing from normal run finalization and manifest artifacts.
3. Move projection code to a small optional function/module or add an explicit CLI flag/command.
4. Generate views exclusively from canonical loaded attempts/aggregates.
5. Keep deterministic ordering, UTF-8, CSV escaping, and Markdown escaping.
6. Update documentation to describe JSON as canonical and tabular files as optional exports.

### Acceptance criteria

- A normal run produces no unused CSV/Markdown leaderboard artifacts.
- Explicit tabular export produces semantically identical rows.
- Reports depend only on canonical JSON.
- Publication verification/export accepts the new artifact set and still accepts committed baseline manifests.
- Net production code decreases; if compatibility requires almost all old code, stop and report that this opportunity does not justify implementation.

### Narrow verification

```sh
python -m pytest -q tests/test_leaderboard.py tests/test_cli.py
python -m pytest -q tests/test_runner.py -k 'leaderboard or manifest or artifacts'
python -m ruff check basecamp_bench/leaderboard.py basecamp_bench/cli.py basecamp_bench/runner.py
python -m mypy basecamp_bench/leaderboard.py basecamp_bench/cli.py basecamp_bench/runner.py
```

## 10. Work package 6 — Reduce the HTML report to the essential decision surface

### Goal

Cut bespoke rendering code while preserving the report’s analytical functionality and complete embedded data.

### Allowed paths

- `basecamp_bench/report_rendering.py`
- `basecamp_bench/reporting.py`, only payload fields that become presentation-dead
- `tests/test_reporting.py`
- `README.md`, only report-content wording
- `docs/METHODOLOGY.md`, only presentation wording

### Required report surface

Each compatible track/contract section must retain:

- one plain-language leader/value verdict;
- score, expected implementation cost, success rate, tokens, and duration per model;
- one compact cost-versus-quality plot with model labels;
- per-dimension scores and weights;
- evaluator overhead and total observed cost;
- uncertainty when multiple repetitions exist;
- failures and ineligibility reasons when present;
- accessible tabular equivalents for charted information;
- embedded deterministic JSON containing complete provenance and raw attempts;
- separate FE/BE and contract sections;
- safe HTML escaping and no filesystem paths.

### Candidates for removal

- duplicated aggregate and raw-attempt tables when a clean single repetition makes them identical;
- expanded provenance HTML already present in embedded JSON;
- repeated methodology prose that belongs in `docs/METHODOLOGY.md`;
- decorative badges, classifications, color explanations, and secondary statistics with no decision value;
- elaborate chart annotations or error bars that duplicate visible numeric uncertainty;
- helper functions used by only one removable presentation element.

### Required work

1. Inventory every payload field and rendered location.
2. Define the minimal DOM sections above before editing.
3. Remove presentation elements and their helpers as a unit.
4. Keep the report a deterministic, offline, single-file HTML document.
5. Keep semantic HTML, keyboard usability, responsive overflow handling, dark/light compatibility, and accessible chart text/table fallback.
6. Preserve complete machine evidence in the embedded JSON payload.
7. Update snapshot/string tests to assert semantics rather than incidental CSS or prose.

### Prohibited outcomes

- Do not remove information from the embedded payload.
- Do not hide failures or mixed eligibility.
- Do not remove the cost-quality relationship.
- Do not introduce JavaScript or external assets merely to shorten Python.
- Do not render unescaped imported text.

### Acceptance criteria

- Generated report remains self-contained and deterministic.
- All required surface items are present.
- The HTML contains no absolute host paths or unsafe unescaped model data.
- The report remains useful with SVG disabled through its tables/text.
- `report_rendering.py` decreases by at least 300 lines, with a stretch target of 500.

### Narrow verification

```sh
python -m pytest -q tests/test_reporting.py
python -m ruff check basecamp_bench/report_rendering.py basecamp_bench/reporting.py tests/test_reporting.py
python -m mypy basecamp_bench/report_rendering.py basecamp_bench/reporting.py
basecamp-bench report baseline/runs --output /tmp/basecamp-bench-simplified-report.html
```

The worker must report the output path and a compact checklist of required visible sections. The managing agent owns final visual inspection.

## 11. Work package 7 — Isolate publication verification and export from the local core

### Goal

Keep all publication guarantees and public APIs while removing publication-only validation/export machinery from modules imported by ordinary local evaluation and reporting.

### Allowed paths

- `basecamp_bench/manifest.py`
- `basecamp_bench/manifest_export.py`
- `basecamp_bench/runner.py`, only imports/calls to publication boundaries
- `basecamp_bench/cli.py`, only verify/export wiring
- `basecamp_bench/config.py`, only publication policy wiring if necessary
- `tests/test_manifest.py`
- `tests/test_e2e.py`, only verify/export cases
- `tests/test_runner.py`, only publication cases
- `tests/test_cli.py`, only verify/export cases
- `schemas/run-manifest.schema.json`
- `docs/METHODOLOGY.md`
- `docs/SECURITY.md`
- `README.md`

### Required architecture

Split responsibilities into explicit boundaries:

```text
run provenance builder
  - builds/checkpoints the manifest data needed by every run

publication verifier
  - validates strict public manifest shape
  - verifies artifact paths, file types, sizes, hashes, and containment
  - enforces publication completeness and eligibility

portable exporter
  - calls verifier
  - captures declared artifacts
  - scans secrets/shareability
  - creates deterministic ZIP
```

Local execution should import the provenance builder only. `verify-run`, `export-run`, publication finalization, and publication reevaluation may import the strict verifier/exporter.

### Required work

1. Map every function and constant in `manifest.py` to provenance, verification, shareability scanning, or export.
2. Remove the lazy compatibility cycle between `manifest.export_run` and `manifest_export.export_run`; preserve the documented import path through a thin stable facade if tests or callers rely on it.
3. Move publication-only validators and artifact scanning behind the publication boundary.
4. Keep a single implementation of manifest shape validation.
5. Keep the public run-manifest schema aligned with runtime validation.
6. Ensure ordinary local report loading does not import export/ZIP/shareability code.
7. Preserve deterministic manifest writes and crash-safe checkpoint behavior.

### Non-negotiable invariants

- strict exact-key manifest validation;
- safe relative paths and rejection of symlinks/path escapes;
- artifact size/member/total limits;
- exact artifact hashes;
- private-artifact exclusion;
- high-signal credential and host-path scanning;
- decoded JSON-string scanning;
- invalid UTF-8 fail-closed behavior for textual artifacts;
- deterministic ZIP bytes and no overwrite;
- complete incurred-cost accounting;
- compatibility with all committed baseline runs.

### Acceptance criteria

- Local run/report imports do not load ZIP/export/shareability implementation modules.
- `verify-run` and `export-run` retain their CLI and importable behavior.
- All committed baseline runs verify.
- Exporting a baseline run twice to different new destinations yields byte-identical archives.
- All negative security tests continue to pass.
- Core module dependency direction is acyclic and documented in module docstrings.
- This package may primarily reduce coupling. If it increases total production LOC by more than 50 lines, the worker must justify the increase with measured reductions in imports or cyclomatic responsibility; otherwise stop and report no worthwhile change.

### Narrow verification

```sh
python -m pytest -q tests/test_manifest.py
python -m pytest -q tests/test_e2e.py -k 'verify or export'
python -m pytest -q tests/test_runner.py -k publication
python -m pytest -q tests/test_cli.py -k 'verify or export'
python -m ruff check basecamp_bench/manifest.py basecamp_bench/manifest_export.py basecamp_bench/runner.py basecamp_bench/cli.py
python -m mypy basecamp_bench/manifest.py basecamp_bench/manifest_export.py basecamp_bench/runner.py basecamp_bench/cli.py
```

## 12. Sequencing and integration rules

Execute packages in this order:

1. Remove dead repetition aggregation.
2. Establish the canonical attempt ledger.
3. Consolidate contract and judge validation.
4. Unify normal and reevaluation pipelines.
5. Make tabular formats optional.
6. Simplify report rendering.
7. Isolate publication verification/export.

Packages 2–7 must start from the accepted output of the previous package. Do not run them concurrently: they overlap in core types, schemas, runner finalization, and reporting tests.

After every accepted package, the managing agent should record:

| Metric | Before | After | Delta |
|---|---:|---:|---:|
| Production Python LOC | | | |
| Test Python LOC | | | |
| Number of persisted evaluation representations | | | |
| Number of full attempt validators | | | |
| `runner.py` LOC | | | |
| `leaderboard.py` + `reporting.py` LOC | | | |
| `report_rendering.py` LOC | | | |
| Full tests passed | | | |

Use this command for stable line measurements:

```sh
wc -l basecamp_bench/*.py tests/*.py | tail -1
wc -l basecamp_bench/runner.py basecamp_bench/leaderboard.py \
  basecamp_bench/reporting.py basecamp_bench/report_rendering.py
```

## 13. Stop conditions

A Grok worker must stop without editing further when:

- preserving committed baseline compatibility requires an undocumented format decision;
- an existing user edit overlaps the proposed change and safe integration is unclear;
- the package cannot achieve a net complexity reduction;
- a security or publication invariant would need to be weakened;
- a required behavior has no test and the worker cannot define a deterministic one;
- the narrow test suite exposes a pre-existing unrelated failure;
- the implementation requires editing outside the allowed path scope.

The worker should return the blocking evidence, the smallest decision needed from the managing agent, and no speculative workaround.

## 14. Final completion criteria

The simplification program is complete only when:

- every persisted evaluation fact has one authoritative representation;
- every derived metric has one implementation;
- every untrusted JSON boundary has one validator/codec;
- normal and reevaluation submissions share evaluation and attempt finalization;
- JSON is the canonical evidence format;
- the HTML report retains the complete decision surface with substantially less renderer code;
- publication verification/export remains fail-closed and baseline-compatible;
- the full formatting, lint, type-check, test, baseline verification, and report-generation gates pass;
- the managing agent independently inspects all diffs and generated outputs;
- total production LOC decreases materially, with a program target of 1,500–2,500 lines.
