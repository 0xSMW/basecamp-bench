# INIT — Basecamp 5 product specification

Product and domain specification for a **Basecamp 5 clone**. Two deliverables
share this document and the repository reference material:

| Track | Deliverable | Primary sources of truth |
|---|---|---|
| **FE** | Single-file SPA HTML prototype | `DESIGN.md` + `reference/screens/` + this INIT |
| **BE** | Single-file production-ready API | `reference/basecamp-sdk/` + this INIT (domain + seed) |

### Out of scope

- Ruby on Rails, Hotwire, databases, migrations, background workers
- Multi-process deploy, Kamal, real SMTP/inbound email, OAuth SSO, billing
- Building a self-hosted production product plan or milestone roadmap

Implement the requested deliverable (SPA or API). Prefer explicit stubs over
fake success when something is incomplete. When this spec and your instinct
disagree, the spec wins; when the spec is silent, FE checks screenshots and
tokens, BE checks the OpenAPI/SDK pack.

### Sources of truth (precedence)

1. **`reference/basecamp-sdk/`** — `openapi.json` (schemas + 131 path templates /
   203 operations), `SPEC.md`, `behavior-model.json`. Canonical **API contract**
   for the BE track.
2. **`DESIGN.md`** + **`reference/screens/`** — design tokens and 9 dark-mode
   captures of the real app. Canonical **visual contract** for the FE track.
3. **This document** — product model, shell/tools behavior, sample seed, domain
   rules, surface checklists.

---

## 1. Product model in one paragraph

An **Account** has **People** and **Projects**. A project is a container
("bucket") holding a customizable set of **Tools** arranged in a "dock":
Message Board, To-dos, Docs & Files, Chat (Campfire), Schedule, Card Table,
Automatic Check-ins, Email Forwards, and External Links. Every piece of content
inside a project — message, to-do, document, upload, chat line, schedule entry,
card, check-in answer — is a **Recording**: one shared abstraction that carries
identity, creator, timestamps, status (active/archived/trashed), client
visibility, comments, boosts (reactions), subscriptions, and events (an audit
trail). Cross-cutting systems (activity feeds, notifications, search,
"Everything" views, trash) all operate on recordings generically. This is the
single most important architectural fact: **model Recordings correctly and every
tool becomes a thin layer on top.**

---

## 2. Shell and signature interactions

### 2.1 Navigation shell

- **Home** — personalized greeting, action buttons (Make a new project, Add a
  folder, Invite people to the account, Adminland), a centered grid of project
  cards (star to pin/favorite, "All-access" badge, member avatars, folders as
  grouped cards), a "Most recent activity" rail, and "N people active in the
  last 24 hours" with avatars. Reference: `reference/screens/home.png`.
- **Jump menu** (`Shift+J` / `⌘J` / `⌘K`) — primary navigation modal: four tiles
  (Activity `1`, Calendar `2`, Reports `3`, Everything `4`), search field,
  "Recently visited", Projects section (folders + starred first, "See all"),
  footer (My Profile · My Activity · Account & Settings). Typing filters
  instantly; `Ctrl+Enter` searches all projects, `Shift+Enter` searches the
  current project; toggle to include archived. Reference:
  `reference/screens/jump-menu.png`.
- **My Bar** (persistent bottom bar) — My Tasks, My Events, Do Today, My
  Bookmarks, My Notes. Each opens a popover over the current screen (no full
  navigation). Bottom-right: "New for you" pill with unread count. Bottom-center:
  event-reminder toast ~15 min before events (optional polish).
- **Sidebar** (`Shift+S`, "New For You") — slide-over: **Pings**, **Bubbled up**,
  **New for you** (notifications with unread dots), **Previous notifications**,
  live **Filter…**. @mentions get a highlighted badge; every row has **bubble-up**.
- **Breadcrumbs** — `Home ‹ Project ‹ Tool ‹ Item` on content pages; account
  logo/name in the header.

### 2.2 Signature interactions

- **Bubble Up** — personal snooze on a notification/reading: Now / Later today /
  Tomorrow / This weekend / Next week / Pick a date; resurfaces in sidebar
  "Bubbled up".
- **Boosts** — emoji/text reaction chips on recordings, chat lines, comments,
  events.
- **Pings** — account-level DMs (1:1 and group), independent of projects.
- **Live filtering** — nearly every list has a filter field that narrows and
  highlights as you type (no submit).
- **Keyboard-first** — holding `Shift` reveals shortcut badges; full map in §6.
- **Unread tracking** — dots/badges; "Mark all read"; per-item read/unread
  (`Shift+X`).
- **Notification granularity** — per-project and per-tool notify toggles;
  account-wide "Shhh…" (`Shift+Z`).
- **Client visibility** — yellow "👁 The client sees this" badge on
  client-visible recordings/folders; Team / Clients divide on projects.

### 2.3 Tools and screens (inventory)

**Must-match for FE** (screenshot-backed): Home, Jump menu, Project dock,
Message Board, To-dos (+ to-do page), Card Table, Docs & Files, Chat. See §7
for layout anatomy and `reference/screens/sample-*.png`.

**Project page** — header: title + star, description, optional "This is a Sample
Project" badge, people avatars. Toolbar: status/gauge, dates, timesheet pill
(optional), notifications, `…` menu. Activity block, then the **dock**: grid of
tool cards with live previews. Cards are renameable/reorderable; dashed `+`
adds tools. Empty tools show placeholder + CTA. Reference:
`sample-project-dock.png`.

**Message Board** — list with author, category emoji/label (✨ FYI, 📣
Announcement, 💡 Pitch, ❤️ Heartbeat, 👋 Question), excerpt, unread, pins at top.
Message page: rich-text body, boosts, comments, subscription footer. Reference:
`sample-message-board.png`.

**To-dos** — one to-do set → **lists** (completion pie, description) → optional
**groups** → **to-dos** (checkbox, title, assignees, when-done notify, due date,
subtask progress). To-do page: complete, assign, due, notify, notes, **subtasks
(Steps)**, comments. Completed collapse under "N completed". Reference:
`sample-todos.png`, `sample-todo-page.png`.

**Card Table** — **Triage** (renameable) + user **columns** (color, counts,
watchers, optional per-column **On Hold** sub-lane) + built-in **Not Now** and
**Done** edge strips. Cards: title, creator/date, assignees, subtask badge.
Card page: "Move along to…" stepper, assignees, due, notes, steps, comments.
Reference: `sample-card-table.png`.

**Docs & Files** — folders, documents, uploads, cloud links; `+ New…`, type tabs,
sort, filter, bulk actions when selected. Reference: `sample-docs-files.png`.

**Chat** — day-divided timeline of lines (avatar, name suppression for consecutive
lines, boosts, unfurls); composer with post / emoji / attach. Reference:
`sample-chat.png`.

**Optional surfaces** (surface-coverage headroom; not screenshot-required):

| Surface | One-line brief |
|---|---|
| Schedule / Calendar | Month grid; events + due to-dos; project colors; Mine/Everyone |
| Automatic Check-ins | Recurring questions → dated answer log |
| Email Forwards | Inbound email inbox as recordings |
| External links (Doors) | Dock cards out to Figma, Drive, Zoom, … |
| Activity | Timeline + Wrap-up feeds over Events |
| Everything | Account-wide lists: messages, docs, comments, … |
| Reports | Assignments, overdue, Lineup, Hilltop, gauges, timesheet |
| Timesheet | Time entries on project/recording; project header total |
| Pings | Full DM conversation from sidebar |
| Adminland | People, roles, groups, trash, account settings |
| Search | Full-text over recordings with project/type/person filters |
| Project settings | Name, description, dates, gauge/activity toggles |
| My Assignments | Cross-project to-dos/cards assigned to me |

---

## 3. Sample project seed — "Launch the new website"

The product includes a rich, deterministic sample graph. Modeled on Basecamp
5's own seed shape, with original content.

**Theme:** a small fictional team shipping the company site. Project description:
"👋 This is a sample project that shows how a team works together here. Poke
around, click into things — and delete this project whenever you're ready."

**Cast — 8 sample People**, flagged `sample: true` (no login, excluded from plan
limits / real people management; removed with the project if unused elsewhere).
Bundled or placeholder avatars are fine.

| Person | Role | In the seed |
|---|---|---|
| Maya Chen | Project lead | Posts kickoff, creates all cards, runs the show |
| Sam Whitaker | Writer | Copy pitch thread, launch-announcement to-dos, doc draft |
| Omar Haddad | Designer | Logo upload, design comments, card assignee |
| Priya Nair | Developer | DNS/docs to-dos, technical comments |
| Lena Kowalski | Marketing | Traffic heartbeat w/ chart, social to-dos |
| Diego Ramos | Community | Beta-tester FYI, chat banter |
| Grace Okafor | QA | Card watcher, chat participant |
| Felix Berg | Ops | Mostly reacts — boosts + one chat line |

**Content inventory** (T = account / seed creation time; every timestamp is a
**fixed offset from T** — deterministic, no randomness):

| Tool | Seed |
|---|---|
| Message Board (5) | 📢 "Kickoff: the plan" by Maya — **pinned**, @mentions four teammates with their roles, 👏 boosts from all 7 others, 4 comments (two boosted), 5 subscribers · 💡 "Pitch: trim the homepage copy" by Sam — longest thread, 7 comments · ✨ "Nice note from a beta tester" by Diego — quoted feedback, 0 comments · ❤️ "Traffic this week" by Lena — embedded chart image, 0 comments · ❤️ "Local press opportunity" by Maya — 1 comment. Posted T−4d, morning→afternoon |
| To-dos (2 lists) | "Pre-launch checklist" — 2 open ("Set up analytics" with 1 subtask; "Point DNS at the new host" with 1 comment), 2 completed · "Launch week: content" with description — 5 open with assignee chips ("Email newsletter" due **T+3d**, when-done notify Maya), 1 completed |
| Card Table | Triage renamed **"Page ideas"** (2 cards, one with 2 steps; watchers: Maya, Omar, Grace) · "Writing" (2 — one assigned + one **on hold** with 4 steps and 1 comment; on-hold enabled) · "Design" (0) · "Review" (1 — two assignees) · "Ready" (1) · Done (5 — completed T−1d…T) · Not now (2, moved T−4d). Cards created T−20d; a couple of columns colored |
| Docs & Files | Rich-text doc "Homepage copy — draft" (headings + paragraphs, 1 comment) · image upload "logo-concepts.png" · cloud link "Content calendar" (Google-sheet type, placeholder URL). One row color-labeled. Created T−3d |
| Calendar | "Launch day 🚀" all-day, T+7w (Saturday) · "Content review call" T+2w, 10:00–11:00am, 3 participants · the T+3d due to-do appears via due-date overlay |
| Chat | One day (T−4d), ~16 lines, 5 people: opener (🙌 boost) → multi-paragraph reflection → external link + text boost → "heads up, deploying now" → Q/A (👍, 🙏) → customer quote → "Awww! 💖". Includes consecutive lines from one author (name-suppression case) |

**Mechanics**

- Materialize this graph **deterministically on boot** (or first request). Same
  structure every run; prefer stable IDs and fixed timestamp offsets from T.
- **Events are written** so Activity / change logs look real.
- **Readings are not** fanned out to the real viewing user — the sidebar starts
  clean for that person.
- Project is **All-access**; people stack shows the cast (matching B5 sample
  behavior: real user can browse without necessarily being listed as a member).
- No Check-ins / Forwards / External links required in the seed.
- The seed intentionally exercises: pins, categories, @mention chips,
  subscriptions, boosts (message/comment/chat), subtask steps, completions, due
  dates + notify-when-done, on-hold, column watchers/colors, renamed triage,
  cloud links, image uploads, all-day + timed events, calendar due-to-do overlay.

---

## 4. Domain model

Canonical field-level schemas and operations live in
`reference/basecamp-sdk/openapi.json` and `SPEC.md`. This section is the
conceptual model both tracks need.

### 4.1 The Recording pattern

Every content entity shares this envelope (SDK `Recording` — required fields •):

```
id•, status• (active|archived|trashed), visible_to_clients•, inherits_status•,
type• (Message|Todo|Todolist|Document|Upload|Comment|…),
title•, created_at•, updated_at•, creator• (Person),
bucket• {id, name, type:"Project"}, parent {id, title, type, url, app_url},
content (rich text), comments_count, boosts_count, position,
url•, app_url•, bookmark_url, subscription_url
```

- One shared recording identity + tree: parent chain (e.g. message board →
  message → comment; todoset → todolist → group → todo; card table → column →
  card).
- Tool-specific fields live on the concrete type; trash/archive, comments,
  boosts, subscriptions, events, bookmarks, search, and client visibility attach
  to Recording once.
- **Project-content rich text lives on the recording** (`content`); personal
  My Notes are not recordings and own their own body.

### 4.2 Entity catalog

**Account & people**

| Model | Key fields / associations |
|---|---|
| Account | name, logo, settings |
| Person | name, email_address, title, avatar, company; flags: admin, owner, client, employee (collaborator = employee and client both false); lifecycle; caps: can_manage_projects, can_manage_people, can_ping, can_access_timesheet, can_access_hill_charts |
| PersonCompany / ClientCompany | id, name |
| Group | name, members — @mentionable; appears in assignee/notify pickers |
| OutOfOffice | person, starts_on, ends_on, reason |
| Preferences | time_zone, first_week_day, time_format, appearance, notification prefs |

**Project structure**

| Model | Key fields / associations |
|---|---|
| Project ("bucket") | name, description, purpose, status, clients_enabled, bookmarked, starts_on/ends_on, folder; has one dock |
| Calendar (account-level bucket) | optional personal calendar outside projects |
| Tool / DockItem | type (`Message::Board`, `Todoset`, `Vault`, `Schedule`, `Chat::Transcript`, `Kanban::Board`, `Questionnaire`, `Inbox`), title, enabled, position; **multi-instance** allowed |
| Door (external link) | service type, url, title, description, image |
| Folder (home grouping) | name, projects |
| Template + construction | project blueprints (optional for prototypes) |

**Tools & recordables** (all are Recordings unless noted)

| Tool root | Children |
|---|---|
| MessageBoard | Message (subject, content, category → MessageType {name, icon}; pinnable) |
| Todoset (+ optional HillChart) | Todolist → TodolistGroup → Todo (assignees, due_on, completion_subscribers, completed; **subtasks = Steps**) |
| Vault (nestable) | Document, Upload, CloudFile links |
| Campfire (chat) | CampfireLine (content, attachments) |
| Schedule | ScheduleEntry (all_day, starts_at/ends_at, participants, recurrence) |
| Questionnaire | Question (schedule) → QuestionAnswer |
| Inbox (email forwards) | Forward → ForwardReply |
| CardTable | Triage + CardColumn (color, on-hold sub-lane, watchers) + Not Now & Done → Card → Step |
| Gauge | GaugeNeedle — project progress pill |
| TimesheetEntry | date, hours, description, person |
| LineupMarker | account-level name + date |

**Cross-cutting**

| Model | Purpose |
|---|---|
| Comment | Recording; parent = any commentable |
| Boost | content, booster, recording (also on Events) |
| Event | recording_id, action, details, creator — Activity + notifications |
| Subscription | recording ↔ subscribers[] |
| Reading | person-scoped read state over a recording; powers sidebar; **Bubble Up** is an action on a Reading (`resurface_at`) |
| Bookmark | person ↔ recording or tool |
| DoTodayItem | person ↔ todo/card for My Bar "Do Today" |
| Draft | Recording with `status: drafted` (messages/docs) |
| MyNote | person-scoped scratchpad (not a Recording) |
| Ping | account-level conversation + lines (not project API) |
| MyAssignment | derived: my to-dos/cards across projects |
| Webhook / SearchResult | optional ecosystem surfaces |

### 4.3 Status and lifecycle

- `status ∈ {drafted, active, archived, trashed}`; children with
  `inherits_status: true` follow their parent.
- Trash is browsable and restorable; permanent delete is delayed (~25 days in
  product copy) — prototypes may simplify permanent delete.
- **Drafts**: created without publishing → `drafted` (creator-only; no events/
  notifications). Publish → `active` and fire `created` Event.
- Archiving a project freezes it read-only; archived projects excluded from
  default search/jump unless toggled.
- `visible_to_clients` is per-recording (and folders/lists); flipping it is a
  first-class action and shows the client badge.

### 4.4 Roles and authorization (behavioral)

| Role | Intent | Person flags |
|---|---|---|
| **Employee** | Full company members: can create projects, invite, become admins | `employee: true` |
| **Collaborator** (vendor) | Outside partner: work on invited projects only; cannot create projects or be admin | `employee: false, client: false` |
| **Client** | Client access; only `visible_to_clients` content | `client: true` |
| **Admin** | Employee flag: manage people, groups, categories, etc. | `admin` on employee |
| **Owner** | Admin powers + cancel/export/billing-class actions; can access any project | `owner` |

Rules prototypes should respect when implementing auth or UI gating:

1. **Project visibility**: access grant, or all-access + employee, or owner.
   All-access never includes collaborators/clients without invite.
2. **Client filtering**: clients only see `visible_to_clients: true` content
   everywhere (lists, dock, search, feeds).
3. **Create projects / invite to account / add people**: employees (people
   management: admins).
4. **Recording mutations** (reasonable default): members who can see a tool can
   create; structural items editable by members; personal-voice items
   (messages, comments, chat lines, boosts) editable/trashable by creator;
   admins/owners can trash more broadly.

### 4.5 Domain corrections (easy to get wrong)

**Subtasks are Steps — one generic model.** Subtasks on to-dos and cards share
the same Step shape (title, assignees, optional due date, completed). Do **not**
model to-do subtasks as nested todos. Card steps in the SDK (`CardStep`) are the
same concept.

**Card Table mechanics.** Table = **Triage** (renameable) + user columns +
built-in **Not Now** (edge) and **Done** (edge). **On Hold is a per-column
sub-lane**, not a global lane. Card "Move along to…" lists: Triage, each column,
each column's ": On hold", Done, Not now.

**Sidebar / Bubble Up.** Notification rows are **Readings** (person-scoped read
state). Bubble Up is an action on a Reading storing `resurface_at`.

**New projects start empty.** No default tools — only a `+` add-tool affordance.
Add-tool catalog includes Message Board, To-dos, Docs & Files, Calendar
(Schedule), Chat, Card Table, Automatic Check-ins, Email Forwards, External link.

**URL shapes**

- HTML/app style: `/{account_id}/projects/{id}`,
  `/{account_id}/buckets/{project_id}/…` for tools and recordings.
- SDK JSON: match `openapi.json` exactly (including root-level paths like
  `/{account_id}/messages/{id}` and required `.json` suffixes). **No `/api/v1`
  prefix** — the SDK builds `/{accountId}/{path}` on its base URL.

**Rich text** — shared editor concept across messages, docs, comments, chat,
notes. Toolbar checklist for FE: images/files, bold/italic/strike/underline,
headings, color, link, quote, code, lists, table, divider, voice note,
undo/redo. Full production toolbar detail is optional; basic formatting +
@mentions is enough for a strong prototype.

### 4.6 Time zones and dates (short)

- Store timestamps as UTC instants; date-only fields (`due_on`, `starts_on`,
  timesheet date) are plain calendar dates.
- Render times in the **viewer**'s zone; profile/ping headers may show *their*
  local time.
- Timed schedule entries = absolute instants; all-day = dates.
- Bubble-up menu labels are viewer-local; store `resurface_at` as UTC.

---

## 5. Cross-cutting behaviors

Implement as product behavior (in-memory or otherwise) — not as a Rails job
stack:

1. **Auth & tenancy** — at least one account, people, project access, roles.
   BE: Bearer personal access tokens (or equivalent) matching SDK expectations
   where claimed; digest-only storage is ideal. FE: can assume a signed-in
   sample owner browsing the seed.
2. **Recording core** — parent tree, bucket scoping, status lifecycle, client
   visibility, position.
3. **Events** — user-visible mutations write an Event; private state (readings,
   bookmarks, prefs) does not pollute shared Activity.
4. **Subscriptions & @mentions** — subscriber sets; mentions create
   subscriptions / notifications.
5. **Comments + Boosts** — generic on commentable/boostable targets.
6. **Readings, Bubble Up, Bookmarks, unread** — person-scoped state.
7. **Search / live filter** — list filters are client-side or simple string
   match; account search optional.
8. **Calendar semantics** — entries + due-todo overlay when schedule surfaces
   exist.
9. **Explicit stubs** — incomplete operations should fail clearly or be marked
   unsupported (supports BE "scope honesty" and FE "stubbed vs working").

---

## 6. Keyboard map

| Key | Action | Key | Action |
|---|---|---|---|
| `Shift+J` / `⌘J` / `⌘K` | Jump menu | `Shift+S` | Sidebar |
| `Shift+H` | Home | `Shift+P` | New ping |
| `1` / `2` / `3` / `4` | Activity / Calendar / Reports / Everything | `Shift+Z` | Shhh… |
| `Shift+T` / `E` / `V` / `B` / `N` | My Tasks / Events / Do Today / Bookmarks / Notes | `Shift+X` | Mark read/unread |
| `Shift+A` / `Y` / `W` / `O` / `U` | Assignments / My Activity / Drafts / Boosts / Account | `?` | Help |
| `Shift+G` | Back to project | `:wq` | Log out (easter egg) |
| `Shift+M` | New item in current tool | `Shift+F` | Focus filter |
| `Shift+C` | Focus chat/comment | `Shift+K` / `Shift+D` | Assign to me / Mark done |

Holding `Shift` overlays shortcut hint badges on visible controls (FE polish).

---

## 7. Surface catalog (replication checklist)

Layout and control anatomy for the screenshot-backed surfaces. Routes shown are
the production shapes for orientation; FE may use hash/client routes, BE follows
OpenAPI for JSON.

### 7.0 Global shell

1. Skip link → main content.
2. **Main nav**: Home (`Shift+H`); My Stuff (logo) → Assignments / Activity /
   Drafts / Boosts; links Activity · Calendar · Reports · Everything.
3. **`<main>`** — breadcrumbs on tool/recording pages; bookmark toggle; Edit /
   Options (`…`) where applicable.
4. **My Bar** footer: account avatar menu (Profile, Activity, Drafts, Boosts,
   Settings, Log out); My Tasks / Events; Do Today; Bookmarks / Notes.
5. **Sidebar** trigger ("Pings + New for you" + badge); expanded: Pings, bubble-ups,
   New for you, Previous notifications.
6. Live regions for flashes/toasts.

Recurring: **live Filter…** on lists; tool title rename; boost chips on boostable
targets.

### 7.1 Home — `reference/screens/home.png`

Greeting + quick actions (new project, invite, Adminland); optional theme
control; projects grid/list with star and member avatars; recent activity rail.

### 7.2 Project dock — `sample-project-dock.png`

Header (avatars, title, star, description, sample badge). Dock articles per tool
with banner (title + quick-add) and live preview:

- Message Board — pinned + recent messages  
- To-dos — lists + first items / completion  
- Card Table — column strips with counts  
- Docs & Files — file/doc rows  
- Chat — recent lines  
- Calendar — mini month + upcoming (if present)

### 7.3 Message Board — `sample-message-board.png`

Toolbar: New message · Categories · Sort · Filter. Pinned region then list rows
(avatar, title, excerpt, category, date). Default categories: Announcement, FYI,
Heartbeat, Pitch, Question.

**Message page:** title, author, category, body, boosts, comments + composer,
subscription footer ("N people will be notified…").

### 7.4 To-dos — `sample-todos.png`, `sample-todo-page.png`

Toolbar: New list · View as · Filter; optional loose "Add a to-do"; hide
completed. Lists with pie icon, to-do rows (checkbox, title, assignees, due,
subtask badge), "Add a to-do", "N completed".

**To-do page:** title, Mark complete, Assigned to, When done, Due on, Notes,
Subtasks (Steps), comments, subscription footer.

### 7.5 Card Table — `sample-card-table.png`

Add a card · Filter. Layout: Triage · Not now strip · user columns · Done strip.
Column options: watchers, color, on-hold enable, rename, archive. Card tiles with
assignees / step badge; on-hold under in-column divider.

**Card page:** Move along to…, Assigned to, Due on, Notes, Subtasks, comments
(with move events interleaved when present).

### 7.6 Docs & Files — `sample-docs-files.png`

`+ New…` (document, folder, upload, cloud link). Tabs/sort/filter; rows with
type-specific metadata; selection → bulk actions.

### 7.7 Chat — `sample-chat.png`

Day dividers; lines with avatar/name/timestamp/content/boosts; consecutive-author
name suppression; composer (post, emoji, attach).

### 7.8 Jump menu — `jump-menu.png`

Search, four tiles, recently visited, projects (+ stars, include archived).

### 7.9 My Bar trays & sidebar (brief)

| Tray | Empty / content cue |
|---|---|
| My Tasks | Assignments due soon → link to My Assignments |
| My Events | Next 7 days |
| Do Today | Person-flagged items |
| My Bookmarks | Bookmarked recordings/tools |
| My Notes | Personal rich-text scratchpad |

Sidebar expanded: Pings · bubble-up count · New for you · Previous notifications.

### 7.10 Options menu (canonical set, varies by type)

Common recording actions: Bubble up · Move · Copy · Pin/Unpin · Archive · Trash ·
Close comments · Share/Notify · View change log. Tool-specific extras (hill chart
on to-dos, chatbots on chat, calendar subscribe, etc.) are optional polish.

### 7.11 Optional surfaces (one-liners)

- **Schedule / global calendar** — month navigator; day cells with events and
  due to-dos; project color panel on global calendar.
- **Everything** — tiles to account-wide messages / files / comments lists.
- **Reports** — Lineup, gauges, hill charts, overdue, assignments, activity.
- **Activity** — Timeline vs Wrap-up; project/person filters.
- **Adminland** — people, admins, groups, companies, trash, owners tools.
- **Search** — query + project/type/person facets.
- **Project settings** — name, description, dates, progress/activity toggles.

---

## 8. Track-specific notes

### 8.1 Frontend (SPA HTML)

- Single-file (or minimal multi-file) prototype is fine; **zero or few
  dependencies** preferred unless justified.
- Use **`DESIGN.md` tokens** (light default; dark via the documented override
  blocks; 10px root font). Do not invent a parallel palette.
- Match **`reference/screens/`** geometry, density, and component anatomy for
  the must-match surfaces; seed content should match §3 so the narrative
  cross-links (same people, pins, columns, chat day).
- Distinguish **absent / visually present / stubbed / working / persistent** in
  what you ship — working mutations that survive navigation (and ideally reload
  via `localStorage` or similar) are preferable to inert chrome.
- Include responsive reflow and basic accessibility (landmarks, names,
  keyboard, focus).

### 8.2 Backend (single-file API)

- Contract SoT: **`openapi.json`**, **`SPEC.md`**, **`behavior-model.json`**.
  Path templates, methods, response shapes, pagination (`Link` rel=next,
  `X-Total-Count`), and error envelopes should match what you claim to implement.
- Seed §3 on boot; interconnected, navigable, deterministic.
- Prefer **stateful** create/read/update/delete and lifecycle that persist for
  the process lifetime (and optional reset endpoint for tests).
- **Auth**: Bearer tokens per person (or documented equivalent); reject bad
  credentials and unauthorized actions consistently.
- **Validation / hardening**: required fields, unknown IDs, method not allowed,
  sensible 4xx — not only happy paths.
- **Operability**: health check, config via env where natural, CORS if needed,
  graceful shutdown, request logging/IDs, packaging as one runnable file.
- **Scope honesty**: stubbed or unsupported ops should not return misleading
  200 bodies that imply full behavior.

---

## 9. Screenshot index

| File | Surface |
|---|---|
| `reference/screens/home.png` | Home |
| `reference/screens/jump-menu.png` | Jump menu |
| `reference/screens/sample-project-dock.png` | Sample project dock |
| `reference/screens/sample-message-board.png` | Message Board |
| `reference/screens/sample-todos.png` | To-dos set |
| `reference/screens/sample-todo-page.png` | Single to-do |
| `reference/screens/sample-card-table.png` | Card Table |
| `reference/screens/sample-docs-files.png` | Docs & Files |
| `reference/screens/sample-chat.png` | Chat |
