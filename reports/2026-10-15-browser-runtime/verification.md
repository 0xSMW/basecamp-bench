# Future frontend browser evaluation — verified October 15, 2026

The runner now owns a GET/HEAD-only static server over each frozen FE submission copy, bound to loopback on an independently assigned port. HTTP readiness is verified before invoking the evaluator. Chrome document requests and the evaluator’s desktop/mobile screenshot and click/type/reload observations are required before scores can be accepted. Browser setup failures remain incomplete; submission defects remain scoreable. Listener cleanup and browser proof hashing cover success, failure, exception, and interruption paths. FE contract 2026-10-15.1 preserves the previous weights and anchors; the old contract and rubric are archived.

Verified a standalone fixture in real Chrome through computer use: loaded the served page, typed `Chrome read-only server verified`, clicked Save note, observed count 1 and the saved status, and reloaded with both retained. Desktop screenshot: 1440×900. Mobile screenshot: 390×844. Server audit records Chrome document requests, and the fixture’s files remained byte-identical. The listener was closed and a subsequent HTTP request failed as expected.

Validation: 584 tests and 394 subtests passed; formatting, lint, and type checking passed. Regression tests cover missing/blocked proof, product interaction failures, server readiness/entry selection, concurrent ports, path/symlink restrictions, write rejection, and listener cleanup and manifest hashes after exceptions/interruption.

Previous benchmarks were not rerun or modified. All eight Muse results still match all 188 declared artifact hashes. No commits, branches, or pushes were made.

Evidence: [Server audit](server-proof.json), [actual Chrome observations](browser-evidence.json), [validation receipt](validation.json).
