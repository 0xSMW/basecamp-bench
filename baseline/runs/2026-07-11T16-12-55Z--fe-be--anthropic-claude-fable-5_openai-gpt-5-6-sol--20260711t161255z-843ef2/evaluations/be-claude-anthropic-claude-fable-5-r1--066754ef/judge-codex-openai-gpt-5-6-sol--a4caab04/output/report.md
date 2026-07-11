# Backend evaluation report

Submission: `id-066754ef`  
Track: `be`  
Contract: `21192e19c6a56d2cc2c2e3c746f210d67f800754afd70ad932c38bebb2730e71`

## Evaluation method

- Compared the complete seed and submission trees. The entire delta is one added file: `submission/server.js` (5,769 lines). No seed file was changed or removed.
- Ran `node --check submission/server.js`; it passed under Node 22.23.1. The implementation declares Node >=18 and uses only built-in modules.
- Parsed `seed/reference/basecamp-sdk/openapi.json` and compared every canonical method/path/operation ID against the route declarations. The reference contains 131 path templates and 203 operations; the submission contains 203 unique canonical registrations with no missing, extra, duplicate, or operation-ID-mismatched entries.
- Attempted to start an isolated copy on a loopback port. The evaluation sandbox denied `listen(2)` with `EPERM`, so network binding was not used as negative product evidence. The same HTTP request pipeline and handlers were invoked directly from an in-memory-instrumented copy, with deterministic seed epoch/token secret and no writes to the evidence directories.
- Exercised authentication, account/project/people reads, 31 resource/report families, nested create/read/update operations, completion, inherited archive/unarchive, mutation rejection on archived content, subscriptions, comments, boosts, card moves, attachments/uploads, access isolation, reset authorization, ETag/HEAD, CORS preflight, 405/Allow, malformed JSON, media types, invalid IDs/pages/assignees, oversized bodies, rate limiting, invalid dates, and webhook validation.
- Schema-checked 80 seeded GET operations against their exact OpenAPI 200-response schemas. Sixty-nine passed. An entity-wide seeded serialization check found 38 of 107 entities invalid, almost entirely because optional string fields were emitted as `null` where the OpenAPI permits only strings.

## Scores

### Architecture and domain modeling — 8.4/10

The domain is coherent and substantially shared. `submission/server.js:439-469` defines one store for people, projects, recordings, parent order, events, subscriptions, readings, attachments, tools, and integrations. `submission/server.js:566-661` supplies a common Recording shape, ordered parent tree, reparenting, traversal, effective lifecycle inheritance, and tool-root lookup. Projects and their dock/tool hierarchy are centralized at `submission/server.js:666-740`; visibility and personal-voice mutation rules are centralized at `submission/server.js:746-785`; events, notifications, and webhooks share cross-cutting primitives at `submission/server.js:828-951`. Runtime tests confirmed parent archive status propagated to a child todo, rejected child mutation while archived, and reversed on unarchive; a card move also persisted as a parent change.

Deductions: extending the model requires coordinated edits across type sets, URL/title switches, serializers, and handlers (`submission/server.js:513-558`, `submission/server.js:992-1497`). `createRec` itself does not enforce same-bucket or acyclic-parent invariants, leaving each handler to protect them. Recurrence is absent, todo subtasks are represented as description text, and a new project internally creates eight disabled tools plus three Kanban edge lanes despite the design's “no default tools” wording (`submission/server.js:53-68`, `submission/server.js:679-727`; `seed/INIT.md:341-343`).

### Endpoint surface coverage — 10/10

All 203 operations across all 131 OpenAPI paths are registered exactly once with the canonical methods, templates, and operation IDs. The router uses exact segment matching plus strict numeric/date path parsing and distinguishes 404 from 405 (`submission/server.js:1785-1836`). The registered surface is composed of explicit handlers from `submission/server.js:1930-4887`; no TODO/FIXME placeholder handler was found. Direct execution reached representative reads across every major tool plus recordings, categories, templates, notifications, search, assignments, progress, schedules, gauges, timesheets, webhooks, and client/inbox empty collections.

Coverage credit here reflects registration and reachability only. Depth limitations are scored below.

### Behavioral depth and statefulness — 8.6/10

The in-memory service performs real process-lifetime mutations. Direct tests verified project creation and subsequent retrieval, project access isolation, nested todolist/todo creation with correct parent and bucket, todo update, completion/uncompletion, inherited lifecycle, message/comment/boost creation, subscription state, card movement between columns, attachment creation followed by upload materialization and retrieval, and owner-only deterministic reset. Thirty-five of 37 broad state/behavior assertions passed; one failed assertion expected a non-contract `completed_at` field on Todo, and one expected child comment activity in the parent's event list, so neither was treated as a confirmed contract failure.

Cross-resource invariants are often explicit: assignees must exist and see the project (`submission/server.js:1916-1927`); todo moves stay in the same bucket (`submission/server.js:3119-3135`); card moves stay within one board (`submission/server.js:3389-3421`); inherited lifecycle and inactive projects block mutation (`submission/server.js:615-625`, `submission/server.js:780-786`, `submission/server.js:1903-1907`).

Deductions: all persistence is process-lifetime. Recurrence is explicitly unmodeled; upload “versions” always return only the current upload (`submission/server.js:3714-3720`); inbound forwards and client approval/correspondence records have no seed or creator, leaving several registered detail/reply paths unreachable through public state (`submission/server.js:4207-4309`). Reset rebuilds the domain but does not clear the module-global rate limiter. The shared creator also trusts caller-supplied type/bucket/parent combinations, so handler omissions could violate invariants.

### HTTP and schema contract fidelity — 8.2/10

Strong portions include 200/201/204 helpers, page validation, configurable pagination, `X-Total-Count`, next `Link`, canonical API/app URL generation, consistent error envelopes, `Retry-After`, 405 `Allow`, `X-Request-Id`, content length/type, weak ETags, and `If-None-Match` 304 support (`submission/server.js:983-1099`, `submission/server.js:1838-1862`, `submission/server.js:4894-4929`). Runtime checks confirmed ETag-to-304 behavior, bodyless HEAD responses, 204 preflight, 405 with `Allow: GET, POST`, standard 400/401/403/404/413/422/429 envelopes, and correct CRUD success statuses.

The material defect is nullability. Of 80 schema-checked seeded GET operations, 11 failed because serializers emit JSON `null` for fields declared only as strings. Examples include account logo URL (`submission/server.js:1646-1666`), out-of-office dates (`submission/server.js:1720-1732`), Todo dates (`submission/server.js:1243-1255`), Card/CardStep due dates (`submission/server.js:1314-1337`), column color (`submission/server.js:1464-1492`), Campfire topic (`submission/server.js:1286-1293`), and upcoming assignment dates (`submission/server.js:1741-1767`). The broader required fields and types passed. Missing JSON `Content-Type` is accepted and a wrong media type returns 400 instead of 415; those are also hardening defects.

### Seed data fidelity — 8.8/10

The seed creates the owner plus the exact eight named sample people with roles and stable sequential IDs (`submission/server.js:5209-5222`), the “Launch the new website” project and complete eight-tool dock (`submission/server.js:5223-5239`), five rich message-board threads with categories, mentions, comments, subscribers, and boosts (`submission/server.js:5264-5368`), two todo lists and specified tasks (`submission/server.js:5370-5447`), the detailed Kanban lanes/cards/steps/on-hold state (`submission/server.js:5449-5552`), document/image/cloud-link content (`submission/server.js:5554-5613`), two schedule entries (`submission/server.js:5615-5651`), and a 16-line five-person chat with boosts (`submission/server.js:5653-5677`). Runtime reads confirmed one named project, nine people, eight enabled tools, five categories, ten todos, populated messages/docs/uploads/chat/schedule/card table, and navigable IDs/URLs.

Deductions: all sample people receive valid bearer tokens although `seed/INIT.md:161-163` says they have no login. The todo subtask is downgraded to description text; all five Done cards complete on T-1 rather than spanning T-1 through T; and CloudFile is represented as Upload. Relative content and IDs are deterministic, while exact timestamps require `BASECAMP_SEED_EPOCH`; the default epoch is boot time.

### Validation, authorization, and request hardening — 7.3/10

Positive evidence includes strict Bearer parsing with digest-only token lookup (`submission/server.js:4924-4941`), centralized project/recording visibility and employee/admin guards, strict path IDs, 1 MiB JSON and configurable upload caps, bounded typed validation helpers, 4 KiB URL cap, safe opaque 500 responses, signed attachment IDs, 429 plus `Retry-After`, sanitized filenames, and literal private/link-local webhook rejection (`submission/server.js:213-240`, `submission/server.js:283-427`, `submission/server.js:746-785`, `submission/server.js:954-976`, `submission/server.js:4767-4810`). Runtime tests confirmed missing/bad tokens returned 401, cross-project access returned 404, malformed JSON/wrong media type/invalid page returned 400, missing fields and unknown assignees returned 422, an oversized body returned 413, and non-owner reset returned 403.

Deductions are substantive. Non-empty JSON without `Content-Type` succeeded. `2025-02-31` was accepted and persisted because date validation trusts normalized `Date.parse` output (`submission/server.js:178-187`). CreateWebhook coerces a string `active: "false"` to `true` instead of rejecting it (`submission/server.js:4827-4862`). Webhook SSRF checks are lexical and do not resolve or pin DNS. CORS defaults to `*` with Authorization allowed, and every OPTIONS request succeeds before route/auth checks. In production, raw seed tokens still print by default unless explicitly disabled (`submission/server.js:100-125`, `submission/server.js:5720-5727`). Attachment accumulation is unscoped, and oversized-request handling pauses without draining/destroying the stream.

### Operability, testability, and packaging — 7.4/10

The server is a zero-install Node executable with documented host/port and substantial environment configuration (`submission/server.js:1-68`, `submission/server.js:83-137`). It has public health and service-card endpoints, request IDs, leveled structured request logging, request/keepalive timeouts, SIGINT/SIGTERM graceful close with forced shutdown, deterministic epoch support, and an owner-only reset disabled by default in production (`submission/server.js:143-156`, `submission/server.js:4955-4970`, `submission/server.js:5022-5036`, `submission/server.js:5731-5764`). Syntax, seed, HTTP pipeline, and shutdown code loaded successfully; loopback binding was blocked by the evaluator sandbox rather than an application error.

Deductions: no package/runtime manifest, automated tests, conformance harness, or test command was added. `main()` returns no server, and the request handler/close seam is not exported (`submission/server.js:5731-5769`), so isolated integration testing requires a child process or in-memory instrumentation. Reset does not clear rate buckets; direct testing showed requests remained 429-limited after repeated resets. Production defaults still expose all seed tokens to stderr. CORS defaults are broad, readiness is only liveness, integer env parsing accepts suffix garbage via `parseInt`, and decoded newline characters can enter the non-JSON log line.

### Code quality and maintainability — 7.9/10

The file is readable for its size: sections are clear, names are consistent, comments explain behavior and limitations, validators/response helpers/router/domain lookups are reused, and serializers centralize common envelopes (`submission/server.js:83-427`, `submission/server.js:980-1497`, `submission/server.js:1785-1923`). The implementation passes syntax validation and achieves broad behavior with no external dependencies.

The main deduction is structural: 5,769 lines, mutable module-global state, 158 top-level functions, seed construction, routing, domain logic, serializers, webhooks, HTTP transport, and lifecycle all live in one file. Only `CONFIG`, `main`, and `resetAndSeed` are exported, and there are no tests. Resource-type extensibility requires edits across multiple switches and sets rather than a cohesive registry. This is understandable and disciplined, but expensive to navigate, isolate, and safely extend.

### Scope honesty — 7.9/10

The header explicitly discloses process-lifetime memory, absent schedule recurrence, lack of inbound-forward creation, empty client-only resources, CloudFile adaptation, and missing todo-step endpoints (`submission/server.js:53-68`). Unsupported comment types and invalid lifecycle targets fail closed, and no TODO/FIXME success stubs were found. The implementation does not conceal that upload versioning is modeled as a single current version (`submission/server.js:3714-3720`).

Deductions: the top-level statement that all 203 operations are “implemented” overstates operations whose state is publicly unreachable. Forward reply creation and client-recording detail/reply routes cannot be meaningfully exercised because no forward/approval/correspondence seed or creator exists, while the header calls them fully functional. New-project “empty” tool behavior also means disabled resources exist and are serialized. These are disclosed better than typical stubs, yet the capability language remains somewhat optimistic.

## Bottom line

This is a broad, executable, stateful implementation with exact canonical route coverage and a strong shared Recording model. Its strongest evidence is the complete 203-operation surface plus verified CRUD/lifecycle/cross-resource persistence. Its largest measurable contract defect is serializer nullability across common resources. Production shaping is credible but incomplete because tests and clean lifecycle seams are absent, reset isolation is incomplete, validation has edge-case gaps, CORS is permissive, and production token printing is opt-out.
