# Backend evaluation report

Submission: `id-4d93e09d`  
Track: `be`  
Contract: `2026-07-11.2`

## Evaluation method

The complete seed-to-submission delta is one added file, `submission/server.js` (843 lines). `INIT.md`, `DESIGN.md`, `AGENTS.md`, the screenshots, and the SDK reference pack are unchanged. I inspected the full server, enumerated its routes against `submission/reference/basecamp-sdk/openapi.json`, checked the deterministic seed against `submission/INIT.md`, ran `node --check`, and exercised the exported HTTP request handler with real Node request streams and response serialization.

The managed sandbox denied local socket binding with `listen EPERM`. This is an environment restriction, so runtime API testing used the exported `serve()` handler in-process. That path exercised authentication, route matching, query/body validation, headers, serialization, errors, CRUD mutations, lifecycle operations, idempotency, ETags, CORS, pagination, and reset. No seed or submission files were changed.

## Scores

| Dimension | Score | Assessment |
|---|---:|---|
| Architecture and domain modeling | 7.0 | Strong shared Recording primitive and broad graph, weakened by missing resource-type, parent, and lifecycle invariants. |
| Endpoint surface coverage | 10.0 | All 131 paths and 203 method/path operations are compiled from the canonical OpenAPI and reachable; 71 deliberately terminate at 501. |
| Behavioral depth and statefulness | 6.0 | Substantial stateful CRUD and cross-cutting behavior works, but 71 operations are stubs and several core invariants fail. |
| HTTP and schema contract fidelity | 6.0 | Good baseline statuses, headers, errors, pagination, and schema use, with material response-shape and operation-semantic mismatches. |
| Seed data fidelity | 8.0 | Rich, deterministic, navigable sample graph closely matches the required inventory, with several specific content and event gaps. |
| Validation, authorization, and request hardening | 6.5 | Solid bearer auth, limits, media handling, rate limiting, and safe errors; role auth and identifier/relationship validation are materially incomplete. |
| Operability, testability, and packaging | 8.5 | Strong dependency-free runtime, health/config/logging/CORS/shutdown/reset/persistence design; no authored tests, package metadata, or run guide. |
| Code quality and maintainability | 7.0 | Clear helpers and centralized behavior, but a dense single mutable module and overly generic routing make extension unsafe. |
| Scope honesty | 8.0 | Unsupported operations usually fail explicitly, while a handful of shallow handlers return misleading successful results. |

No overall score is computed here.

## Evidence by dimension

### Architecture and domain modeling — 7.0

- `submission/server.js:111-139` and `submission/server.js:325-339` centralize the shared Recording envelope: identity, status, visibility, type, parent, bucket, creator, URLs, comments, boosts, and position. Events, pagination, lifecycle, and common updates are likewise centralized at `submission/server.js:324-369`.
- The deterministic graph contains 85 recordings across 20 concrete types, nested through a project, six tool roots, content, comments, card steps, and events.
- The key design flaw is `routedRecording()` at `submission/server.js:315-320`: it selects a leaf identifier but never verifies the operation's expected resource type or enclosing parent. A direct probe of `GET /999999/todos/2001` returned `200` with a `Message`; `POST /999999/todolists/2001/todos.json` created a Todo whose parent was that Message.
- `inherits_status: true` is serialized but not implemented as a hierarchy rule. Archiving Message 2001 left child Comment 3000 active and readable. Project trash also did not cascade or freeze the project.

### Endpoint surface coverage — 10.0

- `submission/server.js:69-83` dynamically compiles the canonical OpenAPI paths and asserts exactly 203 operations. Independent enumeration found 131 paths: 100 GET, 41 POST, 42 PUT, and 20 DELETE operations.
- All 203 concrete method/path templates matched the router. `submission/server.js:640-651` distinguishes missing paths from wrong methods and supplies `Allow` for 405 responses.
- There are 132 implemented handler keys and 71 registered operations without handlers. Per this dimension's definition, those operations remain reachable and return explicit `501 not_implemented` through `submission/server.js:802-805`; their lack of depth is scored under behavioral depth and scope honesty.

### Behavioral depth and statefulness — 6.0

- Verified working, stateful flows include project create/read/update/trash; Todo create/read/update/complete/uncomplete/trash; messages and drafts; comments with count increments; boosts; subscriptions; card movement checks; schedule validation; search; events; and reset. A created Todo remained readable after a later completion request, with `completed: true`; its event feed recorded creation, update, and completion.
- Idempotency is genuinely stateful: replaying the same project creation key returned the same ID with `Idempotency-Replayed: true`; changing the payload under that key returned 409.
- 71 of 203 operations are explicit stubs, including questionnaires/answers, forwards, templates, most timesheets, webhooks, gauges/hill charts, chatbots, client features, and account-logo/attachment work.
- Direct invariant failures materially limit depth: a Message is accepted as a Todolist parent; typed getters return unrelated recordings; a trashed Todo remains in the normal list because `ListTodos` ignores status; project trash still permits project update and child reads; recording archive does not cascade despite `inherits_status`; deleting a boost does not decrement the target count.
- Optional file persistence is production-shaped in source (`submission/server.js:264-279`) and in-process request state was verified. On-disk restart persistence was not exercised because the evaluation was constrained to the two output artifacts.

### HTTP and schema contract fidelity — 6.0

- Verified positives: structured JSON errors; correct 401/400/409/413/415/422/429/501 handling; 204 bodies are empty; `Location` on project creation; `X-Request-ID`; rate-limit headers; ETags/304; `X-Total-Count`; and next/previous RFC-style `Link` pagination (`submission/server.js:340-352`, `submission/server.js:703-796`). A two-person page returned total 9 and a valid next link.
- The central validator uses OpenAPI required/type/maxLength/pattern/enum information (`submission/server.js:653-701`), but omits formats, numeric bounds, array bounds, and unknown-property policy.
- Material schema/semantic mismatches were observed or traced: typed endpoints return any Recording; Todo update can change `content` while leaving `title` stale; subscription handlers use the wrong request keys and return shapes/statuses inconsistent with the Subscription schema; project-access update returns numeric IDs rather than People; card-column on-hold is stored as a boolean and enable/disable returns `{enabled}` instead of a CardColumn; MoveCard reads a non-contract `on_hold` field.
- Several create handlers omit `Location`, and nested route identifiers are not checked against parent/bucket relationships. These defects affect shapes, semantics, and navigability even when the nominal status is successful.

### Seed data fidelity — 8.0

- `submission/server.js:142-261` creates one exact `Launch the new website` sample project, the exact description, all-access state, one owner plus eight `sample: true` people, and a six-item dock: Message Board, To-dos, Card Table, Docs & Files, Schedule, and Chat.
- Verified inventory: 5 messages, 2 todolists, 10 todos, 7 columns, 13 cards, 1 document, 1 upload, 1 cloud file, 2 schedule entries, 16 chat lines, 15 comments, 70 events, and 8 boosts. Kickoff has four comments, seven boosts, five subscribers, and is pinned; the pitch has seven comments. IDs and seed timestamps are fixed.
- The graph is navigable through list/detail endpoints and linked parent/bucket fields.
- Specific deviations from `submission/INIT.md:152-201`: the Review card lacks the required two assignees; seeded completed cards lack `completed_at` and completion/move events; the two required boosted comments are absent; Chat uses eight authors rather than the specified five-person narrative and lacks the described text/Q&A boosts; the Docs row color label is absent; the Todo subtask is an embedded object rather than the shared stored Step recording used for card steps.

### Validation, authorization, and request hardening — 6.5

- `submission/server.js:281-293` uses Bearer auth, SHA-256 plus timing-safe comparison, and per-actor rate limiting. Production startup rejects the default token. Runtime probes confirmed missing and invalid tokens return 401, the wrong account returns 404, malformed JSON returns 400, text/plain returns 415, a declared oversized body returns 413, absent/invalid required values return 422, and rate exhaustion returns 429 with `Retry-After`.
- Body limits are enforced both before and during streaming, identifiers are numeric at the router, idempotency conflicts fail closed, unknown exceptions are converted to safe 500 envelopes, and CORS uses an allowlist.
- There is only one HTTP credential and it always maps to owner/admin Person 1 (`submission/server.js:281-285`), so collaborator/client/employee authorization branches cannot be exercised through the API. Project get/update/trash/access handlers also bypass recording-style access checks.
- Relationship and identifier validation is weak: valid typed URLs can retrieve the wrong resource type; wrong parents are accepted; `due_on: "garbage"` and a nonexistent `category_id` succeed; project-people listing for a nonexistent project returns `200 []`.
- `submission/server.js:672-675` rejects a valid streamed/chunked JSON body solely because `Content-Length` is absent; the runtime probe returned 422 even though the body was present.

### Operability, testability, and packaging — 8.5

- The executable is dependency-free Node 22 code with environment validation for host, port, public URL, token, persistence path, body limit, rate limit, and CORS (`submission/server.js:4-28`). `node --check submission/server.js` passed.
- Health, readiness, root discovery, and OpenAPI endpoints are unauthenticated; request logs are structured JSON with duration and request ID; caller request IDs are validated; CORS is allowlisted; ETags and HEAD are supported; server/header/keepalive timeouts are set; SIGINT/SIGTERM trigger graceful close and persistence (`submission/server.js:703-839`).
- Reset is gated by configuration and owner auth. Optional state persistence uses a mode-0600 temporary file plus atomic rename (`submission/server.js:264-279`). The module exports routing, validation, state, reset, dispatch, and server seams (`submission/server.js:840-843`).
- No submission-authored package manifest, test suite, README/run guide, or conformance harness was added. Persistence-file validation is only top-level, and synchronous whole-state writes occur after every successful mutation.

### Code quality and maintainability — 7.0

- Strengths include purposeful naming, small reusable domain/HTTP helpers, centralized errors, deterministic seed construction, atomic persistence, and clean separation between route compilation, dispatch, and transport. The implementation is syntax-valid and avoids external dependencies.
- The entire API uses global mutable state and a 262-line handler object (`submission/server.js:372-633`). Many handlers are dense one-liners, and concerns spanning domain rules, serialization, authorization, and persistence remain in one module.
- The generic identifier selection reduces duplication but erases resource type and parent context, directly causing the highest-impact correctness defects. This abstraction makes additional handlers easy to add while making them unsafe by default.

### Scope honesty — 8.0

- `submission/server.js:802-805` returns explicit `501 not_implemented` for every absent handler, including the verified webhook response. Binary and multipart bodies also return explicit 501 (`submission/server.js:683-687`). These are clear, fail-closed boundaries rather than fabricated CRUD.
- Honesty is reduced by nominal handlers that return misleading 2xx responses: typed getters can return unrelated resources, upload versions always returns the current upload as a one-item history, arbitrary schedule occurrence dates are fabricated by copying the entry, question reminders always reports an empty successful collection, and some subscription/on-hold bodies are accepted without performing the contract operation.

## Representative executed checks

- `node --check submission/server.js`
- Static OpenAPI enumeration: 131 paths, 203 operations, 132 handlers, 71 explicit stubs.
- In-process HTTP run: 14/14 intended probes passed, covering auth, pagination, malformed JSON, media types, idempotency replay/conflict, Todo persistence, type confusion, invalid parent acceptance, lifecycle failures, explicit 501, ETag/304, streamed-body handling, CORS, and request IDs.
- Direct deterministic seed audit: 9 people/8 samples, 1 project, 6 tools, 85 recordings, 70 events, 8 boosts, with per-type and relationship counts checked.
