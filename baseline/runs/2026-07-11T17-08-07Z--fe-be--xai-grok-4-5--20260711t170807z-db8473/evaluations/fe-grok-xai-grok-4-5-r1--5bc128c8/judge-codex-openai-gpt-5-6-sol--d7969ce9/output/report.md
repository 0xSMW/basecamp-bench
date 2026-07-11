# Frontend evaluation report

Submission: `id-5bc128c8`  
Track: `fe`  
Contract: `ba46a54cf7c63bf52be8e1f95814676c8d0c7cbb375354460bbdb1eaf24041a1`

## Evaluation method

- Inspected the complete seed-to-submission delta. The only implementation delta is the new 3,478-line `submission/index.html`; the specification, design contract, agent instructions, SDK material, and nine reference screenshots are unchanged.
- Inspected every route, renderer, state mutation, storage path, keyboard handler, responsive rule, semantic attribute, and explicit/inert control in the single-file SPA.
- Executed the extracted inline JavaScript in a controlled DOM/storage harness. This exercised initial boot, all primary route renderers, optional account routes, a real to-do state transition, persistence through a simulated reload, comment escaping, malformed stored state, and malformed hashes.
- Compared the implementation anatomy and CSS geometry against all nine images under `seed/reference/screens/`.
- Attempted direct browser rendering through a static server, the prescribed browser CLI, the in-app browser, and headless Chromium. The managed environment denied local port binding and Chromium MachPort registration, while the browser CLI/in-app browser were unavailable. No visual behavior is credited solely from an unexecuted claim; visual scores use the supplied screenshots plus the complete HTML/CSS, and the runtime limitation is reflected where direct pixel verification would matter.

## Verified runtime facts

- The inline script parsed and executed. The seed contained 9 people (8 sample), 2 projects, 5 messages, 2 to-do lists/10 to-dos, 7 card columns/13 cards, 3 docs/files, 2 events, and 16 chat lines, matching the required sample graph closely (`submission/index.html:1354-1720`).
- Home, project dock, Message Board/list/detail, To-dos/list/detail, Card Table/list/detail, Docs & Files/list/detail, Chat, Schedule, Activity, Calendar, Reports, Everything, Adminland, Assignments, and Search all produced non-empty route output when invoked with valid IDs (`submission/index.html:1766-1802`, `submission/index.html:2934-3004`). Unknown recording IDs render a Not found state.
- Completing `td_dns` changed `false` to `true`, serialized as `true`, and reloaded as `true` (`submission/index.html:3159-3173`).
- A comment containing `<unsafe>` remained raw in state but rendered as `&lt;unsafe&gt;`, confirming the comment output escape path (`submission/index.html:1844-1856`, `submission/index.html:3265-3288`).
- A stored object containing only `{version: 1}` crashed with `Cannot read properties of undefined (reading 'find')`. A malformed encoded search hash crashed with `URI malformed` (`submission/index.html:1738-1747`, `submission/index.html:1777`).
- Creating a project, reloading, then creating another project produced duplicate `proj_1001` IDs because the in-memory counter resets on boot (`submission/index.html:1307`, `submission/index.html:3068-3084`).
- Bubble Up produced a toast without changing readings, confirming it is visually present but stubbed (`submission/index.html:3325-3328`).

## Dimension scores

### Reference fidelity — 7.0/10

The shell, three-column Home anatomy, centered account/project card, persistent My Bar, Jump menu, project dock, and all screenshot-backed tool layouts closely follow the references (`submission/index.html:1894-2188`, `submission/index.html:2220-2602`; `seed/reference/screens/home.png`, `seed/reference/screens/jump-menu.png`, and the seven `sample-*.png` screens). The implementation uses a 920px `.page-shell` cap where the reference content panel is roughly 1,100px wide, so dense detail and dock surfaces are materially compressed (`submission/index.html:585-586`). Colored initials replace portrait avatars, and many production line icons are approximated with emoji or text (`submission/index.html:1826-1831`, `submission/index.html:2677-2694`). These are conspicuous fidelity losses despite strong anatomy and content matching.

### Visual craft and design-system coherence — 7.2/10

The SPA reproduces the supplied light/dark color ramps, 10px rem convention, typography, spacing, radii, shadows, component sizes, and motion tokens in one coherent system (`submission/index.html:7-441`). Panels, buttons, lists, cards, focus states, modals, sidebars, and dark/light persistence consistently use those tokens. Craft falls short of production polish in native `prompt()` creation flows, placeholder media/avatars, emoji iconography, compressed page geometry, and enabled-looking stub controls. The result is coherent and detailed, with visible prototype roughness.

### Product-model and content fidelity — 8.0/10

The deterministic seed richly models the named account/project/cast and required cross-tool narrative: categories, pinned message, mentions, boosts, comments, subscribers, list completion, assignees, due dates, notify-when-done, subtasks, triage/watchers/on-hold/not-now/done, three file types, events, chat, activity, pings, and readings (`submission/index.html:1354-1720`). Cross-screen project → tool → item links are consistent. The model remains a collection of tool-specific ad hoc objects instead of the shared Recording envelope described by `seed/INIT.md`; account Search and Everything are hardcoded to `proj_launch`, and Assignments includes every open to-do rather than only the current user (`submission/index.html:2968-3003`).

### Surface coverage — 7.5/10

All nine required screenshot-backed surfaces are reachable, including item detail pages. The submission also provides Schedule, Activity, Calendar, Reports, Everything, Adminland, Assignments, Search, My Bar trays, notification sidebar, ping detail, keyboard help, account menu, and empty new-project docks (`submission/index.html:1766-1802`, `submission/index.html:2609-3004`). Secondary breadth is shallow: Reports names several stubbed areas, Check-ins/Forwards/Doors are catalog-only, and there is no folders UI, trash, client/team split, full Pings, profile, event detail/create, or working project settings. `/projects/:id/settings` parses but has no render case and falls to Home.

### Functional depth — 6.5/10

Working and persistent paths include project creation, adding supported tools, message posting, list/to-do/subtask creation, completion, assign-me, card creation/movement/step completion, comments, message boosts, document creation, chat posting, filtering, stars, theme, bookmarks, notes, and navigation (`submission/index.html:3029-3385`). The harness directly verified completion/reload and escaping. Editing and deletion are absent. Bubble Up, Invite, Profile, emoji/attachment, and new event are explicit stubs; comment boost controls render but the handler only mutates messages, and the sidebar filter records input without applying it. Many enabled-looking sort/category/view/tab/options/bookmark/notify controls have no action at all (`submission/index.html:2177-2189`, `submission/index.html:2275-2353`, `submission/index.html:2408-2622`, `submission/index.html:3290-3303`, `submission/index.html:3362-3367`). This is meaningful end-to-end depth with major workflow gaps.

### Interaction quality — 6.1/10

Strong details include discoverable shortcuts, Shift-held hints, Jump autofocus/live filtering, Escape dismissal, filter caret preservation, hover/focus feedback, live-region toasts, outside-click tray closure, and reset/theme controls (`submission/index.html:3000-3015`, `submission/index.html:3362-3465`). Interaction quality is reduced by blocking native prompts, click-only non-focusable rows, numerous inert controls, no edit/delete recovery, and incomplete modal focus behavior. Jump has a `jumpIndex` field but no arrow navigation; Enter simply activates the first item.

### State and persistence — 6.0/10

Most real mutations call `saveState`, and the tested to-do completion survived reload (`submission/index.html:1748-1757`, `submission/index.html:3029-3385`). Theme, stars, notes, bookmarks, created content, and reset all use the same persisted graph. Persistence is not production-shaped: same-version stored data is trusted without schema validation/default merging and can crash immediately; there is no migration path; the boot-reset UID counter causes duplicate IDs after reload; and Bubble Up never persists a state transition (`submission/index.html:1307`, `submission/index.html:1738-1750`, `submission/index.html:3325-3328`).

### Responsive adaptation — 4.0/10

Home stacks below 1,100px, the project dock shifts from three to two to one column, toolbars wrap, and the Card Table has horizontal scrolling (`submission/index.html:622-625`, `submission/index.html:721-722`, `submission/index.html:957-974`). These are useful desktop/tablet adaptations. There is no mobile navigation or touch-specific treatment. The fixed footer retains a three-column grid containing five nowrap center actions plus left account/support and right Pings at every width, which will cause severe narrow-viewport overflow and loss of controls (`submission/index.html:486-525`, `submission/index.html:1908-1923`). The 40rem minimum sidebar and desktop page padding add further mobile pressure.

### Accessibility — 4.8/10

Positive evidence includes `lang`, viewport metadata, a skip link, `main`, breadcrumb navigation labels, a global visible focus ring, native buttons/inputs, an `aria-live="polite"` toast, named Jump/sidebar/tray dialogs, and extensive keyboard shortcuts (`submission/index.html:1-5`, `submission/index.html:442-461`, `submission/index.html:1288-1291`, `submission/index.html:1868-1925`, `submission/index.html:3388-3458`). Material failures remain: standard modals lack `role="dialog"`/`aria-modal`; overlays do not trap or restore focus; only Jump gets initial focus; several click targets are unfocusable divs; form labels and checkbox names are inconsistent; the project star is a button nested inside an anchor; motion has no reduced-motion alternative; and small white text on the bright blue primary token is likely below AA contrast.

### Code architecture and maintainability — 5.5/10

The code has clear Seed/State/Router/Helpers/Views/Actions sections, centralized hash routing/state, delegated events, reusable shell/breadcrumb/avatar/comment/boost helpers, and no dependency or build burden (`submission/index.html:1294-3476`). However, CSS, a large seed graph, state, routing, giant HTML-string renderers, and a roughly 440-line action dispatcher are coupled in one 3,478-line file. Every update replaces the complete shell and overlay DOM (`submission/index.html:2934-3007`), some renderers mutate persistence through `visit()`, and there are no modules, tests, schemas, migrations, or injectable seams.

### Reliability, safety, and performance — 6.0/10

The dependency-free static entry point parsed and executed; invalid item routes have fallback UI; most user-entered titles/body/comments/chat are escaped; external links use `rel="noopener"`; and storage parsing/writes are wrapped in `try/catch` (`submission/index.html:1306`, `submission/index.html:1738-1750`, `submission/index.html:2192-2197`, `submission/index.html:2532-2537`, `submission/index.html:3068-3355`). Direct failure testing exposed malformed-state crashes, malformed hash crashes, and duplicate IDs after reload. Full-app `innerHTML` replacement on each live-filter keystroke is also inefficient and discards DOM/focus state that is only partially reconstructed.

## Summary

This is a substantial, recognizable Basecamp 5 prototype with unusually rich sample data, broad route coverage, coherent dark/light styling, and many genuinely persistent workflows. Its strongest areas are product-model/content fidelity and required-surface anatomy. It remains prototype-shaped in workflow completeness, mobile behavior, accessibility, state validation, and maintainability: editing/deletion are absent, many controls are inert, Bubble Up is cosmetic, narrow screens are poorly supported, and malformed or ordinary reloaded state can expose correctness failures.
