# Methodology

Basecamp Bench compares unattended coding-agent configurations against one versioned frontend or backend contract. A report section combines results only when mode, runner-source hash, seed-tree hash, reference-manifest and reference-tree hashes, track prompt/rubric/contract hashes, schema-bundle hash, and canonical dimension labels/weights all match exactly.

## Evidence model

Intelligent evaluator agents own the complete assessment. Each evaluator receives the original seed and an immutable submission snapshot, identifies the delta, decides how to run the chosen implementation, tests it directly, inspects source and behavior, and scores every dimension from cited evidence. The runner does not prescribe filenames, runtimes, frameworks, or product test scripts.

Snapshots omit only standard machine-generated metadata, dependency trees, and runtime caches (`.DS_Store`, Python/tool caches, `node_modules`, Node compile caches, and `.venv`). These paths are also ignored when checking evaluator evidence integrity, so executing a submission cannot create a false mutation failure.

Each track is self-contained under `benchmarks/<track>/`: `prompt.md` is the pure implementation directive, `eval.md` is the complete evaluator context, and `contract.json` owns dimension IDs, scoring anchors, weights, and overall policy.

## Scoring

The runner validates the exact dimension set and computes the weighted score. Evaluators never compute the overall score. Publication results use the median per dimension across valid evaluators. Failed or missing required evidence invalidates an attempt.

Local mode supports inexpensive iteration. Local entries always carry `local_mode`, remain ineligible, and never enter a Pareto frontier. Publication mode requires:

- At least three independent implementation repetitions per configuration.
- At least two valid judge model IDs. An evaluator may share the contestant's
  model ID; that relationship remains explicit in attempt provenance.
- An immutable snapshot and complete hash/provenance manifest.
- Every required evaluator report and score artifact.
- Exact or explicitly pinned pricing; fuzzy model-price matches are ineligible.
- Safe execution settings and no evidence mutation or secret-scan finding.

Leaderboards are scoped to one complete comparison identity. They expose every raw attempt, success rate, median, mean, population standard deviation, range, judge disagreement, duration, token usage, and cost. FE and BE are not combined. Reports may combine later compatible files: exact duplicate attempts are deduplicated, model aggregates are recomputed from the combined raw attempts, and every source timestamp and run ID remains visible. Aggregate fields in source files never override this recomputation. Frontier and dominator identity is the exact `(harness, model_id)` pair, so the same model run through different harnesses remains distinct.

Run manifests also expose incurred implementation, evaluation, and combined known spend under `costs`. Failed or invalid evaluator calls remain included when their cost is known; `complete` and `unknown_job_count` make partial accounting explicit. Reevaluation excludes the reused implementation cost from newly incurred spend while preserving it on the attributed attempt.

Publication reevaluation may reuse submissions only from a completed publication run whose reused attempts have no ineligibility reasons, with identical seed, reference pack, and implementation prompt hashes. This prevents a later environment or tool probe from laundering an ineligible implementation into an eligible publication result. Rubrics and contracts may change intentionally and remain explicit in lineage. Local reevaluation remains available for diagnostics.

## Repeatability limits

Model APIs may not expose deterministic seeds, and vendor behavior can change behind a stable model name. The manifest records this limitation, executable versions, model/provider/family IDs, environment facts, and all input hashes. Repetitions and judge spread measure observed variance; they do not make nondeterministic models deterministic.

## Claim boundary

A result supports only the contract version and evidence it records. It does not establish general intelligence, production fitness of a generated application, or superiority outside these fixed tasks. Ineligible and failed attempts remain visible and are never silently removed from denominators.

## Portable exports

An export contains only `run-manifest.json` and its declared, hash-verified artifacts. Before archive creation, the exporter scans the complete captured contents of every member for high-signal credentials and scans textual members for host-specific POSIX, Windows drive, and UNC paths. JSON string values receive the same checks after escape decoding, and malformed declared JSON fails closed. Configurable safety limits default to 256 MiB per artifact, 256 MiB across all captured members, and 10,000 members; exact-boundary archives remain valid and anything larger fails before payload retention. Textual formats fail closed when they are not valid UTF-8; binary submissions and screenshots are retained as bytes. Undeclared private logs and workspaces remain outside the archive and outside the export scan.

## Official repository baseline

The committed `baseline/<run-id>/` tree is the exact unpacked portable export
of a verified run. It is immutable evidence: snapshots, attempts, evaluator
outputs, leaderboards, the manifest, and `report.html` are never hand-edited or
redacted after execution. Any shareability failure requires a corrected rerun.

The initial baseline uses local mode, one attempt per model and track, and the
canonical Sol evaluator. It supports transparent observed quality/cost
comparisons for those exact inputs and recorded versions. Local-mode points do
not qualify for the publication Pareto frontier; publication claims continue to
require the configured repetitions, distinct evaluator model IDs, complete
pricing, and external isolation.
