# Deterministic Conformance Evaluator Plan

Yes. A deterministic conformance layer would materially improve the evaluator, and the historical submissions give us enough evidence to design it well.

## What the investigation found

For clean recurrence counts, I analyzed the ten substantive completed baseline implementations: five backend and five frontend. I also checked later outcomes, including empty and near-empty submissions, but excluded those from recurrence statistics.

The current evaluator is entirely adaptive. Each judge chooses how to launch and test the submission, then returns qualitative dimension scores. The runner validates result shape and computes weighted scores; it has no structured conformance results, pass counts, skips, or release blockers. See `basecamp_bench/prompts.py:60`, `docs/METHODOLOGY.md:5`, and `schemas/judge-result.schema.json:7`.

That flexibility found valuable defects, but test depth varied substantially between submissions:

- Backend schema validation ranged from representative checks to 80 seeded GET operations.
- Authorization never used a complete actor × operation × resource-state matrix.
- Lifecycle, restart persistence, security, malformed-input, and pagination probes were opportunistic.
- Every frontend judge was unable to launch a real browser, so responsive, visual, focus, and accessibility conclusions often came from source inspection or custom Node harnesses.

### Recurring submission failures

| Failure family | Recurrence |
|---|---:|
| BE complete route registration overstated actual correctness | 5/5 |
| BE response schema/status/HTTP defects | 5/5 |
| BE hierarchy, relationship, or lifecycle defects | 5/5 |
| BE malformed input/type/media/date defects | 5/5 |
| BE partial behavior returned misleading success | 5/5 |
| BE seed or determinism mismatches | 5/5 |
| BE dangerous token/reset/CORS defaults | 4/5 |
| FE visible controls were inert, toast-only, stubbed, or unwired | 5/5 |
| FE persistence/reset/malformed-state defects | 5/5 |
| FE focus, naming, overlay, or keyboard accessibility defects | 5/5 |
| FE mobile/responsive/touch defects | 5/5 |
| FE directly demonstrated unsafe rendering/URL handling | 3/5 |
| Submission-authored tests | 0/10 |

The headline lesson is especially strong: all five substantive backends registered essentially the entire 203-operation surface, yet every one had material contract or behavioral failures.

Examples include:

- 203 registered operations alongside 11/80 invalid seeded GET responses and 38/107 invalid serialized entities: `baseline/runs/2026-07-11T16-12-55Z--fe-be--anthropic-claude-fable-5_openai-gpt-5-6-sol--20260711t161255z-843ef2/evaluations/be-claude-anthropic-claude-fable-5-r1--066754ef/judge-codex-openai-gpt-5-6-sol--a4caab04/output/report.md:9`.
- 71 explicit endpoint stubs plus resource-type and parent confusion: `baseline/runs/2026-07-11T16-12-55Z--fe-be--anthropic-claude-fable-5_openai-gpt-5-6-sol--20260711t161255z-843ef2/evaluations/be-codex-openai-gpt-5-6-sol-r1--4d93e09d/judge-codex-openai-gpt-5-6-sol--43efe948/output/report.md:33`.
- A shadowed canonical route, malformed path handling, and missing-body successes: `baseline/runs/2026-07-11T17-08-07Z--fe-be--xai-grok-4-5--20260711t170807z-db8473/evaluations/be-grok-xai-grok-4-5-r1--8cb2e796/judge-codex-openai-gpt-5-6-sol--589aab95/output/report.md:46`.
- Client-authorized project mutation plus exposed reset credentials: `baseline/runs/2026-07-11T19-00-35Z--fe-be--anthropic-claude-sonnet-5_openai-gpt-5-5--20260711t190035z-d06816/evaluations/be-sonnet5-anthropic-claude-sonnet-5-r1--e747ba1f/judge-codex-openai-gpt-5-6-sol--1f074660/output/report.md:73`.
- Frontend duplicate IDs, malformed-state crashes, and cosmetic-only Bubble Up behavior: `baseline/runs/2026-07-11T17-08-07Z--fe-be--xai-grok-4-5--20260711t170807z-db8473/evaluations/fe-grok-xai-grok-4-5-r1--5bc128c8/judge-codex-openai-gpt-5-6-sol--d7969ce9/output/report.md:15`.
- Persisted rich-content injection, unsafe URLs, and dangling state after deletion: `baseline/runs/2026-07-11T16-12-55Z--fe-be--anthropic-claude-fable-5_openai-gpt-5-6-sol--20260711t161255z-843ef2/evaluations/fe-claude-anthropic-claude-fable-5-r1--0b1ef122/judge-codex-openai-gpt-5-6-sol--7886ff58/output/report.md:69`.

## Recommended evaluator design

Use three outputs per submission:

1. Deterministic conformance results.
2. Release-blocker results.
3. Adaptive judge scores.

Run them in this order:

```text
immutable snapshot
  → deterministic conformance suite
  → verify conformance artifacts
  → adaptive judge receives those results
  → aggregate and publish both result families
```

The judge cannot modify pass counts. Its job becomes targeted investigation, qualitative assessment, and discovery of defects outside the fixed suite.

### Backend fixed suite

The proposed list should expand beyond the original idea:

- Startup, readiness, shutdown, reset, and repeatability.
- Exact 203-operation topology:
  - registered;
  - intended handler reached;
  - explicit unsupported;
  - schema-conformant;
  - behaviorally exercised;
  - crash or fake success.
- All OpenAPI request and response schemas, statuses, and headers.
- `Location`, content types, empty `204`, pagination, ETag/HEAD, request IDs, errors, 404/405/`Allow`, and emitted URL resolvability.
- Exact deterministic seed graph and reset hash.
- Authorization matrix:
  - owner;
  - admin/employee;
  - collaborator;
  - invited client;
  - project outsider;
  - wrong account;
  - invalid/missing credential.
- Stateful CRUD for each major resource family.
- Comments, boosts, subscriptions, events, movement, counters, idempotency, and embedded projections.
- Parent/type/bucket identity checks and cross-project movement rejection.
- Recursive lifecycle matrix across project → tool → recording → child → grandchild.
- Active/archive/trash/restore behavior, inherited status, visibility, read-only freezing, and dangling-reference scans.
- Process-lifetime persistence, deterministic reset, ID uniqueness, token/reset state, and rate-limit clearing.
- Malformed JSON/UTF-8, impossible dates, wrong types, missing bodies, unsupported media, invalid IDs, excessive sizes, malformed pagination, and unknown references.
- Concurrency/threading and atomic persistence checks where relevant.
- Security gates:
  - credential leakage;
  - unauthenticated reset;
  - unauthorized client/admin actions;
  - credentialed wildcard CORS;
  - SSRF;
  - header/log injection;
  - unsafe errors;
  - broken body-limit handling.

“All 203 routes” becomes the first measurement rather than the completion claim.

### Frontend fixed suite

This requires a real pinned browser environment:

- Runtime boot plus console/network error capture.
- Required route, detail, overlay, empty-state, and not-found inventory.
- Exact seed narrative and cross-screen identity consistency.
- Scripted workflows for:
  - create;
  - edit;
  - comment;
  - boost;
  - complete/reopen;
  - filter;
  - card movement;
  - archive/trash/restore/delete;
  - chat;
  - personal state;
  - reset.
- Every success message must correspond to an asserted state transition.
- Navigation-away/back and reload persistence for every workflow.
- Malformed, outdated, partial, wrong-shaped, or unavailable storage.
- ID uniqueness and dangling-reference checks following deletion.
- Stored and reflected XSS tests across titles, comments, rich text, chat, notes, metadata, and URLs.
- Keyboard-only workflow matrix.
- Accessible names, landmarks, roles, state announcements, focus trap/restoration, inert backgrounds, reduced motion, contrast, and touch-target checks.
- Desktop, tablet, and phone viewports with overflow and feature-preservation assertions.
- Light/dark/OS preference modes.
- Deterministic screenshots with tolerant perceptual thresholds.
- Basic performance and dependency/offline checks.

Screenshot differences should contribute evidence and counts. Visual craft and taste still belong to the judge.

## Reporting

Avoid one undifferentiated percentage. Publish category denominators:

```text
Routes                 203/203 registered
Handlers                202/203 reached
Response schemas        165/203 passed
Negative input cases    742/810 passed
Authorization cells     486/504 passed
Lifecycle scenarios      71/84 passed
Frontend workflows        19/27 passed
Accessibility checks     143/161 passed
Security gates             17/20 passed
Release blockers               2
Adaptive judge score          7.6
```

Keep the judge score and conformance counts separate for the first version. Security, authorization, startup, core data integrity, and lifecycle failures should be hard gates; a high visual or breadth score should not average them away.

After one shadow benchmark round, the data can determine whether ranking should become gate → conformance → judge score or retain a multi-axis presentation.

## Preventing overfitting

Historical submissions should identify missing invariant families. Expected outcomes must come from OpenAPI, `behavior-model.json`, `INIT.md`, and the versioned evaluation contracts.

For example:

- Generalize `2025-02-31` into generated impossible-date cases.
- Generalize “Message returned from Todo route” into incompatible type/route combinations.
- Generalize client project deletion into the full authorization matrix.
- Generalize duplicate `proj_1001` into ID-uniqueness properties across reload/reset.
- Generalize one `<img onerror>` into rotating HTML, SVG, URL, CSS, and encoding payloads.

Additional safeguards:

- Reserve 20–30% of cases as untouched holdouts.
- Rotate IDs, dates, nesting depths, page sizes, roles, and malicious encodings.
- Maintain `case_id → source clause → dimension → severity → first observed defect`.
- Freeze and hash the suite before contestant execution.
- Publish taxonomy and category totals; keep live case bodies private until the round closes.
- Replay every suite revision against all historical snapshots.
- Mutation-test the evaluator itself by intentionally breaking auth, cascade, schemas, persistence, and UI actions.
- Validate false positives against different implementation architectures.
- Promote adaptive findings only after grounding them in the contract.

## Required infrastructure change

A deterministic black-box runner needs a tiny public launch protocol. Today, submissions may use any filename, runtime, framework, startup command, token scheme, or route format. Runtime discovery by an LLM would preserve nondeterminism at the most important boundary.

I recommend allowing a metadata sidecar such as `bench-entry.json`, containing:

- track and launch command;
- port/base-URL configuration;
- readiness endpoint;
- deterministic clock/seed controls;
- reset strategy;
- test actor credentials and roles;
- frontend entry URL.

The implementation remains single-file; this sidecar is runner metadata.

The conformance executor also needs a pinned browser/runtime container. All five historical frontend evaluations failed to obtain real browser execution, and backend judges frequently had to bypass denied loopback binding. The suite must run outside the model CLI sandbox inside an isolated, network-denied publication container with a fresh browser profile.

## Concrete implementation path

1. Define the public launch descriptor and isolated executor.
2. Build the backend suite first from OpenAPI, INIT invariants, and historical regression families.
3. Add a browser-capable frontend image with Playwright and accessibility tooling.
4. Add versioned conformance pack/result schemas.
5. Store suite version/hash, environment hash, category totals, cases, skips, infrastructure errors, gates, and artifact hashes.
6. Insert conformance execution between snapshotting and adaptive evaluation.
7. Feed the immutable result to judges.
8. Extend attempt ledgers and HTML reports with category counts and gates.
9. Replay against historical snapshots and mutation-test the suite.
10. Run one shadow round before making conformance gates publication-authoritative.

This requires a methodology and contract version bump because the current methodology explicitly gives the evaluator complete ownership of product testing. No repository files were changed during this investigation.
