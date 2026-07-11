# Frontend evaluation: id-4b548d44

## Evaluation basis

The complete seed/submission comparison adds one implementation file, `submission/index.html` (56,750 bytes, 160 lines), plus two `.DS_Store` files. The seed contains the specification, design tokens, SDK reference, and nine screenshots; the submission contains no package manifest, tests, or implementation modules beyond the single HTML SPA.

I inspected all source, all nine reference screenshots, the relevant `seed/INIT.md` and `seed/DESIGN.md` contracts, and the complete delta. The inline JavaScript passed `node --check`. A Node VM harness executed the submission's actual model and renderer functions and passed 13/13 assertions covering the home/project renderers, message/todo filters and empty states, regex-safe highlighting, invalid detail IDs, persisted chat escaping, theme/chat/todo initialization, malformed JSON, wrong-shaped stored data, and stored todo markup interpolation. A separate model harness verified to-do completion writes `bc5-todos` and rerenders.

Full browser execution was attempted three ways. Local HTTP binding failed with `PermissionError: [Errno 1] Operation not permitted`; the installed browser controller reported no available browser; standalone Chromium failed at the macOS MachPort bootstrap with permission denied. Consequently, geometry and responsive scores use direct CSS-to-reference comparison rather than a new submission screenshot, and browser-only claims such as a console-clean ordinary load are not credited as verified.

## Scores and evidence

### Reference fidelity — 6.5/10

The required anatomy is broadly replicated: centered home hub with side rails, fixed account header and My Bar, capped content sheet, three-column project dock, message rows, nested to-do views, horizontal Card Table, file rows, chat timeline/composer, and jump modal (`submission/index.html:33-49,125-141`). The CSS uses the reference's 10px root scale and many exact dark/light HSL values from `seed/DESIGN.md` (`submission/index.html:9-30`). The submission also follows the specified “Launch the new website” narrative rather than copying the screenshot's podcast content.

Material mismatches remain: people are gradient initials instead of photographic avatars, many icons are emoji/text glyphs, 44px headings and 18px rows are substantially less dense than the captures, and the home/project widths and card sizing diverge. Chat repeats avatar/name on every line instead of suppressing consecutive authors, Docs uses generic file glyphs, and several detailed pages reuse generic bodies/notes. The Jump menu has the required anatomy but simplified result grouping. Because a submission screenshot could not be produced, no credit was added for unverified pixel-level correspondence.

### Visual craft and design-system coherence — 7.0/10

The tokenized palette, radius/shadow scale, consistent typography, focus ring, button/pill/badge families, dark/light themes, sheet hierarchy, transitions, and reduced-motion override form a coherent visual system (`submission/index.html:9-50`). Card Table colors, dock previews, notification treatments, empty states, and modal/tray layering show substantial detail.

Craft is limited by extensive one-off inline styles, emoji/text substitutes for a consistent icon set, synthetic initials, and some controls whose polished appearance overstates their functionality. The 160-line minified monolith also makes consistency harder to maintain.

### Product-model and content fidelity — 6.5/10

All eight specified people, five messages, two lists/ten to-dos, six active cards, three file types, and a launch narrative are represented (`submission/index.html:91-117`). Core terminology and relationships—All-access project, dock tools, pinned/category messages, assignments, due dates, completion, steps, on hold, watchers, boosts, notifications, and My Bar—appear across the proper surfaces.

The model is shallow. There is no shared Recording abstraction or event/readings model. Every message detail uses the same fixed body/comments, todo and card details use generic notes/steps, Done/Not now show counts without records, both home project cards open the same project, and chat has 11 rather than roughly 16 seeded lines (`submission/index.html:114-140`). The sidebar starts with three unread notices despite the clean-viewer seed rule.

### Surface coverage — 7.0/10

All screenshot-backed surfaces are reachable: Home, Jump, project dock, Message Board and message detail, To-dos and detail, Card Table and detail, Docs & Files, and Chat. My Bar trays, notification sidebar, theme control, and empty filter states provide useful secondary coverage (`submission/index.html:54-89,117,125-153`).

The actual router has ten substantive route shapes: `#home`, `#project`, `#messages`, `#message/{id}`, `#todos`, `#todo/{id}`, `#cards`, `#card/{id}`, `#docs`, and `#chat`. Activity, Calendar, Reports, Everything, and every other unknown route resolve to the same generic stub page (`submission/index.html:140-141`); optional/admin surfaces and real detail pages for docs/calendar are absent.

### Functional depth — 4.0/10

Working behavior includes hash navigation, message/todo/docs live filtering, completed-list disclosure, todo complete/reopen, chat posting, theme switching, jump filtering, modal/sidebar/tray opening, keyboard shortcuts, and persistent notes (`submission/index.html:142-157`). The VM harness directly verified renderer filtering, empty states, chat escaping, and todo persistence behavior.

Twelve controls explicitly report prototype status: New project, Invite people, Adminland, Add tool, Project notifications, New message, Boost, New list, Add to-do, Add card, Move card, and New file. Additional polished controls are inert: Card Table filter, notification filter/tabs/bubble-up, message category/sort, Docs type/sort tabs, profile, bookmark/options, comment composers, and detail step checkboxes. There is no create/edit/comment/delete workflow end to end.

### Interaction quality — 5.0/10

Live filters, visible focus styling, toast feedback, keyboard shortcuts, Escape handling, shortcut badge reveal, jump search focus, and trigger focus restoration for jump/sidebar are good prototype interactions (`submission/index.html:31-32,80-89,145-157`). The toast is a polite live region.

Many discoverable controls are inert or only emit a generic prototype toast. Jump/sidebar do not trap focus or inert the rest of the application, tray opening does not move or restore focus, and there is no confirmation/undo/reset/error recovery. Invalid detail IDs silently display the first record, which is misleading.

### State and persistence — 4.5/10

Todos, user chat, theme, and My Notes use localStorage (`submission/index.html:111-115,142-154`). The harness verified persisted arrays/theme initialize, chat content is escaped on rerender, and todo completion writes state. This supports ordinary reload continuity for those four narrow features.

Completed-group expansion, Do Today checks, detail steps, comments, filters, and other UI state are ephemeral. There is no reset or migration path. Direct tests confirmed malformed JSON aborts initialization, `{}` todos fail at `.filter`, `{}` chat fails at `.map`, and missing todo titles fail at `.toLowerCase`; no schema/version validation recovers from outdated or corrupted storage.

### Responsive adaptation — 6.5/10

Two breakpoints reflow the home layout, dock, content padding, jump tiles, message/file rows, metadata, and full-width sheets; Card Table retains horizontal scrolling, and touch manipulation plus viewport-fit/dvh are present (`submission/index.html:5,31,42,48-50`). These rules are production-shaped and cover likely desktop/tablet/mobile widths.

At 1,000px the My Bar hides the third through fifth destinations, causing feature loss; on small screens the notification pill becomes only a dot and several row badges disappear. The Card Table intentionally remains a wide scrolling canvas. Rendered touch behavior and overflow could not be browser-verified in this sandbox.

### Accessibility — 6.0/10

Strengths include document language, landmarks, skip link, focus-visible styling, focusable route target, native controls, labeled jump dialog with `aria-modal`, inert/aria-hidden closed layers, polite status announcements, Escape/keyboard shortcuts, and reduced-motion support (`submission/index.html:2,31-32,50,54-89,145-157`). Jump and sidebar restore focus to their triggers.

Gaps include no focus trap/background inerting for open jump/sidebar, no dialog semantics on sidebar/trays, no focus management for trays, unlabeled star/ellipsis and most bubble-up glyph buttons, unnamed todo/card comment textboxes, and a completed disclosure without an associated controlled region. The completed todo detail still announces “Mark complete.” Several core-looking controls cannot be operated meaningfully by any input method.

### Code architecture and maintainability — 5.0/10

The dependency-free file has reusable helpers for avatars, escaping, crumbs, sheets, row rendering, centralized data, a small hash router, per-view renderer functions, and consolidated event wiring (`submission/index.html:91-157`). The lack of a build/runtime dependency makes the artifact portable.

All CSS, models, persistence, templates, routing, and controllers are compressed into one 56.7KB file with mutable globals, large HTML strings, extensive inline styling, full-main `innerHTML` replacement, and no tests or formal seams. Unknown routes and invalid item IDs share silent fallbacks. Storage parsing and model validation are mixed directly into startup.

### Reliability, safety, and performance — 5.0/10

The JavaScript parses, the artifact has no external runtime dependencies, small static data bounds rendering cost, malformed ordinary route names receive a stub, reduced motion is honored, and submitted chat is HTML-escaped before both immediate and persisted rendering (`submission/index.html:120,139-142`).

Reliability fails closed poorly around client state: malformed localStorage crashes startup, valid wrong-shaped data crashes renderers, malformed percent-encoded hashes throw in `decodeURIComponent`, localStorage failures are uncaught, and invalid detail IDs silently show unrelated records (`submission/index.html:111-115,130,134,137,141`). Persisted todo fields are interpolated into `innerHTML` without escaping; the harness confirmed a stored `<img onerror=…>` title is emitted as markup. Modern `color-mix`, `inert`, and `dvh` also narrow compatibility, and ordinary-load console behavior could not be browser-verified.

## Summary

This is a visually thoughtful, unusually broad single-file prototype with strong Basecamp shell anatomy and a good deterministic launch narrative. It demonstrates a handful of real, persistent workflows. It remains a prototype in functional and state terms: most creation/editing/commenting/movement/deletion controls are explicit stubs or inert, persistence covers only four local keys, and malformed stored state can make the app unusable. The code is portable and readable at the helper level, but its monolithic string-rendering architecture and missing validation/testing prevent production-shaped reliability.
