#!/usr/bin/env node
"use strict";

// Dependency-free Basecamp 5 API. Node.js 22+.
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { URL } = require("node:url");

const VERSION = "1.0.0";
const ACCOUNT = "999999";
const ROOT = __dirname;
const SPEC_FILE = path.join(ROOT, "reference/basecamp-sdk/openapi.json");
const SPEC = JSON.parse(fs.readFileSync(SPEC_FILE, "utf8"));
const PORT = integerEnv("PORT", 3000, 0);
const HOST = process.env.HOST || "127.0.0.1";
const ORIGIN = (process.env.BASECAMP_PUBLIC_URL || ("http://" + HOST + ":" + PORT)).replace(/\/$/, "");
const TOKEN = process.env.BASECAMP_TOKEN || "dev-owner-token";
const TOKEN_HASH = hash(TOKEN);
const DATA_FILE = process.env.BASECAMP_DATA_FILE ? path.resolve(process.env.BASECAMP_DATA_FILE) : null;
const BODY_LIMIT = integerEnv("BASECAMP_MAX_BODY_BYTES", 2_000_000, 1);
const RATE_LIMIT = integerEnv("BASECAMP_RATE_LIMIT", 600, 1);
const CORS = new Set((process.env.BASECAMP_CORS_ORIGINS || "").split(",").map(x => x.trim()).filter(Boolean));
const SEED_TIME = Date.parse("2026-06-01T12:00:00.000Z");
if (process.env.BASECAMP_ENV === "production" && TOKEN === "dev-owner-token") {
  throw new Error("BASECAMP_TOKEN must be set in production");
}

function integerEnv(name, fallback, minimum) {
  const value = process.env[name] === undefined ? fallback : Number(process.env[name]);
  if (!Number.isInteger(value) || value < minimum) throw new Error(name + " must be an integer >= " + minimum);
  return value;
}
function hash(value) { return crypto.createHash("sha256").update(value).digest("hex"); }
function equal(a, b) {
  const aa = Buffer.from(a), bb = Buffer.from(b);
  return aa.length === bb.length && crypto.timingSafeEqual(aa, bb);
}
function iso(value = Date.now()) { return new Date(value).toISOString(); }
function at(days = 0, hours = 0, minutes = 0) { return iso(SEED_TIME + days * 864e5 + hours * 36e5 + minutes * 6e4); }
function api(suffix) { return ORIGIN + "/" + ACCOUNT + suffix; }
function copy(value) { return structuredClone(value); }
function pick(item, keys) { return Object.fromEntries(keys.map(key => [key, item[key]])); }
function parent(item) { return pick(item, ["id", "title", "type", "url", "app_url"]); }
function failure(status, error, message, details) {
  const result = new Error(message);
  Object.assign(result, { status, error, details });
  return result;
}
function string(value, field, required = false, maximum = 100000, trim = true) {
  if (value === undefined || value === null) {
    if (required) throw failure(422, "validation_error", field + " is required", { [field]: ["is required"] });
    return "";
  }
  if (typeof value !== "string") throw failure(422, "validation_error", field + " must be a string");
  const out = trim ? value.trim() : value;
  if (required && !out.trim()) throw failure(422, "validation_error", field + " cannot be blank");
  if (out.length > maximum) throw failure(422, "validation_error", field + " is too long");
  return out;
}
function numeric(value, field) {
  if (!/^\d+$/.test(String(value))) throw failure(400, "bad_request", field + " must be numeric");
  return Number(value);
}
function response(status, body = null, headers = {}) { return { status, body, headers }; }
function empty() { return response(204); }

// Compile all 203 method/path pairs from the canonical contract.
const ROUTES = [];
for (const [template, item] of Object.entries(SPEC.paths)) {
  const names = [...template.matchAll(/{([^}]+)}/g)].map(x => x[1]);
  let source = template.replace(/[|\\()[\]^$+*?.]/g, "\\$&");
  for (const name of names) {
    const part = name === "date" ? "[0-9]{4}-[0-9]{2}-[0-9]{2}" : "[0-9]+";
    source = source.replace("{" + name + "}", "(?<" + name + ">" + part + ")");
  }
  const regex = new RegExp("^" + source + "$");
  for (const verb of ["get", "post", "put", "delete"]) {
    if (item[verb]) ROUTES.push({ method: verb.toUpperCase(), template, regex, operation: item[verb] });
  }
}
if (ROUTES.length !== 203) throw new Error("Expected 203 operations; got " + ROUTES.length);

function resolveSchema(schema) {
  if (!schema || !schema.$ref) return schema || {};
  return schema.$ref.slice(2).split("/").reduce((node, part) => node[part], SPEC);
}
function validate(value, original, location = "body") {
  const schema = resolveSchema(original), errors = [], kind = schema.type;
  const valid = kind === "object" ? value && typeof value === "object" && !Array.isArray(value)
    : kind === "array" ? Array.isArray(value)
    : kind === "string" ? typeof value === "string"
    : kind === "integer" ? Number.isInteger(value)
    : kind === "number" ? typeof value === "number" && Number.isFinite(value)
    : kind === "boolean" ? typeof value === "boolean" : true;
  if (!valid) return [location + " must be " + kind];
  if (kind === "object") {
    for (const key of schema.required || []) if (value[key] === undefined || value[key] === null) errors.push(location + "." + key + " is required");
    for (const [key, child] of Object.entries(value)) if (schema.properties && schema.properties[key]) errors.push(...validate(child, schema.properties[key], location + "." + key));
  } else if (kind === "array") {
    value.forEach((child, index) => errors.push(...validate(child, schema.items, location + "[" + index + "]")));
  } else if (kind === "string") {
    if (schema.maxLength !== undefined && value.length > schema.maxLength) errors.push(location + " is too long");
    if (schema.pattern && !(new RegExp(schema.pattern)).test(value)) errors.push(location + " has invalid format");
  }
  if (schema.enum && !schema.enum.includes(value)) errors.push(location + " must be one of " + schema.enum.join(", "));
  return errors;
}

function recordingShape(state, id, type, title, parentItem, creatorId, when, extra = {}) {
  const bucket = parentItem.type === "Project"
    ? { id: parentItem.id, name: parentItem.title, type: "Project" }
    : state.recordings[parentItem.id].bucket;
  const item = {
    id, status: "active", visible_to_clients: false, created_at: when, updated_at: when,
    title, inherits_status: true, type, url: api("/recordings/" + id),
    app_url: "/" + ACCOUNT + "/buckets/" + bucket.id + "/recordings/" + id,
    bookmark_url: api("/recordings/" + id + "/bookmark.json"),
    subscription_url: api("/recordings/" + id + "/subscription.json"),
    comments_url: api("/recordings/" + id + "/comments.json"),
    boosts_url: api("/recordings/" + id + "/boosts.json"),
    comments_count: 0, boosts_count: 0, position: 1, parent: parentItem, bucket,
    creator: state.people[creatorId], ...extra
  };
  state.recordings[id] = item;
  return item;
}
function eventShape(state, item, action, actor, details = {}, created = iso()) {
  const id = state.next_id++;
  state.events[id] = {
    id, recording_id: item.id, action, details, created_at: created, creator: actor,
    boosts_count: 0, boosts_url: api("/recordings/" + item.id + "/events/" + id + "/boosts.json")
  };
}
function commentShape(state, id, targetId, creatorId, content, when) {
  const target = state.recordings[targetId];
  recordingShape(state, id, "Comment", "Re: " + target.title, parent(target), creatorId, when, { content });
  target.comments_count++;
}

function seed() {
  const people = {};
  const rows = [
    [1, "Alex Morgan", "Owner", true, false], [2, "Maya Chen", "Project lead", false, true],
    [3, "Sam Whitaker", "Writer", false, true], [4, "Omar Haddad", "Designer", false, true],
    [5, "Priya Nair", "Developer", false, true], [6, "Lena Kowalski", "Marketing", false, true],
    [7, "Diego Ramos", "Community", false, true], [8, "Grace Okafor", "QA", false, true],
    [9, "Felix Berg", "Ops", false, true]
  ];
  for (const [id, name, title, owner, sample] of rows) {
    people[id] = {
      id, attachable_sgid: "person_" + id, name,
      email_address: name.toLowerCase().replaceAll(" ", ".") + "@example.test",
      personable_type: "User", title, bio: "", location: "", created_at: at(-90),
      updated_at: at(-30), admin: owner, owner, client: false, employee: true,
      time_zone: "Etc/UTC", avatar_url: "https://api.dicebear.com/9.x/initials/svg?seed=" + encodeURIComponent(name),
      company: { id: 1, name: "Acme Studio" }, can_manage_projects: true,
      can_manage_people: owner, can_ping: true, can_access_timesheet: true,
      can_access_hill_charts: true, sample
    };
  }
  const project = {
    id: 1000, status: "active", created_at: at(-30), updated_at: at(),
    name: "Launch the new website",
    description: "👋 This is a sample project that shows how a team works together here. Poke around, click into things — and delete this project whenever you're ready.",
    purpose: "topic", clients_enabled: false, bookmarked: true, all_access: true, sample: true,
    bookmark_url: api("/recordings/1000/bookmark.json"), url: api("/projects/1000"),
    app_url: "/" + ACCOUNT + "/projects/1000", dock: []
  };
  const state = {
    version: 1, next_id: 10000,
    account: { id: Number(ACCOUNT), name: "Acme Studio", product: "bc5", href: ORIGIN + "/" + ACCOUNT, created_at: at(-90), updated_at: at(), logo: null, settings: {} },
    people, projects: { 1000: project }, project_people: { 1000: [2, 3, 4, 5, 6, 7, 8, 9] },
    recordings: {}, events: {}, boosts: {}, subscriptions: {},
    preferences: { 1: { time_zone: "Etc/UTC", first_week_day: "monday", time_format: "twenty_four_hour", appearance: "system" } },
    readings: {}, generic: {}, timesheets: {}, webhooks: {}
  };
  const root = { id: 1000, title: project.name, type: "Project", url: project.url, app_url: project.app_url };
  const tools = [[1100, "Message::Board", "Message Board"], [1200, "Todoset", "To-dos"], [1300, "Kanban::Board", "Card Table"], [1400, "Vault", "Docs & Files"], [1500, "Schedule", "Schedule"], [1600, "Chat::Transcript", "Chat"]];
  tools.forEach(([id, type, title], index) => {
    const item = recordingShape(state, id, type, title, root, 2, at(-30), { name: type, enabled: true, position: index + 1 });
    project.dock.push(pick(item, ["id", "title", "name", "enabled", "position", "url", "app_url"]));
  });
  state.recordings[1500].include_due_assignments = true;
  const categories = [[101, "Announcement", "📣"], [102, "FYI", "✨"], [103, "Pitch", "💡"], [104, "Heartbeat", "❤️"], [105, "Question", "👋"]];
  state.generic.MessageType = Object.fromEntries(categories.map(([id, name, icon]) => [id, { id, name, icon, created_at: at(-90), updated_at: at(-90) }]));
  const board = parent(state.recordings[1100]);
  const messages = [
    [2001, "Kickoff: the plan", 2, 101, "<p>Welcome! @Sam will shape the story, @Omar the design, @Priya the build, and @Lena the launch.</p>", true],
    [2002, "Pitch: trim the homepage copy", 3, 103, "<p>One promise, one proof point, one clear next step. Let the work speak.</p>", false],
    [2003, "Nice note from a beta tester", 7, 102, "<blockquote>It finally feels obvious where to go.</blockquote>", false],
    [2004, "Traffic this week", 6, 104, "<p>Visits are up 18%. <img src=\"/assets/traffic-chart.png\" alt=\"Traffic chart\"></p>", false],
    [2005, "Local press opportunity", 2, 104, "<p>The city desk would like a launch-day briefing.</p>", false]
  ];
  messages.forEach(([id, title, creator, category, content, pinned], i) => {
    recordingShape(state, id, "Message", title, board, creator, at(-4, i), { subject: title, content, category: state.generic.MessageType[category], pinned });
    state.subscriptions[id] = id === 2001 ? [2, 3, 4, 5, 6] : [creator];
  });
  const todoset = parent(state.recordings[1200]);
  [[2100, "Pre-launch checklist", ""], [2200, "Launch week: content", "Everything that must land around announcement day."]]
    .forEach(([id, name, description], index) => recordingShape(state, id, "Todolist", name, todoset, 2, at(-10), { name, description, completed: false, completed_ratio: "0/0", position: index + 1 }));
  const todos = [
    [2110, 2100, "Set up analytics", [5], null, false], [2111, 2100, "Point DNS at the new host", [5, 9], null, false],
    [2112, 2100, "Choose launch URL", [2], null, true], [2113, 2100, "Review privacy copy", [8], null, true],
    [2210, 2200, "Email newsletter", [3, 6], "2026-06-04", false], [2211, 2200, "Publish launch post", [3], null, false],
    [2212, 2200, "Queue social posts", [6, 7], null, false], [2213, 2200, "Prepare support replies", [7], null, false],
    [2214, 2200, "Send press brief", [2, 6], null, false], [2215, 2200, "Draft announcement", [3], null, true]
  ];
  todos.forEach(([id, list, title, assigned, due_on, completed], index) => recordingShape(state, id, "Todo", title, parent(state.recordings[list]), 2, at(-8), {
    content: title, description: "", completed, assignees: assigned.map(x => people[x]),
    completion_subscribers: id === 2210 ? [people[2]] : [], due_on, position: index + 1,
    steps: id === 2110 ? [{ id: 9001, title: "Verify events in production", completed: false, assignees: [people[5]] }] : []
  }));
  const table = parent(state.recordings[1300]);
  const columns = [[2300, "Page ideas", null, false], [2301, "Writing", "blue", true], [2302, "Design", "aqua", false], [2303, "Review", null, false], [2304, "Ready", "green", false], [2305, "Done", null, false], [2306, "Not Now", null, false]];
  columns.forEach(([id, title, color, on_hold], index) => recordingShape(state, id, "Kanban::Column", title, table, 2, at(-20), {
    name: title, color, on_hold, position: index, subscribers: id === 2300 ? [people[2], people[4], people[8]] : []
  }));
  const cardNames = ["Pricing section", "Customer stories", "Homepage headline", "About page", "Responsive QA", "Launch checklist", "Logo lockup", "Footer links", "SEO metadata", "Press kit", "404 page", "Careers copy", "Team photos"];
  const cardColumns = [2300, 2300, 2301, 2301, 2303, 2304, 2305, 2305, 2305, 2305, 2305, 2306, 2306];
  cardNames.forEach((title, index) => recordingShape(state, 2400 + index, "Kanban::Card", title, parent(state.recordings[cardColumns[index]]), 2, at(-20), {
    content: "<p>" + title + " work item.</p>", assignees: title === "Homepage headline" ? [people[4]] : [],
    steps: title === "Pricing section" ? [{ id: 9101, title: "Draft tiers", completed: false }, { id: 9102, title: "Review copy", completed: false }] : [],
    completed: cardColumns[index] === 2305, on_hold: title === "About page", due_on: null
  }));
  state.recordings[2400].steps = [
    recordingShape(state, 2800, "Kanban::Step", "Draft tiers", parent(state.recordings[2400]), 2, at(-19), { completed: false, assignees: [people[3]] }),
    recordingShape(state, 2801, "Kanban::Step", "Review copy", parent(state.recordings[2400]), 2, at(-19), { completed: false, assignees: [people[8]] })
  ];
  state.recordings[2403].steps = [
    recordingShape(state, 2810, "Kanban::Step", "Gather team bios", parent(state.recordings[2403]), 2, at(-19), { completed: true, assignees: [people[3]] }),
    recordingShape(state, 2811, "Kanban::Step", "Select portraits", parent(state.recordings[2403]), 2, at(-19), { completed: false, assignees: [people[4]] }),
    recordingShape(state, 2812, "Kanban::Step", "Draft company story", parent(state.recordings[2403]), 2, at(-19), { completed: false, assignees: [people[3]] }),
    recordingShape(state, 2813, "Kanban::Step", "Legal review", parent(state.recordings[2403]), 2, at(-19), { completed: false, assignees: [people[8]] })
  ];
  const vault = parent(state.recordings[1400]);
  recordingShape(state, 2500, "Document", "Homepage copy — draft", vault, 3, at(-3), { content: "<h1>A calmer way to work</h1><p>Everything your team needs, together.</p>" });
  recordingShape(state, 2501, "Upload", "logo-concepts.png", vault, 4, at(-3), { filename: "logo-concepts.png", content_type: "image/png", byte_size: 184320, download_url: api("/uploads/2501/download/logo-concepts.png") });
  recordingShape(state, 2502, "CloudFile", "Content calendar", vault, 6, at(-3), { service_name: "Google Sheets", external_url: "https://docs.google.com/spreadsheets/d/example" });
  const schedule = parent(state.recordings[1500]);
  recordingShape(state, 2600, "Schedule::Entry", "Launch day 🚀", schedule, 2, at(-2), { summary: "Launch day 🚀", description: "", all_day: true, starts_at: "2026-07-18", ends_at: "2026-07-18", participants: [] });
  recordingShape(state, 2601, "Schedule::Entry", "Content review call", schedule, 2, at(-2), { summary: "Content review call", description: "Final content review.", all_day: false, starts_at: at(14), ends_at: at(14, 1), participants: [people[2], people[3], people[4]] });
  const chat = parent(state.recordings[1600]);
  const lines = ["Morning team 🙌", "The new flow feels much calmer now.", "I tightened the copy and kept the strongest proof point.", "Preview is up: https://example.test/preview", "heads up, deploying now", "Any risk with DNS?", "TTL is already low. We’re good.", "Nice 🙏", "A customer said: “I found everything immediately.”", "That is the whole goal.", "QA pass is green.", "Checking mobile once more.", "Social queue is ready.", "Press brief sent.", "We made it!", "Awww! 💖"];
  const authors = [7, 3, 3, 4, 5, 2, 9, 2, 7, 3, 8, 8, 6, 6, 2, 7];
  lines.forEach((content, index) => recordingShape(state, 2700 + index, "Chat::Line", "", chat, authors[index], at(-4, 0, index * 4), { content, attachments: [] }));
  for (const item of Object.values(state.recordings)) eventShape(state, item, "created", item.creator, {}, item.created_at);
  const comments = [[2001, 3, "Clear plan — I’m on the copy."], [2001, 4, "I’ll share first concepts today."], [2001, 5, "Build path looks solid."], [2001, 6, "Launch calendar is blocked out."], ...Array.from({ length: 7 }, (_, i) => [2002, 2 + i % 7, "Thread note " + (i + 1) + ": keep the page focused."]), [2005, 6, "I can prepare the press notes."], [2111, 5, "DNS values are staged and ready."], [2403, 4, "Holding until the new portraits arrive."], [2500, 2, "The new opening lands well."]];
  comments.forEach(([target, creator, content], index) => commentShape(state, 3000 + index, target, creator, content, at(-3, index)));
  for (let who = 3; who <= 9; who++) {
    const id = 3500 + who;
    state.boosts[id] = { id, content: "👏", created_at: at(-3), booster: people[who], recording: parent(state.recordings[2001]), recording_id: 2001 };
    state.recordings[2001].boosts_count++;
  }
  state.boosts[3599] = { id: 3599, content: "🙌", created_at: at(-4), booster: people[2], recording: parent(state.recordings[2700]), recording_id: 2700 };
  state.recordings[2700].boosts_count++;
  for (const item of Object.values(state.recordings)) {
    for (const [key, value] of Object.entries(item)) if (value === null || value === undefined) delete item[key];
  }
  return state;
}

function loadState() {
  if (!DATA_FILE || !fs.existsSync(DATA_FILE)) return seed();
  const loaded = JSON.parse(fs.readFileSync(DATA_FILE, "utf8"));
  for (const key of ["version", "next_id", "account", "people", "projects", "recordings", "events", "boosts", "subscriptions", "generic"]) {
    if (loaded[key] === undefined) throw new Error("Invalid data file: missing " + key);
  }
  return loaded;
}
let STATE = loadState();
function persist() {
  if (!DATA_FILE) return;
  fs.mkdirSync(path.dirname(DATA_FILE), { recursive: true });
  const temp = DATA_FILE + "." + process.pid + ".tmp";
  fs.writeFileSync(temp, JSON.stringify(STATE), { mode: 0o600 });
  fs.renameSync(temp, DATA_FILE);
}

function authenticate(req) {
  const header = req.headers.authorization || "";
  if (!header.startsWith("Bearer ")) throw failure(401, "unauthorized", "A Bearer access token is required");
  if (!equal(hash(header.slice(7)), TOKEN_HASH)) throw failure(401, "unauthorized", "The access token is invalid");
  return STATE.people[1];
}
const RATE = new Map();
function rateLimit(actor) {
  const now = Date.now(), start = now - 60000, key = String(actor.id);
  const hits = (RATE.get(key) || []).filter(x => x > start);
  if (hits.length >= RATE_LIMIT) throw failure(429, "rate_limit_exceeded", "Too many requests", { retry_after: Math.max(1, Math.ceil((hits[0] + 60000 - now) / 1000)) });
  hits.push(now); RATE.set(key, hits);
  return { remaining: RATE_LIMIT - hits.length, reset: Math.ceil((now + 60000) / 1000) };
}
function nextId() { return STATE.next_id++; }
function getPerson(id) {
  const item = STATE.people[id];
  if (!item) throw failure(404, "not_found", "Person not found");
  return item;
}
function getProject(id) {
  const item = STATE.projects[id];
  if (!item) throw failure(404, "not_found", "Project not found");
  return item;
}
function getRecording(id, actor, mutate = false) {
  const item = STATE.recordings[id];
  if (!item) throw failure(404, "not_found", "Recording not found");
  const project = STATE.projects[item.bucket.id], members = new Set(STATE.project_people[item.bucket.id] || []);
  const access = actor.owner || members.has(actor.id) || (project && project.all_access && actor.employee);
  if (!access || (actor.client && !item.visible_to_clients)) throw failure(404, "not_found", "Recording not found");
  if (mutate && project.status === "archived") throw failure(409, "project_archived", "Archived projects are read-only");
  return item;
}
// Leaf identifiers precede container identifiers for routes containing both.
const RECORD_KEYS = ["lineId", "stepId", "cardId", "todoId", "commentId", "documentId", "uploadId", "entryId", "messageId", "recordingId", "toolId", "columnId", "todolistId", "groupId", "boardId", "todosetId", "vaultId", "campfireId", "scheduleId", "cardTableId", "id"];
function routedRecording(params, actor, mutate = false) {
  const key = RECORD_KEYS.find(x => params[x] !== undefined);
  return getRecording(numeric(params[key], key), actor, mutate);
}
function childRecordings(id, type) {
  return Object.values(STATE.recordings).filter(x => x.parent && x.parent.id === id && (!type || x.type === type));
}
function addEvent(item, action, actor, details = {}) { eventShape(STATE, item, action, actor, details); }
function createRecording(type, title, parentItem, actor, payload = {}) {
  const id = nextId(), now = iso();
  const bucket = parentItem.type === "Project" ? { id: parentItem.id, name: parentItem.title, type: "Project" } : STATE.recordings[parentItem.id].bucket;
  const item = {
    id, status: payload.status || "active", visible_to_clients: Boolean(payload.visible_to_clients),
    created_at: now, updated_at: now, title, inherits_status: true, type,
    url: api("/recordings/" + id), app_url: "/" + ACCOUNT + "/buckets/" + bucket.id + "/recordings/" + id,
    bookmark_url: api("/recordings/" + id + "/bookmark.json"), subscription_url: api("/recordings/" + id + "/subscription.json"),
    comments_url: api("/recordings/" + id + "/comments.json"), boosts_url: api("/recordings/" + id + "/boosts.json"),
    comments_count: 0, boosts_count: 0, position: 1, parent: parent(parentItem), bucket, creator: actor, ...payload
  };
  item.id = id; item.type = type; item.title = title; STATE.recordings[id] = item;
  if (item.status !== "drafted") addEvent(item, "created", actor);
  return item;
}
function paginate(items, query, pathname) {
  const page = Number(query.get("page") || 1), size = Number(query.get("per_page") || 50);
  if (!Number.isInteger(page) || page < 1 || !Number.isInteger(size) || size < 1 || size > 50) throw failure(400, "bad_request", "page must be >= 1 and per_page must be between 1 and 50");
  const start = (page - 1) * size, headers = { "X-Total-Count": String(items.length) }, links = [];
  const pageUrl = number => {
    const url = new URL(ORIGIN + pathname);
    for (const [key, value] of query.entries()) if (key !== "page") url.searchParams.append(key, value);
    url.searchParams.set("page", String(number)); return url.toString();
  };
  if (start + size < items.length) links.push("<" + pageUrl(page + 1) + ">; rel=\"next\"");
  if (page > 1) links.push("<" + pageUrl(page - 1) + ">; rel=\"prev\"");
  if (links.length) headers.Link = links.join(", ");
  return response(200, items.slice(start, start + size), headers);
}
function lifecycle(params, actor, status) {
  const item = routedRecording(params, actor, true); item.status = status; item.updated_at = iso(); addEvent(item, status, actor); return empty();
}
function updateRecording(params, body, actor, allowed) {
  const item = routedRecording(params, actor, true);
  if (["Message", "Comment", "Chat::Line"].includes(item.type) && item.creator.id !== actor.id && !actor.admin) throw failure(403, "forbidden", "Only the creator or an admin may update this recording");
  const aliases = { subject: "title", summary: "title", name: "title", base_name: "title", assignee_ids: "assignees", participant_ids: "participants", completion_subscriber_ids: "completion_subscribers" };
  for (const key of allowed) if (body[key] !== undefined) {
    let value = body[key];
    if (["title", "subject", "summary", "name", "base_name"].includes(key)) value = string(value, key, true, 255);
    if (["content", "description"].includes(key)) value = string(value, key, false, 100000, false);
    if (key.endsWith("_ids")) value = value.map(getPerson);
    item[aliases[key] || key] = value;
    if (aliases[key] && !key.endsWith("_ids")) item[key] = value;
  }
  item.updated_at = iso(); addEvent(item, "updated", actor); return response(200, item);
}

const HANDLERS = {
  GetAccount: () => response(200, STATE.account),
  UpdateAccountName: (p, q, b) => {
    STATE.account.name = string(b.name, "name", true, 255); STATE.account.updated_at = iso(); return response(200, STATE.account);
  },
  GetMyProfile: (p, q, b, actor) => response(200, actor),
  UpdateMyProfile: (p, q, b, actor) => {
    for (const key of ["name", "email_address", "title", "bio", "location", "time_zone"]) if (b[key] !== undefined) actor[key] = string(b[key], key, false, 1000);
    actor.updated_at = iso(); return empty();
  },
  GetMyPreferences: (p, q, b, actor) => response(200, STATE.preferences[actor.id] || {}),
  UpdateMyPreferences: (p, q, b, actor) => {
    STATE.preferences[actor.id] = { ...(STATE.preferences[actor.id] || {}), ...b }; return response(200, STATE.preferences[actor.id]);
  },
  ListPeople: (p, q, b, a, pathname) => paginate(Object.values(STATE.people), q, pathname),
  ListPingablePeople: (p, q, b, a, pathname) => paginate(Object.values(STATE.people).filter(x => x.can_ping), q, pathname),
  GetPerson: p => response(200, getPerson(numeric(p.personId, "personId"))),
  ListProjects: (p, q, b, a, pathname) => {
    const status = q.get("status") || "active";
    if (!["active", "archived", "trashed"].includes(status)) throw failure(400, "bad_request", "status must be active, archived, or trashed");
    return paginate(Object.values(STATE.projects).filter(x => x.status === status), q, pathname);
  },
  CreateProject: (p, q, b, actor) => {
    if (!actor.employee) throw failure(403, "forbidden", "Employees are required to create projects");
    const id = nextId(), now = iso(), name = string(b.name, "name", true, 255);
    const item = { id, status: "active", created_at: now, updated_at: now, name, description: string(b.description || "", "description", false, 100000, false), purpose: "topic", clients_enabled: false, bookmarked: false, all_access: false, dock: [], bookmark_url: api("/recordings/" + id + "/bookmark.json"), url: api("/projects/" + id), app_url: "/" + ACCOUNT + "/projects/" + id };
    STATE.projects[id] = item; STATE.project_people[id] = [actor.id]; return response(201, item, { Location: item.url });
  },
  GetProject: p => response(200, getProject(numeric(p.projectId, "projectId"))),
  UpdateProject: (p, q, b) => {
    const item = getProject(numeric(p.projectId, "projectId"));
    if (b.name !== undefined) item.name = string(b.name, "name", true, 255);
    if (b.description !== undefined) item.description = string(b.description, "description", false, 100000, false);
    item.updated_at = iso(); return response(200, item);
  },
  TrashProject: p => { const item = getProject(numeric(p.projectId, "projectId")); item.status = "trashed"; item.updated_at = iso(); return empty(); },
  ListProjectPeople: (p, q, b, a, pathname) => paginate((STATE.project_people[numeric(p.projectId, "projectId")] || []).map(getPerson), q, pathname),
  UpdateProjectAccess: (p, q, b) => {
    const id = numeric(p.projectId, "projectId"); getProject(id);
    const grant = b.grant || b.user_ids || [], revoke = b.revoke || []; grant.forEach(getPerson);
    const current = new Set(STATE.project_people[id] || []); grant.forEach(x => current.add(x)); revoke.forEach(x => current.delete(x));
    STATE.project_people[id] = [...current]; return response(200, { granted: grant, revoked: revoke });
  },
  ListRecordings: (p, q, b, actor, pathname) => {
    const type = q.get("type"); if (!type) throw failure(400, "bad_request", "type is required");
    const status = q.get("status") || "active", bucket = q.get("bucket") || q.get("bucket_id");
    let items = Object.values(STATE.recordings).filter(x => x.type === type && x.status === status && (!bucket || String(x.bucket.id) === bucket));
    if (actor.client) items = items.filter(x => x.visible_to_clients);
    return paginate(items, q, pathname);
  },
  GetProjectTimeline: (p, q, b, a, pathname) => {
    const id = numeric(p.projectId, "projectId"); getProject(id);
    return paginate(Object.values(STATE.events).filter(x => STATE.recordings[x.recording_id] && STATE.recordings[x.recording_id].bucket.id === id).sort((x, y) => y.created_at.localeCompare(x.created_at)), q, pathname);
  },
  GetRecording: (p, q, b, a) => response(200, routedRecording(p, a)),
  SetClientVisibility: (p, q, b, a) => {
    const item = routedRecording(p, a, true); item.visible_to_clients = b.visible_to_clients; item.updated_at = iso(); addEvent(item, "client_visibility_changed", a); return response(200, item);
  },
  ArchiveRecording: (p, q, b, a) => lifecycle(p, a, "archived"),
  UnarchiveRecording: (p, q, b, a) => lifecycle(p, a, "active"),
  TrashRecording: (p, q, b, a) => lifecycle(p, a, "trashed"),
  ListComments: (p, q, b, a, pathname) => paginate(childRecordings(routedRecording(p, a).id, "Comment").filter(x => x.status === "active"), q, pathname),
  CreateComment: (p, q, b, a) => {
    const target = routedRecording(p, a, true), content = string(b.content, "content", true, 100000, false);
    const item = createRecording("Comment", "Re: " + target.title, target, a, { content }); target.comments_count++;
    return response(201, item, { Location: item.url });
  },
  GetComment: (p, q, b, a) => response(200, routedRecording(p, a)),
  UpdateComment: (p, q, b, a) => updateRecording(p, b, a, ["content"]),
  ListRecordingBoosts: (p, q, b, a, pathname) => {
    const target = routedRecording(p, a); return paginate(Object.values(STATE.boosts).filter(x => x.recording_id === target.id), q, pathname);
  },
  CreateRecordingBoost: (p, q, b, a) => {
    const target = routedRecording(p, a, true), id = nextId(), content = string(b.content, "content", true, 16);
    const item = { id, content, created_at: iso(), booster: a, recording: parent(target), recording_id: target.id };
    STATE.boosts[id] = item; target.boosts_count++; addEvent(target, "boosted", a); return response(201, item);
  },
  GetBoost: p => { const item = STATE.boosts[numeric(p.boostId, "boostId")]; if (!item) throw failure(404, "not_found", "Boost not found"); return response(200, item); },
  DeleteBoost: (p, q, b, a) => {
    const id = numeric(p.boostId, "boostId"), item = STATE.boosts[id]; if (!item) throw failure(404, "not_found", "Boost not found");
    if (item.booster.id !== a.id && !a.admin) throw failure(403, "forbidden", "Only the booster or an admin may delete this boost");
    delete STATE.boosts[id]; return empty();
  },
  ListEvents: (p, q, b, a, pathname) => { const target = routedRecording(p, a); return paginate(Object.values(STATE.events).filter(x => x.recording_id === target.id), q, pathname); },
  GetSubscription: (p, q, b, a) => {
    const target = routedRecording(p, a), ids = STATE.subscriptions[target.id] || [];
    return response(200, { subscribed: ids.includes(a.id), subscribers: ids.map(getPerson) });
  },
  Subscribe: (p, q, b, a) => {
    const target = routedRecording(p, a, true), ids = STATE.subscriptions[target.id] ||= [];
    if (!ids.includes(a.id)) ids.push(a.id); return empty();
  },
  Unsubscribe: (p, q, b, a) => {
    const target = routedRecording(p, a, true), ids = STATE.subscriptions[target.id] ||= [];
    STATE.subscriptions[target.id] = ids.filter(x => x !== a.id); return empty();
  },
  UpdateSubscription: (p, q, b, a) => {
    const target = routedRecording(p, a, true), ids = b.subscribers || b.subscriber_ids || [];
    ids.forEach(getPerson); STATE.subscriptions[target.id] = [...new Set(ids)]; return response(200, { subscribers: ids.map(getPerson) });
  },
  GetTool: (p, q, b, a) => response(200, routedRecording(p, a)),
  UpdateTool: (p, q, b, a) => updateRecording(p, b, a, ["title"]),
  DeleteTool: (p, q, b, a) => lifecycle(p, a, "trashed"),
  EnableTool: (p, q, b, a) => { routedRecording(p, a, true).enabled = true; return empty(); },
  DisableTool: (p, q, b, a) => { routedRecording(p, a, true).enabled = false; return empty(); },
  RepositionTool: (p, q, b, a) => { routedRecording(p, a, true).position = b.position; return empty(); },
  GetMessageBoard: (p, q, b, a) => response(200, routedRecording(p, a)),
  ListMessages: (p, q, b, a, pathname) => {
    const board = routedRecording(p, a), key = q.get("sort") || "created_at", direction = q.get("direction") || "desc";
    return paginate(childRecordings(board.id, "Message").sort((x, y) => (direction === "desc" ? -1 : 1) * String(x[key]).localeCompare(String(y[key]))), q, pathname);
  },
  CreateMessage: (p, q, b, a) => {
    const board = routedRecording(p, a, true), subject = string(b.subject, "subject", true, 255);
    if (b.status && !["active", "drafted"].includes(b.status)) throw failure(422, "validation_error", "status must be active or drafted");
    const item = createRecording("Message", subject, board, a, { subject, content: string(b.content || "", "content", false, 100000, false), status: b.status || "active", category: b.category_id ? STATE.generic.MessageType[b.category_id] : undefined });
    STATE.subscriptions[item.id] = b.subscriptions || [a.id]; return response(201, item, { Location: item.url });
  },
  GetMessage: (p, q, b, a) => response(200, routedRecording(p, a)),
  UpdateMessage: (p, q, b, a) => updateRecording(p, b, a, ["subject", "content"]),
  PinMessage: (p, q, b, a) => { const item = routedRecording(p, a, true); item.pinned = true; addEvent(item, "pinned", a); return empty(); },
  UnpinMessage: (p, q, b, a) => { const item = routedRecording(p, a, true); item.pinned = false; addEvent(item, "unpinned", a); return empty(); },
  GetTodoset: (p, q, b, a) => response(200, routedRecording(p, a)),
  ListTodolists: (p, q, b, a, pathname) => paginate(childRecordings(routedRecording(p, a).id, "Todolist"), q, pathname),
  CreateTodolist: (p, q, b, a) => {
    const owner = routedRecording(p, a, true), name = string(b.name, "name", true, 255);
    return response(201, createRecording("Todolist", name, owner, a, { name, description: string(b.description || "", "description", false, 100000, false), completed: false, completed_ratio: "0/0" }));
  },
  GetTodolistOrGroup: (p, q, b, a) => response(200, routedRecording(p, a)),
  UpdateTodolistOrGroup: (p, q, b, a) => updateRecording(p, b, a, ["name", "description"]),
  ListTodos: (p, q, b, a, pathname) => {
    let items = childRecordings(routedRecording(p, a).id, "Todo"), completed = q.get("completed");
    if (completed !== null) items = items.filter(x => Boolean(x.completed) === (completed === "true"));
    return paginate(items, q, pathname);
  },
  CreateTodo: (p, q, b, a) => {
    const owner = routedRecording(p, a, true), content = string(b.content, "content", true, 100000, false);
    return response(201, createRecording("Todo", content, owner, a, { content, description: string(b.description || "", "description", false, 100000, false), completed: false, assignees: (b.assignee_ids || []).map(getPerson), completion_subscribers: (b.completion_subscriber_ids || []).map(getPerson), due_on: b.due_on || null, starts_on: b.starts_on || null, steps: [] }));
  },
  GetTodo: (p, q, b, a) => response(200, routedRecording(p, a)),
  UpdateTodo: (p, q, b, a) => updateRecording(p, b, a, ["content", "description", "due_on", "starts_on", "assignee_ids", "completion_subscriber_ids"]),
  TrashTodo: (p, q, b, a) => lifecycle(p, a, "trashed"),
  CompleteTodo: (p, q, b, a) => { const item = routedRecording(p, a, true); item.completed = true; item.completed_at = iso(); item.completer = a; addEvent(item, "completed", a); return empty(); },
  UncompleteTodo: (p, q, b, a) => { const item = routedRecording(p, a, true); item.completed = false; item.completed_at = null; item.completer = null; addEvent(item, "uncompleted", a); return empty(); },
  RepositionTodo: (p, q, b, a) => { routedRecording(p, a, true).position = b.position; return empty(); },
  GetCardTable: (p, q, b, a) => { const item = copy(routedRecording(p, a)); item.lists = childRecordings(item.id, "Kanban::Column"); return response(200, item); },
  GetCardColumn: (p, q, b, a) => response(200, routedRecording(p, a)),
  CreateCardColumn: (p, q, b, a) => {
    const owner = routedRecording(p, a, true), title = string(b.title, "title", true, 255);
    return response(201, createRecording("Kanban::Column", title, owner, a, { name: title, color: b.color || null, on_hold: false }));
  },
  UpdateCardColumn: (p, q, b, a) => updateRecording(p, b, a, ["title"]),
  SetCardColumnColor: (p, q, b, a) => { const item = routedRecording(p, a, true); item.color = b.color; return response(200, item); },
  EnableCardColumnOnHold: (p, q, b, a) => { routedRecording(p, a, true).on_hold = true; return response(200, { enabled: true }); },
  DisableCardColumnOnHold: (p, q, b, a) => { routedRecording(p, a, true).on_hold = false; return response(200, { enabled: false }); },
  ListCards: (p, q, b, a, pathname) => paginate(childRecordings(routedRecording(p, a).id, "Kanban::Card"), q, pathname),
  CreateCard: (p, q, b, a) => {
    const owner = routedRecording(p, a, true), title = string(b.title, "title", true, 255);
    return response(201, createRecording("Kanban::Card", title, owner, a, { content: string(b.content || "", "content", false, 100000, false), due_on: b.due_on || null, assignees: [], steps: [], completed: false, on_hold: false }));
  },
  GetCard: (p, q, b, a) => response(200, routedRecording(p, a)),
  UpdateCard: (p, q, b, a) => updateRecording(p, b, a, ["title", "content", "due_on"]),
  MoveCard: (p, q, b, a) => {
    const item = routedRecording(p, a, true), column = getRecording(b.column_id, a, true);
    if (column.type !== "Kanban::Column" || column.bucket.id !== item.bucket.id) throw failure(422, "validation_error", "column_id must identify a column in this project");
    item.parent = parent(column); item.on_hold = Boolean(b.on_hold); item.updated_at = iso(); addEvent(item, "moved", a, { column_id: column.id }); return empty();
  },
  CreateCardStep: (p, q, b, a) => {
    const card = routedRecording(p, a, true), title = string(b.title, "title", true, 255);
    const item = createRecording("Kanban::Step", title, card, a, { due_on: b.due_on || null, assignees: (b.assignee_ids || []).map(getPerson), completed: false });
    card.steps ||= []; card.steps.push(item); return response(201, item);
  },
  GetCardStep: (p, q, b, a) => response(200, routedRecording(p, a)),
  UpdateCardStep: (p, q, b, a) => updateRecording(p, b, a, ["title", "due_on", "assignee_ids"]),
  SetCardStepCompletion: (p, q, b, a) => { const item = routedRecording(p, a, true); item.completed = Boolean(b.completion ?? b.completed); addEvent(item, item.completed ? "completed" : "uncompleted", a); return response(200, item); },
  RepositionCardStep: (p, q, b, a) => {
    const card = routedRecording(p, a, true), index = (card.steps || []).findIndex(x => x.id === b.source_id);
    if (index < 0) throw failure(404, "not_found", "Card step not found");
    const [step] = card.steps.splice(index, 1); card.steps.splice(Math.max(0, b.position - 1), 0, step); addEvent(card, "repositioned", a); return empty();
  },
  MoveCardColumn: (p, q, b, a) => {
    const table = routedRecording(p, a, true), source = getRecording(b.source_id, a, true), target = getRecording(b.target_id, a, true);
    if (source.parent.id !== table.id || target.parent.id !== table.id) throw failure(422, "validation_error", "Columns must belong to this card table");
    source.position = b.position ?? target.position; addEvent(source, "repositioned", a); return empty();
  },
  GetVault: (p, q, b, a) => response(200, routedRecording(p, a)),
  UpdateVault: (p, q, b, a) => updateRecording(p, b, a, ["title"]),
  ListDocuments: (p, q, b, a, pathname) => paginate(childRecordings(routedRecording(p, a).id, "Document"), q, pathname),
  CreateDocument: (p, q, b, a) => { const owner = routedRecording(p, a, true), title = string(b.title, "title", true, 255); return response(201, createRecording("Document", title, owner, a, { content: string(b.content || "", "content", false, 100000, false), status: b.status || "active" })); },
  GetDocument: (p, q, b, a) => response(200, routedRecording(p, a)),
  UpdateDocument: (p, q, b, a) => updateRecording(p, b, a, ["title", "content"]),
  ListUploads: (p, q, b, a, pathname) => paginate(childRecordings(routedRecording(p, a).id).filter(x => ["Upload", "CloudFile"].includes(x.type)), q, pathname),
  CreateUpload: (p, q, b, a) => { const owner = routedRecording(p, a, true), title = string(b.base_name || b.attachable_sgid, "base_name", true, 255); return response(201, createRecording("Upload", title, owner, a, { attachable_sgid: b.attachable_sgid, description: b.description || "", filename: title, content_type: "application/octet-stream", byte_size: 0 })); },
  GetUpload: (p, q, b, a) => response(200, routedRecording(p, a)),
  UpdateUpload: (p, q, b, a) => updateRecording(p, b, a, ["description", "base_name"]),
  ListUploadVersions: (p, q, b, a, pathname) => paginate([routedRecording(p, a)], q, pathname),
  ListVaults: (p, q, b, a, pathname) => paginate(childRecordings(routedRecording(p, a).id, "Vault"), q, pathname),
  CreateVault: (p, q, b, a) => { const owner = routedRecording(p, a, true), title = string(b.title, "title", true, 255); return response(201, createRecording("Vault", title, owner, a, {})); },
  ListCampfires: (p, q, b, a, pathname) => paginate(Object.values(STATE.recordings).filter(x => x.type === "Chat::Transcript"), q, pathname),
  GetCampfire: (p, q, b, a) => response(200, routedRecording(p, a)),
  ListCampfireLines: (p, q, b, a, pathname) => paginate(childRecordings(routedRecording(p, a).id, "Chat::Line").sort((x, y) => y.created_at.localeCompare(x.created_at)), q, pathname),
  CreateCampfireLine: (p, q, b, a) => { const owner = routedRecording(p, a, true), content = string(b.content, "content", true, 100000, false); return response(201, createRecording("Chat::Line", "", owner, a, { content, attachments: [] })); },
  GetCampfireLine: (p, q, b, a) => response(200, routedRecording(p, a)),
  DeleteCampfireLine: (p, q, b, a) => lifecycle(p, a, "trashed"),
  GetSchedule: (p, q, b, a) => response(200, routedRecording(p, a)),
  UpdateScheduleSettings: (p, q, b, a) => updateRecording(p, b, a, ["include_due_assignments"]),
  ListScheduleEntries: (p, q, b, a, pathname) => {
    let items = childRecordings(routedRecording(p, a).id, "Schedule::Entry"), start = q.get("start_date") || q.get("starts_at"), end = q.get("end_date") || q.get("ends_at");
    if (start) items = items.filter(x => String(x.ends_at) >= start); if (end) items = items.filter(x => String(x.starts_at) <= end);
    return paginate(items, q, pathname);
  },
  CreateScheduleEntry: (p, q, b, a) => {
    const owner = routedRecording(p, a, true), summary = string(b.summary, "summary", true, 255);
    if (String(b.ends_at) < String(b.starts_at)) throw failure(422, "validation_error", "ends_at must be after starts_at");
    return response(201, createRecording("Schedule::Entry", summary, owner, a, { summary, description: string(b.description || "", "description", false, 100000, false), starts_at: b.starts_at, ends_at: b.ends_at, all_day: Boolean(b.all_day), participants: (b.participant_ids || []).map(getPerson) }));
  },
  GetScheduleEntry: (p, q, b, a) => response(200, routedRecording(p, a)),
  GetScheduleEntryOccurrence: (p, q, b, a) => { const item = copy(routedRecording(p, a)); item.starts_at = p.date; return response(200, item); },
  UpdateScheduleEntry: (p, q, b, a) => updateRecording(p, b, a, ["summary", "description", "starts_at", "ends_at", "all_day", "participant_ids"]),
  GetMyAssignments: (p, q, b, a, pathname) => assignments(q, a, pathname),
  GetMyCompletedAssignments: (p, q, b, a, pathname) => assignments(q, a, pathname, true),
  GetMyDueAssignments: (p, q, b, a, pathname) => assignments(q, a, pathname, false),
  GetOverdueTodos: (p, q, b, a, pathname) => { const today = iso().slice(0, 10); return paginate(Object.values(STATE.recordings).filter(x => x.type === "Todo" && x.due_on && x.due_on < today && !x.completed), q, pathname); },
  Search: (p, q, b, a, pathname) => {
    const term = (q.get("q") || "").toLowerCase(); if (!term) throw failure(400, "bad_request", "q is required");
    let items = Object.values(STATE.recordings).filter(x => x.status === "active" && ((x.title || "") + " " + (x.content || "")).toLowerCase().includes(term));
    if (a.client) items = items.filter(x => x.visible_to_clients); return paginate(items, q, pathname);
  },
  GetSearchMetadata: () => response(200, { recording_types: [...new Set(Object.values(STATE.recordings).map(x => x.type))].sort(), projects: Object.values(STATE.projects), people: Object.values(STATE.people) }),
  GetMyNotifications: (p, q, b, a, pathname) => paginate(Object.values(STATE.readings).filter(x => x.person_id === a.id), q, pathname),
  MarkAsRead: (p, q, b, a) => { for (const id of b.readable_ids || b.ids || []) STATE.readings[a.id + ":" + id] = { recording_id: id, person_id: a.id, read: true, updated_at: iso() }; return empty(); },
  ListMessageTypes: (p, q, b, a, pathname) => paginate(Object.values(STATE.generic.MessageType), q, pathname),
  GetMessageType: p => { const item = STATE.generic.MessageType[numeric(p.typeId, "typeId")]; if (!item) throw failure(404, "not_found", "Message type not found"); return response(200, item); },
  CreateMessageType: (p, q, b) => { const id = nextId(), now = iso(), item = { id, name: string(b.name, "name", true, 255), icon: string(b.icon || "", "icon", false, 16), created_at: now, updated_at: now }; STATE.generic.MessageType[id] = item; return response(201, item); },
  UpdateMessageType: (p, q, b) => { const item = STATE.generic.MessageType[numeric(p.typeId, "typeId")]; if (!item) throw failure(404, "not_found", "Message type not found"); if (b.name !== undefined) item.name = string(b.name, "name", true, 255); if (b.icon !== undefined) item.icon = string(b.icon, "icon", false, 16); item.updated_at = iso(); return response(200, item); },
  DeleteMessageType: p => { const id = numeric(p.typeId, "typeId"); if (!STATE.generic.MessageType[id]) throw failure(404, "not_found", "Message type not found"); delete STATE.generic.MessageType[id]; return empty(); },
  ListTodolistGroups: (p, q, b, a, pathname) => paginate(childRecordings(routedRecording(p, a).id, "Todolist::Group"), q, pathname),
  CreateTodolistGroup: (p, q, b, a) => { const owner = routedRecording(p, a, true), name = string(b.name, "name", true, 255); return response(201, createRecording("Todolist::Group", name, owner, a, { name })); },
  RepositionTodolistGroup: (p, q, b, a) => { routedRecording(p, a, true).position = b.position; return empty(); },
  GetOutOfOffice: p => {
    const who = getPerson(numeric(p.personId, "personId"));
    if (!who.out_of_office) throw failure(404, "not_found", "Out of office period not found");
    return response(200, who.out_of_office);
  },
  EnableOutOfOffice: (p, q, b) => {
    const who = getPerson(numeric(p.personId, "personId")); who.out_of_office = { enabled: true, person: who, ...b.out_of_office }; return response(201, who.out_of_office);
  },
  DisableOutOfOffice: p => { delete getPerson(numeric(p.personId, "personId")).out_of_office; return empty(); },
  SubscribeToCardColumn: (p, q, b, a) => {
    const item = routedRecording(p, a, true), ids = STATE.subscriptions[item.id] ||= []; if (!ids.includes(a.id)) ids.push(a.id); return empty();
  },
  UnsubscribeFromCardColumn: (p, q, b, a) => {
    const item = routedRecording(p, a, true); STATE.subscriptions[item.id] = (STATE.subscriptions[item.id] || []).filter(x => x !== a.id); return empty();
  },
  ListAssignablePeople: (p, q, b, a, pathname) => paginate(Object.values(STATE.people), q, pathname),
  GetAssignedTodos: (p, q) => {
    const id = numeric(p.personId, "personId"); getPerson(id);
    return response(200, { person: getPerson(id), grouped_by: q.get("grouped_by") || "project", todos: Object.values(STATE.recordings).filter(x => x.type === "Todo" && (x.assignees || []).some(y => y.id === id)) });
  },
  GetUpcomingSchedule: () => response(200, { schedule_entries: Object.values(STATE.recordings).filter(x => x.type === "Schedule::Entry" && String(x.ends_at) >= iso()), recurring_schedule_entry_occurrences: [], assignables: [] }),
  GetProgressReport: (p, q, b, a, pathname) => paginate(Object.values(STATE.events).sort((x, y) => y.created_at.localeCompare(x.created_at)), q, pathname),
  GetQuestionReminders: (p, q, b, a, pathname) => paginate([], q, pathname)
};
function assignments(query, actor, pathname, completed) {
  let items = Object.values(STATE.recordings).filter(x => ["Todo", "Kanban::Card"].includes(x.type) && (x.assignees || []).some(y => y.id === actor.id));
  if (completed !== undefined) items = items.filter(x => Boolean(x.completed) === completed);
  return paginate(items, query, pathname);
}

function match(method, pathname) {
  const pathMatches = ROUTES.filter(route => route.regex.test(pathname));
  const found = pathMatches.find(route => route.method === method);
  if (!found) {
    if (pathMatches.length) {
      const error = failure(405, "method_not_allowed", "Method not allowed");
      error.allow = [...new Set(pathMatches.map(x => x.method))].sort().join(", ");
      throw error;
    }
    throw failure(404, "not_found", "Route not found");
  }
  return { ...found, params: found.regex.exec(pathname).groups || {} };
}
function validateParameters(operation, routeParams, query) {
  const issues = [];
  for (const parameter of operation.parameters || []) {
    const raw = parameter.in === "path" ? routeParams[parameter.name] : query.get(parameter.name);
    if ((raw === undefined || raw === null || raw === "") && parameter.required) {
      issues.push(parameter.in + "." + parameter.name + " is required"); continue;
    }
    if (raw === undefined || raw === null) continue;
    const schema = resolveSchema(parameter.schema);
    if (schema.type === "integer" && !/^-?\d+$/.test(raw)) issues.push(parameter.in + "." + parameter.name + " must be integer");
    if (schema.type === "boolean" && !["true", "false"].includes(raw)) issues.push(parameter.in + "." + parameter.name + " must be boolean");
    if (schema.pattern && !(new RegExp(schema.pattern)).test(raw)) issues.push(parameter.in + "." + parameter.name + " has invalid format");
    if (schema.enum && !schema.enum.includes(raw)) issues.push(parameter.in + "." + parameter.name + " must be one of " + schema.enum.join(", "));
  }
  if (issues.length) throw failure(400, "bad_request", "Request parameter validation failed", issues);
}
async function readBody(req, operation) {
  if (!["POST", "PUT"].includes(req.method)) return null;
  const bodySpec = operation.requestBody, required = Boolean(bodySpec && bodySpec.required);
  if (req.headers["content-length"] === undefined) {
    if (required) throw failure(422, "validation_error", "A request body is required");
    return null;
  }
  const length = Number(req.headers["content-length"]);
  if (!Number.isInteger(length) || length < 0) throw failure(400, "bad_request", "Invalid Content-Length");
  if (length > BODY_LIMIT) throw failure(413, "payload_too_large", "Request body exceeds " + BODY_LIMIT + " bytes");
  if (length === 0) {
    if (required) throw failure(422, "validation_error", "A request body is required");
    return null;
  }
  const media = String(req.headers["content-type"] || "").split(";")[0].trim().toLowerCase();
  const content = bodySpec && bodySpec.content || {};
  if (media !== "application/json") {
    if (content[media]) throw failure(501, "not_implemented", "Binary and multipart upload bodies are registered but are not implemented by this release");
    throw failure(415, "unsupported_media_type", "Content-Type must be application/json");
  }
  const chunks = []; let total = 0;
  for await (const chunk of req) {
    total += chunk.length;
    if (total > BODY_LIMIT) throw failure(413, "payload_too_large", "Request body exceeds " + BODY_LIMIT + " bytes");
    chunks.push(chunk);
  }
  let body;
  try { body = JSON.parse(Buffer.concat(chunks).toString("utf8")); }
  catch { throw failure(400, "bad_request", "Malformed JSON request body"); }
  const schema = content["application/json"] && content["application/json"].schema;
  const errors = validate(body, schema);
  if (errors.length) throw failure(422, "validation_error", "Request validation failed", errors);
  return body;
}
function commonHeaders(req, requestId) {
  const headers = {
    "X-Request-ID": requestId, "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer", "Cache-Control": "private, no-cache"
  };
  const origin = req.headers.origin;
  if (origin && (CORS.has(origin) || CORS.has("*"))) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers.Vary = "Origin";
  }
  return headers;
}
function send(req, res, requestId, answer, head = false) {
  let status = answer.status;
  let payload = status === 204 || status === 304 ? Buffer.alloc(0) : Buffer.from(JSON.stringify(answer.body));
  const headers = { ...commonHeaders(req, requestId), ...answer.headers };
  if (["GET", "HEAD"].includes(req.method) && payload.length) {
    const etag = "\"" + hash(payload) + "\"";
    headers.ETag = etag;
    if (req.headers["if-none-match"] === etag) { status = 304; payload = Buffer.alloc(0); }
  }
  if (![204, 304].includes(status)) headers["Content-Type"] = "application/json; charset=utf-8";
  headers["Content-Length"] = String(payload.length);
  res.writeHead(status, headers);
  if (!head && payload.length) res.end(payload); else res.end();
  return status;
}
const IDEMPOTENCY = new Map();
async function serve(req, res) {
  const started = process.hrtime.bigint();
  const suppliedId = String(req.headers["x-request-id"] || "");
  const requestId = /^[A-Za-z0-9._:-]{1,128}$/.test(suppliedId) ? suppliedId : crypto.randomUUID();
  let status = 500;
  try {
    const url = new URL(req.url, ORIGIN), pathname = url.pathname.replace(/\/+$/, "") || "/";
    if (req.method === "OPTIONS") {
      status = send(req, res, requestId, response(204, null, {
        "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key, If-None-Match, X-Request-ID"
      })); return;
    }
    if (pathname === "/healthz" || pathname === "/readyz") {
      status = send(req, res, requestId, response(200, { status: "ok", version: VERSION }), req.method === "HEAD"); return;
    }
    if (pathname === "/openapi.json") {
      status = send(req, res, requestId, response(200, SPEC), req.method === "HEAD"); return;
    }
    if (pathname === "/") {
      status = send(req, res, requestId, response(200, { name: "Basecamp 5 API", version: VERSION, health: "/healthz", openapi: "/openapi.json" }), req.method === "HEAD"); return;
    }
    if (pathname === "/_test/reset") {
      if (process.env.BASECAMP_ENABLE_TEST_RESET !== "true") throw failure(404, "not_found", "Route not found");
      if (req.method !== "POST") {
        const error = failure(405, "method_not_allowed", "Method not allowed"); error.allow = "POST"; throw error;
      }
      const actor = authenticate(req); if (!actor.owner) throw failure(403, "forbidden", "Owner access required");
      STATE = seed(); IDEMPOTENCY.clear(); RATE.clear(); persist(); status = send(req, res, requestId, empty()); return;
    }
    const actor = authenticate(req), rate = rateLimit(actor);
    const effective = req.method === "HEAD" ? "GET" : req.method;
    const route = match(effective, pathname);
    if (route.params.accountId !== ACCOUNT) throw failure(404, "not_found", "Account not found");
    validateParameters(route.operation, route.params, url.searchParams);
    const body = await readBody(req, route.operation);
    const idem = req.headers["idempotency-key"];
    let answer;
    if (idem && ["POST", "PUT", "DELETE"].includes(effective)) {
      if (String(idem).length > 255) throw failure(400, "bad_request", "Idempotency-Key is too long");
      const key = actor.id + ":" + effective + ":" + pathname + ":" + idem;
      const fingerprint = hash(JSON.stringify(body || {})), cached = IDEMPOTENCY.get(key);
      if (cached && cached.fingerprint !== fingerprint) throw failure(409, "idempotency_conflict", "Idempotency-Key was already used with a different request");
      if (cached) answer = { ...copy(cached.answer), headers: { ...cached.answer.headers, "Idempotency-Replayed": "true" } };
      else {
        answer = dispatch(route, url.searchParams, body || {}, actor, pathname);
        IDEMPOTENCY.set(key, { fingerprint, answer: copy(answer) });
      }
    } else answer = dispatch(route, url.searchParams, body || {}, actor, pathname);
    answer.headers["X-RateLimit-Remaining"] = String(rate.remaining);
    answer.headers["X-RateLimit-Reset"] = String(rate.reset);
    if (["POST", "PUT", "DELETE"].includes(effective) && answer.status < 400) persist();
    status = send(req, res, requestId, answer, req.method === "HEAD");
  } catch (error) {
    const known = Number.isInteger(error.status);
    if (!known) console.error(JSON.stringify({ level: "error", message: "unhandled request error", request_id: requestId, stack: error.stack }));
    const code = known ? error.status : 500;
    const body = { error: known ? error.error : "internal_server_error", message: known ? String(error.message).slice(0, 500) : "An unexpected error occurred" };
    if (known && error.details !== undefined) {
      if (code === 429 && error.details.retry_after) body.retry_after = error.details.retry_after;
      else body.details = error.details;
    }
    const headers = {};
    if (code === 429) headers["Retry-After"] = String(error.details.retry_after);
    if (code === 405 && error.allow) headers.Allow = error.allow;
    status = send(req, res, requestId, response(code, body, headers), req.method === "HEAD");
  } finally {
    const elapsed = Number(process.hrtime.bigint() - started) / 1e6;
    console.log(JSON.stringify({ level: "info", method: req.method, path: req.url.split("?")[0], status, duration_ms: Number(elapsed.toFixed(2)), request_id: requestId }));
  }
}
function dispatch(route, query, body, actor, pathname) {
  const handler = HANDLERS[route.operation.operationId];
  if (!handler) return response(501, { error: "not_implemented", message: route.operation.operationId + " is registered but is not implemented by this release" });
  return handler(route.params, query, body, actor, pathname);
}

let server;
function startServer(port = PORT, host = HOST) {
  server = http.createServer((req, res) => { void serve(req, res); });
  server.requestTimeout = 30000;
  server.headersTimeout = 15000;
  server.keepAliveTimeout = 5000;
  server.maxRequestsPerSocket = 1000;
  server.on("error", error => {
    console.error(JSON.stringify({ level: "error", message: "server error", code: error.code, detail: error.message }));
    process.exitCode = 1;
  });
  server.listen(port, host, () => {
    const address = server.address();
    console.log(JSON.stringify({ level: "info", message: "Basecamp 5 API listening", host, port: address.port, account_id: ACCOUNT, operations: ROUTES.length }));
  });
  return server;
}
let stopping = false;
function shutdown(signal) {
  if (stopping || !server) return;
  stopping = true;
  console.log(JSON.stringify({ level: "info", message: "shutdown started", signal }));
  server.close(error => {
    try { persist(); } catch (persistError) { console.error(persistError); process.exitCode = 1; }
    if (error) { console.error(error); process.exitCode = 1; }
    console.log(JSON.stringify({ level: "info", message: "shutdown complete" }));
  });
  setTimeout(() => { console.error("forced shutdown"); process.exit(1); }, 10000).unref();
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
if (require.main === module) startServer();
module.exports = {
  ACCOUNT, SPEC, ROUTES, HANDLERS, dispatch, match, seed, validate, validateParameters, serve, startServer,
  persist, state: () => STATE, reset: () => { STATE = seed(); return STATE; }
};
