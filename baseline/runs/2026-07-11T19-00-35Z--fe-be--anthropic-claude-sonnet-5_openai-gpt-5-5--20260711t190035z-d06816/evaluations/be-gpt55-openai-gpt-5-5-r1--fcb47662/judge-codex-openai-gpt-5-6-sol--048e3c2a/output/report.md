# Backend evaluation report — id-fcb47662

## Outcome

The submission adds a substantial dependency-free Python/SQLite API in one 2,811-line file. It registers the complete canonical surface (131 paths, 203 operations), provides a rich sample project, and implements persistent CRUD and cross-cutting recording behavior across the main Basecamp tools. Its strongest qualities are surface breadth, the shared recording/store model, durable core mutations, authentication, and operational controls. Its main limitations are response-schema drift, incorrect success statuses, incomplete lifecycle invariants, weak type/format validation, and generic handlers that return successful but semantically inaccurate resources for several peripheral families.

No overall score is computed here.

| Dimension | Score |
|---|---:|
| Architecture and domain modeling | 7.2 |
| Endpoint surface coverage | 10.0 |
| Behavioral depth and statefulness | 7.3 |
| HTTP and schema contract fidelity | 5.3 |
| Seed data fidelity | 7.0 |
| Validation, authorization, and request hardening | 7.0 |
| Operability, testability, and packaging | 7.3 |
| Code quality and maintainability | 6.5 |
| Scope honesty | 4.5 |

## Evaluation method

- Compared the complete trees with `git diff --no-index` and `diff`; the only submission delta is `submission/basecamp5_api.py` (2,811 added lines). The canonical reference files are unchanged.
- Counted the canonical OpenAPI surface and constructed every path template: 131 paths and 203 operations (100 GET, 42 PUT, 41 POST, 20 DELETE). Every method/template matched the intended operation in the application's generated route table, with no collisions.
- Attempted the documented file-backed server command. The managed evaluator sandbox denied local socket binding with `PermissionError` before a listener could start. This is an environment restriction, so runtime behavior was exercised through the same `App.handle` router/auth/validation/transaction path using fresh SQLite files under `/tmp`; only the socket listener and `BaseHTTPRequestHandler` wire emission were bypassed.
- Ran a 203-operation route matrix with generated schema-aware inputs and seeded identifiers. It produced 160 successes, 42 controlled 4xx responses, and one uncaught handler defect (`MoveCardColumn` expects a nonexistent `columnId` path parameter). Fourteen successful operations returned a status absent from their OpenAPI success contract.
- Exercised authentication, owner/client authorization, pagination, media types, malformed and oversized bodies, invalid identifiers, idempotency, rate limiting, project/todo/card/message/comment/boost/subscription lifecycles, restart persistence, and deterministic reset.
- Validated 26 representative successful seeded GET payloads with `jsonschema` Draft 2020-12 against the documented OpenAPI response schemas. Ten passed and sixteen failed. Create responses were also checked against their documented 201 schemas.

## Dimension evidence

### Architecture and domain modeling — 7.2/10

The design has a real shared recording foundation. SQLite stores generic resources with `kind`, parent, bucket, status, ordering, indexed projections, and JSON data; separate tables support events, comments, boosts, subscriptions, readings, idempotency, and counters (`submission/basecamp5_api.py:263`, `submission/basecamp5_api.py:291`). The shared recording constructor supplies creator, parent, bucket, status, visibility, URLs, comments, boosts, and subscription fields (`submission/basecamp5_api.py:916`). Core handlers reuse that structure across messages, todos, cards, documents, uploads, chat, and schedule resources.

The model does not consistently maintain its own invariants. Disabling tool 201 changed the tool resource to `enabled: false`, while project 12345's embedded dock projection remained `enabled: true`. Trashing project 12345 made the project unreadable (404), yet child recording 3001 remained readable (200); stored `inherits_status` is not propagated. `UpdateProjectAccess` persists an `access` object, but authorization never consults it. Generic families also collapse distinct concepts such as questions/answers and templates/constructions into a single kind (`submission/basecamp5_api.py:2583`).

### Endpoint surface coverage — 10.0/10

The canonical OpenAPI contains 131 path templates and 203 operations. `build_routes` loads each supported HTTP operation directly from the spec (`submission/basecamp5_api.py:227`), and exhaustive matching verified 203/203 exact operation/method matches with zero route collisions. The HTTP handler exposes every method used by the spec: GET, POST, PUT, and DELETE (`submission/basecamp5_api.py:2677`). Depth and correctness defects are scored in other dimensions; they do not erase the registered surface.

### Behavioral depth and statefulness — 7.3/10

Core behavior is real and persistent:

- Project create returned 201 plus `Location`, update persisted, a new `App` over the same DB read the update, delete returned 204, normal GET then returned 404, and `status=all` included the trashed project.
- Todo create under todolist 4001 returned 201 and appeared in the parent's list. Completion and archive/unarchive transitions persisted. Card 5101 moved from column 5001 to 5002 and changed membership in both lists.
- Comment creation updated the recording's list/count; boosts, subscriptions, events, and idempotency were stateful. Reusing an idempotency key replayed the original 201 with `Idempotency-Replayed: true`; reusing it for a different body returned 409.
- Reopening the same SQLite DB preserved created todos and comments. Owner reset removed created data and restored the stable seed.

Depth falls short of full coherence. Project trash does not cascade or hide child recordings, tool changes drift from the embedded dock, owners can still GET trashed recordings, and access updates are not enforced. Peripheral generic handlers often ignore parent relationships: creating an answer under nonexistent question 999999 returned 201 as a new `type: "Question"`. `MarkAsRead` accepts the contract's `readables` request and returns success, but notification listing still reports every item as `read: false`.

### HTTP and schema contract fidelity — 5.3/10

The implementation has centralized JSON envelopes, pagination headers, `Location`, request IDs, ETags, conditional 304 handling, CORS/security headers, and structured error bodies (`submission/basecamp5_api.py:1829`, `submission/basecamp5_api.py:2725`). Pagination was observed with `X-Total-Count: 10` and a correct next-page `Link`.

Material contract defects are widespread:

- Draft 2020-12 validation passed only 10 of 26 representative successful seeded GET responses. Examples: messages' `parent` lacks required `app_url`; todolists lack required `name`; todos emit `starts_on: null` where a string is required; all-day schedule entries emit null string fields; events lack required `creator`; upcoming schedule returns an array instead of the documented aggregate object.
- Fourteen successful operations in the matrix used an undocumented success status. `CompleteTodo`, `ArchiveRecording`, `UnarchiveRecording`, `MoveCard`, pin/unpin, and several tool/card lifecycle actions returned 200 bodies where the spec requires 204. `MarkAsRead` returned 204 where the spec documents 200.
- Seven of eight representative create responses failed their documented 201 schema. A spec-valid schedule entry using `summary`, `starts_at`, and `ends_at` was rejected because the generic creator requires `title` or `name` (`submission/basecamp5_api.py:2395`, `submission/basecamp5_api.py:2112`).
- The API accepts only JSON bodies globally, so documented octet-stream/multipart operations such as attachment and campfire upload returned 415. Resource and `Location` URLs are relative while `app_url` is absolute.

### Seed data fidelity — 7.0/10

The seed is broad and navigable: one account; ten people with exactly eight sample people; project 12345; eight dock tools; five categories and five messages; two todolists, ten todos, and eight steps; seven card columns and thirteen cards; one document and two uploads/cloud files; eight chat lines; two schedule entries; and 21 events, 15 comments, and 11 boosts. Stable IDs and the major required titles/relationships make the graph easy to traverse (`submission/basecamp5_api.py:961`).

Several specified details are absent or nondeterministic. The kickoff message has seven boosts and four comments but zero of the required five subscriptions; no comments are boosted; the traffic post lacks its embedded chart; Done cards lack completion/move timestamps; chat has eight lines rather than roughly sixteen and uses seven people rather than the specified five. Fresh boot hashes differed because seed events, comments, boosts, recounts, and some `updated_at` values call wall-clock `utcnow`, contrary to the fixed-offset requirement. The deterministic seed contract is defined in `submission/INIT.md:119`.

### Validation, authorization, and request hardening — 7.0/10

Observed protections include 401 for missing/invalid bearer tokens, 403 for account mismatch and client mutations, client visibility enforcement, required-field validation, 400 malformed JSON with line/column detail, 422 for non-object bodies and unknown assignees, 415 media-type rejection, 413 over 10 MiB, 405 plus `Allow`, 404 unknown routes, idempotency conflict detection, and configurable rate limiting with 429 plus `Retry-After` (`submission/basecamp5_api.py:1434`, `submission/basecamp5_api.py:1495`). Errors include a request ID and safe generic 500 handling at the HTTP layer.

Hardening is incomplete. Nonnumeric path IDs and invalid UTF-8 raise uncaught `ValueError`/`UnicodeDecodeError` and become 500 responses. Nonnumeric assignee values can do the same. Dates and many property types/formats are not schema-validated (`due_on: "yesterdayish"` was accepted). Reset requires only an owner bearer token unless an optional environment secret is configured. The default credentials are documented and logged, appropriate for a prototype but weak for a production-shaped server.

### Operability, testability, and packaging — 7.3/10

The server is stdlib-only and exposes CLI/environment configuration for host, port, DB, log level, body limits, CORS, rate limits, tokens, and reset token. It uses SQLite WAL and busy timeouts, has health/OpenAPI/reset endpoints, emits request IDs and elapsed-time logs, wraps unexpected errors, supports threaded serving, and installs SIGINT/SIGTERM shutdown (`submission/basecamp5_api.py:47`, `submission/basecamp5_api.py:1421`, `submission/basecamp5_api.py:1475`, `submission/basecamp5_api.py:2698`, `submission/basecamp5_api.py:2777`). File-backed initialization, health, reset, and persistence were verified.

The health endpoint does not probe storage, CORS defaults to `*`, the default DB is written beside source, and startup logs the default credential. There are no submission tests, package metadata, or conformance harness. `App(':memory:')` is unsafe with the threaded design because each thread-local SQLite connection gets a separate database; a cross-thread request failed with `no such table: tokens`. The actual socket listener could not be exercised because the evaluator sandbox prohibits binding, so graceful shutdown and wire-level ETag behavior are source-supported rather than runtime-verified here.

### Code quality and maintainability — 6.5/10

Names are generally clear, domain helpers are reusable, `ApiError`, `Route`, and `Actor` are typed dataclasses/classes, persistence is centralized, and handler groupings make the broad behavior traceable. The implementation is readable despite its size.

Maintainability is constrained by placing persistence, seed data, routing, auth, serializers, every domain handler, HTTP emission, and process lifecycle in a single 2,811-line module. Indexed/projection columns duplicate mutable JSON state and demonstrably drift. Substring dispatch and one generic kind per peripheral family erase important semantics. There are no tests to protect the many operation-specific branches.

### Scope honesty — 4.5/10

There is a good explicit 501 fallback that describes an unimplemented operation as unsupported rather than returning a no-op (`submission/basecamp5_api.py:1760`). In practice, all current operation IDs are routed before that fallback, and several broad handlers return misleading success instead of 501 or a faithful implementation.

Examples observed directly: creating a project construction under nonexistent template 999999 returned 201 as a new `Template`; creating an answer under nonexistent question 999999 returned 201 as a new `Question`; `GetQuestionReminders` always returns 200 with an empty array; and `MarkAsRead` reports success without changing subsequent notification `read` flags. These operations are present and successful at the HTTP layer but are semantically stubbed. The contract explicitly prefers visible stubs over fake success (`submission/INIT.md:15`).

## Runtime limitations and evidence integrity

The socket-bind denial came from the managed evaluation environment, not from application code, and was not treated as a server startup defect. All generated databases and test artifacts were kept outside the seed and submission trees. Final tree comparison confirmed the submission delta remained exactly the single added Python source file.
