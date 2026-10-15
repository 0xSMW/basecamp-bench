# Muse — Basecamp Bench, October 14, 2026

All four Muse models ran FE and BE through Muse Code 1.4.7 at high effort, Standard mode, one logical implementation per track; two interrupted frontends continued in their original sessions. GPT-6.1 Sol high evaluated immutable copies using the current contracts.

Wall-clock elapsed time for all four models together: **7:22:46**. All eight evaluations completed successfully; no implementation was rebuilt.

## Comparison with the previous top ten

| Model | Frontend | Backend | Total time | Total cost |
| --- | ---: | ---: | ---: | ---: |
| Claude Fable 5.1 | 8.183 | 8.965 | 3:04:06 | $92.96 |
| GPT-6 Astra | 7.607 | 9.305 | 1:36:31 | $25.57 |
| Claude Sonnet 5.5 (High) | 7.575 | 9.011 | 2:18:20 | $52.30 |
| Claude Opus 5 | 7.583 | 8.390 | 2:26:31 | $78.11 |
| Claude Fable 5 | 7.578 | 8.392 | 2:06:41 | $85.87 |
| Claude Opus 5.5 (High) | 6.625 | 8.683 | 1:59:02 | $32.30 |
| Grok 4.6 | 7.104 | 7.926 | 0:57:58 | $10.85 |
| GPT-6.1 Sol (High) | 6.195 | 8.775 | 1:52:05 | $4.92 |
| Grok 4.7 High* | 6.650 | 7.605 | 4:20:06 | ≥$10.46 |
| Claude Sonnet 5 | 6.982 | 7.243 | 1:27:09 | $36.23 |
| **Muse Wonder Full 1.0 (High)** | **5.360** | **6.635** | **3:18:28** | **Unknown** |
| **Muse Spark 1.3 (High)** | **5.127** | **5.570** | **3:28:46** | **$7.29** |
| **Muse Wonder 1.0 (High)** | **4.980** | **5.470** | **1:30:35** | **$7.71** |
| **Muse Spark 1.2 (High)** | **4.070** | **4.745** | **0:54:09** | **$3.23** |

Ranked by the mean FE/BE score. Total time sums implementation and evaluator stage durations, including concurrent stages and CLI shutdown waits. Cost includes implementation and evaluation API-rate estimates, excludes readiness smoke tests, and does not allocate subscription fees. Wonder Full has no published token price; only its evaluation cost is known.

Historical rows retain their saved judges and contracts. Earlier rows used GPT-5.6 Sol or GPT-6 Sol, and earlier FE contracts differ. These are one-attempt results, not a controlled estimate of model-only differences. * Grok 4.7 FE used Sol assistance; its saved full-lineage cost is a lower bound.

Current evaluations disclosed environment limits: rendered-browser access was blocked for the completed frontends, so visual and responsive scores rely on reference images, source and non-rendering DOM evidence and are provisional. Backend evaluators exercised unmodified HTTP handlers through in-process transport, local socket pairs or byte streams; listening-socket startup/shutdown was blocked. The per-track findings retain each evaluator’s specific limitations.

The CSV explicitly includes `assisted`, `cost_lower_bound` and `cost_known` fields. A blank numeric cost with `cost_known=False` means unknown, not zero.

## Harness recovery

Wonder Full FE and Spark 1.3 FE exhausted the shared Muse subscription window. Their same native sessions were resumed in the existing folders after reset. Costs include cumulative session usage and stage time includes the interrupted work; idle waiting for quota is excluded from stage totals and included in wall clock.

Muse exports deliberately omit ephemeral tool-output deltas and mark them as gaps. The initial parser rejected those gaps. The parser was corrected to accept only explicitly marked ephemeral status omissions while rejecting missing durable records. Completed implementations were reparsed and evaluated from frozen copies of their existing folders; no implementation was rebuilt.

Wonder finished its root FE/BE turns, and Wonder Full finished its root FE/BE turns, but kept their CLIs alive while background test servers remained running. After verifying exact process parents and workspace directories, those servers were stopped. The detailed tables show both native turn time and total CLI build time so the shutdown delay is visible.

## Muse Spark 1.2 (High)

Mean FE/BE score: **4.408**.

| Track | Score | Native build time | CLI build time | Eval time | Build cost | Eval cost | Total cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| FE | 4.070 | 0:12:08 | 0:12:14 | 0:16:45 | $0.53 | $0.85 | $1.38 |
| BE | 4.745 | 0:08:40 | 0:08:46 | 0:16:24 | $0.99 | $0.86 | $1.86 |
| Combined | 4.408 | 0:20:48 | 0:21:00 | 0:33:09 | $1.52 | $1.71 | $3.23 |

### FE findings

Recognizable Basecamp single-file prototype with broad route scaffolding and several working in-memory workflows. Material navigation, card-step, input-escaping and state-continuity failures; many file, administrative and lifecycle controls are stubs. Notes and theme alone persist. Browser rendering was blocked by environment policy, so visual and responsive assessments rely on inspected references, source and DOM evidence and remain provisional.

| Dimension | Score | Findings |
| --- | ---: | --- |
| reference fidelity | 4.000 | Recognizable shell and screenshot-backed tool anatomy, with material sheet, heading, toolbar, preview, and card-table differences. Source/reference comparison only; rendered geometry was blocked. |
| visual craft | 5.000 | A reasonably coherent token-based visual system with shared cards, buttons, chips, and avatars, but uneven micro-UI and simplified presentation. Browser rendering, motion, and optical alignment remain unverified. |
| product model | 5.500 | Substantial recognizable Basecamp sample data and relationships, but the shared Recording/lifecycle model and cross-tool narrative are shallow or inconsistent. |
| surface coverage | 5.500 | Broad primary route scaffolding and detail pages are present, with major gaps in Docs & Files details, administration, secondary states, and useful account-wide surfaces. |
| functional depth | 4.000 | Several create, completion, comment, filtering, and card-move workflows work in memory; editing/deletion, file workflows, scheduling, and multiple advertised controls are absent, stubbed, or broken. |
| interaction quality | 3.000 | Basic click feedback, prompt cancellation, filtering and a few shortcuts work, but focus loss, draft destruction, shortcut collisions and misleading destinations impede ordinary use. |
| state persistence | 3.000 | Core data survives navigation during one document lifetime, while nearly all product mutations reset on a fresh boot. Notes and theme persist; editable notes fields and draft continuity fail even before reload. |
| responsive adaptation | 4.000 | Explicit home, dock and field-grid reflow plus horizontal board scrolling show adaptation intent. Fixed shell geometry and control sizing remain weak; actual desktop/mobile layout and touch behavior could not be verified. |
| accessibility | 2.500 | Some landmarks, dialog names, native buttons and labeled step checkboxes exist. Critical custom controls lack semantics/keyboard access, and focus, contrast, announcements and reduced-motion support are inadequate. |
| code architecture | 4.500 | The dependency-free single file has readable logical sections and reusable helpers, but global state, string rendering, inline handlers, duplicated concerns and an incorrectly patched router hinder reliable extension. |
| reliability | 3.000 | Dependency-free startup and most seeded routes execute, with basic unknown-ID handling. Confirmed injection, navigation, storage and interaction failures prevent robust ordinary use. |

Evidence: [FE evaluator result](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T09-17-02Z--fe--meta-muse-spark-1-2--20261014t091702z-d3fdc0/evaluations/fe-muse-spark-12-meta-muse-spark-1-2-r1--93e50c21/judge-sol61-openai-gpt-6-1-sol--80e28a73/output/result.json) and [saved attempt](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T09-17-02Z--fe--meta-muse-spark-1-2--20261014t091702z-d3fdc0/attempts/fe-muse-spark-12-meta-muse-spark-1-2-r1--93e50c21.json).

### BE findings

Complete registered endpoint breadth with substantial in-memory core CRUD and a recognizable sample graph. Contract serialization and navigation, lifecycle/cross-resource invariants, authentic credential/role checks, packaging and explicit handling of incomplete subsystems are substantially below production-shaped expectations. Evaluation ran through FastAPI's in-process HTTP transport; the sandbox prevented listening-socket verification.

| Dimension | Score | Findings |
| --- | ---: | --- |
| architecture domain | 4.500 | A real shared recording index and typed stores support the principal content types, but tools, projects, steps, groups, and check-ins do not form a coherent extensible recording graph. Parent and account ownership are frequently hard-coded or unenforced. |
| endpoint coverage | 10.000 | The full canonical surface is registered and dispatches with the required methods and URL structures. This is breadth credit only; numerous handlers are shallow or stubbed, as assessed separately. |
| behavioral depth | 4.500 | Substantial process-lifetime CRUD and basic recording lifecycle work. Cross-resource consistency, inheritance, tool wiring, card completion/reordering, subscription deltas, and several stateful subsystems fail. |
| http contract | 3.500 | Many core JSON envelopes and create/delete statuses are usable, but significant schema, status, URL, query, pagination, and generated-OpenAPI defects prevent SDK contract fidelity. |
| seed fidelity | 5.500 | The main sample cast, project, tool content quantities and several links between resources are present with stable IDs. Missing seed mechanics, mismatched dates/counters, disconnected dock roots and broken serialized links substantially weaken fidelity and navigability. |
| request hardening | 2.000 | Some required-field and identifier checks, missing-auth rejection and safe generic error bodies exist, but credential bypasses, absent authorization, weak type validation, permissive ownership and request limits are major failures. |
| operability | 4.000 | Importable FastAPI app, health/docs, request IDs and a Uvicorn entry point provide basic operability and in-process testability. Dependency packaging, configurable deployment URLs/auth/CORS, logging correlation, reset seams and accurate generated API docs are incomplete. |
| code quality | 4.500 | The single file is organized and named clearly enough to inspect, with some shared helpers and straightforward CRUD. Extensive duplicated dictionaries, untyped request bodies, unused abstractions and incomplete shared invariants make extension fragile. |
| scope honesty | 2.000 | Some limitations are acknowledged in source comments and some unavailable details fail with 404. Many incomplete operations nevertheless report successful mutation or fabricated completion, conflicting with the production-shaped/full-surface claims. |

Evidence: [BE evaluator result](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T09-17-02Z--be--meta-muse-spark-1-2--20261014t091702z-af821c/evaluations/be-muse-spark-12-meta-muse-spark-1-2-r1--b4c3fe1f/judge-sol61-openai-gpt-6-1-sol--f342051d/output/result.json) and [saved attempt](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T09-17-02Z--be--meta-muse-spark-1-2--20261014t091702z-af821c/attempts/be-muse-spark-12-meta-muse-spark-1-2-r1--b4c3fe1f.json).
## Muse Spark 1.3 (High)

Mean FE/BE score: **5.349**.

| Track | Score | Native build time | CLI build time | Eval time | Build cost | Eval cost | Total cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| FE | 5.127 | 2:41:18 | 2:41:29 | 0:14:24 | $4.11 | $0.78 | $4.89 |
| BE | 5.570 | 0:16:04 | 0:16:11 | 0:16:42 | $1.56 | $0.84 | $2.40 |
| Combined | 5.349 | 2:57:22 | 2:57:40 | 0:31:06 | $5.67 | $1.62 | $7.29 |

### FE findings

A broad, self-contained Basecamp prototype with rich sample content and several working persistent direct workflows. DOM execution exposed shared modal/jump click blockers, incorrect comment targeting, hidden held cards, focus loss, and malformed-state crashes. Visual, responsive, and browser accessibility judgments are source/DOM-backed and provisional because browser execution was blocked. No overall score was computed.

| Dimension | Score | Findings |
| --- | ---: | --- |
| reference fidelity | 4.500 | Recognizable shell and all screenshot-backed tool anatomy, but several material token, density, and component mismatches. Source/DOM comparison only; actual rendered geometry was unavailable. |
| visual craft | 5.600 | A reasonably coherent tokenized dark/light system with reusable controls, chips, avatars, and feedback. Detail work is simplified and actual visual finish could not be judged in a rendering browser. |
| product model | 6.500 | Rich website-launch narrative and most Basecamp concepts are recognizable, but global tool arrays and inconsistent relationships fall short of the shared recording/project model. |
| surface coverage | 7.300 | Broad generated and navigable surface inventory, including all required tools, details, trays, and many optional account views. Administrative, structural, editing, and lifecycle secondary states remain shallow or absent. |
| functional depth | 4.200 | Several direct mutations work and save, but modal-based creation is blocked and there is no coherent edit/delete lifecycle. Working handlers receive credit separately from visible forms and stubs. |
| interaction quality | 3.800 | Direct actions have useful feedback and some keyboard shortcuts, but input continuity, modal recovery, click navigation, and status accuracy are weak. |
| state persistence | 5.200 | Ordinary direct mutations persist through fresh sessions and reset works. Version checks and parse recovery exist, but nested validation, cross-view invariants, and storage-failure feedback are insufficient. |
| responsive adaptation | 4.300 | Source provides basic reflow and bounded overlays, but footer, composer, and dense calendar adaptation are incomplete. CSS/DOM emulation was exercised; actual browser overflow and touch usability remain unverified. |
| accessibility | 4.400 | Useful landmarks, names, shortcut handlers, live feedback, and reduced-motion support are present. Dialog naming/focus, step controls, tab semantics, and some keyboard reachability have material defects. |
| code architecture | 5.700 | The required single-file format is readable and self-contained, with named view/helper sections. Global state, single-project assumptions, and a long delegated handler make extension and correctness difficult. |
| reliability | 5.000 | Dependency-free startup and seeded navigation were stable in DOM execution, with good escaping of ordinary text. Defensive storage handling, route isolation, rich-HTML trust, and whole-view redraw behavior limit robustness. |

Evidence: [FE evaluator result](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T13-37-13Z--fe--meta-muse-spark-1-3--20261014t133713z-2b0620/evaluations/fe-muse-spark-13-meta-muse-spark-1-3-r1--18940919/judge-sol61-openai-gpt-6-1-sol--c9808065/output/result.json) and [saved attempt](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T13-37-13Z--fe--meta-muse-spark-1-3--20261014t133713z-2b0620/attempts/fe-muse-spark-13-meta-muse-spark-1-3-r1--18940919.json).

### BE findings

Complete canonical route registration and a rich repeatable sample graph support substantial process-persistent CRUD. Broad serializer/request/status incompatibilities, silent settings no-ops, missing parent lifecycle rules and confirmed authorization leaks keep the API well short of a contract-compliant production-shaped implementation. TCP startup/shutdown could not be live-verified because the evaluation sandbox prohibited socket binding; HTTP parser and handler behavior was exercised over local socket pairs.

| Dimension | Score | Findings |
| --- | ---: | --- |
| architecture domain | 5.800 | Shared recording builders and resource stores support a substantial domain graph, but tools, columns, steps and other roots bypass the abstraction; hierarchy and lifecycle rules remain shallow. |
| endpoint coverage | 10.000 | All 131 canonical path templates and 203 method/path operations are registered and reachable at the authentication boundary. This score covers registration, independently of payload/schema/state correctness. |
| behavioral depth | 4.800 | Core CRUD, comments, subscriptions, completion, publication and restore persist for the process lifetime, but parent/status invariants, card movement, counts and many canonical settings mutations are incomplete. |
| http contract | 3.500 | Basic JSON, error envelopes and pagination work, but request bodies, serializers, action statuses, URLs and query semantics materially diverge from OpenAPI/SDK expectations. |
| seed fidelity | 7.500 | The required sample graph is broad, repeatable and largely faithful in content/counts, but API navigation, some seed semantics and surfaced sample flags are incomplete. |
| request hardening | 2.500 | Bearer validation, several ownership gates and direct project access checks work, but private/client data leaks, unauthorized mutations, weak types/media/limits and 500s on bad input prevent consistent hardening. |
| operability | 6.000 | Portable stdlib packaging, health, configuration, reset and basic concurrency are useful. CORS, request diagnostics, empty-store usability and production polish have gaps; live listening/shutdown remains unverified due sandbox binding denial. |
| code quality | 6.300 | The single-file implementation is readable and dependency-light, with named handlers and reusable helpers, but manual store/route duplication and mixed internal/response models cause maintainability problems. |
| scope honesty | 3.500 | In-memory persistence and development credentials are disclosed, and many missing resources fail visibly. Broad contract/CORS claims and success responses for ignored settings or metadata-only file operations overstate implementation. |

Evidence: [BE evaluator result](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T09-16-08Z--be--meta-muse-spark-1-3--20261014t091608z-e7303f/evaluations/be-muse-spark-13-meta-muse-spark-1-3-r1--497922ce/judge-sol61-openai-gpt-6-1-sol--59ac9b75/output/result.json) and [saved attempt](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T09-16-08Z--be--meta-muse-spark-1-3--20261014t091608z-e7303f/attempts/be-muse-spark-13-meta-muse-spark-1-3-r1--497922ce.json).
## Muse Wonder 1.0 (High)

Mean FE/BE score: **5.225**.

| Track | Score | Native build time | CLI build time | Eval time | Build cost | Eval cost | Total cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| FE | 4.980 | 0:09:47 | 0:29:45 | 0:09:54 | $2.26 | $0.77 | $3.03 |
| BE | 5.470 | 0:15:28 | 0:29:32 | 0:21:23 | $3.39 | $1.29 | $4.68 |
| Combined | 5.225 | 0:25:15 | 0:59:18 | 0:31:17 | $5.65 | $2.06 | $7.71 |

### FE findings

Broad, dependency-free Basecamp prototype with a rich launch-project seed and many persistent local mutations. File/event creation timing bugs, fake lifecycle/draft success, shared project state, incomplete accessibility and weak malformed-state handling materially limit completeness. Full delta and all reference images were inspected; executable results use a non-rendering DOM harness because sandbox/browser policy blocked rendered-browser access. Visual, responsive and real-browser interaction assessments therefore have limited confidence. No overall score was computed.

| Dimension | Score | Findings |
| --- | ---: | --- |
| reference fidelity | 4.000 | Recognizable shell and all nine reference-backed surface templates, with material anatomy and density departures. Source-to-reference assessment only; rendered fidelity was blocked. |
| visual craft | 6.000 | Substantial token reuse and shared component styling establish a coherent foundation, but compact typography, emoji-heavy icons, and implementation commentary weaken finish. No rendered polish claim is made. |
| product model | 6.000 | Rich website-launch narrative and several correct Basecamp relationships, undermined by global tool state and shallow recording lifecycle semantics. |
| surface coverage | 6.500 | Broad set of primary and secondary templates with all must-match destinations represented, but several detail, administrative and lifecycle surfaces are placeholders or absent. |
| functional depth | 5.000 | Several substantive local workflows execute and persist, while file/event creation, drafts, lifecycle, subscriptions and some filtering stop short of coherent end-to-end behavior. |
| interaction quality | 4.500 | Useful immediate feedback and some efficient input handling, but misleading success, prompt-based editors, incomplete shortcuts and weak recovery materially reduce usability. |
| state persistence | 5.000 | Many core mutations and current route survive a storage round-trip, but consistency, recovery and same-version data validation are incomplete. |
| responsive adaptation | 4.000 | Explicit reflow rules and bounded overlays provide some adaptation, but several fixed layouts lack mobile treatment. Viewport usability was not rendered or touch-tested. |
| accessibility | 4.000 | Meaningful semantic groundwork is present, but naming, keyboard access, dialog focus, contrast and motion support fall below a strong accessible prototype. |
| code architecture | 5.500 | Reasonably navigable single-file organization with shared helpers and no dependencies, but global state and repeated imperative rendering mix domain, persistence and UI concerns. |
| reliability | 4.000 | Dependency-free startup and many normal paths work in execution, but deferred exceptions, unsanitized rich HTML and weak error boundaries prevent production-shaped reliability. Browser console/performance were not verified. |

Evidence: [FE evaluator result](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T09-22-05Z--fe--meta-muse-wonder-1-0--20261014t092205z-1e8a76/evaluations/fe-muse-wonder-meta-muse-wonder-1-0-r1--220abd9b/judge-sol61-openai-gpt-6-1-sol--0ec5425f/output/result.json) and [saved attempt](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T09-22-05Z--fe--meta-muse-wonder-1-0--20261014t092205z-1e8a76/attempts/fe-muse-wonder-meta-muse-wonder-1-0-r1--220abd9b.json).

### BE findings

A broad, substantially stateful standard-library prototype with a rich deterministic seed and nearly complete reachable registration. It falls short of the supplied contract on schemas, URL navigation, lifecycle and cross-resource invariants, authorization, and explicit handling of partial features. TCP start/shutdown remain unverified because this evaluator sandbox denies binding; the actual HTTP parser and handler were exercised over local socket pairs. No aggregate score was computed.

| Dimension | Score | Findings |
| --- | ---: | --- |
| architecture domain | 6.000 | A real shared recording store, parent/bucket graph and generic serialization/events provide a useful foundation. Lifecycle inheritance is only a flag, relationships are weakly constrained, and some recording-shaped resources bypass the common model. |
| endpoint coverage | 9.500 | All 203 canonical operations across 131 paths are registered, with one owner-reset extension. One canonical GET is shadowed by an earlier parameterized route; breadth does not imply complete implementation. |
| behavioral depth | 4.500 | Many CRUD and personal-state operations genuinely persist for the process lifetime. Major defects affect post-create lists, inherited lifecycle, drafts, completion, cross-bucket moves, audit events and mutation atomicity. |
| http contract | 4.000 | Core success statuses, JSON errors and Link pagination work. Required fields, scalar types, report shapes, navigable URLs and query semantics have material contract defects. |
| seed fidelity | 6.500 | The deterministic account, exact eight-person cast and most required counts/content relationships are substantial. Several linked seed features are absent, misrepresented or inaccessible through returned navigation. |
| request hardening | 3.000 | Bearer rejection, some permission checks, required fields and size limits work. Project/client isolation and mutation permissions are bypassable, deployment token override retains default credentials, and malformed/media validation is inconsistent. |
| operability | 6.000 | Portable standard-library packaging, health, configuration, correlated logs, rate limit, CORS and deterministic test reset are useful. Listener/startup/shutdown could not be verified under sandbox bind restrictions, and operational safeguards are incomplete. |
| code quality | 6.000 | The single-file implementation is organized and uses named helpers, type annotations and real reusable recording behavior. Global mutable coupling, ad hoc dictionaries/serializers, duplicated guards and shallow contract tests make extension fragile. |
| scope honesty | 3.000 | The module correctly discloses process-local memory and its tool-creation extension. Several incomplete features silently return success or fabricated data, contrary to the explicit-stub requirement. |

Evidence: [BE evaluator result](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T09-22-05Z--be--meta-muse-wonder-1-0--20261014t092205z-67f9c4/evaluations/be-muse-wonder-meta-muse-wonder-1-0-r1--79eed2dc/judge-sol61-openai-gpt-6-1-sol--21c501cc/output/result.json) and [saved attempt](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T09-22-05Z--be--meta-muse-wonder-1-0--20261014t092205z-67f9c4/attempts/be-muse-wonder-meta-muse-wonder-1-0-r1--79eed2dc.json).
## Muse Wonder Full 1.0 (High)

Mean FE/BE score: **5.998**.

| Track | Score | Native build time | CLI build time | Eval time | Build cost | Eval cost | Total cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| FE | 5.360 | 1:40:44 | 1:46:45 | 0:16:26 | Unknown | $1.44 | Unknown |
| BE | 6.635 | 0:51:36 | 0:57:10 | 0:18:06 | Unknown | $1.03 | Unknown |
| Combined | 5.998 | 2:32:20 | 2:43:55 | 0:34:32 | Unknown | $2.47 | Unknown |

### FE findings

A broad, token-driven Basecamp prototype with many verified local CRUD and collaboration workflows. Basic reload persistence works, but project isolation, archive/restore semantics, subscriptions, rich editing, calendar dates, and input safety have material defects. Browser rendering was blocked by environment policy; visual and responsive scores are explicitly source-based estimates. No overall score was computed.

| Dimension | Score | Findings |
| --- | ---: | --- |
| reference fidelity | 5.500 | Recognizable anatomy for all nine references and matching core tokens, with material structural departures. Source-based assessment: browser rendering was blocked by the environment. |
| visual craft | 6.000 | A coherent token-driven visual system with shared controls and surfaces; emoji approximations and partial theme handling limit refinement. Rendered visual polish was not verifiable. |
| product model | 6.500 | Rich, largely accurate sample narrative and tool terminology, weakened by personal-view semantics, notification seeding, and incomplete recording/lifecycle relationships. |
| surface coverage | 7.500 | All screenshot-backed surfaces and numerous secondary destinations are reachable; optional administrative, aggregate, and lifecycle screens remain shallow or absent. |
| functional depth | 6.500 | Substantial CRUD and collaboration behavior works in the prototype, with important end-to-end gaps in rich editing, restoration, subscription handling, and secondary controls. |
| interaction quality | 5.000 | Core actions have usable feedback and shortcuts, but filtering, recovery, focus, and misleading controls interrupt ordinary workflows. |
| state persistence | 4.000 | Broad basic localStorage continuity is verified, but project isolation, subscription state, lifecycle transitions, and malformed-state recovery have serious defects. |
| responsive adaptation | 3.500 | Content has explicit reflow rules, but the persistent navigation and several dense controls have no mobile adaptation. Score is source-based because actual viewport rendering was unavailable. |
| accessibility | 4.500 | Useful native elements, landmarks, shortcuts, and live toasts are present, but skip navigation, accessible names, modal focus management, and custom keyboard behavior are materially incomplete. |
| code architecture | 5.000 | Readable named view/helper boundaries and useful shared primitives, but dense global state, mixed concerns, hard-coded project assumptions, and ad hoc binding undermine extensibility. |
| reliability | 3.000 | Dependency-free startup and ordinary navigation are stable in DOM tests, but two verified script-execution paths, weak stored-state validation, and a date bug prevent a strong safety/stability score. |

Evidence: [FE evaluator result](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T15-15-09Z--fe--meta-muse-wonder-full-1-0--20261014t151509z-017835/evaluations/fe-muse-wonder-full-meta-muse-wonder-full-1-0-r1--aecc1126/judge-sol61-openai-gpt-6-1-sol--041a67fd/output/result.json) and [saved attempt](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T15-15-09Z--fe--meta-muse-wonder-full-1-0--20261014t151509z-017835/attempts/fe-muse-wonder-full-meta-muse-wonder-full-1-0-r1--aecc1126.json).

### BE findings

A broad in-memory Recording-based implementation reaches all 203 canonical operations and provides a rich deterministic seed with substantial working workflows. Reproduced client-hidden webhook disclosure, malformed-input 500s, partial mutations after rejection, lifecycle/descendant inconsistencies, wrong response envelopes and filter-losing pagination materially limit fidelity and production readiness. Socket binding was environment-blocked; actual unmodified HTTP parsing/handling was exercised through byte streams. Major integration and durability exclusions are disclosed, but completeness claims exceed verified behavior.

| Dimension | Score | Findings |
| --- | ---: | --- |
| architecture domain | 8.000 | A coherent shared Recording core models tool roots, parent trees, buckets, events, subscriptions, readings and lifecycle inheritance. Dictionary-based type-specific extras and incomplete descendant/group invariants limit extensibility. |
| endpoint coverage | 10.000 | All 131 canonical path templates and 203 method/path operations are registered and reachable through the unmodified HTTP handler. Coverage credit is for routing, independent of schema or semantic correctness; TCP binding was environment-blocked. |
| behavioral depth | 6.000 | Broad CRUD and cross-cutting workflows retain real changes within the process, including cards, check-ins, notifications and templates. Failed-request atomicity, draft isolation, descendant migration, group rollups and project lifecycle have substantial gaps. Restart durability is explicitly absent. |
| http contract | 5.000 | Core recording envelopes, JSON errors, status codes, primary URLs, ETags and basic list headers work. Several wrong response envelopes, invalid optional nulls, an incorrect update status and filter-losing pagination materially break OpenAPI/SDK compatibility. |
| seed fidelity | 8.500 | The required account, eight named sample people, project, six tools and rich linked content are deterministic and largely navigable. Smaller cast, timestamp and presentation-field deviations prevent full fidelity. |
| request hardening | 3.500 | Bearer authentication, ordinary access rules, some required-field/type/date checks, body caps and optional rate limits work. Reproduced hidden-content disclosure and malformed-input 500s, along with permissive media and field coercion, prevent consistent fail-closed behavior. |
| operability | 6.000 | Dependency-free packaging, operational endpoints, configurable credentials, reset, structured logging and request IDs are useful. Wildcard-only CORS, incomplete public OpenAPI and service-wide blocking body reads limit production readiness. Socket startup and signal shutdown remain unverified due environment restrictions. |
| code quality | 7.000 | The required single-file implementation is well sectioned and uses shared helpers rather than 203 unrelated stubs. Dynamic dictionaries, a long serializer, dispersed permission checks and mutation-before-validation reduce maintainability and make invariants easy to bypass. |
| scope honesty | 5.500 | The documentation clearly discloses in-memory storage and major out-of-scope integrations. It nevertheless calls the API complete and all operations stateful despite material contract defects and several successful responses that represent incomplete or fabricated behavior. |

Evidence: [BE evaluator result](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T09-52-00Z--be--meta-muse-wonder-full-1-0--20261014t095200z-969630/evaluations/be-muse-wonder-full-meta-muse-wonder-full-1-0-r1--2ca639c4/judge-sol61-openai-gpt-6-1-sol--4a5277e6/output/result.json) and [saved attempt](/Users/stephenwalker/Code/projects/basecamp-bench/runs/2026-10-14T09-52-00Z--be--meta-muse-wonder-full-1-0--20261014t095200z-969630/attempts/be-muse-wonder-full-meta-muse-wonder-full-1-0-r1--2ca639c4.json).
