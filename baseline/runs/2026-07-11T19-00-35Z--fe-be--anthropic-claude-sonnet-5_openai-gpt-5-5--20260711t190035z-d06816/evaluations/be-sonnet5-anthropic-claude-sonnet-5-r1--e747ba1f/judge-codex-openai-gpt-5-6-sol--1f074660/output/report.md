# Backend evaluation report

Submission: `id-e747ba1f`  
Track: `be`  
Contract: `21192e19c6a56d2cc2c2e3c746f210d67f800754afd70ad932c38bebb2730e71`

## Evaluation method

The complete seed-to-submission delta is one added file: `submission/server.js` (4,325 lines); all reference and instruction files are unchanged. I inspected the full route/domain/seed implementation, parsed the canonical OpenAPI document, ran `node --check submission/server.js`, and exercised the actual request handler with streamed request/response objects.

A direct loopback launch reached seed initialization but the evaluation sandbox denied socket binding with `listen EPERM`. To separate that environmental restriction from implementation behavior, the live tests replaced only `http.createServer` with an in-process listener capture, then invoked the server's real request handler, routing, authentication, body parsing, state, serialization, and error handling. Tests covered authentication, tenancy, malformed bodies, identifiers, methods, media types, rate limits, body limits, CORS, reset, shutdown, CRUD persistence, comments, todo completion, lifecycle transitions, client visibility, and authorization.

No weighted overall score is reported; aggregation belongs to the runner.

## Dimension scores

| Dimension | Score | Assessment |
|---|---:|---|
| Architecture and domain modeling | 8.0 | Strong shared recording/tool primitives and relationships, with weak cascade/invariant enforcement and monolithic storage/domain/HTTP coupling. |
| Endpoint surface coverage | 10.0 | All 203 OpenAPI operations are registered and reachable at their specified runtime method/path; one attachment-download route is extra. |
| Behavioral depth and statefulness | 7.5 | Broad, real in-process CRUD and lifecycle behavior works, but parent/bucket invariants and several partial subsystems are incomplete. |
| HTTP and schema contract fidelity | 6.8 | Success statuses and core shapes are broadly right; pagination, query semantics, nullable values, and navigational URLs have material defects. |
| Seed data fidelity | 7.5 | Rich deterministic sample account/project with all six required tools, but several exact content relationships differ from the seed contract. |
| Validation, authorization, and request hardening | 4.5 | Good baseline parsing/auth/limits are undermined by a critical project-authorization flaw and default unauthenticated credential/reset exposure. |
| Operability, testability, and packaging | 7.5 | Dependency-free, configurable, observable, health-checked, resettable, and gracefully stoppable; memory-only state and unsafe debug defaults reduce production readiness. |
| Code quality and maintainability | 6.5 | Readable helpers and naming, but 4,325 lines in one mutable module create mixed concerns, repeated scans, and fragile invariants. |
| Scope honesty | 5.0 | Process-lifetime storage and some unsupported behavior are disclosed, while the production/full-contract claims overstate successful empty or partial features. |

## Evidence by dimension

### Architecture and domain modeling — 8.0

- `submission/server.js:224-293` defines one in-memory database and a shared `createRecording` envelope covering identity, type, lifecycle, visibility, creator, bucket, parent, and timestamps.
- `submission/server.js:363-532` centralizes type paths, tool slugs, parent/bucket projections, and recording capabilities such as comments, subscriptions, and boosts.
- `submission/server.js:575-625` centralizes project membership, client visibility, recording access, and creator/admin rules; `submission/server.js:1805-1823` supplies reusable dock/tool factories.
- Lifecycle transitions at `submission/server.js:1049-1077` update only the selected recording. Trashing a project left its child message board and five messages reachable in live tests, so `inherits_status` is not enforced as a hierarchy invariant.
- Card moves at `submission/server.js:2535-2549` accept any existing column without verifying that the card and column share a bucket/table, allowing inconsistent cross-project parentage.
- The entire storage, HTTP, domain, projections, seed, and bootstrap implementation is coupled in one file, and many relationships are resolved by repeated full-map scans.

### Endpoint surface coverage — 10.0

- `submission/reference/basecamp-sdk/openapi.json` declares 131 paths and 203 GET/POST/PUT/DELETE operations.
- Static route extraction found 204 unique `route()` registrations in `submission/server.js`. After normalizing parameters, every declared operation has the correct method and reachable concrete path.
- The apparent template difference for `GET /reports/users/progress/{personId}.json` is intentional: `submission/server.js:3490-3493` captures the entire final segment and strips `.json`.
- `GET /attachments/:sgid` at `submission/server.js:1222-1227` is the sole extra operation and supports upload/download behavior.

### Behavioral depth and statefulness — 7.5

- Live project flow: create returned 201; direct GET and listing retained it; PUT retained the renamed value; DELETE returned 204; subsequent GET returned 404 and the active list excluded it.
- Live message flow: create returned 201; GET retained content; comment creation increased `comments_count`; archive and reactivate returned 204 and persisted status. A non-owner/non-admin author edit returned 403.
- Live todo flow: create returned 201; complete returned 204 and GET showed `completed: true`; uncomplete returned 204. Reset removed created state and invalidated pre-reset tokens.
- `submission/server.js:905-1185`, `:1445-1774`, and `:1829-3808` implement genuine mutations across recordings, projects, tools, messages, todos, cards, vaults, chat, schedules, questions, inbox/client resources, reports, timesheets, templates, webhooks, and search.
- State lasts only for the process lifetime (`submission/server.js:224-254`), as permitted by the task baseline, and is not durable across restart.
- Project trash does not cascade or hide descendants. Card moves do not enforce same-bucket/table targets. Generic status actions also lack hierarchy propagation.
- `/my/readings` and question reminders are successful empty surfaces (`submission/server.js:1391-1404`); webhook deliveries are never produced; template construction immediately reports completion while creating an empty project (`submission/server.js:3658-3712`).

### HTTP and schema contract fidelity — 6.8

- Automated comparison found that all 203 declared operations have an implemented literal success status matching an OpenAPI 2xx response. Live tests confirmed representative 200/201/204 responses, JSON error envelopes, `X-Request-Id`, `X-Total-Count` where pagination is used, and empty 204 bodies.
- Only 26 of the 44 operations marked `x-basecamp-pagination` call `paginate`; 18 omit required page slicing/count/link behavior. Examples include message types (`submission/server.js:1931-1934`), campfires (`:2844-2847`), project timeline (`:1538-1550`), upload versions (`:2794-2801`), webhooks (`:3717-3723`), progress reports (`:3399-3420`, `:3490-3505`), and search (`:3811-3824`).
- Declared query controls are ignored in material places: message sort/direction (`submission/server.js:1866-1873`), assignment scope (`:1351-1355`), recording sort/direction (`:911-927`), and search sort/pagination (`:3811-3824`).
- Runtime schema checks found systematic `null` values where the OpenAPI schemas require strings: todo dates (`submission/server.js:2025-2035`), card colors/dates (`:2286-2348`), upload dimensions (`:2633-2642`), and account logo URL (`:452-477`).
- `jsonUrlFor` appends `.json` to individual-resource URLs (`submission/server.js:406-410`), and projects do the same at `:1410-1423`, while the canonical and registered individual routes omit that suffix. Following a returned project URL produced 400; the canonical route returned the resource. Bookmark URLs are also emitted without corresponding routes.

### Seed data fidelity — 7.5

- Runtime inspection found one `Sample Co.` account, 10 people (owner, client, and the exact eight-person sample cast), one all-access `Launch the new website` project, six required dock tools, and 85 recordings.
- The seed contains five messages, two todo lists/10 todos, 13 cards, one document/two uploads, two schedule entries, 16 chat lines, boosts, comments, subscriptions, categories, assignees, client-visible and internal content. See `submission/server.js:3834-4270`.
- Exact mismatches with `submission/INIT.md:152-197`: “Set up analytics” describes a subtask but has no child; the second triage card has one step rather than two; the Writing on-hold lane is created empty while both cards remain in Writing; no Docs & Files item has the required row color; and the 16 chat lines use eight people rather than five.
- Only five events are seeded across 85 recordings, limiting initial activity navigation. Reset replaces the database but does not reset the global ID counter at `submission/server.js:214-217`, so identifiers drift on each reset.
- Generated `url` fields frequently use the unusable `.json` suffix described above, weakening hyperlink navigability even though collection-to-ID traversal works.

### Validation, authorization, and request hardening — 4.5

- Verified good behavior: missing/bogus bearer tokens returned 401; wrong account returned 404; malformed JSON and nonnumeric IDs returned 400; missing required fields returned 422; wrong methods returned 405; hidden client content returned 404; oversized bodies returned 400; configured rate limiting returned 429 with `Retry-After`.
- Input helpers at `submission/server.js:807-856`, hashed bearer lookup at `:552-572`, the 25 MiB cap at `:47-48`/`:152-168`, and rate limiting at `:675-700` are real and fail safely in the tested cases.
- Critical verified authorization defect: a client token could `DELETE /1/projects/1011` and received 204, trashing the whole sample project. Project PUT/DELETE require project access but no employee/admin capability at `submission/server.js:1481-1499`; a client could also rename the sample project.
- `ALLOW_RESET` defaults to enabled (`submission/server.js:39`) and exposes unauthenticated `GET /_seed/tokens` and `POST /_reset` at `:744-755`. The former returns every raw bearer credential, making normal API authentication bypassable in the default configuration. Startup also logs all raw seed tokens at info level (`:4284-4298`).
- JSON mutation requests with `Content-Type: text/plain` were accepted and created a project (201); unsupported media types are not rejected.

### Operability, testability, and packaging — 7.5

- `submission/server.js` has no external dependencies, passes Node syntax checking, is executable, and documents its startup/environment variables at `:5-20`.
- Environment configuration covers host, port, public URL, account identity, log level, rate limit, CORS, and reset. Health endpoints, structured JSON logs, UUID request IDs, configurable CORS, stale-rate-bucket cleanup, client-error handling, and SIGINT/SIGTERM shutdown are implemented at `submission/server.js:25-69`, `:653-809`, and `:4278-4323`.
- Live handler tests verified health/no-store, request IDs, CORS preflight, rate limits, reset isolation, the hardened `ALLOW_RESET=false` mode, and graceful shutdown callbacks.
- State is process-memory only, there is no packaged test suite or conformance runner, and reset is nondeterministic in IDs. Default credential/reset exposure and raw-token logging are unsafe operational defaults.
- The sandbox's loopback `EPERM` prevented a real socket exchange; seed/bootstrap and the real handler were still executed in process. No source evidence indicates a server-listen defect.

### Code quality and maintainability — 6.5

- Clear sectioning, names, validation/auth helpers, projection helpers, route registration, shared recording primitives, and tool factories make the large surface understandable. `node --check` passed.
- The single 4,325-line file mixes transport, authorization, mutable storage, domain operations, serializers, seed fixtures, and process lifecycle. This raises the cost of extension and testing.
- Plain mutable maps/objects and repeated full-recording scans make invariants implicit. Similar handlers duplicate lookup/access/mutation logic, and several comments have drifted from behavior (notably the supposedly moved on-hold card at `submission/server.js:4129-4135`).
- Contract defects are systematic rather than localized: pagination is applied inconsistently, URL generation disagrees with registered paths, and optional values are serialized without a schema-aware omission/null policy.

### Scope honesty — 5.0

- Honest disclosures include the process-lifetime store (`submission/server.js:9-10`), reset/debug warning (`:4292-4295`), and explicit rejection of unsupported dock tool types (`:1819-1822`).
- The header calls the implementation a “single-file production server” and the root response describes the OpenAPI file as the “full contract” (`submission/server.js:5`, `:736-742`), despite unsafe default debug access, incomplete pagination/query behavior, and broken emitted resource URLs.
- A `notImplemented` error exists at `submission/server.js:114` but is never used. Partial areas instead return successful empty or fabricated-complete responses: question reminders, readings, webhook deliveries, and immediate template construction.
- Missing behavior therefore remains visible in source but is not consistently fail-closed to API consumers.

## Key risk summary

The submission is unusually broad and substantively stateful for a single-file backend, with complete route coverage and a strong shared recording model. The dominant blockers to production-shaped behavior are default unauthenticated credential/reset access, insufficient authorization on project mutation, non-cascading hierarchy lifecycle, incomplete pagination/query fidelity, and generated URLs that do not resolve to the registered individual-resource paths.
