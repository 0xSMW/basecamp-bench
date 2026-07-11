# Frontend evaluation report

Submission: `id-0b1ef122`  
Track: `fe`  
Contract: `ba46a54cf7c63bf52be8e1f95814676c8d0c7cbb375354460bbdb1eaf24041a1`

## Evaluation method and runtime evidence

- The complete delta contains one substantive deliverable, `submission/prototype.html` (5,171 lines, approximately 316 KB), plus an empty submission-only `.claude` directory. The seed specifications and reference screenshots are unchanged.
- The deliverable is a dependency-free single-file SPA using inline CSS, inline JavaScript, a hash router, and `localStorage`; it requires no build step (`submission/prototype.html:1749-1778`, `submission/prototype.html:5160-5167`).
- The inline JavaScript was extracted in memory and compiled with Node's JavaScript parser; syntax validation passed.
- Rendered-browser execution was attempted through the available static-server, connected-browser, Chromium, and Firefox paths. The environment denied local port binding, exposed no connected browser, and denied browser-process startup before a page could load. Consequently, no score below claims a visually observed browser result. Visual, responsive, and interaction findings are grounded in the supplied screenshots, implemented DOM/CSS, route/action code, and executable syntax validation. This limitation particularly constrains pixel-level visual assertions.
- The reference set comprises nine 3456×1780 dark-mode captures covering Home, Jump, project dock, Message Board, To-dos, to-do detail, Card Table, Docs & Files, and Chat. These were inspected as the canonical visual comparison.

## Dimension scores

| Dimension | Score | Assessment |
|---|---:|---|
| Reference fidelity | 8.0 | All nine required screen anatomies are represented with recognizable Basecamp shell geometry and dense matching content. Pixel-level rendering could not be verified in this environment. |
| Visual craft | 8.1 | The source implements the supplied token system, dark/light behavior, typography, coherent surfaces, micro-UI, animation, and reduced motion with unusual completeness. |
| Product model | 9.2 | The shared Recording graph, exact sample cast/narrative, tools, events, readings, subscriptions, boosts, and relationships closely follow the contract. |
| Surface coverage | 9.0 | Required surfaces plus most optional account-wide and administrative destinations are routed and populated; a few secondary capabilities are explicit stubs. |
| Functional depth | 8.1 | Most core create/edit/complete/comment/filter/move/archive/trash/restore workflows have real mutations and persistence. Broken copying and several honest stubs prevent top credit. |
| Interaction quality | 7.2 | Shortcuts, feedback, live filtering, confirmations, and recovery are strong; overlay/menu focus behavior, hover-only controls, and legacy prompts weaken polish. |
| State persistence | 6.8 | Nearly all mutations save and reload through one shared graph, but malformed-v1 handling, copied-record identity corruption, and shallow deletion violate important invariants. |
| Responsive adaptation | 6.5 | The core shell, home, dock, chat, calendar, and kanban adapt or scroll, but touch loses discoverability for multiple hover-revealed actions and dense tables remain desktop-shaped. |
| Accessibility | 5.0 | Strong baseline landmarks, focus rings, skip link, live region, and reduced motion are offset by missing mobile names, invalid nested controls, weak form/editor labeling, and incomplete overlay focus behavior. |
| Code architecture | 7.5 | The single-file constraint is handled with clear sections and reusable model/view/action primitives, but global mutable state and mixed 5k-line concerns limit maintainability and testability. |
| Reliability | 6.2 | Startup code parses and route rendering has a fallback, but unsafe stored HTML/URLs and verified data-integrity defects are material reliability and security risks. |

## Detailed evidence

### Reference fidelity — 8.0

The implementation recreates the persistent centered account header, fixed My Bar, home three-column composition, sheet pages, breadcrumbs, jump dialog, project dock, list/detail tool anatomy, kanban columns, file rows, and chat timeline/composer (`submission/prototype.html:741-808`, `submission/prototype.html:1082-1315`, `submission/prototype.html:1490-1624`). The required screens all have dedicated renderers: Home (`submission/prototype.html:3186-3228`), Jump (`submission/prototype.html:2535-2610`), project dock (`submission/prototype.html:3377-3410`), Message Board (`submission/prototype.html:3593-3675`), To-dos/detail (`submission/prototype.html:3781-4000`), Card Table (`submission/prototype.html:4018-4196`), Docs & Files (`submission/prototype.html:4222-4388`), and Chat (`submission/prototype.html:4399-4478`). Dark-mode ramps and sheet surfaces mirror the canonical token/reference treatment (`submission/prototype.html:421-688`).

The reference screenshots use the “Making a Podcast” production sample while the contract explicitly specifies the new “Launch the new website” narrative; the submission correctly prioritizes the contract narrative. Exact rendered geometry, text wrapping, and density could not be observed because the evaluation runtime could not launch a browser, so this score does not assume pixel parity.

### Visual craft and design-system coherence — 8.1

The CSS transcribes the production token architecture: light default, explicit/OS dark blocks, 10px root scale, semantic ramps, page tints, shadows, radii, motion, toolbar sizes, kanban widths, and editor tokens (`submission/prototype.html:16-688`). Shared primitives cover buttons, pills, segmented controls, inputs, switches, avatars, sheets, modals, popovers, toasts, empty states, tool cards, and record rows (`submission/prototype.html:703-1739`). Icons are consistently generated from one inline SVG helper (`submission/prototype.html:1826-1898`). The design includes hover/active/focus states, micro-animations, dark-mode-specific contrast adjustments, and `prefers-reduced-motion` (`submission/prototype.html:730-733`).

Deductions reflect source-visible rough edges: several controls appear only through hover opacity, some layouts use dense inline styles, native `prompt`/`confirm` interrupts the otherwise custom system, and browser rendering could not be inspected for clipping or token compatibility.

### Product-model and content fidelity — 9.2

The central store models a shared Recording envelope and tree, generic children, comments, boosts, events, readings, subscriptions, status, project/tool identity, and common URL construction (`submission/prototype.html:1944-2034`). This directly supports cross-cutting activity, search, notifications, trash, bookmarks, and comments rather than implementing each screen as unrelated static markup.

The deterministic sample graph is exceptionally faithful: the viewer plus all eight named people, the exact project narrative and All-access cast, six project tools, five message threads, the pinned kickoff with mentions/seven boosts/comments/subscribers, two to-do lists with due/notify/steps/completions, renamed Card Table triage with watchers/on-hold/colors, document/upload/cloud link, scheduled events, and the multi-person chat narrative (`submission/prototype.html:2060-2338`). Minor deductions are for shallow client-visibility enforcement, limited mutation-driven notification generation, and optional concepts that are represented mainly as destinations.

### Surface coverage — 9.0

The router exposes Home, projects, Activity, Calendar, Reports, Everything, Search, Adminland, Trash, profiles, Pings, four My pages, project pages, tool indexes, composers, and item details (`submission/prototype.html:2473-2528`). The bucket dispatcher covers Message Board/messages, To-dos/lists/items, Card Table/cards, Vault/folders/documents/uploads, Chat, Schedule/entries, Check-ins/questions, and Email Forwards (`submission/prototype.html:3532-3559`). External Doors are addable to the dock (`submission/prototype.html:3480-3507`).

Secondary breadth includes activity timeline/wrap-up, account calendar, reports, account-wide Everything lists, faceted search, settings, trash, profiles, assignments, drafts, boosts, and direct messages (`submission/prototype.html:4665-5082`). Explicitly disclosed omissions include the To-dos Sheet view, chat attachments/voice, scheduled firing of check-ins, real inbound email, export, and Lineup/Hill charts/Timesheet (`submission/prototype.html:3829`, `submission/prototype.html:4463-4464`, `submission/prototype.html:4602`, `submission/prototype.html:4655`, `submission/prototype.html:4796-4812`, `submission/prototype.html:4956`).

### Functional depth — 8.1

Real, stateful workflows cover project/folder/person creation; project settings and tool management; message draft/publish/edit/category/pin; comments and subscriptions; list/to-do creation, completion, assignment, dates, steps, and notes; cards/columns, moves, holds, and completion; docs/folders/cloud links/uploads and bulk actions; chat messages/emoji/boosts; events; check-in answers; pings; notification bubbling; bookmarks; themes; and trash/restore (`submission/prototype.html:2965-3166`, `submission/prototype.html:3230-3526`, `submission/prototype.html:3673-3723`, `submission/prototype.html:3805-4478`, `submission/prototype.html:4517-4644`). Live filtering is delegated across list surfaces (`submission/prototype.html:5136-5144`).

One core generic workflow is broken: “Make a copy” passes `id: undefined` into `addRec`, whose `Object.assign` overwrites the generated ID. The resulting copied record has no valid identity (`submission/prototype.html:1964-1973`, `submission/prototype.html:3085-3089`). Several secondary integrations remain explicit stubs rather than fake success.

### Interaction quality — 7.2

Interaction support includes a broad shortcut map, Shift-revealed shortcut hints, Jump keyboard navigation, Enter-to-send, live filters, focused modal inputs, toasts, native confirmation before irreversible deletion/reset, trash restoration, undoable archive/trash paths, and input validation (`submission/prototype.html:2535-2610`, `submission/prototype.html:2737-2796`, `submission/prototype.html:3039-3166`, `submission/prototype.html:5105-5156`). The notification sidebar and My Bar preserve context rather than forcing page navigation.

Overlay behavior is incomplete: modals focus the first field but do not trap focus, restore focus to the trigger, or consistently supply a dialog name; popovers do not restore trigger focus (`submission/prototype.html:2356-2400`). Menus declare `role="menu"` but their children lack menu-item roles and arrow-key behavior (`submission/prototype.html:2374-2386`). Several actions are revealed only on hover, and editing/link creation relies on deprecated `execCommand` and blocking native prompts (`submission/prototype.html:2867-2914`).

### State and persistence — 6.8

The whole graph persists under a versioned `localStorage` key, resets to a deterministic seed, and is reused across views; nearly every mutation calls `save()` (`submission/prototype.html:1927-1940`, `submission/prototype.html:5160-5167`). Notes autosave on input, and preferences, readings, recents, bookmarks, Do Today, notifications, and content mutations share the same persisted state (`submission/prototype.html:2409-2416`, `submission/prototype.html:2674-2731`). Invalid JSON and wrong-version objects fall back to a fresh seed.

The version check is only `d.v === 1`; a parseable partial v1 object is accepted without schema validation/default merging. `render()` dereferences `db.prefs.theme` before its route-level try/catch, so malformed stored data can fail startup (`submission/prototype.html:1932-1937`, `submission/prototype.html:2473-2483`). The copy identity bug corrupts persisted record identity. Permanent delete removes only a record and direct children, leaving grandchildren and related events/boosts/readings/bookmarks dangling (`submission/prototype.html:3099-3103`). Project/person deletion similarly leaves cross-cutting references.

### Responsive adaptation — 6.5

The implementation has mobile root overrides and meaningful reflow: home collapses from three columns below 1200px, the dock moves from three to two to one columns, sheet content compresses, bottom-bar labels hide, chat padding reduces, calendar sources stack, the keyboard grid collapses, and kanban columns use horizontal scrolling (`submission/prototype.html:691-700`, `submission/prototype.html:1134-1140`, `submission/prototype.html:1257-1261`, `submission/prototype.html:1314-1315`, `submission/prototype.html:1500-1501`, `submission/prototype.html:1624`, `submission/prototype.html:1647`, `submission/prototype.html:1731-1745`). Sidebars, trays, dialogs, and jump surfaces cap width against the viewport.

Touch discoverability remains materially weaker. Project stars, dock quick-add, card add buttons, file selection, chat-line options, and notification bubble controls are opacity-hidden until hover, with no mobile override or `:focus-visible` reveal (`submission/prototype.html:1118-1123`, `submission/prototype.html:1289-1290`, `submission/prototype.html:1529-1530`, `submission/prototype.html:1562-1571`, `submission/prototype.html:1601-1602`). The Jump tiles remain four columns, modal actions do not wrap, and many 20–32px controls are undersized for touch. The calendar retains a seven-column desktop grid with smaller cells rather than a mobile-specific representation. Rendered viewport checks were blocked by the environment.

### Accessibility — 5.0

Positive evidence includes a skip link, focus-visible ring, semantic main/footer/navigation regions, accessible names on many icon buttons and filters, pressed states, dialog roles, a polite live status region, an accessible SVG chart description, and reduced-motion support (`submission/prototype.html:703-733`, `submission/prototype.html:1749-1769`, `submission/prototype.html:2393-2397`, `submission/prototype.html:2420-2467`). Native buttons, anchors, form controls, headings, and labels provide a strong baseline, and the shortcut/help system is extensive.

Material gaps remain. Dialogs are not focus-trapped or focus-restoring and often lack `aria-labelledby`; sidebar/tray overlays do not consistently move focus, and menu descendants do not implement menu semantics/keyboard navigation. Generic rich-text `contenteditable` bodies lack a role and accessible label (`submission/prototype.html:2867-2884`), while many visually adjacent form labels have no `for` association. Below 760px, the My Bar hides each tray button's text while its SVG is `aria-hidden`, leaving five fixed controls without accessible names (`submission/prototype.html:1257-1261`, `submission/prototype.html:2436-2440`). Project cards contain star buttons inside links, sidebar links contain bubble buttons, and the Pings button nests a non-focusable `role="button"` span (`submission/prototype.html:2443-2448`, `submission/prototype.html:2622-2634`, `submission/prototype.html:3191-3201`). Opacity-hidden controls remain keyboard-focusable without becoming visible on focus, and dynamic list changes are generally not announced.

### Code architecture and maintainability — 7.5

Within the required single-file constraint, the file is clearly sectioned and has useful reusable boundaries: token/component CSS, store and lookup helpers, shared Recording operations, URL helpers, one seed factory, sheet/breadcrumb primitives, one router, shared editor/comments/boost/options components, and centralized delegated actions (`submission/prototype.html:1927-2034`, `submission/prototype.html:2342-2532`, `submission/prototype.html:2807-3185`, `submission/prototype.html:5105-5156`). It has no dependency or build fragility.

Maintainability is capped by the 5k-line global script, mutable global `db`, `ui`, and `ACTIONS`, interleaved templates/business logic/state mutations, broad entity assumptions, lack of automated tests, and route lookup that does not consistently validate record ownership/type against the route. The structure is understandable, though modification safety is weaker than a modular state/view architecture.

### Reliability, safety, and performance — 6.2

The inline script parses successfully. The router provides a visible error fallback, unknown routes get a 404 state, destructive operations ask for confirmation, uploads are size-limited, paste is forced to plain text, and most text labels pass through `esc()` (`submission/prototype.html:1782-1788`, `submission/prototype.html:2482-2528`, `submission/prototype.html:2908-2914`, `submission/prototype.html:3561-3570`, `submission/prototype.html:4277-4295`). With no external dependencies or fetches, startup is resilient to network failure.

Security and integrity deductions are substantial. Rich text is stored from `innerHTML` and later rendered raw in comments, documents, chat, pings, and notes; no sanitizer protects malformed/tampered stored state (`submission/prototype.html:2730`, `submission/prototype.html:2882-2914`, `submission/prototype.html:2921-2933`, `submission/prototype.html:4329-4344`, `submission/prototype.html:4399-4420`, `submission/prototype.html:5050-5062`). Door and editor-created URLs are accepted without scheme validation, enabling dangerous stored links (`submission/prototype.html:2888-2898`, `submission/prototype.html:3490-3507`). The verified copy-ID corruption, shallow deletion, malformed-store startup failure, and silent quota failure further reduce reliability (`submission/prototype.html:1931-1937`, `submission/prototype.html:3085-3103`).

## Summary

This is a high-effort, unusually complete Basecamp frontend prototype. Its strongest qualities are product-model fidelity, breadth of real surfaces, faithful deterministic seed content, and a coherent source-level design system. It behaves like a connected product graph rather than a gallery of static screens. The most consequential weaknesses are state-integrity defects, unsafe persisted rich content and URL handling, incomplete overlay accessibility, and touch-hostile hover-only controls. Browser-process restrictions prevented rendered validation, so the report deliberately avoids claiming pixel-level or runtime interaction observations that were not obtained.
