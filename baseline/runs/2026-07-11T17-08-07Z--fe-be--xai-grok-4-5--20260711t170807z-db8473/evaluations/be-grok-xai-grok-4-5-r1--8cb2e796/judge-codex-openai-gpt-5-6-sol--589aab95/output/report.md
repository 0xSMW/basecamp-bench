# Backend evaluation report

Submission: `id-8cb2e796`  
Track: `be`  
Contract: `2026-07-11.2` / `21192e19c6a56d2cc2c2e3c746f210d67f800754afd70ad932c38bebb2730e71`

## Executive assessment

This is a broad, working, process-stateful backend prototype. It registers the complete OpenAPI surface and implements substantial project, recording, todo, message, card, document, schedule, chat, cross-cutting, reporting, and administrative behavior. The deterministic sample graph is unusually complete and navigable.

The strongest evidence is the combination of full surface registration and working multi-step mutations. The largest deductions are for one shadowed canonical endpoint, shallow lifecycle propagation, several required-body and status-code contract violations, unsafe path parsing that turns client mistakes into 500 responses, a public reset/default credential posture, and partial operations that return successful completion responses.

No overall score is computed here; aggregation belongs to the runner.

## Evidence and method

### Complete delta

`diff -qr seed submission` found exactly one addition: `submission/server.py` (5,131 lines). No seed file was modified or removed. `INIT.md`, `DESIGN.md`, the screenshots, `SPEC.md`, `openapi.json`, and `behavior-model.json` are byte-identical between seed and submission.

### Runtime strategy

The implementation is dependency-free Python using `BaseHTTPRequestHandler`. `python3 server.py --check` completed successfully and reported 283 routes, 9 people, 87 recordings, one project, five messages, ten todos, thirteen cards, and sixteen chat lines.

The sandbox prohibited binding a listening TCP port. Runtime tests therefore drove the real `APIHandler` over local socket pairs. This exercised HTTP parsing, routing, bearer authentication, body decoding, handler dispatch, serialization, headers, and error mapping without changing the evidence directories.

An automated sweep sent a request through the HTTP handler for every one of the 203 canonical OpenAPI operations using valid account authentication and placeholder resource IDs. Results were: 28 responses with 200, one with 201, three with 204, 159 with 404, eleven with 422, and one with 500. No operation returned 405. The sole 500 was the shadowed canonical recordings route described below.

Targeted stateful tests additionally covered:

- Health, readiness, CORS, request IDs, missing/bad bearer credentials, wrong account, malformed JSON, unsupported media, invalid IDs, method handling, and reset.
- Seed traversal through the account, nine people, project dock, five messages, two todo lists, card table, one document, two uploads, two schedule entries, and paginated chat lines.
- Project create/get/update/trash, with a newly created project correctly starting with an empty dock.
- Todo create/get/complete/uncomplete/trash. Assignee, due date, parent, and bucket serialized correctly; mutations persisted for the process lifetime and trashed items left the active list.
- Message create, comment, boost, subscription, event, archive, and activate. Counts and status changes persisted.
- Pagination after creating 20 projects: page 1 returned 15, `X-Total-Count: 21`, a correct `Link` next URL, and page 2 returned six.

## Dimension scores

### Architecture and domain modeling — 7.5/10

The central store uses one recordings table plus generic events, boosts, subscriptions, readings, and bookmarks (`submission/server.py:230-276`). `new_recording` provides a shared identity, creator, bucket, parent, status, visibility, content, position, and timestamps (`submission/server.py:382-428`). Generic comments, events, subscriptions, lifecycle actions, and serializers reuse those primitives (`submission/server.py:430-579`, `submission/server.py:660-833`). This is coherent enough to support many resource types and cross-cutting behaviors.

Hierarchy enforcement is incomplete. `set_status` changes only immediate children (`submission/server.py:559-579`). The live test archived a message board and observed its message become `archived` while the message's comment remained `active`. Models are also stringly typed dictionaries with no centralized validation of cross-bucket parent changes. These defects keep the domain from the fully coherent, extensible anchor.

### Endpoint surface coverage — 9.7/10

AST comparison found exact decorators for all 203 OpenAPI operations across 131 paths: 100 GET, 41 POST, 42 PUT, and 20 DELETE. There are 80 extra registrations, primarily `.json` aliases plus health, readiness, root, reset, and a project-tool convenience route.

One canonical operation is registered but unreachable as intended. `GET /{accountId}/projects/recordings.json` is intercepted by the earlier `/{accountId}/projects/{projectId}.json` route because routing is first-match (`submission/server.py:2063-2104`, `submission/server.py:2335-2340`, `submission/server.py:2451-2476`). The live sweep confirmed a 500 for that operation. Thus 203/203 are registered and 202/203 reach their intended handlers.

### Behavioral depth and statefulness — 7.5/10

Project and todo CRUD/lifecycle worked end to end, and message comments, boosts, subscriptions, events, archive, activation, and pagination updated shared process state coherently. New projects correctly start empty (`submission/server.py:2307-2333`). Recording mutations write events, and todo completion/trash are persistent during the process (`submission/server.py:2761-2859`, `submission/server.py:3581-3699`).

The broken grandchild status cascade is a material lifecycle invariant failure. State is intentionally process-lifetime only (`submission/server.py:234-240`), which is acceptable for this prototype contract but provides no restart durability. Tool cloning creates only a fresh root (`submission/server.py:2009-2014`), and template construction reports completion after creating an empty project rather than materializing template content (`submission/server.py:4694-4729`).

### HTTP and schema contract fidelity — 7.0/10

Representative serializers produced rich canonical recording, person, project, parent, bucket, URL, and timestamp shapes. Creation generally returned 201, deletion/lifecycle returned 204, error responses used `{error, message}`, and pagination emitted both `X-Total-Count` and `Link`. Response plumbing consistently added JSON content type, content length, request ID, runtime, cache, and CORS headers (`submission/server.py:4836-4862`).

Material defects remain:

- The canonical recordings path returns 500 because of route shadowing.
- `PUT /{accountId}/my/profile.json` returned 200 with a body, while OpenAPI requires 204 (`submission/server.py:2254-2264`; `submission/reference/basecamp-sdk/openapi.json:8620-8647`).
- `PUT /{accountId}/question_answers/{answerId}` returns 200 with a body instead of 204 (`submission/server.py:4469-4479`; `submission/reference/basecamp-sdk/openapi.json:11037-11074`).
- Required bodies can be absent and still return success for out-of-office, project gauge, question-answer update, and template construction. Live tests observed 200 for the first three missing-body cases.
- Router literals are compiled as regex without escaping. A live request to `/1/projects/1000Xjson` incorrectly matched the `.json` project route and returned project 1000 (`submission/server.py:2066-2071`).

### Seed data fidelity — 9.0/10

The API exposed one `Launch the new website` project, the real owner plus all eight named sample people, and the six required dock tools with stable IDs 2001–2006. Runtime traversal verified five messages, two todo lists, the card table, one document, two uploads, two schedule entries, and sixteen chat lines across pagination. The source contains the detailed message/comment/boost graph, todo lists, cards, docs/uploads, schedule, and chat seed (`submission/server.py:1400-1917`). `--check` independently confirmed the principal counts.

The fixed-offset timestamp rule is not fully deterministic. Seed helpers call `touch`, which uses the current clock (`submission/server.py:283-284`, `submission/server.py:466-540`). Two clean runs gave recording 3001 the same fixed `created_at` but different current-time `updated_at` values. That is a real, localized deviation from `submission/INIT.md:190-193`.

### Validation, authorization, and request hardening — 5.0/10

Bearer authentication, digest-stored tokens, account checks, project access checks, creator/admin restrictions, malformed-JSON handling, rate-limit support, a 25 MiB body guard, structured 4xx errors, and method-not-allowed support are present (`submission/server.py:204-208`, `submission/server.py:307-334`, `submission/server.py:4864-4910`, `submission/server.py:4998-5017`). Live tests returned 401 for missing and malformed auth, 404 for a wrong account, and 400 for malformed JSON.

The gaps match the rubric's midpoint: an invalid numeric project ID returns 500; unescaped route regex accepts malformed paths; unsupported `text/plain` JSON produces 422 rather than a media-type error; multiple required bodies succeed when absent; malformed pagination silently becomes page 1; and the default unauthenticated reset successfully destroyed runtime mutations (`submission/server.py:2137-2152`). The public root and startup log disclose the working owner token (`submission/server.py:2125-2135`, `submission/server.py:5071-5077`).

### Operability, testability, and packaging — 6.5/10

The server is one runnable standard-library file with environment configuration, health/readiness routes, structured request logging, request IDs, CORS, signal-based shutdown, a reset seam, and `--check` smoke mode (`submission/server.py:65-98`, `submission/server.py:2110-2152`, `submission/server.py:4816-4843`, `submission/server.py:5042-5127`). The check mode worked on the available Python runtime without installing dependencies.

Production-shape deductions come from global process-only state, a readiness check that only observes a boolean, wildcard CORS by default, reset enabled without a secret by default, credential disclosure in public output/logging, and the absence of a test suite or packaging metadata. The server's actual socket bind could not be assessed because the evaluator sandbox rejects listener creation; the handler itself was exercised directly.

### Code quality and maintainability — 6.5/10

The required single-file packaging is well organized into named sections. Errors, configuration, storage primitives, serializers, pagination, routing, and HTTP dispatch are centralized and reusable. Naming is generally clear, the lock and ID generator are explicit, and comments usually describe real invariants.

Maintainability is reduced by pervasive untyped dictionaries and string keys, repeated near-identical route handlers/decorators, and 5,131 lines mixing seed data, persistence, domain logic, serialization, routing, and transport. The first-match/unescaped router caused a real canonical endpoint failure, and lifecycle recursion is incomplete. These are substantive abstraction failures rather than style-only concerns.

### Scope honesty — 4.5/10

The file header claims a production-shaped implementation with no hollow OpenAPI surface (`submission/server.py:3-18`). Several gaps return successful responses instead of explicit unsupported/partial errors: question reminders always return an empty list (`submission/server.py:4027-4029`), cloning silently performs a root-only clone (`submission/server.py:2009-2014`), and template construction immediately reports `completed` for an empty project (`submission/server.py:4694-4729`). Required-body omissions also return successful mutations in several handlers.

Some limitations are visible in source comments and the process-lifetime store is accurately documented. The user-visible contract, however, does not distinguish the successful placeholders from complete behavior, so this dimension stays below the midpoint.

## Reproduction notes

Key commands used from the submission root:

```sh
diff -qr ../seed ../submission
python3 server.py --check
```

Additional Python probes loaded `openapi.json`, compared all method/path pairs to `@route` decorators through the AST, and drove `APIHandler` over socket pairs for the full 203-operation sweep and targeted lifecycle scenarios. No seed or submission files were written.
