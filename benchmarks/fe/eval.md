# Frontend evaluation rubric

Evaluate from direct evidence in the rendered prototype, source code, and reference screenshots. Distinguish absent, visually present, stubbed, working, and persistent functionality; support every score with specific evidence; avoid rewarding the same capability across multiple dimensions. `benchmarks/fe/contract.json` is canonical for dimension IDs, anchors, and weights, and the runner computes the overall score.

| Dimension | Weight | Evaluation context |
|---|---:|---|
| Reference fidelity | 14% | Accuracy against supplied screenshots and specifications, including shell geometry, layouts, component anatomy, density, content, and visible behavior. |
| Visual craft and design-system coherence | 10% | Internal quality and consistency of typography, color, spacing, tokens, theming, icons, surfaces, motion, and micro-UI details. |
| Product-model and content fidelity | 9% | Accuracy and richness of Basecamp concepts, relationships, terminology, sample data, and the cross-screen narrative. |
| Surface coverage | 10% | Breadth of reachable screens, routes, overlays, detail pages, empty states, and account-wide or administrative destinations. |
| Functional depth | 12% | Whether workflows operate end-to-end, including creation, editing, completion, commenting, filtering, navigation, and deletion instead of ending in inert controls or stubs. |
| Interaction quality | 8% | Usability of working interactions: discoverability, feedback, keyboard efficiency, direct manipulation, focus behavior, confirmations, and error recovery. |
| State and persistence | 10% | Correctness and continuity of application state across views, mutations, navigation, reloads, resets, and malformed or outdated stored data. |
| Responsive adaptation | 8% | Usability and feature preservation across viewport sizes, including reflow, overflow control, navigation changes, touch targets, and mobile-specific behavior. |
| Accessibility | 7% | Semantic structure, accessible names, keyboard operability, focus management, contrast, reduced-motion support, announcements, and assistive-technology compatibility. |
| Code architecture and maintainability | 6% | Readability, modularity, reuse, separation of concerns, routing and state architecture, naming, extensibility, and ease of testing or modifying the prototype. |
| Reliability, safety, and performance | 6% | Runtime stability, input escaping, defensive handling, route safety, browser compatibility, dependency resilience, rendering efficiency, and absence of console errors. |

Inspect the complete submission delta and exercise enough routes, viewports, interactions, state transitions, and failure cases to support every score. The implementation may choose any architecture or runtime; evaluate the result it actually provides.

Browser verification is mandatory. Use `cua_repl` with Chrome to open the exact HTTP URL supplied by the runner, which serves the immutable submission snapshot. Before scoring, inspect the actual served document. Read the current CUA documentation and bind the Chrome browser to discover viewport capabilities; do not guess API methods. Do not start another server, use a headless browser, open browser `file:` URLs, or substitute simulated DOM tests for observed browser behavior.

Capture screenshots at desktop (approximately 1440px wide) and mobile (approximately 390px wide) viewports. Perform at least one click, type, and reload action in the rendered application. Record specific observed outcomes, including product controls that fail to work; failed interactions are valid evidence and must be marked `passed: false`. When a required control or input is absent, record the attempted action and the observed absence with `passed: false`.

Write browser evidence to the supplied browser evidence JSON path, alongside the Markdown report and exact result JSON contract. Successful evidence must have `schema_version: "1.0"`, the exact supplied `url`, `rendered: true`, `status: "complete"`, `screenshots` entries for both `desktop` and `mobile` with specific screenshot/tool observations in `evidence`, and `interactions` entries with `action` (`click`, `type`, or `reload`), specific `observed` outcomes, and boolean `passed` values. Include at least one of each action. Keep all output free of absolute host paths.

`rendered: true` means that the actual served document was inspected in Chrome, not that the product works. If the submission's own JavaScript causes a blank page or error, capture the actual page/error and score those product defects with `status: "complete"`. Failed controls also remain product evidence.

Only when an environmental browser/tool/setup failure prevents Chrome from loading or inspecting the served document, write evidence with `schema_version: "1.0"`, the exact supplied `url`, `rendered: false`, `status: "blocked"`, and a `blocker` describing the observed failure. Keep a useful Markdown evidence report and stop without numerical `result.json`; source-only visual scores are invalid.
