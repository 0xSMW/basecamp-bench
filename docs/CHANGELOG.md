# Changelog

## FE contract 2026-10-15.1 - 2026-10-15

- Require rendered Chrome evidence through computer use before accepting frontend scores: desktop/mobile screenshots and observed click, type, and reload checks. Submission bugs remain scoreable; unavailable browser setup makes an evaluation incomplete.
- Serve each immutable frontend copy from a runner-owned, GET/HEAD-only loopback server outside the evaluator sandbox, with an independent port, HTTP readiness check, request audit, and guaranteed listener cleanup. Record browser runtime and evaluator evidence in the run manifest.
- Keep dimension weights and scoring anchors unchanged. Archive the previous FE contract and rubric under `benchmarks/fe/versions/2026-09-05.1/`; previous results and the BE contract are unchanged.

## FE contract 2026-09-05.1 - 2026-09-05

- Explicitly allow evaluators to inspect and interact with final HTML prototypes in Chrome using computer use. Dimension weights and scoring anchors are unchanged.
- Preserve existing runs under their recorded contracts. The BE contract remains 2026-07-11.2.

## 1.0.0a1 - 2026-07-11

- Introduce canonical, weighted FE and BE evaluation contracts.
- Use intelligent evaluator agents for the complete evidence-backed assessment.
- Add publication eligibility, repetitions, multi-judge aggregation, immutable evidence, strict schemas, provenance, verification, and portable export requirements.
- Default to workspace-only execution and document the disposable-runtime boundary.
- Make local workspace execution the zero-acknowledgement default, keep external
  isolation optional locally, and define the verified repository baseline workflow.
- Run independent implementations and ready evaluator calls concurrently with
  attributable live progress and coordinated process-group cancellation.
- Add packaging, tests, CI, licensing, asset provenance, methodology, security, and contribution policy.
