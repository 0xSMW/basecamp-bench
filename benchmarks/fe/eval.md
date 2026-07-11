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

Inspect the complete submission delta, decide how to run it, and exercise enough routes, viewports, interactions, state transitions, and failure cases to support every score. The implementation may choose any architecture or runtime; evaluate the result it actually provides.
