# Backend evaluation rubric

Evaluate from direct evidence in the running API, source code, OpenAPI specification, and SDK behavior. Distinguish registered, stubbed, working, contract-compliant, and persistent operations; support every score with specific evidence; avoid counting the same capability across multiple dimensions. `benchmarks/be/contract.json` is canonical for dimension IDs, anchors, and weights, and the runner computes the overall score.

| Dimension | Weight | Evaluation context |
|---|---:|---|
| Architecture and domain modeling | 12% | Quality and fidelity of the domain model, including shared recording primitives, resource relationships, tool hierarchy, lifecycle design, and ease of extending the model with new resource types. |
| Endpoint surface coverage | 14% | Breadth of OpenAPI paths and operations registered and reachable with the correct methods and path templates, independent of implementation depth. |
| Behavioral depth and statefulness | 16% | Whether endpoints perform coherent create, read, update, delete, lifecycle, and cross-resource operations whose effects persist and respect parent, bucket, and status invariants. |
| HTTP and schema contract fidelity | 16% | Accuracy of request and response shapes, serializers, status codes, headers, pagination, URLs, query semantics, and error envelopes relative to the OpenAPI and SDK contracts. |
| Seed data fidelity | 10% | Completeness, determinism, internal consistency, and navigability of the required sample account, people, projects, tools, and interconnected content. |
| Validation, authorization, and request hardening | 12% | Handling of credentials, permissions, required and malformed input, body limits, unsupported media, invalid identifiers, and safe, consistent client-error responses. |
| Operability, testability, and packaging | 8% | Health checks, configuration, logging and request IDs, CORS policy, graceful shutdown, dependency portability, deterministic reset or isolation, test seams, and conformance-test support. |
| Code quality and maintainability | 7% | Readability, layering, reuse, separation of concerns, complexity, naming, documentation, and whether abstractions make real behavior easier to understand and extend. |
| Scope honesty | 5% | Accuracy and clarity about implemented, partial, stubbed, and unsupported behavior, including whether incomplete operations fail explicitly instead of returning misleading successful responses. |

Inspect the complete submission delta, decide how to run it, and exercise enough endpoints, payloads, authentication, state transitions, malformed inputs, and lifecycle behavior to support every score. The implementation may choose any architecture or runtime; evaluate the result it actually provides.
