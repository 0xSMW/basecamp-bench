#!/usr/bin/env node
'use strict';

/**
 * Basecamp 5 API — single-file production server.
 *
 * Implements the account-scoped JSON API described by
 * reference/basecamp-sdk/{openapi.json,SPEC.md,behavior-model.json} against
 * an in-memory, process-lifetime data store seeded with the "Launch the new
 * website" sample project (INIT.md §3).
 *
 * Run:   node server.js
 * Env:   PORT (3000), HOST (0.0.0.0), PUBLIC_BASE_URL (http://HOST:PORT),
 *        ACCOUNT_ID (1), ACCOUNT_NAME ("Sample Co."), LOG_LEVEL (info),
 *        ALLOW_RESET (unset) — enables POST /_reset for test isolation,
 *        RATE_LIMIT_PER_MIN (600), CORS_ORIGIN (*)
 *
 * Zero external dependencies — Node.js built-ins only.
 */

const http = require('http');
const crypto = require('crypto');
const { URL } = require('url');

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const CONFIG = Object.freeze({
  port: parseInt(process.env.PORT, 10) || 3000,
  host: process.env.HOST || '0.0.0.0',
  accountId: parseInt(process.env.ACCOUNT_ID, 10) || 1,
  accountName: process.env.ACCOUNT_NAME || 'Sample Co.',
  logLevel: process.env.LOG_LEVEL || 'info',
  allowReset: process.env.ALLOW_RESET !== 'false',
  rateLimitPerMin: parseInt(process.env.RATE_LIMIT_PER_MIN, 10) || 600,
  corsOrigin: process.env.CORS_ORIGIN || '*',
  apiVersion: '2026-03-23',
});

const PUBLIC_BASE_URL = (
  process.env.PUBLIC_BASE_URL || `http://localhost:${CONFIG.port}`
).replace(/\/+$/, '');

const MAX_ERROR_MESSAGE_LENGTH = 500;
const MAX_BODY_BYTES = 25 * 1024 * 1024; // 25 MiB request body cap

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };
function log(level, msg, extra) {
  if (LOG_LEVELS[level] > LOG_LEVELS[CONFIG.logLevel]) return;
  const line = {
    ts: new Date().toISOString(),
    level,
    msg,
    ...(extra || {}),
  };
  const stream = level === 'error' ? process.stderr : process.stdout;
  stream.write(JSON.stringify(line) + '\n');
}

// ---------------------------------------------------------------------------
// Time helpers — deterministic seed offsets from boot instant T
// ---------------------------------------------------------------------------

const BOOT_T = new Date(); // T — process boot instant; seed timestamps are fixed offsets from this

function isoAt(date) {
  return date.toISOString();
}
function fromT(msOffset) {
  return new Date(BOOT_T.getTime() + msOffset);
}
const DAY = 24 * 60 * 60 * 1000;
const HOUR = 60 * 60 * 1000;
const MIN = 60 * 1000;

function dateOnly(d) {
  return d.toISOString().slice(0, 10);
}

// ---------------------------------------------------------------------------
// Errors — mirrors SPEC.md §6 error taxonomy so any conformant SDK client
// (retry/error-mapping logic) observes exactly the documented contract.
// ---------------------------------------------------------------------------

class ApiError extends Error {
  constructor(status, code, message, opts) {
    super(message);
    this.status = status;
    this.code = code;
    this.hint = opts && opts.hint;
    this.retryAfter = opts && opts.retryAfter;
  }
}

function truncate(msg) {
  if (typeof msg !== 'string') return msg;
  if (msg.length <= MAX_ERROR_MESSAGE_LENGTH) return msg;
  return msg.slice(0, MAX_ERROR_MESSAGE_LENGTH - 3) + '...';
}

const Errors = {
  badRequest: (msg, hint) => new ApiError(400, 'validation', msg || 'Bad request', { hint }),
  validation: (msg, hint) => new ApiError(422, 'validation', msg || 'Validation failed', { hint }),
  unauthorized: (msg) => new ApiError(401, 'auth_required', msg || 'Authentication required'),
  forbidden: (msg) => new ApiError(403, 'forbidden', msg || 'You are not permitted to do that'),
  notFound: (msg) => new ApiError(404, 'not_found', msg || 'Not found'),
  methodNotAllowed: (msg) => new ApiError(405, 'not_found', msg || 'Method not allowed'),
  rateLimited: (retryAfter) => new ApiError(429, 'rate_limit', 'Rate limit exceeded', { retryAfter }),
  notImplemented: (msg) => new ApiError(501, 'api_error', msg || 'Not implemented in this prototype'),
  internal: (msg) => new ApiError(500, 'api_error', msg || 'Internal server error'),
};

// ---------------------------------------------------------------------------
// HTTP plumbing
// ---------------------------------------------------------------------------

function sendJson(res, status, body, headers) {
  const json = body === undefined ? '' : JSON.stringify(body);
  const h = Object.assign(
    {
      'Content-Type': 'application/json; charset=utf-8',
      'Content-Length': Buffer.byteLength(json),
    },
    headers
  );
  res.writeHead(status, h);
  res.end(json);
}

function sendNoContent(res, headers) {
  res.writeHead(204, headers || {});
  res.end();
}

function sendError(res, err, requestId) {
  const status = err.status || 500;
  const code = err.code || 'api_error';
  const message = truncate(err.expose === false ? 'Internal server error' : err.message || 'Error');
  const payload = { error: message };
  if (err.hint) payload.hint = err.hint;
  const headers = {};
  if (err.retryAfter) headers['Retry-After'] = String(err.retryAfter);
  if (requestId) headers['X-Request-Id'] = requestId;
  sendJson(res, status, payload, headers);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(Errors.badRequest('Request body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

async function parseJsonBody(req) {
  const raw = await readBody(req);
  if (raw.length === 0) return {};
  const text = raw.toString('utf8');
  try {
    const parsed = JSON.parse(text);
    if (parsed === null || typeof parsed !== 'object') {
      throw new Error('not an object');
    }
    return parsed;
  } catch (e) {
    throw Errors.badRequest('Request body must be valid JSON');
  }
}

// ---------------------------------------------------------------------------
// Pagination — Link (rel="next") + X-Total-Count, per SPEC.md §8
// ---------------------------------------------------------------------------

const DEFAULT_PAGE_SIZE = 50;

function paginate(items, req, res, opts) {
  const perPage = (opts && opts.perPage) || DEFAULT_PAGE_SIZE;
  const url = new URL(req.url, PUBLIC_BASE_URL);
  const page = Math.max(1, parseInt(url.searchParams.get('page'), 10) || 1);
  const total = items.length;
  const start = (page - 1) * perPage;
  const pageItems = items.slice(start, start + perPage);
  const hasNext = start + perPage < total;

  res.setHeader('X-Total-Count', String(total));
  if (hasNext) {
    url.searchParams.set('page', String(page + 1));
    res.setHeader('Link', `<${url.toString()}>; rel="next"`);
  }
  return pageItems;
}

// ---------------------------------------------------------------------------
// ID generation — one global counter across every entity so the generic
// `/recordings/{id}` surface (and dock-item ids shared with tool roots) never
// collides, mirroring how Basecamp's own IDs are account-wide unique.
// ---------------------------------------------------------------------------

let __id = 1000;
function nextId() {
  __id += 1;
  return __id;
}

// ---------------------------------------------------------------------------
// In-memory database
// ---------------------------------------------------------------------------

function freshDb() {
  return {
    seeded: false,
    account: null,
    people: new Map(), // id -> Person
    tokens: new Map(), // token -> personId
    projects: new Map(), // id -> Project (bucket)
    recordings: new Map(), // id -> generic recording envelope + type-specific fields
    boosts: new Map(), // id -> Boost
    events: [], // {id, recording_id, action, details, created_at, creator_id}
    subscriptions: new Map(), // recordingId -> Set<personId>
    readings: new Map(), // personId -> Map<recordingId, {read_at, resurface_at}>
    bookmarks: new Map(), // personId -> Set<recordingId|toolId>
    messageTypes: new Map(), // id -> MessageType (account-level categories)
    chatbots: new Map(), // id -> Chatbot
    webhooks: new Map(), // id -> Webhook (bucket-scoped)
    webhookDeliveries: new Map(), // webhookId -> [] deliveries
    templates: new Map(), // id -> Template
    projectConstructions: new Map(), // id -> ProjectConstruction
    lineupMarkers: new Map(), // id -> LineupMarker
    outOfOffice: new Map(), // personId -> {enabled, start_date, end_date}
    preferences: new Map(), // personId -> Preferences payload
    myNotes: new Map(), // personId -> {content, updated_at}
    doToday: new Map(), // personId -> Set<recordingId>
    uploadVersions: new Map(), // uploadId -> [Upload snapshots]
    attachments: new Map(), // sgid -> {contentType, data: Buffer, filename}
    companies: new Map(), // id -> name
  };
}

let db = freshDb();
const seedTokenLog = new Map(); // display name -> raw bearer token, populated once at seed time (for operator/test convenience)

// ---------------------------------------------------------------------------
// Recording core (INIT.md §4.1) — every content entity shares this envelope.
// ---------------------------------------------------------------------------

/**
 * Create and register a recording. `fields` holds type-specific properties;
 * shared envelope fields are filled in / defaulted here.
 */
function createRecording(type, fields) {
  const id = fields.id || nextId();
  const now = fields.created_at || BOOT_T;
  const rec = Object.assign(
    {
      id,
      type,
      status: 'active',
      visible_to_clients: false,
      inherits_status: true,
      title: '',
      content: undefined,
      position: undefined,
      created_at: now,
      updated_at: fields.updated_at || now,
      creator_id: null,
      bucket_id: null,
      parent_id: null,
      parent_type: null,
      trashed_at: null,
      archived_at: null,
      pinned: false,
    },
    fields,
    { id, type }
  );
  db.recordings.set(id, rec);
  return rec;
}

function getRecordingOr404(id, expectedTypes) {
  const rec = db.recordings.get(Number(id));
  if (!rec) throw Errors.notFound('Recording not found');
  if (expectedTypes) {
    const types = Array.isArray(expectedTypes) ? expectedTypes : [expectedTypes];
    if (!types.includes(rec.type)) throw Errors.notFound('Recording not found');
  }
  return rec;
}

function touch(rec) {
  rec.updated_at = new Date();
  return rec;
}

function recordEvent(recordingId, action, creatorId, details) {
  const ev = {
    id: nextId(),
    recording_id: recordingId,
    action,
    details: details || {},
    created_at: new Date(),
    creator_id: creatorId,
  };
  db.events.push(ev);
  return ev;
}

function commentsCountFor(recordingId) {
  let n = 0;
  for (const rec of db.recordings.values()) {
    if (rec.type === 'Comment' && rec.parent_id === recordingId && rec.status === 'active') n++;
  }
  return n;
}

function boostsCountFor(recordingOrEventKey) {
  let n = 0;
  for (const b of db.boosts.values()) {
    if (b.target_key === recordingOrEventKey) n++;
  }
  return n;
}

function subscribersFor(recordingId) {
  return Array.from(db.subscriptions.get(recordingId) || []);
}

function setSubscribers(recordingId, personIds) {
  db.subscriptions.set(recordingId, new Set(personIds));
}

function subscribe(recordingId, personId) {
  if (!db.subscriptions.has(recordingId)) db.subscriptions.set(recordingId, new Set());
  db.subscriptions.get(recordingId).add(personId);
}

function unsubscribe(recordingId, personId) {
  const set = db.subscriptions.get(recordingId);
  if (set) set.delete(personId);
}

// ============================================================================
// Path templates — single source of truth shared by the router and the
// `url` field builder, so every URL this server hands out is one it also
// actually serves.
// ============================================================================

const TYPE_PATH = {
  'Message::Board': (r) => `/message_boards/${r.id}`,
  Message: (r) => `/messages/${r.id}`,
  Todoset: (r) => `/todosets/${r.id}`,
  Todolist: (r) => `/todolists/${r.id}`,
  'Todolist::Group': (r) => `/todolists/${r.id}`,
  Todo: (r) => `/todos/${r.id}`,
  'Kanban::Board': (r) => `/card_tables/${r.id}`,
  'Kanban::Column': (r) => `/card_tables/columns/${r.id}`,
  'Kanban::Card': (r) => `/card_tables/cards/${r.id}`,
  'Kanban::Step': (r) => `/card_tables/steps/${r.id}`,
  Vault: (r) => `/vaults/${r.id}`,
  Document: (r) => `/documents/${r.id}`,
  Upload: (r) => `/uploads/${r.id}`,
  'Chat::Transcript': (r) => `/chats/${r.id}`,
  'Chat::Line': (r) => `/chats/${r.parent_id}/lines/${r.id}`,
  Schedule: (r) => `/schedules/${r.id}`,
  'Schedule::Entry': (r) => `/schedule_entries/${r.id}`,
  Questionnaire: (r) => `/questionnaires/${r.id}`,
  'Questionnaire::Question': (r) => `/questions/${r.id}`,
  'Questionnaire::Answer': (r) => `/question_answers/${r.id}`,
  Inbox: (r) => `/inboxes/${r.id}`,
  'Inbox::Forward': (r) => `/inbox_forwards/${r.id}`,
  'Inbox::Reply': (r) => `/inbox_forwards/${r.parent_id}/replies/${r.id}`,
  'Client::Approval': (r) => `/client/approvals/${r.id}`,
  'Client::Correspondence': (r) => `/client/correspondences/${r.id}`,
  'Client::Reply': (r) => `/client/recordings/${r.parent_id}/replies/${r.id}`,
  Comment: (r) => `/comments/${r.id}`,
  'Gauge::Needle': (r) => `/gauge_needles/${r.id}`,
  'Timesheet::Entry': (r) => `/timesheet_entries/${r.id}`,
};

const TOOL_SLUG = {
  'Message::Board': 'message_board',
  Todoset: 'todos',
  'Kanban::Board': 'card_table',
  Vault: 'vault',
  'Chat::Transcript': 'chat',
  Schedule: 'schedule',
  Questionnaire: 'questionnaire',
  Inbox: 'inbox',
};

function jsonUrlFor(rec) {
  const builder = TYPE_PATH[rec.type];
  const path = builder ? builder(rec) : `/recordings/${rec.id}`;
  return `${PUBLIC_BASE_URL}/${CONFIG.accountId}${path}.json`.replace(/([^:]\/)\/+/g, '$1');
}

function appUrlFor(rec) {
  const bucketId = rec.bucket_id || rec.id;
  const toolSlug = TOOL_SLUG[rec.type] || (rec.type || 'recordings').toLowerCase().replace(/[^a-z0-9]+/g, '_');
  return `${PUBLIC_BASE_URL}/${CONFIG.accountId}/buckets/${bucketId}/${toolSlug}/${rec.id}`;
}

// ============================================================================
// People / Account projections
// ============================================================================

function personProjection(person) {
  if (!person) return null;
  return {
    id: person.id,
    attachable_sgid: `gid://sample/Person/${person.id}`,
    name: person.name,
    email_address: person.email_address,
    personable_type: person.client ? 'Client' : 'User',
    title: person.title || '',
    bio: person.bio || '',
    location: person.location || '',
    created_at: isoAt(person.created_at),
    updated_at: isoAt(person.updated_at),
    admin: !!person.admin,
    owner: !!person.owner,
    client: !!person.client,
    employee: !!person.employee,
    time_zone: person.time_zone || 'America/Chicago',
    avatar_url: person.avatar_url || `${PUBLIC_BASE_URL}/assets/avatars/${person.id}.png`,
    company: person.company_id
      ? { id: person.company_id, name: db.companies && db.companies.get(person.company_id) }
      : undefined,
    can_manage_projects: !!person.can_manage_projects,
    can_manage_people: !!person.can_manage_people,
    can_ping: !!person.can_ping,
    can_access_timesheet: !!person.can_access_timesheet,
    can_access_hill_charts: !!person.can_access_hill_charts,
  };
}

function accountProjection() {
  const a = db.account;
  return {
    id: a.id,
    name: a.name,
    owner_name: a.owner_name,
    active: true,
    created_at: isoAt(a.created_at),
    updated_at: isoAt(a.updated_at),
    trial: false,
    frozen: false,
    paused: false,
    limits: { can_create_projects: true, can_pin_projects: true, can_create_users: true, can_upload_files: true },
    subscription: {
      short_name: 'sample',
      proper_name: 'Sample Plan',
      project_limit: 0,
      teams: true,
      clients: true,
      templates: true,
      logo: true,
      timesheet: true,
    },
    settings: { company_hq_enabled: false, teams_enabled: true, projects_enabled: true },
    logo: { url: a.logo_url || null },
  };
}

// ============================================================================
// Recording envelope projection
// ============================================================================

function recordingBucket(bucketId) {
  const project = db.projects.get(bucketId);
  if (!project) return { id: bucketId, name: 'Account', type: 'Account' };
  return { id: project.id, name: project.name, type: 'Project' };
}

function recordingParent(rec) {
  if (!rec.parent_id) {
    // Tool roots parent to the project itself.
    return { id: rec.bucket_id, title: (db.projects.get(rec.bucket_id) || {}).name || '', type: 'Project', url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/projects/${rec.bucket_id}.json`, app_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/projects/${rec.bucket_id}` };
  }
  const parent = db.recordings.get(rec.parent_id);
  if (!parent) return { id: rec.parent_id, title: '', type: 'Recording', url: '', app_url: '' };
  return { id: parent.id, title: parent.title || parent.name || parent.summary || '', type: parent.type, url: jsonUrlFor(parent), app_url: appUrlFor(parent) };
}

/** Shared Recording envelope fields common to every recordable type. */
function recordingEnvelope(rec) {
  const creator = db.people.get(rec.creator_id);
  const env = {
    id: rec.id,
    status: rec.status,
    visible_to_clients: !!rec.visible_to_clients,
    created_at: isoAt(rec.created_at),
    updated_at: isoAt(rec.updated_at),
    title: rec.title || rec.name || rec.summary || rec.subject || '',
    inherits_status: !!rec.inherits_status,
    type: rec.type,
    url: jsonUrlFor(rec),
    app_url: appUrlFor(rec),
    bookmark_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/recordings/${rec.id}/bookmark.json`,
    parent: recordingParent(rec),
    bucket: recordingBucket(rec.bucket_id),
    creator: personProjection(creator),
  };
  if (rec.content !== undefined) env.content = rec.content;
  if (COMMENTABLE_TYPES.has(rec.type)) {
    env.comments_count = commentsCountFor(rec.id);
    env.comments_url = `${PUBLIC_BASE_URL}/${CONFIG.accountId}/recordings/${rec.id}/comments.json`;
  }
  if (SUBSCRIBABLE_TYPES.has(rec.type)) {
    env.subscription_url = `${PUBLIC_BASE_URL}/${CONFIG.accountId}/recordings/${rec.id}/subscription.json`;
  }
  if (BOOSTABLE_TYPES.has(rec.type)) {
    env.boosts_count = boostsCountFor(`recording:${rec.id}`);
    env.boosts_url = `${PUBLIC_BASE_URL}/${CONFIG.accountId}/recordings/${rec.id}/boosts.json`;
  }
  if (rec.position !== undefined && rec.position !== null) env.position = rec.position;
  return env;
}

const COMMENTABLE_TYPES = new Set([
  'Message', 'Todo', 'Todolist', 'Todolist::Group', 'Document', 'Upload', 'Kanban::Card',
  'Gauge::Needle', 'Questionnaire::Answer',
]);
const SUBSCRIBABLE_TYPES = new Set([
  'Message::Board', 'Message', 'Todoset', 'Todolist', 'Todo', 'Kanban::Board', 'Kanban::Column',
  'Vault', 'Document', 'Upload', 'Chat::Transcript', 'Schedule', 'Client::Approval',
  'Client::Correspondence', 'Questionnaire::Question', 'Gauge::Needle',
]);
const BOOSTABLE_TYPES = new Set([
  'Message', 'Todo', 'Kanban::Card', 'Comment', 'Chat::Line', 'Schedule::Entry', 'Questionnaire::Answer',
]);

// ============================================================================
// Auth
// ============================================================================

function sha256(s) {
  return crypto.createHash('sha256').update(s).digest('hex');
}

function issueToken(personId) {
  const raw = crypto.randomBytes(24).toString('hex');
  db.tokens.set(sha256(raw), personId);
  return raw;
}

function authenticate(req) {
  const header = req.headers['authorization'] || '';
  const match = /^Bearer\s+(.+)$/i.exec(header.trim());
  if (!match) throw Errors.unauthorized('Missing or malformed Authorization header');
  const personId = db.tokens.get(sha256(match[1].trim()));
  if (!personId) throw Errors.unauthorized('Invalid or expired access token');
  const person = db.people.get(personId);
  if (!person) throw Errors.unauthorized('Invalid or expired access token');
  return person;
}

// ---- Authorization helpers (INIT.md §4.4) ----------------------------------

function projectMembers(project) {
  return project.memberIds || [];
}

function canAccessProject(person, project) {
  if (!project) return false;
  if (person.owner) return true;
  if (project.allAccess && person.employee) return true;
  return projectMembers(project).includes(person.id);
}

function requireProjectAccess(person, project) {
  if (!canAccessProject(person, project)) {
    throw Errors.forbidden('You do not have access to this project');
  }
}

function requireEmployee(person) {
  if (!person.employee) throw Errors.forbidden('Only company employees can do that');
}

function requireAdmin(person) {
  if (!person.admin && !person.owner) throw Errors.forbidden('Admin access required');
}

function requireOwner(person) {
  if (!person.owner) throw Errors.forbidden('Owner access required');
}

/** Clients only ever see client-visible content (INIT §4.4 rule 2). */
function assertClientVisible(person, rec) {
  if (person.client && !rec.visible_to_clients) {
    throw Errors.notFound('Not found');
  }
}

function projectOf(rec) {
  return db.projects.get(rec.bucket_id);
}

function requireRecordingAccess(person, rec) {
  const project = projectOf(rec);
  if (project) requireProjectAccess(person, project);
  assertClientVisible(person, rec);
}

/** Personal-voice items (messages, comments, chat lines, boosts) — creator, or admins/owners, may mutate. */
function requireOwnVoiceOrAdmin(person, rec) {
  if (rec.creator_id === person.id) return;
  if (person.admin || person.owner) return;
  throw Errors.forbidden('Only the creator or an admin can do that');
}

// ============================================================================
// Router
// ============================================================================

const routes = []; // {method, segments, handler}

function compilePattern(pattern) {
  // pattern like "/message_boards/:boardId/messages.json"
  return pattern.split('/').filter((s) => s.length > 0);
}

function route(method, pattern, handler, opts) {
  routes.push({ method, segments: compilePattern(pattern), pattern, handler, raw: !!(opts && opts.raw) });
}

function matchSegments(routeSegments, pathSegments) {
  if (routeSegments.length !== pathSegments.length) return null;
  const params = {};
  for (let i = 0; i < routeSegments.length; i++) {
    const rs = routeSegments[i];
    const ps = decodeURIComponent(pathSegments[i]);
    if (rs.startsWith(':')) {
      params[rs.slice(1)] = ps;
    } else if (rs !== ps) {
      return null;
    }
  }
  return params;
}

function findRoute(method, pathname) {
  const pathSegments = pathname.split('/').filter((s) => s.length > 0);
  let pathMatchedAnyMethod = false;
  for (const r of routes) {
    const params = matchSegments(r.segments, pathSegments);
    if (params) {
      pathMatchedAnyMethod = true;
      if (r.method === method) return { handler: r.handler, params, raw: r.raw };
    }
  }
  if (pathMatchedAnyMethod) throw Errors.methodNotAllowed(`${method} not supported on this path`);
  return null;
}

// ---- Simple in-memory rate limiter (per bearer token / per IP fallback) ---

const rateBuckets = new Map(); // key -> {count, windowStart}
function checkRateLimit(key) {
  const now = Date.now();
  const windowMs = 60 * 1000;
  let bucket = rateBuckets.get(key);
  if (!bucket || now - bucket.windowStart >= windowMs) {
    bucket = { count: 0, windowStart: now };
    rateBuckets.set(key, bucket);
  }
  bucket.count += 1;
  if (bucket.count > CONFIG.rateLimitPerMin) {
    const retryAfter = Math.ceil((bucket.windowStart + windowMs - now) / 1000);
    throw Errors.rateLimited(Math.max(1, retryAfter));
  }
}

// periodically sweep stale rate-limit buckets so long-lived processes don't leak memory
setInterval(() => {
  const now = Date.now();
  for (const [key, bucket] of rateBuckets) {
    if (now - bucket.windowStart > 5 * 60 * 1000) rateBuckets.delete(key);
  }
}, 5 * 60 * 1000).unref();

// ============================================================================
// Request handling
// ============================================================================

function accountPrefix(pathname) {
  // Every account-scoped path is "/{accountId}/...". Strip and validate.
  const segments = pathname.split('/').filter((s) => s.length > 0);
  if (segments.length === 0) return { rest: '/', accountId: null };
  const maybeId = segments[0];
  if (/^\d+$/.test(maybeId)) {
    return { rest: '/' + segments.slice(1).join('/'), accountId: parseInt(maybeId, 10) };
  }
  return { rest: pathname, accountId: null };
}

async function handleRequest(req, res) {
  const requestId = crypto.randomUUID();
  res.setHeader('X-Request-Id', requestId);
  res.setHeader('Access-Control-Allow-Origin', CONFIG.corsOrigin);
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, PATCH, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type, Accept, If-None-Match');
  res.setHeader('Access-Control-Expose-Headers', 'X-Total-Count, Link, X-Request-Id, Retry-After');

  const start = process.hrtime.bigint();
  const url = new URL(req.url, PUBLIC_BASE_URL);
  const method = req.method.toUpperCase();

  if (method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  try {
    if (url.pathname === '/up' || url.pathname === '/health' || url.pathname === '/healthz') {
      sendJson(res, 200, { status: 'ok', uptime_s: process.uptime(), seeded: db.seeded }, { 'Cache-Control': 'no-store' });
      return;
    }
    if (url.pathname === '/' && method === 'GET') {
      sendJson(res, 200, {
        service: 'basecamp-5-api',
        api_version: CONFIG.apiVersion,
        account_id: CONFIG.accountId,
        docs: 'See reference/basecamp-sdk/openapi.json for the full contract.',
      });
      return;
    }
    if (url.pathname === '/_reset' && method === 'POST') {
      if (!CONFIG.allowReset) throw Errors.forbidden('Reset endpoint disabled (ALLOW_RESET=false)');
      db = freshDb();
      seedAll();
      sendJson(res, 200, { reset: true, seeded: true });
      return;
    }
    if (url.pathname === '/_seed/tokens' && method === 'GET') {
      if (!CONFIG.allowReset) throw Errors.forbidden('Debug endpoints disabled (ALLOW_RESET=false)');
      sendJson(res, 200, { tokens: Array.from(seedTokenLog.entries()).map(([name, token]) => ({ person: name, token })) });
      return;
    }

    const { rest, accountId } = accountPrefix(url.pathname);
    if (accountId === null) throw Errors.notFound('Unknown route');
    if (accountId !== CONFIG.accountId) throw Errors.notFound('Unknown account');

    const found = findRoute(method, rest);
    if (!found) throw Errors.notFound(`No route for ${method} ${rest}`);

    const person = authenticate(req);
    checkRateLimit(person.id);

    const ctx = { req, res, params: found.params, url, person, requestId };
    if (found.raw) {
      ctx.body = {};
      ctx.rawBody = await readBody(req);
      ctx.contentType = req.headers['content-type'] || 'application/octet-stream';
    } else if (['POST', 'PUT', 'PATCH'].includes(method)) {
      ctx.body = await parseJsonBody(req);
    } else {
      ctx.body = {};
    }

    await found.handler(ctx);

    const durMs = Number(process.hrtime.bigint() - start) / 1e6;
    log('info', 'request', { requestId, method, path: rest, status: res.statusCode, ms: Math.round(durMs) });
  } catch (err) {
    const durMs = Number(process.hrtime.bigint() - start) / 1e6;
    if (err instanceof ApiError) {
      sendError(res, err, requestId);
      log(err.status >= 500 ? 'error' : 'warn', 'request_error', {
        requestId,
        method,
        path: url.pathname,
        status: err.status,
        code: err.code,
        message: err.message,
        ms: Math.round(durMs),
      });
    } else {
      sendError(res, Errors.internal(), requestId);
      log('error', 'unhandled_error', { requestId, method, path: url.pathname, error: String((err && err.stack) || err), ms: Math.round(durMs) });
    }
  }
}

// ============================================================================
// Validation helpers
// ============================================================================

function requireString(body, field, opts) {
  const v = body[field];
  if (v === undefined || v === null || (typeof v === 'string' && v.trim() === '')) {
    throw Errors.validation(`\`${field}\` is required`);
  }
  if (typeof v !== 'string') throw Errors.validation(`\`${field}\` must be a string`);
  if (opts && opts.maxLength && v.length > opts.maxLength) {
    throw Errors.validation(`\`${field}\` must be ${opts.maxLength} characters or fewer`);
  }
  return v;
}

function optionalString(body, field) {
  const v = body[field];
  if (v === undefined || v === null) return undefined;
  if (typeof v !== 'string') throw Errors.validation(`\`${field}\` must be a string`);
  return v;
}

function optionalBool(body, field) {
  const v = body[field];
  if (v === undefined || v === null) return undefined;
  if (typeof v !== 'boolean') throw Errors.validation(`\`${field}\` must be a boolean`);
  return v;
}

function optionalIdArray(body, field) {
  const v = body[field];
  if (v === undefined || v === null) return undefined;
  if (!Array.isArray(v)) throw Errors.validation(`\`${field}\` must be an array of ids`);
  return v.map((x) => {
    const n = Number(x);
    if (!Number.isFinite(n)) throw Errors.validation(`\`${field}\` must contain only ids`);
    return n;
  });
}

function requireDateString(body, field) {
  const v = requireString(body, field);
  if (Number.isNaN(Date.parse(v))) throw Errors.validation(`\`${field}\` must be a valid date/time`);
  return v;
}

function paramId(params, name) {
  const n = Number(params[name]);
  if (!Number.isInteger(n)) throw Errors.badRequest(`Invalid ${name}`);
  return n;
}

function peopleByIds(ids) {
  return (ids || []).map((id) => db.people.get(id)).filter(Boolean);
}

// ============================================================================
// Cross-cutting projections
// ============================================================================

function commentProjection(rec) {
  return recordingEnvelope(rec);
}

function boostProjection(b) {
  return {
    id: b.id,
    content: b.content,
    created_at: isoAt(b.created_at),
    booster: personProjection(db.people.get(b.booster_id)),
    recording: b.recording_ref,
  };
}

function eventProjection(ev) {
  return {
    id: ev.id,
    recording_id: ev.recording_id,
    action: ev.action,
    details: ev.details || {},
    created_at: isoAt(ev.created_at),
    creator: personProjection(db.people.get(ev.creator_id)),
    boosts_count: boostsCountFor(`event:${ev.id}`),
    boosts_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/recordings/${ev.recording_id}/events/${ev.id}/boosts.json`,
  };
}

function subscriptionProjection(recordingId) {
  const subs = subscribersFor(recordingId);
  return {
    subscribed: false, // overwritten per-viewer by caller
    count: subs.length,
    url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/recordings/${recordingId}/subscription.json`,
    subscribers: peopleByIds(subs).map(personProjection),
  };
}

// ============================================================================
// Generic Recording endpoints (Automation / Boosts / People tags)
// ============================================================================

route('GET', '/recordings/:recordingId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, recordingEnvelope(rec));
});

route('GET', '/projects/recordings.json', (ctx) => {
  const url = ctx.url;
  const bucketFilter = url.searchParams.getAll('bucket').map(Number);
  const typeFilter = url.searchParams.getAll('type');
  const statusFilter = url.searchParams.get('status') || 'active';
  let all = Array.from(db.recordings.values()).filter((r) => {
    if (bucketFilter.length && !bucketFilter.includes(r.bucket_id)) return false;
    if (typeFilter.length && !typeFilter.includes(r.type)) return false;
    if (statusFilter !== 'all' && r.status !== statusFilter) return false;
    const project = projectOf(r);
    if (project && !canAccessProject(ctx.person, project)) return false;
    if (ctx.person.client && !r.visible_to_clients) return false;
    return true;
  });
  all.sort((a, b) => b.created_at - a.created_at);
  const page = paginate(all, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(recordingEnvelope));
});

route('GET', '/recordings/:recordingId/comments.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  const comments = Array.from(db.recordings.values())
    .filter((r) => r.type === 'Comment' && r.parent_id === rec.id && r.status === 'active')
    .sort((a, b) => a.created_at - b.created_at);
  const page = paginate(comments, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(commentProjection));
});

route('POST', '/recordings/:recordingId/comments.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  const content = requireString(ctx.body, 'content');
  const comment = createRecording('Comment', {
    title: 'Comment',
    content,
    creator_id: ctx.person.id,
    bucket_id: rec.bucket_id,
    parent_id: rec.id,
    visible_to_clients: rec.visible_to_clients,
  });
  recordEvent(rec.id, 'commented', ctx.person.id, { comment_id: comment.id });
  sendJson(ctx.res, 201, commentProjection(comment));
});

route('GET', '/comments/:commentId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'commentId'), 'Comment');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, commentProjection(rec));
});

route('PUT', '/comments/:commentId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'commentId'), 'Comment');
  requireRecordingAccess(ctx.person, rec);
  requireOwnVoiceOrAdmin(ctx.person, rec);
  rec.content = requireString(ctx.body, 'content');
  touch(rec);
  sendJson(ctx.res, 200, commentProjection(rec));
});

// ---- Boosts -----------------------------------------------------------------

function createBoost(targetKey, recordingRef, content, boosterId) {
  const b = {
    id: nextId(),
    target_key: targetKey,
    content,
    created_at: new Date(),
    booster_id: boosterId,
    recording_ref: recordingRef,
  };
  db.boosts.set(b.id, b);
  return b;
}

route('GET', '/recordings/:recordingId/boosts.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  const list = Array.from(db.boosts.values()).filter((b) => b.target_key === `recording:${rec.id}`);
  const page = paginate(list, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(boostProjection));
});

route('POST', '/recordings/:recordingId/boosts.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  const content = requireString(ctx.body, 'content', { maxLength: 16 });
  const ref = { id: rec.id, title: rec.title || '', type: rec.type, url: jsonUrlFor(rec), app_url: appUrlFor(rec) };
  const b = createBoost(`recording:${rec.id}`, ref, content, ctx.person.id);
  sendJson(ctx.res, 201, boostProjection(b));
});

route('GET', '/boosts/:boostId', (ctx) => {
  const b = db.boosts.get(paramId(ctx.params, 'boostId'));
  if (!b) throw Errors.notFound('Boost not found');
  sendJson(ctx.res, 200, boostProjection(b));
});

route('DELETE', '/boosts/:boostId', (ctx) => {
  const b = db.boosts.get(paramId(ctx.params, 'boostId'));
  if (!b) throw Errors.notFound('Boost not found');
  if (b.booster_id !== ctx.person.id && !ctx.person.admin && !ctx.person.owner) {
    throw Errors.forbidden('Only the booster or an admin can remove this boost');
  }
  db.boosts.delete(b.id);
  sendNoContent(ctx.res);
});

route('GET', '/recordings/:recordingId/events/:eventId/boosts.json', (ctx) => {
  const eventId = paramId(ctx.params, 'eventId');
  const list = Array.from(db.boosts.values()).filter((b) => b.target_key === `event:${eventId}`);
  const page = paginate(list, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(boostProjection));
});

route('POST', '/recordings/:recordingId/events/:eventId/boosts.json', (ctx) => {
  const recordingId = paramId(ctx.params, 'recordingId');
  const eventId = paramId(ctx.params, 'eventId');
  const ev = db.events.find((e) => e.id === eventId);
  if (!ev) throw Errors.notFound('Event not found');
  const content = requireString(ctx.body, 'content', { maxLength: 16 });
  const ref = { id: recordingId, title: '', type: 'Event', url: '', app_url: '' };
  const b = createBoost(`event:${eventId}`, ref, content, ctx.person.id);
  sendJson(ctx.res, 201, boostProjection(b));
});

// ---- Events / timeline -------------------------------------------------------

route('GET', '/recordings/:recordingId/events.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  const list = db.events.filter((e) => e.recording_id === rec.id).sort((a, b) => b.created_at - a.created_at);
  const page = paginate(list, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(eventProjection));
});

// ---- Status lifecycle (archive / trash / unarchive) --------------------------

route('PUT', '/recordings/:recordingId/status/active.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  rec.status = 'active';
  rec.trashed_at = null;
  rec.archived_at = null;
  touch(rec);
  recordEvent(rec.id, 'unarchived', ctx.person.id);
  sendNoContent(ctx.res);
});

route('PUT', '/recordings/:recordingId/status/archived.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  rec.status = 'archived';
  rec.archived_at = new Date();
  touch(rec);
  recordEvent(rec.id, 'archived', ctx.person.id);
  sendNoContent(ctx.res);
});

route('PUT', '/recordings/:recordingId/status/trashed.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  rec.status = 'trashed';
  rec.trashed_at = new Date();
  touch(rec);
  recordEvent(rec.id, 'trashed', ctx.person.id);
  sendNoContent(ctx.res);
});

// ---- Subscriptions ------------------------------------------------------------

route('GET', '/recordings/:recordingId/subscription.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  const s = subscriptionProjection(rec.id);
  s.subscribed = subscribersFor(rec.id).includes(ctx.person.id);
  sendJson(ctx.res, 200, s);
});

route('POST', '/recordings/:recordingId/subscription.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  subscribe(rec.id, ctx.person.id);
  const s = subscriptionProjection(rec.id);
  s.subscribed = true;
  sendJson(ctx.res, 200, s);
});

route('PUT', '/recordings/:recordingId/subscription.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  const grant = optionalIdArray(ctx.body, 'subscriptions') || [];
  const revoke = optionalIdArray(ctx.body, 'unsubscriptions') || [];
  grant.forEach((id) => subscribe(rec.id, id));
  revoke.forEach((id) => unsubscribe(rec.id, id));
  const s = subscriptionProjection(rec.id);
  s.subscribed = subscribersFor(rec.id).includes(ctx.person.id);
  sendJson(ctx.res, 200, s);
});

route('DELETE', '/recordings/:recordingId/subscription.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  unsubscribe(rec.id, ctx.person.id);
  sendNoContent(ctx.res);
});

// ---- Pin / unpin (messages) ---------------------------------------------------

route('POST', '/recordings/:messageId/pin.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'messageId'), 'Message');
  requireRecordingAccess(ctx.person, rec);
  rec.pinned = true;
  touch(rec);
  sendNoContent(ctx.res);
});

route('DELETE', '/recordings/:messageId/pin.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'messageId'), 'Message');
  requireRecordingAccess(ctx.person, rec);
  rec.pinned = false;
  touch(rec);
  sendNoContent(ctx.res);
});

// ---- Client visibility ---------------------------------------------------------

route('PUT', '/recordings/:recordingId/client_visibility.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, rec);
  rec.visible_to_clients = !!requireBool(ctx.body, 'visible_to_clients');
  touch(rec);
  sendJson(ctx.res, 200, recordingEnvelope(rec));
});

function requireBool(body, field) {
  const v = body[field];
  if (typeof v !== 'boolean') throw Errors.validation(`\`${field}\` must be a boolean`);
  return v;
}

// ---- Tool (dock item) position -------------------------------------------------

route('POST', '/recordings/:toolId/position.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'toolId'));
  const project = projectOf(rec);
  requireProjectAccess(ctx.person, project);
  requireEmployee(ctx.person);
  const dockItem = (project.dock || []).find((d) => d.id === rec.id);
  if (dockItem) dockItem.enabled = true;
  rec.status = 'active';
  touch(rec);
  sendJson(ctx.res, 201, toolProjection(rec));
});

route('DELETE', '/recordings/:toolId/position.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'toolId'));
  const project = projectOf(rec);
  requireProjectAccess(ctx.person, project);
  requireEmployee(ctx.person);
  const dockItem = (project.dock || []).find((d) => d.id === rec.id);
  if (dockItem) dockItem.enabled = false;
  sendNoContent(ctx.res);
});

route('PUT', '/recordings/:toolId/position.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'toolId'));
  const project = projectOf(rec);
  requireProjectAccess(ctx.person, project);
  const position = ctx.body.position;
  if (!Number.isInteger(position)) throw Errors.validation('`position` must be an integer');
  const dockItem = (project.dock || []).find((d) => d.id === rec.id);
  if (dockItem) dockItem.position = position;
  sendJson(ctx.res, 200, {});
});

// ============================================================================
// Account
// ============================================================================

route('GET', '/account.json', (ctx) => sendJson(ctx.res, 200, accountProjection()));

route('PUT', '/account/name.json', (ctx) => {
  requireOwner(ctx.person);
  db.account.name = requireString(ctx.body, 'name');
  db.account.updated_at = new Date();
  sendJson(ctx.res, 200, accountProjection());
});

route('PUT', '/account/logo.json', (ctx) => {
  requireOwner(ctx.person);
  const sgid = `gid://sample/Logo/${nextId()}`;
  db.attachments.set(sgid, { contentType: ctx.contentType, data: ctx.rawBody, filename: 'logo' });
  db.account.logo_url = `${PUBLIC_BASE_URL}/${CONFIG.accountId}/attachments/${encodeURIComponent(sgid)}`;
  sendNoContent(ctx.res);
}, { raw: true });

route('DELETE', '/account/logo.json', (ctx) => {
  requireOwner(ctx.person);
  db.account.logo_url = null;
  sendNoContent(ctx.res);
});

route('POST', '/attachments.json', (ctx) => {
  const name = ctx.url.searchParams.get('name');
  if (!name) throw Errors.validation('`name` query parameter is required');
  const sgid = `gid://sample/Attachment/${nextId()}`;
  db.attachments.set(sgid, { contentType: ctx.contentType, data: ctx.rawBody, filename: name });
  sendJson(ctx.res, 201, { attachable_sgid: sgid });
}, { raw: true });

route('GET', '/attachments/:sgid', (ctx) => {
  const att = db.attachments.get(decodeURIComponent(ctx.params.sgid));
  if (!att) throw Errors.notFound('Attachment not found');
  ctx.res.writeHead(200, { 'Content-Type': att.contentType || 'application/octet-stream', 'Content-Length': att.data.length });
  ctx.res.end(att.data);
});

// ============================================================================
// People
// ============================================================================

route('GET', '/people.json', (ctx) => {
  let people = Array.from(db.people.values());
  if (!ctx.person.employee) {
    // Clients/collaborators only see people on projects they can access.
    people = people.filter((p) => p.id === ctx.person.id || p.employee || p.client);
  }
  people.sort((a, b) => a.name.localeCompare(b.name));
  const page = paginate(people, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(personProjection));
});

route('GET', '/people/:personId', (ctx) => {
  const person = db.people.get(paramId(ctx.params, 'personId'));
  if (!person) throw Errors.notFound('Person not found');
  sendJson(ctx.res, 200, personProjection(person));
});

route('GET', '/circles/people.json', (ctx) => {
  const people = Array.from(db.people.values()).filter((p) => p.can_ping && p.id !== ctx.person.id);
  sendJson(ctx.res, 200, people.map(personProjection));
});

route('GET', '/people/:personId/out_of_office.json', (ctx) => {
  const personId = paramId(ctx.params, 'personId');
  const person = db.people.get(personId);
  if (!person) throw Errors.notFound('Person not found');
  const rec = db.outOfOffice.get(personId) || { enabled: false, ongoing: false };
  sendJson(ctx.res, 200, {
    person: { id: person.id, name: person.name, avatar_url: person.avatar_url },
    enabled: !!rec.enabled,
    ongoing: !!rec.enabled && rec.start_date <= dateOnly(new Date()) && rec.end_date >= dateOnly(new Date()),
    start_date: rec.start_date,
    end_date: rec.end_date,
  });
});

route('POST', '/people/:personId/out_of_office.json', (ctx) => {
  const personId = paramId(ctx.params, 'personId');
  if (personId !== ctx.person.id && !ctx.person.admin && !ctx.person.owner) throw Errors.forbidden('Can only set your own out-of-office');
  const person = db.people.get(personId);
  if (!person) throw Errors.notFound('Person not found');
  const payload = ctx.body.out_of_office;
  if (!payload || typeof payload !== 'object') throw Errors.validation('`out_of_office` is required');
  const start = requireString(payload, 'start_date');
  const end = requireString(payload, 'end_date');
  db.outOfOffice.set(personId, { enabled: true, start_date: start, end_date: end });
  const rec = db.outOfOffice.get(personId);
  sendJson(ctx.res, 200, {
    person: { id: person.id, name: person.name, avatar_url: person.avatar_url },
    enabled: true,
    ongoing: rec.start_date <= dateOnly(new Date()) && rec.end_date >= dateOnly(new Date()),
    start_date: rec.start_date,
    end_date: rec.end_date,
  });
});

route('DELETE', '/people/:personId/out_of_office.json', (ctx) => {
  const personId = paramId(ctx.params, 'personId');
  if (personId !== ctx.person.id && !ctx.person.admin && !ctx.person.owner) throw Errors.forbidden('Can only clear your own out-of-office');
  db.outOfOffice.delete(personId);
  sendNoContent(ctx.res);
});

// ---- My* (viewer-scoped) ------------------------------------------------------

route('GET', '/my/profile.json', (ctx) => sendJson(ctx.res, 200, personProjection(ctx.person)));

route('PUT', '/my/profile.json', (ctx) => {
  const p = ctx.person;
  const name = optionalString(ctx.body, 'name');
  if (name !== undefined) p.name = name;
  const email = optionalString(ctx.body, 'email_address');
  if (email !== undefined) p.email_address = email;
  ['title', 'bio', 'location', 'time_zone_name', 'time_format'].forEach((f) => {
    const v = optionalString(ctx.body, f);
    if (v !== undefined) p[f === 'time_zone_name' ? 'time_zone' : f] = v;
  });
  p.updated_at = new Date();
  sendNoContent(ctx.res);
});

function preferencesProjection(personId) {
  const prefs = db.preferences.get(personId) || {};
  return {
    url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/my/preferences.json`,
    app_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/my/preferences`,
    time_zone_name: prefs.time_zone_name || 'America/Chicago',
    first_week_day: prefs.first_week_day || 'Sunday',
    time_format: prefs.time_format || '12h',
  };
}

route('GET', '/my/preferences.json', (ctx) => sendJson(ctx.res, 200, preferencesProjection(ctx.person.id)));

route('PUT', '/my/preferences.json', (ctx) => {
  const payload = ctx.body.person;
  if (!payload || typeof payload !== 'object') throw Errors.validation('`person` is required');
  const existing = db.preferences.get(ctx.person.id) || {};
  db.preferences.set(ctx.person.id, Object.assign(existing, payload));
  sendJson(ctx.res, 200, preferencesProjection(ctx.person.id));
});

route('GET', '/my/assignments.json', (ctx) => {
  const all = myAssignments(ctx.person, { includeCompleted: false });
  const priorities = all.filter((a) => a.priority);
  const nonPriorities = all.filter((a) => !a.priority);
  sendJson(ctx.res, 200, {
    priorities: priorities.map(myAssignmentProjection),
    non_priorities: nonPriorities.map(myAssignmentProjection),
  });
});

route('GET', '/my/assignments/completed.json', (ctx) => {
  const all = myAssignments(ctx.person, { includeCompleted: true }).filter((a) => a.rec.completed);
  const page = paginate(all, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(myAssignmentProjection));
});

route('GET', '/my/assignments/due.json', (ctx) => {
  const all = myAssignments(ctx.person, { includeCompleted: false }).filter((a) => a.rec.due_on);
  all.sort((a, b) => (a.rec.due_on < b.rec.due_on ? -1 : 1));
  const page = paginate(all, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(myAssignmentProjection));
});

function myAssignments(person, opts) {
  const out = [];
  for (const rec of db.recordings.values()) {
    if (rec.type !== 'Todo' && rec.type !== 'Kanban::Card') continue;
    if (!opts.includeCompleted && rec.completed) continue;
    const assigneeIds = rec.assignee_ids || [];
    if (!assigneeIds.includes(person.id)) continue;
    const project = projectOf(rec);
    if (project && !canAccessProject(person, project)) continue;
    out.push({ rec, priority: !!rec.due_on });
  }
  return out;
}

function myAssignmentProjection(entry) {
  const rec = entry.rec;
  const project = projectOf(rec);
  return {
    id: rec.id,
    app_url: appUrlFor(rec),
    content: rec.title || rec.content || '',
    starts_on: rec.starts_on || undefined,
    due_on: rec.due_on || undefined,
    bucket: project ? { id: project.id, name: project.name, app_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/projects/${project.id}` } : undefined,
    completed: !!rec.completed,
    type: rec.type,
    assignees: peopleByIds(rec.assignee_ids).map((p) => ({ id: p.id, name: p.name, avatar_url: p.avatar_url })),
    comments_count: commentsCountFor(rec.id),
    has_description: !!rec.description,
    parent: rec.parent_id ? { id: rec.parent_id, title: (db.recordings.get(rec.parent_id) || {}).title || '', app_url: db.recordings.has(rec.parent_id) ? appUrlFor(db.recordings.get(rec.parent_id)) : undefined } : undefined,
  };
}

route('GET', '/my/readings.json', (ctx) => {
  sendJson(ctx.res, 200, { unreads: [], reads: [], memories: [] });
});

route('PUT', '/my/unreads.json', (ctx) => {
  const readables = ctx.body.readables;
  if (!Array.isArray(readables)) throw Errors.validation('`readables` is required');
  const map = db.readings.get(ctx.person.id) || new Map();
  readables.forEach((sgid) => map.set(sgid, { read_at: new Date() }));
  db.readings.set(ctx.person.id, map);
  sendJson(ctx.res, 200, {});
});

route('GET', '/my/question_reminders.json', (ctx) => sendJson(ctx.res, 200, []));

// ============================================================================
// Projects
// ============================================================================

function projectProjection(project) {
  return {
    id: project.id,
    status: project.status,
    created_at: isoAt(project.created_at),
    updated_at: isoAt(project.updated_at),
    name: project.name,
    description: project.description || '',
    purpose: project.purpose || 'topic',
    clients_enabled: !!project.clientsEnabled,
    bookmark_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/projects/${project.id}/bookmark.json`,
    url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/projects/${project.id}.json`,
    app_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/projects/${project.id}`,
    dock: (project.dock || []).map(dockItemProjection),
    bookmarked: false,
    client_company: project.clientCompanyId ? { id: project.clientCompanyId, name: db.companies.get(project.clientCompanyId) } : undefined,
  };
}

function dockItemProjection(item) {
  return {
    id: item.id,
    title: item.title,
    name: item.type,
    enabled: !!item.enabled,
    position: item.position,
    url: jsonUrlFor(db.recordings.get(item.id) || { id: item.id, type: item.type }),
    app_url: appUrlFor(db.recordings.get(item.id) || { id: item.id, type: item.type, bucket_id: item.bucket_id }),
  };
}

function visibleProjects(person) {
  return Array.from(db.projects.values()).filter((p) => canAccessProject(person, p) && p.status !== 'trashed');
}

route('GET', '/projects.json', (ctx) => {
  const status = ctx.url.searchParams.get('status') || 'active';
  let list = visibleProjects(ctx.person).filter((p) => status === 'all' || p.status === status);
  list.sort((a, b) => a.name.localeCompare(b.name));
  const page = paginate(list, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(projectProjection));
});

route('POST', '/projects.json', (ctx) => {
  requireEmployee(ctx.person);
  const name = requireString(ctx.body, 'name');
  const description = optionalString(ctx.body, 'description') || '';
  const project = {
    id: nextId(),
    status: 'active',
    name,
    description,
    purpose: 'topic',
    clientsEnabled: false,
    allAccess: false,
    memberIds: [ctx.person.id],
    dock: [],
    created_at: new Date(),
    updated_at: new Date(),
  };
  db.projects.set(project.id, project);
  sendJson(ctx.res, 201, projectProjection(project));
});

route('GET', '/projects/:projectId', (ctx) => {
  const project = db.projects.get(paramId(ctx.params, 'projectId'));
  if (!project || project.status === 'trashed') throw Errors.notFound('Project not found');
  requireProjectAccess(ctx.person, project);
  sendJson(ctx.res, 200, projectProjection(project));
});

route('PUT', '/projects/:projectId', (ctx) => {
  const project = db.projects.get(paramId(ctx.params, 'projectId'));
  if (!project) throw Errors.notFound('Project not found');
  requireProjectAccess(ctx.person, project);
  project.name = requireString(ctx.body, 'name');
  const description = optionalString(ctx.body, 'description');
  if (description !== undefined) project.description = description;
  const admissions = optionalString(ctx.body, 'admissions');
  if (admissions !== undefined) project.allAccess = admissions !== 'invite';
  project.updated_at = new Date();
  sendJson(ctx.res, 200, projectProjection(project));
});

route('DELETE', '/projects/:projectId', (ctx) => {
  const project = db.projects.get(paramId(ctx.params, 'projectId'));
  if (!project) throw Errors.notFound('Project not found');
  requireProjectAccess(ctx.person, project);
  project.status = 'trashed';
  project.updated_at = new Date();
  sendNoContent(ctx.res);
});

route('GET', '/projects/:projectId/people.json', (ctx) => {
  const project = db.projects.get(paramId(ctx.params, 'projectId'));
  if (!project) throw Errors.notFound('Project not found');
  requireProjectAccess(ctx.person, project);
  const members = project.allAccess
    ? Array.from(db.people.values()).filter((p) => p.employee || project.memberIds.includes(p.id))
    : peopleByIds(project.memberIds);
  sendJson(ctx.res, 200, members.map(personProjection));
});

route('PUT', '/projects/:projectId/people/users.json', (ctx) => {
  const project = db.projects.get(paramId(ctx.params, 'projectId'));
  if (!project) throw Errors.notFound('Project not found');
  requireProjectAccess(ctx.person, project);
  requireEmployee(ctx.person);
  const grant = optionalIdArray(ctx.body, 'grant') || [];
  const revoke = optionalIdArray(ctx.body, 'revoke') || [];
  const created = [];
  if (Array.isArray(ctx.body.create)) {
    for (const c of ctx.body.create) {
      const p = createPerson({ name: requireString(c, 'name'), email_address: requireString(c, 'email_address'), title: c.title, employee: false });
      created.push(p);
      grant.push(p.id);
    }
  }
  const set = new Set(project.memberIds);
  grant.forEach((id) => set.add(id));
  revoke.forEach((id) => set.delete(id));
  project.memberIds = Array.from(set);
  sendJson(ctx.res, 200, {
    granted: peopleByIds(grant).map(personProjection),
    revoked: peopleByIds(revoke).map(personProjection),
  });
});

route('GET', '/projects/:projectId/timeline.json', (ctx) => {
  const project = db.projects.get(paramId(ctx.params, 'projectId'));
  if (!project) throw Errors.notFound('Project not found');
  requireProjectAccess(ctx.person, project);
  const events = db.events
    .filter((e) => {
      const rec = db.recordings.get(e.recording_id);
      return rec && rec.bucket_id === project.id;
    })
    .sort((a, b) => b.created_at - a.created_at)
    .map((e) => timelineEventProjection(e, project));
  sendJson(ctx.res, 200, events);
});

function timelineEventProjection(ev, project) {
  const rec = db.recordings.get(ev.recording_id);
  return {
    id: ev.id,
    created_at: isoAt(ev.created_at),
    kind: rec ? rec.type : 'Recording',
    parent_recording_id: rec ? rec.parent_id : null,
    url: rec ? jsonUrlFor(rec) : '',
    app_url: rec ? appUrlFor(rec) : '',
    creator: personProjection(db.people.get(ev.creator_id)),
    action: ev.action,
    target: rec ? rec.title || '' : '',
    title: rec ? rec.title || '' : '',
    summary_excerpt: rec && rec.content ? String(rec.content).replace(/<[^>]+>/g, '').slice(0, 140) : '',
    bucket: { id: project.id, name: project.name, type: 'Project' },
  };
}

route('GET', '/projects/:projectId/timesheet.json', (ctx) => {
  const project = db.projects.get(paramId(ctx.params, 'projectId'));
  if (!project) throw Errors.notFound('Project not found');
  requireProjectAccess(ctx.person, project);
  requireTimesheetAccess(ctx.person);
  const from = ctx.url.searchParams.get('from');
  const to = ctx.url.searchParams.get('to');
  const personId = ctx.url.searchParams.get('person_id');
  let entries = Array.from(db.recordings.values()).filter((r) => r.type === 'Timesheet::Entry' && r.bucket_id === project.id);
  if (from) entries = entries.filter((e) => e.date >= from);
  if (to) entries = entries.filter((e) => e.date <= to);
  if (personId) entries = entries.filter((e) => e.person_id === Number(personId));
  sendJson(ctx.res, 200, entries.map(timesheetEntryProjection));
});

function requireTimesheetAccess(person) {
  if (!person.can_access_timesheet && !person.owner && !person.admin) throw Errors.forbidden('Timesheet access required');
}

function timesheetEntryProjection(rec) {
  return Object.assign(recordingEnvelope(rec), {
    date: rec.date,
    description: rec.description || '',
    hours: rec.hours,
    person: personProjection(db.people.get(rec.person_id)),
  });
}

// ---- Gauges (per-project progress pill) --------------------------------------

function ensureGauge(project) {
  if (project.gaugeId && db.recordings.has(project.gaugeId)) return db.recordings.get(project.gaugeId);
  const gauge = createRecording('Gauge', {
    title: 'Progress',
    bucket_id: project.id,
    creator_id: project.memberIds[0],
    enabled: false,
    description: '',
    last_needle_position: 0,
    last_needle_color: 'green',
  });
  project.gaugeId = gauge.id;
  return gauge;
}

function gaugeProjection(rec) {
  return Object.assign(recordingEnvelope(rec), {
    description: rec.description || '',
    enabled: !!rec.enabled,
    last_needle_color: rec.last_needle_color,
    last_needle_position: rec.last_needle_position,
    previous_needle_position: rec.previous_needle_position,
  });
}

function gaugeNeedleProjection(rec) {
  return Object.assign(recordingEnvelope(rec), {
    description: rec.description || '',
    color: rec.color,
    position: rec.position,
  });
}

route('PUT', '/projects/:projectId/gauge.json', (ctx) => {
  const project = db.projects.get(paramId(ctx.params, 'projectId'));
  if (!project) throw Errors.notFound('Project not found');
  requireProjectAccess(ctx.person, project);
  const payload = ctx.body.gauge;
  if (!payload || typeof payload.enabled !== 'boolean') throw Errors.validation('`gauge.enabled` is required');
  const gauge = ensureGauge(project);
  gauge.enabled = payload.enabled;
  touch(gauge);
  sendJson(ctx.res, 200, gaugeProjection(gauge));
});

route('GET', '/projects/:projectId/gauge/needles.json', (ctx) => {
  const project = db.projects.get(paramId(ctx.params, 'projectId'));
  if (!project) throw Errors.notFound('Project not found');
  requireProjectAccess(ctx.person, project);
  const gauge = ensureGauge(project);
  const needles = Array.from(db.recordings.values())
    .filter((r) => r.type === 'Gauge::Needle' && r.parent_id === gauge.id)
    .sort((a, b) => b.created_at - a.created_at);
  const page = paginate(needles, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(gaugeNeedleProjection));
});

route('POST', '/projects/:projectId/gauge/needles.json', (ctx) => {
  const project = db.projects.get(paramId(ctx.params, 'projectId'));
  if (!project) throw Errors.notFound('Project not found');
  requireProjectAccess(ctx.person, project);
  const payload = ctx.body.gauge_needle;
  if (!payload || typeof payload.position !== 'number') throw Errors.validation('`gauge_needle.position` is required');
  const gauge = ensureGauge(project);
  const needle = createRecording('Gauge::Needle', {
    title: `Update — ${payload.position}%`,
    bucket_id: project.id,
    parent_id: gauge.id,
    creator_id: ctx.person.id,
    description: payload.description || '',
    color: payload.color || 'green',
    position: payload.position,
  });
  gauge.previous_needle_position = gauge.last_needle_position;
  gauge.last_needle_position = payload.position;
  gauge.last_needle_color = payload.color || 'green';
  touch(gauge);
  recordEvent(gauge.id, 'gauge_needle_created', ctx.person.id, { needle_id: needle.id });
  sendJson(ctx.res, 201, gaugeNeedleProjection(needle));
});

route('GET', '/gauge_needles/:needleId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'needleId'), 'Gauge::Needle');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, gaugeNeedleProjection(rec));
});

route('PUT', '/gauge_needles/:needleId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'needleId'), 'Gauge::Needle');
  requireRecordingAccess(ctx.person, rec);
  const payload = ctx.body.gauge_needle || {};
  if (payload.description !== undefined) rec.description = payload.description;
  touch(rec);
  sendJson(ctx.res, 200, gaugeNeedleProjection(rec));
});

route('DELETE', '/gauge_needles/:needleId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'needleId'), 'Gauge::Needle');
  requireRecordingAccess(ctx.person, rec);
  rec.status = 'trashed';
  touch(rec);
  sendNoContent(ctx.res);
});

route('GET', '/reports/gauges.json', (ctx) => {
  const gauges = visibleProjects(ctx.person)
    .map((p) => (p.gaugeId ? db.recordings.get(p.gaugeId) : null))
    .filter(Boolean);
  sendJson(ctx.res, 200, gauges.map(gaugeProjection));
});

// ============================================================================
// Dock / Tools
// ============================================================================

route('GET', '/dock/tools/:toolId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'toolId'));
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, toolProjection(rec));
});

function toolProjection(rec) {
  return {
    id: rec.id,
    status: rec.status,
    created_at: isoAt(rec.created_at),
    updated_at: isoAt(rec.updated_at),
    title: rec.title,
    name: rec.type,
    enabled: rec.status === 'active',
    position: rec.position,
    url: jsonUrlFor(rec),
    app_url: appUrlFor(rec),
    bucket: recordingBucket(rec.bucket_id),
  };
}

route('PUT', '/dock/tools/:toolId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'toolId'));
  requireRecordingAccess(ctx.person, rec);
  requireEmployee(ctx.person);
  rec.title = requireString(ctx.body, 'title');
  touch(rec);
  const dockItem = (projectOf(rec).dock || []).find((d) => d.id === rec.id);
  if (dockItem) dockItem.title = rec.title;
  sendJson(ctx.res, 200, toolProjection(rec));
});

route('DELETE', '/dock/tools/:toolId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'toolId'));
  const project = projectOf(rec);
  requireProjectAccess(ctx.person, project);
  requireEmployee(ctx.person);
  rec.status = 'trashed';
  touch(rec);
  project.dock = (project.dock || []).filter((d) => d.id !== rec.id);
  sendNoContent(ctx.res);
});

/** Clone (duplicate, empty) an existing dock tool alongside its source, per CloneTool's single source_recording_id contract. */
route('POST', '/dock/tools.json', (ctx) => {
  const sourceId = Number(ctx.body.source_recording_id);
  if (!Number.isInteger(sourceId)) throw Errors.validation('`source_recording_id` is required');
  const source = getRecordingOr404(sourceId);
  const project = projectOf(source);
  requireProjectAccess(ctx.person, project);
  requireEmployee(ctx.person);
  const title = optionalString(ctx.body, 'title') || source.title;
  const clone = instantiateTool(source.type, project, ctx.person, title);
  sendJson(ctx.res, 201, toolProjection(clone));
});

// ============================================================================
// Shared factories — people + dock tool instantiation
// ============================================================================

function createPerson(fields) {
  const id = nextId();
  const person = {
    id,
    name: fields.name,
    email_address: fields.email_address,
    title: fields.title || '',
    bio: fields.bio || '',
    location: fields.location || '',
    created_at: new Date(),
    updated_at: new Date(),
    admin: !!fields.admin,
    owner: !!fields.owner,
    client: !!fields.client,
    employee: fields.employee !== false,
    time_zone: fields.time_zone || 'America/Chicago',
    avatar_url: fields.avatar_url || null,
    company_id: fields.company_id,
    can_manage_projects: !!fields.can_manage_projects || !!fields.admin || !!fields.owner,
    can_manage_people: !!fields.can_manage_people || !!fields.admin || !!fields.owner,
    can_ping: fields.can_ping !== false,
    can_access_timesheet: !!fields.can_access_timesheet,
    can_access_hill_charts: !!fields.can_access_hill_charts,
    sample: !!fields.sample,
  };
  db.people.set(id, person);
  return person;
}

function addToDock(project, rec, opts) {
  project.dock = project.dock || [];
  const position = (opts && opts.position) || project.dock.length + 1;
  project.dock.push({ id: rec.id, type: rec.type, title: rec.title, enabled: true, position, bucket_id: project.id });
  rec.position = position;
  // Tool containers themselves are visible whenever the project has clients enabled;
  // fine-grained visibility is enforced per-item on the content within.
  rec.visible_to_clients = project.clientsEnabled !== false;
  return rec;
}

/** Registry of tool-root factories, keyed by dock tool type. Filled in by each tool's section below. */
const TOOL_FACTORIES = {};

function instantiateTool(type, project, creator, title) {
  const factory = TOOL_FACTORIES[type];
  if (!factory) throw Errors.validation(`Unsupported tool type: ${type}`);
  return factory(project, creator, title);
}

// ============================================================================
// Message Board
// ============================================================================

function messageBoardProjection(rec) {
  return Object.assign(recordingEnvelope(rec), {
    messages_count: Array.from(db.recordings.values()).filter((r) => r.type === 'Message' && r.parent_id === rec.id && r.status === 'active').length,
    messages_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/message_boards/${rec.id}/messages.json`,
    app_messages_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/buckets/${rec.bucket_id}/message_board/${rec.id}`,
  });
}

function messageTypeProjection(mt) {
  return { id: mt.id, name: mt.name, icon: mt.icon, created_at: isoAt(mt.created_at), updated_at: isoAt(mt.updated_at) };
}

function messageProjection(rec) {
  const env = recordingEnvelope(rec);
  env.subject = rec.subject || rec.title;
  env.content = rec.content || '';
  env.category = rec.category_id && db.messageTypes.has(rec.category_id) ? messageTypeProjection(db.messageTypes.get(rec.category_id)) : null;
  env.pinned = !!rec.pinned;
  return env;
}

TOOL_FACTORIES['Message::Board'] = function createMessageBoardTool(project, creator, title) {
  const board = createRecording('Message::Board', {
    title: title || 'Message Board',
    bucket_id: project.id,
    creator_id: creator.id,
  });
  addToDock(project, board);
  return board;
};

route('GET', '/message_boards/:boardId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'boardId'), 'Message::Board');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, messageBoardProjection(rec));
});

route('GET', '/message_boards/:boardId/messages.json', (ctx) => {
  const board = getRecordingOr404(paramId(ctx.params, 'boardId'), 'Message::Board');
  requireRecordingAccess(ctx.person, board);
  let messages = Array.from(db.recordings.values()).filter((r) => r.type === 'Message' && r.parent_id === board.id && r.status === 'active');
  if (ctx.person.client) messages = messages.filter((m) => m.visible_to_clients);
  messages.sort((a, b) => (b.pinned - a.pinned) || (b.created_at - a.created_at));
  const page = paginate(messages, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(messageProjection));
});

route('POST', '/message_boards/:boardId/messages.json', (ctx) => {
  const board = getRecordingOr404(paramId(ctx.params, 'boardId'), 'Message::Board');
  requireRecordingAccess(ctx.person, board);
  const subject = requireString(ctx.body, 'subject');
  const content = optionalString(ctx.body, 'content') || '';
  const status = optionalString(ctx.body, 'status') === 'drafted' ? 'drafted' : 'active';
  const categoryId = ctx.body.category_id ? Number(ctx.body.category_id) : null;
  if (categoryId && !db.messageTypes.has(categoryId)) throw Errors.validation('Unknown `category_id`');
  const message = createRecording('Message', {
    title: subject,
    subject,
    content,
    status,
    creator_id: ctx.person.id,
    bucket_id: board.bucket_id,
    parent_id: board.id,
    category_id: categoryId,
    visible_to_clients: false,
  });
  const subs = optionalIdArray(ctx.body, 'subscriptions');
  if (subs) setSubscribers(message.id, subs);
  if (status === 'active') recordEvent(message.id, 'created', ctx.person.id);
  sendJson(ctx.res, 201, messageProjection(message));
});

route('GET', '/messages/:messageId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'messageId'), 'Message');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, messageProjection(rec));
});

route('PUT', '/messages/:messageId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'messageId'), 'Message');
  requireRecordingAccess(ctx.person, rec);
  requireOwnVoiceOrAdmin(ctx.person, rec);
  const subject = optionalString(ctx.body, 'subject');
  if (subject !== undefined) { rec.subject = subject; rec.title = subject; }
  const content = optionalString(ctx.body, 'content');
  if (content !== undefined) rec.content = content;
  const status = optionalString(ctx.body, 'status');
  const wasDraft = rec.status === 'drafted';
  if (status !== undefined) rec.status = status === 'drafted' ? 'drafted' : 'active';
  if (ctx.body.category_id !== undefined) {
    const categoryId = ctx.body.category_id ? Number(ctx.body.category_id) : null;
    if (categoryId && !db.messageTypes.has(categoryId)) throw Errors.validation('Unknown `category_id`');
    rec.category_id = categoryId;
  }
  if (wasDraft && rec.status === 'active') recordEvent(rec.id, 'created', ctx.person.id);
  else recordEvent(rec.id, 'updated', ctx.person.id);
  touch(rec);
  sendJson(ctx.res, 200, messageProjection(rec));
});

// ---- Message categories (account-level) --------------------------------------

route('GET', '/categories.json', (ctx) => {
  const list = Array.from(db.messageTypes.values());
  sendJson(ctx.res, 200, list.map(messageTypeProjection));
});

route('POST', '/categories.json', (ctx) => {
  requireEmployee(ctx.person);
  const name = requireString(ctx.body, 'name');
  const icon = requireString(ctx.body, 'icon');
  const mt = { id: nextId(), name, icon, created_at: new Date(), updated_at: new Date() };
  db.messageTypes.set(mt.id, mt);
  sendJson(ctx.res, 201, messageTypeProjection(mt));
});

route('GET', '/categories/:typeId', (ctx) => {
  const mt = db.messageTypes.get(paramId(ctx.params, 'typeId'));
  if (!mt) throw Errors.notFound('Category not found');
  sendJson(ctx.res, 200, messageTypeProjection(mt));
});

route('PUT', '/categories/:typeId', (ctx) => {
  requireEmployee(ctx.person);
  const mt = db.messageTypes.get(paramId(ctx.params, 'typeId'));
  if (!mt) throw Errors.notFound('Category not found');
  const name = optionalString(ctx.body, 'name');
  if (name !== undefined) mt.name = name;
  const icon = optionalString(ctx.body, 'icon');
  if (icon !== undefined) mt.icon = icon;
  mt.updated_at = new Date();
  sendJson(ctx.res, 200, messageTypeProjection(mt));
});

route('DELETE', '/categories/:typeId', (ctx) => {
  requireEmployee(ctx.person);
  const id = paramId(ctx.params, 'typeId');
  if (!db.messageTypes.has(id)) throw Errors.notFound('Category not found');
  db.messageTypes.delete(id);
  sendNoContent(ctx.res);
});

// ============================================================================
// To-dos
// ============================================================================

function todosUnder(parentId) {
  return Array.from(db.recordings.values()).filter((r) => r.type === 'Todo' && r.parent_id === parentId && r.status !== 'trashed');
}
function ratioFor(items) {
  const done = items.filter((t) => t.completed).length;
  return `${done}/${items.length}`;
}

function todosetProjection(rec) {
  const lists = Array.from(db.recordings.values()).filter((r) => r.type === 'Todolist' && r.parent_id === rec.id && r.status === 'active');
  const allTodos = lists.flatMap((l) => todosUnder(l.id)).concat(
    Array.from(db.recordings.values()).filter((r) => r.type === 'Todolist::Group' && lists.some((l) => l.id === r.parent_id)).flatMap((g) => todosUnder(g.id))
  );
  return Object.assign(recordingEnvelope(rec), {
    name: rec.title,
    todolists_count: lists.length,
    todolists_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/todosets/${rec.id}/todolists.json`,
    app_todolists_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/buckets/${rec.bucket_id}/todos/${rec.id}`,
    completed_ratio: ratioFor(allTodos),
    completed: allTodos.length > 0 && allTodos.every((t) => t.completed),
  });
}

function todolistProjection(rec) {
  const todos = todosUnder(rec.id);
  return Object.assign(recordingEnvelope(rec), {
    name: rec.title,
    completed: todos.length > 0 && todos.every((t) => t.completed),
    completed_ratio: ratioFor(todos),
    todos_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/todolists/${rec.id}/todos.json`,
    app_todos_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/buckets/${rec.bucket_id}/todos/${rec.id}`,
    groups_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/todolists/${rec.id}/groups.json`,
  });
}

function todolistGroupProjection(rec) {
  const todos = todosUnder(rec.id);
  return Object.assign(recordingEnvelope(rec), {
    name: rec.title,
    completed: todos.length > 0 && todos.every((t) => t.completed),
    completed_ratio: ratioFor(todos),
    todos_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/todolists/${rec.id}/todos.json`,
    app_todos_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/buckets/${rec.bucket_id}/todos/${rec.id}`,
  });
}

function todolistOrGroupProjection(rec) {
  return rec.type === 'Todolist' ? { todolist: todolistProjection(rec) } : { group: todolistGroupProjection(rec) };
}

function todoProjection(rec) {
  return Object.assign(recordingEnvelope(rec), {
    description: rec.description || '',
    completed: !!rec.completed,
    content: rec.content || rec.title,
    starts_on: rec.starts_on || null,
    due_on: rec.due_on || null,
    assignees: peopleByIds(rec.assignee_ids).map(personProjection),
    completion_subscribers: peopleByIds(rec.completion_subscriber_ids).map(personProjection),
    completion_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/todos/${rec.id}/completion.json`,
  });
}

TOOL_FACTORIES['Todoset'] = function createTodosetTool(project, creator, title) {
  const todoset = createRecording('Todoset', {
    title: title || 'To-dos',
    bucket_id: project.id,
    creator_id: creator.id,
  });
  addToDock(project, todoset);
  return todoset;
};

function createTodolist(todoset, creator, { name, description }) {
  return createRecording('Todolist', {
    title: name,
    description: description || '',
    bucket_id: todoset.bucket_id,
    parent_id: todoset.id,
    creator_id: creator.id,
  });
}

route('GET', '/todosets/:todosetId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'todosetId'), 'Todoset');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, todosetProjection(rec));
});

route('GET', '/todosets/:todosetId/todolists.json', (ctx) => {
  const todoset = getRecordingOr404(paramId(ctx.params, 'todosetId'), 'Todoset');
  requireRecordingAccess(ctx.person, todoset);
  const status = ctx.url.searchParams.get('status') || 'active';
  let lists = Array.from(db.recordings.values()).filter((r) => r.type === 'Todolist' && r.parent_id === todoset.id);
  lists = lists.filter((l) => status === 'all' || l.status === status);
  if (ctx.person.client) lists = lists.filter((l) => l.visible_to_clients);
  lists.sort((a, b) => (a.position || 0) - (b.position || 0));
  const page = paginate(lists, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(todolistProjection));
});

route('POST', '/todosets/:todosetId/todolists.json', (ctx) => {
  const todoset = getRecordingOr404(paramId(ctx.params, 'todosetId'), 'Todoset');
  requireRecordingAccess(ctx.person, todoset);
  const name = requireString(ctx.body, 'name');
  const description = optionalString(ctx.body, 'description') || '';
  const list = createTodolist(todoset, ctx.person, { name, description });
  recordEvent(list.id, 'created', ctx.person.id);
  sendJson(ctx.res, 201, todolistProjection(list));
});

route('GET', '/todolists/:todolistId/groups.json', (ctx) => {
  const list = getRecordingOr404(paramId(ctx.params, 'todolistId'), 'Todolist');
  requireRecordingAccess(ctx.person, list);
  const groups = Array.from(db.recordings.values())
    .filter((r) => r.type === 'Todolist::Group' && r.parent_id === list.id && r.status === 'active')
    .sort((a, b) => (a.position || 0) - (b.position || 0));
  const page = paginate(groups, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(todolistGroupProjection));
});

route('POST', '/todolists/:todolistId/groups.json', (ctx) => {
  const list = getRecordingOr404(paramId(ctx.params, 'todolistId'), 'Todolist');
  requireRecordingAccess(ctx.person, list);
  const name = requireString(ctx.body, 'name');
  const group = createRecording('Todolist::Group', {
    title: name,
    bucket_id: list.bucket_id,
    parent_id: list.id,
    creator_id: ctx.person.id,
  });
  recordEvent(group.id, 'created', ctx.person.id);
  sendJson(ctx.res, 201, todolistGroupProjection(group));
});

route('PUT', '/todolists/:groupId/position.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'groupId'), 'Todolist::Group');
  requireRecordingAccess(ctx.person, rec);
  const position = ctx.body.position;
  if (!Number.isInteger(position)) throw Errors.validation('`position` must be an integer');
  rec.position = position;
  touch(rec);
  sendJson(ctx.res, 200, {});
});

route('GET', '/todolists/:id', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'id'), ['Todolist', 'Todolist::Group']);
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, todolistOrGroupProjection(rec));
});

route('PUT', '/todolists/:id', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'id'), ['Todolist', 'Todolist::Group']);
  requireRecordingAccess(ctx.person, rec);
  const name = optionalString(ctx.body, 'name');
  if (name !== undefined) rec.title = name;
  if (rec.type === 'Todolist') {
    const description = optionalString(ctx.body, 'description');
    if (description !== undefined) rec.description = description;
  }
  touch(rec);
  sendJson(ctx.res, 200, todolistOrGroupProjection(rec));
});

route('GET', '/todolists/:todolistId/todos.json', (ctx) => {
  const list = getRecordingOr404(paramId(ctx.params, 'todolistId'), ['Todolist', 'Todolist::Group']);
  requireRecordingAccess(ctx.person, list);
  const status = ctx.url.searchParams.get('status') || 'active';
  const completedParam = ctx.url.searchParams.get('completed');
  let todos = Array.from(db.recordings.values()).filter((r) => r.type === 'Todo' && r.parent_id === list.id);
  todos = todos.filter((t) => status === 'all' || t.status === status);
  if (completedParam !== null) todos = todos.filter((t) => t.completed === (completedParam === 'true'));
  if (ctx.person.client) todos = todos.filter((t) => t.visible_to_clients);
  todos.sort((a, b) => (a.position || 0) - (b.position || 0));
  const page = paginate(todos, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(todoProjection));
});

route('POST', '/todolists/:todolistId/todos.json', (ctx) => {
  const list = getRecordingOr404(paramId(ctx.params, 'todolistId'), ['Todolist', 'Todolist::Group']);
  requireRecordingAccess(ctx.person, list);
  const content = requireString(ctx.body, 'content');
  const description = optionalString(ctx.body, 'description') || '';
  const assigneeIds = optionalIdArray(ctx.body, 'assignee_ids') || [];
  const completionSubscriberIds = optionalIdArray(ctx.body, 'completion_subscriber_ids') || [];
  const dueOn = optionalString(ctx.body, 'due_on');
  const startsOn = optionalString(ctx.body, 'starts_on');
  const todo = createRecording('Todo', {
    title: content,
    content,
    description,
    bucket_id: list.bucket_id,
    parent_id: list.id,
    creator_id: ctx.person.id,
    completed: false,
    assignee_ids: assigneeIds,
    completion_subscriber_ids: completionSubscriberIds,
    due_on: dueOn || null,
    starts_on: startsOn || null,
    visible_to_clients: list.visible_to_clients,
  });
  assigneeIds.forEach((id) => subscribe(todo.id, id));
  recordEvent(todo.id, 'created', ctx.person.id, { added_person_ids: assigneeIds });
  sendJson(ctx.res, 201, todoProjection(todo));
});

route('GET', '/todos/:todoId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'todoId'), 'Todo');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, todoProjection(rec));
});

route('PUT', '/todos/:todoId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'todoId'), 'Todo');
  requireRecordingAccess(ctx.person, rec);
  const content = optionalString(ctx.body, 'content');
  if (content !== undefined) { rec.content = content; rec.title = content; }
  const description = optionalString(ctx.body, 'description');
  if (description !== undefined) rec.description = description;
  const assigneeIds = optionalIdArray(ctx.body, 'assignee_ids');
  if (assigneeIds !== undefined) rec.assignee_ids = assigneeIds;
  const completionSubscriberIds = optionalIdArray(ctx.body, 'completion_subscriber_ids');
  if (completionSubscriberIds !== undefined) rec.completion_subscriber_ids = completionSubscriberIds;
  if (ctx.body.due_on !== undefined) rec.due_on = ctx.body.due_on || null;
  if (ctx.body.starts_on !== undefined) rec.starts_on = ctx.body.starts_on || null;
  touch(rec);
  recordEvent(rec.id, 'updated', ctx.person.id);
  sendJson(ctx.res, 200, todoProjection(rec));
});

route('DELETE', '/todos/:todoId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'todoId'), 'Todo');
  requireRecordingAccess(ctx.person, rec);
  rec.status = 'trashed';
  touch(rec);
  recordEvent(rec.id, 'trashed', ctx.person.id);
  sendNoContent(ctx.res);
});

route('POST', '/todos/:todoId/completion.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'todoId'), 'Todo');
  requireRecordingAccess(ctx.person, rec);
  rec.completed = true;
  touch(rec);
  recordEvent(rec.id, 'completed', ctx.person.id, { notified_recipient_ids: rec.completion_subscriber_ids || [] });
  sendNoContent(ctx.res);
});

route('DELETE', '/todos/:todoId/completion.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'todoId'), 'Todo');
  requireRecordingAccess(ctx.person, rec);
  rec.completed = false;
  touch(rec);
  recordEvent(rec.id, 'uncompleted', ctx.person.id);
  sendNoContent(ctx.res);
});

route('PUT', '/todos/:todoId/position.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'todoId'), 'Todo');
  requireRecordingAccess(ctx.person, rec);
  const position = ctx.body.position;
  if (!Number.isInteger(position)) throw Errors.validation('`position` must be an integer');
  rec.position = position;
  if (ctx.body.parent_id !== undefined && ctx.body.parent_id !== null) {
    const newParent = getRecordingOr404(Number(ctx.body.parent_id), ['Todolist', 'Todolist::Group']);
    rec.parent_id = newParent.id;
  }
  touch(rec);
  sendJson(ctx.res, 200, {});
});

// ---- Hill chart (minimal) -----------------------------------------------------

route('GET', '/todosets/:todosetId/hill.json', (ctx) => {
  const todoset = getRecordingOr404(paramId(ctx.params, 'todosetId'), 'Todoset');
  requireRecordingAccess(ctx.person, todoset);
  const hc = todoset.hillChart || { trackedIds: [] };
  sendJson(ctx.res, 200, {
    enabled: (hc.trackedIds || []).length > 0,
    stale: false,
    updated_at: isoAt(todoset.updated_at),
    app_update_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/buckets/${todoset.bucket_id}/todos/${todoset.id}/hill_chart`,
    app_versions_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/buckets/${todoset.bucket_id}/todos/${todoset.id}/hill_chart/versions`,
    dots: [],
  });
});

route('PUT', '/todosets/:todosetId/hills/settings.json', (ctx) => {
  const todoset = getRecordingOr404(paramId(ctx.params, 'todosetId'), 'Todoset');
  requireRecordingAccess(ctx.person, todoset);
  const tracked = optionalIdArray(ctx.body, 'tracked') || [];
  todoset.hillChart = { trackedIds: tracked };
  touch(todoset);
  sendJson(ctx.res, 200, {
    enabled: tracked.length > 0,
    stale: false,
    updated_at: isoAt(todoset.updated_at),
    app_update_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/buckets/${todoset.bucket_id}/todos/${todoset.id}/hill_chart`,
    app_versions_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/buckets/${todoset.bucket_id}/todos/${todoset.id}/hill_chart/versions`,
    dots: [],
  });
});

// ============================================================================
// Card Table (Kanban)
// ============================================================================

function cardsIn(columnId) {
  return Array.from(db.recordings.values()).filter((r) => r.type === 'Kanban::Card' && r.parent_id === columnId && r.status === 'active');
}

function cardColumnProjection(rec) {
  const env = recordingEnvelope(rec);
  const cards = cardsIn(rec.id);
  env.color = rec.color || null;
  env.description = rec.description || '';
  env.cards_count = cards.length;
  env.comments_count = cards.reduce((n, c) => n + commentsCountFor(c.id), 0);
  env.cards_url = `${PUBLIC_BASE_URL}/${CONFIG.accountId}/card_tables/lists/${rec.id}/cards.json`;
  env.subscribers = peopleByIds(subscribersFor(rec.id)).map(personProjection);
  if (rec.onHoldId && db.recordings.has(rec.onHoldId)) {
    const onHold = db.recordings.get(rec.onHoldId);
    env.on_hold = {
      id: onHold.id,
      status: onHold.status,
      inherits_status: onHold.inherits_status,
      title: onHold.title,
      created_at: isoAt(onHold.created_at),
      updated_at: isoAt(onHold.updated_at),
      cards_count: cardsIn(onHold.id).length,
      cards_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/card_tables/lists/${onHold.id}/cards.json`,
    };
  }
  return env;
}

function cardTableProjection(rec) {
  const columns = Array.from(db.recordings.values()).filter((r) => r.type === 'Kanban::Column' && r.parent_id === rec.id && !r.onHoldParentId);
  const order = { triage: 0, column: 1, not_now: 2, done: 3 };
  columns.sort((a, b) => (order[a.kind] - order[b.kind]) || (a.position || 0) - (b.position || 0));
  return Object.assign(recordingEnvelope(rec), {
    subscribers: peopleByIds(subscribersFor(rec.id)).map(personProjection),
    lists: columns.map(cardColumnProjection),
  });
}

function cardProjection(rec) {
  const env = recordingEnvelope(rec);
  env.content = rec.content || '';
  env.description = rec.description || '';
  env.due_on = rec.due_on || null;
  env.completed = !!rec.completed;
  env.completed_at = rec.completed_at ? isoAt(rec.completed_at) : null;
  env.completion_url = `${PUBLIC_BASE_URL}/${CONFIG.accountId}/card_tables/cards/${rec.id}/completion.json`;
  env.assignees = peopleByIds(rec.assignee_ids).map(personProjection);
  env.completion_subscribers = peopleByIds(rec.completion_subscriber_ids).map(personProjection);
  env.steps = cardStepsFor(rec.id).map(cardStepProjection);
  return env;
}

function cardStepsFor(cardId) {
  return Array.from(db.recordings.values())
    .filter((r) => r.type === 'Kanban::Step' && r.parent_id === cardId && r.status === 'active')
    .sort((a, b) => (a.position || 0) - (b.position || 0));
}

function cardStepProjection(rec) {
  const env = recordingEnvelope(rec);
  env.due_on = rec.due_on || null;
  env.completed = !!rec.completed;
  env.completed_at = rec.completed_at ? isoAt(rec.completed_at) : null;
  env.assignees = peopleByIds(rec.assignee_ids).map(personProjection);
  env.completion_url = `${PUBLIC_BASE_URL}/${CONFIG.accountId}/card_tables/steps/${rec.id}/completions.json`;
  return env;
}

function createCardColumn(table, creator, { title, description, kind, position, color }) {
  const col = createRecording('Kanban::Column', {
    title,
    description: description || '',
    bucket_id: table.bucket_id,
    parent_id: table.id,
    creator_id: creator.id,
    kind: kind || 'column',
    color: color || null,
    position: position || 0,
  });
  return col;
}

TOOL_FACTORIES['Kanban::Board'] = function createCardTableTool(project, creator, title) {
  const table = createRecording('Kanban::Board', {
    title: title || 'Card Table',
    bucket_id: project.id,
    creator_id: creator.id,
  });
  addToDock(project, table);
  createCardColumn(table, creator, { title: 'Triage', kind: 'triage', position: 0 });
  createCardColumn(table, creator, { title: 'Not now', kind: 'not_now', position: 9998 });
  createCardColumn(table, creator, { title: 'Done', kind: 'done', position: 9999 });
  return table;
};

route('GET', '/card_tables/:cardTableId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'cardTableId'), 'Kanban::Board');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, cardTableProjection(rec));
});

route('POST', '/card_tables/:cardTableId/columns.json', (ctx) => {
  const table = getRecordingOr404(paramId(ctx.params, 'cardTableId'), 'Kanban::Board');
  requireRecordingAccess(ctx.person, table);
  const title = requireString(ctx.body, 'title');
  const description = optionalString(ctx.body, 'description') || '';
  const userColumns = Array.from(db.recordings.values()).filter((r) => r.type === 'Kanban::Column' && r.parent_id === table.id && r.kind === 'column');
  const col = createCardColumn(table, ctx.person, { title, description, kind: 'column', position: userColumns.length + 1 });
  recordEvent(col.id, 'created', ctx.person.id);
  sendJson(ctx.res, 201, cardColumnProjection(col));
});

route('POST', '/card_tables/:cardTableId/moves.json', (ctx) => {
  const table = getRecordingOr404(paramId(ctx.params, 'cardTableId'), 'Kanban::Board');
  requireRecordingAccess(ctx.person, table);
  const sourceId = Number(ctx.body.source_id);
  const targetId = ctx.body.target_id !== undefined ? Number(ctx.body.target_id) : null;
  const source = getRecordingOr404(sourceId, 'Kanban::Column');
  if (source.kind !== 'column') throw Errors.validation('Only user-created columns can be reordered');
  if (targetId !== null) getRecordingOr404(targetId, 'Kanban::Column');
  if (ctx.body.position !== undefined) source.position = Number(ctx.body.position);
  touch(source);
  sendNoContent(ctx.res);
});

route('GET', '/card_tables/columns/:columnId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'columnId'), 'Kanban::Column');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, cardColumnProjection(rec));
});

route('PUT', '/card_tables/columns/:columnId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'columnId'), 'Kanban::Column');
  requireRecordingAccess(ctx.person, rec);
  if (rec.kind === 'not_now' || rec.kind === 'done') throw Errors.forbidden('Built-in columns cannot be renamed');
  const title = optionalString(ctx.body, 'title');
  if (title !== undefined) rec.title = title;
  const description = optionalString(ctx.body, 'description');
  if (description !== undefined) rec.description = description;
  touch(rec);
  sendJson(ctx.res, 200, cardColumnProjection(rec));
});

route('PUT', '/buckets/:bucketId/card_tables/columns/:columnId/color.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'columnId'), 'Kanban::Column');
  requireRecordingAccess(ctx.person, rec);
  rec.color = requireString(ctx.body, 'color');
  touch(rec);
  sendJson(ctx.res, 200, cardColumnProjection(rec));
});

route('POST', '/buckets/:bucketId/card_tables/columns/:columnId/on_hold.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'columnId'), 'Kanban::Column');
  requireRecordingAccess(ctx.person, rec);
  if (!rec.onHoldId) {
    const onHold = createRecording('Kanban::Column', {
      title: `${rec.title}: On hold`,
      bucket_id: rec.bucket_id,
      parent_id: rec.parent_id,
      creator_id: ctx.person.id,
      kind: 'on_hold',
      onHoldParentId: rec.id,
    });
    rec.onHoldId = onHold.id;
  }
  touch(rec);
  sendJson(ctx.res, 200, cardColumnProjection(rec));
});

route('DELETE', '/buckets/:bucketId/card_tables/columns/:columnId/on_hold.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'columnId'), 'Kanban::Column');
  requireRecordingAccess(ctx.person, rec);
  if (rec.onHoldId && db.recordings.has(rec.onHoldId)) {
    const onHold = db.recordings.get(rec.onHoldId);
    // Cards on hold move back into the parent column.
    cardsIn(onHold.id).forEach((c) => { c.parent_id = rec.id; });
    onHold.status = 'trashed';
  }
  rec.onHoldId = null;
  touch(rec);
  sendJson(ctx.res, 200, cardColumnProjection(rec));
});

route('GET', '/card_tables/lists/:columnId/cards.json', (ctx) => {
  const column = getRecordingOr404(paramId(ctx.params, 'columnId'), 'Kanban::Column');
  requireRecordingAccess(ctx.person, column);
  let cards = cardsIn(column.id);
  if (ctx.person.client) cards = cards.filter((c) => c.visible_to_clients);
  cards.sort((a, b) => (a.position || 0) - (b.position || 0));
  const page = paginate(cards, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(cardProjection));
});

route('POST', '/card_tables/lists/:columnId/cards.json', (ctx) => {
  const column = getRecordingOr404(paramId(ctx.params, 'columnId'), 'Kanban::Column');
  requireRecordingAccess(ctx.person, column);
  const title = requireString(ctx.body, 'title');
  const content = optionalString(ctx.body, 'content') || '';
  const dueOn = optionalString(ctx.body, 'due_on');
  const card = createRecording('Kanban::Card', {
    title,
    content,
    due_on: dueOn || null,
    completed: column.kind === 'done',
    completed_at: column.kind === 'done' ? new Date() : null,
    bucket_id: column.bucket_id,
    parent_id: column.id,
    creator_id: ctx.person.id,
    assignee_ids: [],
    completion_subscriber_ids: [],
    position: cardsIn(column.id).length + 1,
    visible_to_clients: column.visible_to_clients,
  });
  recordEvent(card.id, 'created', ctx.person.id);
  sendJson(ctx.res, 201, cardProjection(card));
});

route('DELETE', '/card_tables/lists/:columnId/subscription.json', (ctx) => {
  const column = getRecordingOr404(paramId(ctx.params, 'columnId'), 'Kanban::Column');
  requireRecordingAccess(ctx.person, column);
  unsubscribe(column.id, ctx.person.id);
  sendNoContent(ctx.res);
});

route('POST', '/card_tables/lists/:columnId/subscription.json', (ctx) => {
  const column = getRecordingOr404(paramId(ctx.params, 'columnId'), 'Kanban::Column');
  requireRecordingAccess(ctx.person, column);
  subscribe(column.id, ctx.person.id);
  sendNoContent(ctx.res);
});

route('GET', '/card_tables/cards/:cardId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'cardId'), 'Kanban::Card');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, cardProjection(rec));
});

route('PUT', '/card_tables/cards/:cardId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'cardId'), 'Kanban::Card');
  requireRecordingAccess(ctx.person, rec);
  const title = optionalString(ctx.body, 'title');
  if (title !== undefined) rec.title = title;
  const content = optionalString(ctx.body, 'content');
  if (content !== undefined) rec.content = content;
  if (ctx.body.due_on !== undefined) rec.due_on = ctx.body.due_on || null;
  const assigneeIds = optionalIdArray(ctx.body, 'assignee_ids');
  if (assigneeIds !== undefined) rec.assignee_ids = assigneeIds;
  touch(rec);
  recordEvent(rec.id, 'updated', ctx.person.id);
  sendJson(ctx.res, 200, cardProjection(rec));
});

route('POST', '/card_tables/cards/:cardId/moves.json', (ctx) => {
  const card = getRecordingOr404(paramId(ctx.params, 'cardId'), 'Kanban::Card');
  requireRecordingAccess(ctx.person, card);
  const columnId = Number(ctx.body.column_id);
  const column = getRecordingOr404(columnId, 'Kanban::Column');
  const position = Number.isInteger(ctx.body.position) ? ctx.body.position : 1;
  const fromColumnId = card.parent_id;
  card.parent_id = column.id;
  card.position = position;
  card.completed = column.kind === 'done';
  card.completed_at = card.completed ? new Date() : null;
  touch(card);
  recordEvent(card.id, 'moved', ctx.person.id, { from_column_id: fromColumnId, to_column_id: column.id });
  sendNoContent(ctx.res);
});

route('POST', '/card_tables/cards/:cardId/steps.json', (ctx) => {
  const card = getRecordingOr404(paramId(ctx.params, 'cardId'), 'Kanban::Card');
  requireRecordingAccess(ctx.person, card);
  const title = requireString(ctx.body, 'title');
  const dueOn = optionalString(ctx.body, 'due_on');
  const assigneeIds = optionalIdArray(ctx.body, 'assignee_ids') || [];
  const step = createRecording('Kanban::Step', {
    title,
    due_on: dueOn || null,
    completed: false,
    bucket_id: card.bucket_id,
    parent_id: card.id,
    creator_id: ctx.person.id,
    assignee_ids: assigneeIds,
    position: cardStepsFor(card.id).length + 1,
  });
  sendJson(ctx.res, 201, cardStepProjection(step));
});

route('POST', '/card_tables/cards/:cardId/positions.json', (ctx) => {
  const card = getRecordingOr404(paramId(ctx.params, 'cardId'), 'Kanban::Card');
  requireRecordingAccess(ctx.person, card);
  const sourceId = Number(ctx.body.source_id);
  const step = getRecordingOr404(sourceId, 'Kanban::Step');
  if (step.parent_id !== card.id) throw Errors.validation('Step does not belong to this card');
  step.position = Number(ctx.body.position);
  touch(step);
  sendJson(ctx.res, 200, {});
});

route('GET', '/card_tables/steps/:stepId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'stepId'), 'Kanban::Step');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, cardStepProjection(rec));
});

route('PUT', '/card_tables/steps/:stepId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'stepId'), 'Kanban::Step');
  requireRecordingAccess(ctx.person, rec);
  const title = optionalString(ctx.body, 'title');
  if (title !== undefined) rec.title = title;
  if (ctx.body.due_on !== undefined) rec.due_on = ctx.body.due_on || null;
  const assigneeIds = optionalIdArray(ctx.body, 'assignee_ids');
  if (assigneeIds !== undefined) rec.assignee_ids = assigneeIds;
  touch(rec);
  sendJson(ctx.res, 200, cardStepProjection(rec));
});

route('PUT', '/card_tables/steps/:stepId/completions.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'stepId'), 'Kanban::Step');
  requireRecordingAccess(ctx.person, rec);
  const completion = ctx.body.completion;
  if (typeof completion !== 'string') throw Errors.validation('`completion` is required');
  rec.completed = completion === 'on';
  rec.completed_at = rec.completed ? new Date() : null;
  touch(rec);
  sendJson(ctx.res, 200, cardStepProjection(rec));
});

// ============================================================================
// Docs & Files (Vault)
// ============================================================================

function vaultChildren(vaultId, type) {
  return Array.from(db.recordings.values()).filter((r) => r.type === type && r.parent_id === vaultId && r.status === 'active');
}

function vaultProjection(rec) {
  return Object.assign(recordingEnvelope(rec), {
    documents_count: vaultChildren(rec.id, 'Document').length,
    documents_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/vaults/${rec.id}/documents.json`,
    uploads_count: vaultChildren(rec.id, 'Upload').length,
    uploads_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/vaults/${rec.id}/uploads.json`,
    vaults_count: vaultChildren(rec.id, 'Vault').length,
    vaults_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/vaults/${rec.id}/vaults.json`,
  });
}

function documentProjection(rec) {
  return recordingEnvelope(rec);
}

function uploadProjection(rec) {
  const env = recordingEnvelope(rec);
  env.description = rec.description || '';
  env.content_type = rec.content_type;
  env.byte_size = rec.byte_size;
  env.width = rec.width || null;
  env.height = rec.height || null;
  env.download_url = rec.download_url;
  env.filename = rec.filename;
  return env;
}

TOOL_FACTORIES['Vault'] = function createVaultTool(project, creator, title) {
  const vault = createRecording('Vault', {
    title: title || 'Docs & Files',
    bucket_id: project.id,
    creator_id: creator.id,
  });
  addToDock(project, vault);
  return vault;
};

route('GET', '/vaults/:vaultId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'vaultId'), 'Vault');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, vaultProjection(rec));
});

route('PUT', '/vaults/:vaultId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'vaultId'), 'Vault');
  requireRecordingAccess(ctx.person, rec);
  rec.title = optionalString(ctx.body, 'title') || rec.title;
  touch(rec);
  sendJson(ctx.res, 200, vaultProjection(rec));
});

route('GET', '/vaults/:vaultId/vaults.json', (ctx) => {
  const parent = getRecordingOr404(paramId(ctx.params, 'vaultId'), 'Vault');
  requireRecordingAccess(ctx.person, parent);
  const list = vaultChildren(parent.id, 'Vault');
  const page = paginate(list, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(vaultProjection));
});

route('POST', '/vaults/:vaultId/vaults.json', (ctx) => {
  const parent = getRecordingOr404(paramId(ctx.params, 'vaultId'), 'Vault');
  requireRecordingAccess(ctx.person, parent);
  const title = requireString(ctx.body, 'title');
  const folder = createRecording('Vault', {
    title,
    bucket_id: parent.bucket_id,
    parent_id: parent.id,
    creator_id: ctx.person.id,
  });
  recordEvent(folder.id, 'created', ctx.person.id);
  sendJson(ctx.res, 201, vaultProjection(folder));
});

route('GET', '/vaults/:vaultId/documents.json', (ctx) => {
  const vault = getRecordingOr404(paramId(ctx.params, 'vaultId'), 'Vault');
  requireRecordingAccess(ctx.person, vault);
  let docs = vaultChildren(vault.id, 'Document');
  if (ctx.person.client) docs = docs.filter((d) => d.visible_to_clients);
  docs.sort((a, b) => b.created_at - a.created_at);
  const page = paginate(docs, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(documentProjection));
});

route('POST', '/vaults/:vaultId/documents.json', (ctx) => {
  const vault = getRecordingOr404(paramId(ctx.params, 'vaultId'), 'Vault');
  requireRecordingAccess(ctx.person, vault);
  const title = requireString(ctx.body, 'title');
  const content = optionalString(ctx.body, 'content') || '';
  const status = optionalString(ctx.body, 'status') === 'drafted' ? 'drafted' : 'active';
  const doc = createRecording('Document', {
    title,
    content,
    status,
    bucket_id: vault.bucket_id,
    parent_id: vault.id,
    creator_id: ctx.person.id,
  });
  const subs = optionalIdArray(ctx.body, 'subscriptions');
  if (subs) setSubscribers(doc.id, subs);
  if (status === 'active') recordEvent(doc.id, 'created', ctx.person.id);
  sendJson(ctx.res, 201, documentProjection(doc));
});

route('GET', '/documents/:documentId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'documentId'), 'Document');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, documentProjection(rec));
});

route('PUT', '/documents/:documentId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'documentId'), 'Document');
  requireRecordingAccess(ctx.person, rec);
  requireOwnVoiceOrAdmin(ctx.person, rec);
  const title = optionalString(ctx.body, 'title');
  if (title !== undefined) rec.title = title;
  const content = optionalString(ctx.body, 'content');
  if (content !== undefined) rec.content = content;
  touch(rec);
  recordEvent(rec.id, 'updated', ctx.person.id);
  sendJson(ctx.res, 200, documentProjection(rec));
});

route('GET', '/vaults/:vaultId/uploads.json', (ctx) => {
  const vault = getRecordingOr404(paramId(ctx.params, 'vaultId'), 'Vault');
  requireRecordingAccess(ctx.person, vault);
  let uploads = vaultChildren(vault.id, 'Upload');
  if (ctx.person.client) uploads = uploads.filter((u) => u.visible_to_clients);
  uploads.sort((a, b) => b.created_at - a.created_at);
  const page = paginate(uploads, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(uploadProjection));
});

route('POST', '/vaults/:vaultId/uploads.json', (ctx) => {
  const vault = getRecordingOr404(paramId(ctx.params, 'vaultId'), 'Vault');
  requireRecordingAccess(ctx.person, vault);
  const sgid = requireString(ctx.body, 'attachable_sgid');
  const att = db.attachments.get(sgid);
  const baseName = optionalString(ctx.body, 'base_name') || (att ? att.filename : 'file');
  const upload = createRecording('Upload', {
    title: baseName,
    description: optionalString(ctx.body, 'description') || '',
    bucket_id: vault.bucket_id,
    parent_id: vault.id,
    creator_id: ctx.person.id,
    content_type: att ? att.contentType : 'application/octet-stream',
    byte_size: att ? att.data.length : 0,
    filename: baseName,
    download_url: att ? `${PUBLIC_BASE_URL}/${CONFIG.accountId}/attachments/${encodeURIComponent(sgid)}` : null,
  });
  db.uploadVersions.set(upload.id, [uploadProjection(upload)]);
  const subs = optionalIdArray(ctx.body, 'subscriptions');
  if (subs) setSubscribers(upload.id, subs);
  recordEvent(upload.id, 'created', ctx.person.id);
  sendJson(ctx.res, 201, uploadProjection(upload));
});

route('GET', '/uploads/:uploadId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'uploadId'), 'Upload');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, uploadProjection(rec));
});

route('PUT', '/uploads/:uploadId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'uploadId'), 'Upload');
  requireRecordingAccess(ctx.person, rec);
  const description = optionalString(ctx.body, 'description');
  if (description !== undefined) rec.description = description;
  const baseName = optionalString(ctx.body, 'base_name');
  if (baseName !== undefined) { rec.filename = baseName; rec.title = baseName; }
  touch(rec);
  const versions = db.uploadVersions.get(rec.id) || [];
  versions.push(uploadProjection(rec));
  db.uploadVersions.set(rec.id, versions);
  sendJson(ctx.res, 200, uploadProjection(rec));
});

route('GET', '/uploads/:uploadId/versions.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'uploadId'), 'Upload');
  requireRecordingAccess(ctx.person, rec);
  const versions = db.uploadVersions.get(rec.id) || [uploadProjection(rec)];
  sendJson(ctx.res, 200, versions);
});

// ============================================================================
// Chat (Campfire)
// ============================================================================

function campfireProjection(rec) {
  return Object.assign(recordingEnvelope(rec), {
    topic: rec.topic || '',
    lines_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/chats/${rec.id}/lines.json`,
    files_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/chats/${rec.id}/uploads.json`,
  });
}

function campfireLineProjection(rec) {
  const env = recordingEnvelope(rec);
  env.content = rec.content || '';
  env.attachments = rec.attachments || [];
  return env;
}

function chatbotProjection(rec) {
  return {
    id: rec.id,
    created_at: isoAt(rec.created_at),
    updated_at: isoAt(rec.updated_at),
    service_name: rec.service_name,
    command_url: rec.command_url || null,
    url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/chats/${rec.campfire_id}/integrations/${rec.id}.json`,
    app_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/buckets/${rec.bucket_id}/chats/${rec.campfire_id}/integrations/${rec.id}`,
    lines_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/chats/${rec.campfire_id}/integrations/${rec.id}/lines.json`,
  };
}

TOOL_FACTORIES['Chat::Transcript'] = function createCampfireTool(project, creator, title) {
  const campfire = createRecording('Chat::Transcript', {
    title: title || 'Chat',
    bucket_id: project.id,
    creator_id: creator.id,
    topic: '',
  });
  addToDock(project, campfire);
  return campfire;
};

route('GET', '/chats.json', (ctx) => {
  const list = Array.from(db.recordings.values()).filter((r) => r.type === 'Chat::Transcript' && r.status === 'active' && canAccessProject(ctx.person, projectOf(r)));
  sendJson(ctx.res, 200, list.map(campfireProjection));
});

route('GET', '/chats/:campfireId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'campfireId'), 'Chat::Transcript');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, campfireProjection(rec));
});

route('GET', '/chats/:campfireId/lines.json', (ctx) => {
  const campfire = getRecordingOr404(paramId(ctx.params, 'campfireId'), 'Chat::Transcript');
  requireRecordingAccess(ctx.person, campfire);
  let lines = Array.from(db.recordings.values()).filter((r) => r.type === 'Chat::Line' && r.parent_id === campfire.id && r.status === 'active');
  const direction = ctx.url.searchParams.get('direction') === 'asc' ? 1 : -1;
  lines.sort((a, b) => direction * (b.created_at - a.created_at));
  const page = paginate(lines, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(campfireLineProjection));
});

route('POST', '/chats/:campfireId/lines.json', (ctx) => {
  const campfire = getRecordingOr404(paramId(ctx.params, 'campfireId'), 'Chat::Transcript');
  requireRecordingAccess(ctx.person, campfire);
  const content = requireString(ctx.body, 'content');
  const line = createRecording('Chat::Line', {
    title: 'Chat line',
    content,
    bucket_id: campfire.bucket_id,
    parent_id: campfire.id,
    creator_id: ctx.person.id,
    attachments: [],
  });
  sendJson(ctx.res, 201, campfireLineProjection(line));
});

route('GET', '/chats/:campfireId/lines/:lineId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'lineId'), 'Chat::Line');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, campfireLineProjection(rec));
});

route('DELETE', '/chats/:campfireId/lines/:lineId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'lineId'), 'Chat::Line');
  requireRecordingAccess(ctx.person, rec);
  requireOwnVoiceOrAdmin(ctx.person, rec);
  rec.status = 'trashed';
  touch(rec);
  sendNoContent(ctx.res);
});

route('GET', '/chats/:campfireId/uploads.json', (ctx) => {
  const campfire = getRecordingOr404(paramId(ctx.params, 'campfireId'), 'Chat::Transcript');
  requireRecordingAccess(ctx.person, campfire);
  const lines = Array.from(db.recordings.values()).filter((r) => r.type === 'Chat::Line' && r.parent_id === campfire.id && r.status === 'active' && (r.attachments || []).length);
  sendJson(ctx.res, 200, lines.map(campfireLineProjection));
});

route('POST', '/chats/:campfireId/uploads.json', (ctx) => {
  const campfire = getRecordingOr404(paramId(ctx.params, 'campfireId'), 'Chat::Transcript');
  requireRecordingAccess(ctx.person, campfire);
  const name = ctx.url.searchParams.get('name');
  if (!name) throw Errors.validation('`name` query parameter is required');
  const sgid = `gid://sample/CampfireUpload/${nextId()}`;
  db.attachments.set(sgid, { contentType: ctx.contentType, data: ctx.rawBody, filename: name });
  const downloadUrl = `${PUBLIC_BASE_URL}/${CONFIG.accountId}/attachments/${encodeURIComponent(sgid)}`;
  const line = createRecording('Chat::Line', {
    title: 'Upload',
    content: '',
    bucket_id: campfire.bucket_id,
    parent_id: campfire.id,
    creator_id: ctx.person.id,
    attachments: [{ title: name, url: downloadUrl, filename: name, content_type: ctx.contentType, byte_size: ctx.rawBody.length, download_url: downloadUrl }],
  });
  sendJson(ctx.res, 201, campfireLineProjection(line));
}, { raw: true });

route('GET', '/chats/:campfireId/integrations.json', (ctx) => {
  const campfire = getRecordingOr404(paramId(ctx.params, 'campfireId'), 'Chat::Transcript');
  requireRecordingAccess(ctx.person, campfire);
  const list = Array.from(db.chatbots.values()).filter((c) => c.campfire_id === campfire.id);
  sendJson(ctx.res, 200, list.map(chatbotProjection));
});

route('POST', '/chats/:campfireId/integrations.json', (ctx) => {
  const campfire = getRecordingOr404(paramId(ctx.params, 'campfireId'), 'Chat::Transcript');
  requireRecordingAccess(ctx.person, campfire);
  const bot = {
    id: nextId(),
    campfire_id: campfire.id,
    bucket_id: campfire.bucket_id,
    service_name: requireString(ctx.body, 'service_name'),
    command_url: optionalString(ctx.body, 'command_url') || null,
    created_at: new Date(),
    updated_at: new Date(),
  };
  db.chatbots.set(bot.id, bot);
  sendJson(ctx.res, 201, chatbotProjection(bot));
});

route('GET', '/chats/:campfireId/integrations/:chatbotId', (ctx) => {
  const bot = db.chatbots.get(paramId(ctx.params, 'chatbotId'));
  if (!bot || bot.campfire_id !== paramId(ctx.params, 'campfireId')) throw Errors.notFound('Chatbot not found');
  sendJson(ctx.res, 200, chatbotProjection(bot));
});

route('PUT', '/chats/:campfireId/integrations/:chatbotId', (ctx) => {
  const bot = db.chatbots.get(paramId(ctx.params, 'chatbotId'));
  if (!bot || bot.campfire_id !== paramId(ctx.params, 'campfireId')) throw Errors.notFound('Chatbot not found');
  bot.service_name = requireString(ctx.body, 'service_name');
  bot.command_url = optionalString(ctx.body, 'command_url') || null;
  bot.updated_at = new Date();
  sendJson(ctx.res, 200, chatbotProjection(bot));
});

route('DELETE', '/chats/:campfireId/integrations/:chatbotId', (ctx) => {
  const bot = db.chatbots.get(paramId(ctx.params, 'chatbotId'));
  if (!bot || bot.campfire_id !== paramId(ctx.params, 'campfireId')) throw Errors.notFound('Chatbot not found');
  db.chatbots.delete(bot.id);
  sendNoContent(ctx.res);
});

// ============================================================================
// Schedule (Calendar)
// ============================================================================

function scheduleProjection(rec) {
  const entries = Array.from(db.recordings.values()).filter((r) => r.type === 'Schedule::Entry' && r.parent_id === rec.id && r.status === 'active');
  return Object.assign(recordingEnvelope(rec), {
    include_due_assignments: rec.include_due_assignments !== false,
    entries_count: entries.length,
    entries_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/schedules/${rec.id}/entries.json`,
  });
}

function scheduleEntryProjection(rec) {
  const env = recordingEnvelope(rec);
  env.summary = rec.summary;
  env.description = rec.description || '';
  env.all_day = !!rec.all_day;
  env.starts_at = isoAt(rec.starts_at);
  env.ends_at = isoAt(rec.ends_at);
  env.participants = peopleByIds(rec.participant_ids).map(personProjection);
  return env;
}

TOOL_FACTORIES['Schedule'] = function createScheduleTool(project, creator, title) {
  const schedule = createRecording('Schedule', {
    title: title || 'Schedule',
    bucket_id: project.id,
    creator_id: creator.id,
    include_due_assignments: true,
  });
  addToDock(project, schedule);
  return schedule;
};

route('GET', '/schedules/:scheduleId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'scheduleId'), 'Schedule');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, scheduleProjection(rec));
});

route('PUT', '/schedules/:scheduleId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'scheduleId'), 'Schedule');
  requireRecordingAccess(ctx.person, rec);
  rec.include_due_assignments = !!requireBool(ctx.body, 'include_due_assignments');
  touch(rec);
  sendJson(ctx.res, 200, scheduleProjection(rec));
});

route('GET', '/schedules/:scheduleId/entries.json', (ctx) => {
  const schedule = getRecordingOr404(paramId(ctx.params, 'scheduleId'), 'Schedule');
  requireRecordingAccess(ctx.person, schedule);
  const status = ctx.url.searchParams.get('status') || 'active';
  let entries = Array.from(db.recordings.values()).filter((r) => r.type === 'Schedule::Entry' && r.parent_id === schedule.id);
  entries = entries.filter((e) => status === 'all' || e.status === status);
  if (ctx.person.client) entries = entries.filter((e) => e.visible_to_clients);
  entries.sort((a, b) => a.starts_at - b.starts_at);
  const page = paginate(entries, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(scheduleEntryProjection));
});

route('POST', '/schedules/:scheduleId/entries.json', (ctx) => {
  const schedule = getRecordingOr404(paramId(ctx.params, 'scheduleId'), 'Schedule');
  requireRecordingAccess(ctx.person, schedule);
  const summary = requireString(ctx.body, 'summary');
  const startsAt = requireDateString(ctx.body, 'starts_at');
  const endsAt = requireDateString(ctx.body, 'ends_at');
  const entry = createRecording('Schedule::Entry', {
    title: summary,
    summary,
    description: optionalString(ctx.body, 'description') || '',
    all_day: !!optionalBool(ctx.body, 'all_day'),
    starts_at: new Date(startsAt),
    ends_at: new Date(endsAt),
    bucket_id: schedule.bucket_id,
    parent_id: schedule.id,
    creator_id: ctx.person.id,
    participant_ids: optionalIdArray(ctx.body, 'participant_ids') || [],
    visible_to_clients: schedule.visible_to_clients,
  });
  const subs = optionalIdArray(ctx.body, 'subscriptions');
  if (subs) setSubscribers(entry.id, subs);
  recordEvent(entry.id, 'created', ctx.person.id);
  sendJson(ctx.res, 201, scheduleEntryProjection(entry));
});

route('GET', '/schedule_entries/:entryId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'entryId'), 'Schedule::Entry');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, scheduleEntryProjection(rec));
});

route('PUT', '/schedule_entries/:entryId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'entryId'), 'Schedule::Entry');
  requireRecordingAccess(ctx.person, rec);
  const summary = optionalString(ctx.body, 'summary');
  if (summary !== undefined) { rec.summary = summary; rec.title = summary; }
  const description = optionalString(ctx.body, 'description');
  if (description !== undefined) rec.description = description;
  if (ctx.body.starts_at !== undefined) rec.starts_at = new Date(requireDateString({ starts_at: ctx.body.starts_at }, 'starts_at'));
  if (ctx.body.ends_at !== undefined) rec.ends_at = new Date(requireDateString({ ends_at: ctx.body.ends_at }, 'ends_at'));
  const allDay = optionalBool(ctx.body, 'all_day');
  if (allDay !== undefined) rec.all_day = allDay;
  const participantIds = optionalIdArray(ctx.body, 'participant_ids');
  if (participantIds !== undefined) rec.participant_ids = participantIds;
  touch(rec);
  recordEvent(rec.id, 'updated', ctx.person.id);
  sendJson(ctx.res, 200, scheduleEntryProjection(rec));
});

/** Recurrence is not modeled; every entry is a single instance, so the occurrence for its own date is itself. */
route('GET', '/schedule_entries/:entryId/occurrences/:date', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'entryId'), 'Schedule::Entry');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, scheduleEntryProjection(rec));
});

// ============================================================================
// Automatic Check-ins (Questionnaire / Question / QuestionAnswer)
// ============================================================================

function questionnaireProjection(rec) {
  const questions = Array.from(db.recordings.values()).filter((r) => r.type === 'Questionnaire::Question' && r.parent_id === rec.id && r.status === 'active');
  return Object.assign(recordingEnvelope(rec), {
    name: rec.title,
    questions_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/questionnaires/${rec.id}/questions.json`,
    questions_count: questions.length,
  });
}

function questionProjection(rec) {
  const answers = Array.from(db.recordings.values()).filter((r) => r.type === 'Questionnaire::Answer' && r.parent_id === rec.id && r.status === 'active');
  return Object.assign(recordingEnvelope(rec), {
    paused: !!rec.paused,
    schedule: rec.schedule || null,
    answers_count: answers.length,
    answers_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/questions/${rec.id}/answers.json`,
  });
}

function questionAnswerProjection(rec) {
  const env = recordingEnvelope(rec);
  env.group_on = rec.group_on || null;
  return env;
}

TOOL_FACTORIES['Questionnaire'] = function createQuestionnaireTool(project, creator, title) {
  const q = createRecording('Questionnaire', {
    title: title || 'Automatic Check-ins',
    bucket_id: project.id,
    creator_id: creator.id,
  });
  addToDock(project, q);
  return q;
};

route('GET', '/questionnaires/:questionnaireId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'questionnaireId'), 'Questionnaire');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, questionnaireProjection(rec));
});

route('GET', '/questionnaires/:questionnaireId/questions.json', (ctx) => {
  const q = getRecordingOr404(paramId(ctx.params, 'questionnaireId'), 'Questionnaire');
  requireRecordingAccess(ctx.person, q);
  const list = Array.from(db.recordings.values()).filter((r) => r.type === 'Questionnaire::Question' && r.parent_id === q.id && r.status === 'active');
  const page = paginate(list, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(questionProjection));
});

route('POST', '/questionnaires/:questionnaireId/questions.json', (ctx) => {
  const q = getRecordingOr404(paramId(ctx.params, 'questionnaireId'), 'Questionnaire');
  requireRecordingAccess(ctx.person, q);
  const title = requireString(ctx.body, 'title');
  const schedule = ctx.body.schedule;
  if (!schedule || typeof schedule !== 'object') throw Errors.validation('`schedule` is required');
  const question = createRecording('Questionnaire::Question', {
    title,
    bucket_id: q.bucket_id,
    parent_id: q.id,
    creator_id: ctx.person.id,
    paused: false,
    schedule,
  });
  sendJson(ctx.res, 201, questionProjection(question));
});

route('GET', '/questions/:questionId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'questionId'), 'Questionnaire::Question');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, questionProjection(rec));
});

route('PUT', '/questions/:questionId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'questionId'), 'Questionnaire::Question');
  requireRecordingAccess(ctx.person, rec);
  const title = optionalString(ctx.body, 'title');
  if (title !== undefined) rec.title = title;
  if (ctx.body.schedule !== undefined) rec.schedule = ctx.body.schedule;
  const paused = optionalBool(ctx.body, 'paused');
  if (paused !== undefined) rec.paused = paused;
  touch(rec);
  sendJson(ctx.res, 200, questionProjection(rec));
});

route('POST', '/questions/:questionId/pause.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'questionId'), 'Questionnaire::Question');
  requireRecordingAccess(ctx.person, rec);
  rec.paused = true;
  touch(rec);
  sendJson(ctx.res, 200, { paused: true });
});

route('DELETE', '/questions/:questionId/pause.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'questionId'), 'Questionnaire::Question');
  requireRecordingAccess(ctx.person, rec);
  rec.paused = false;
  touch(rec);
  sendJson(ctx.res, 200, { paused: false });
});

route('PUT', '/questions/:questionId/notification_settings.json', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'questionId'), 'Questionnaire::Question');
  requireRecordingAccess(ctx.person, rec);
  rec.notifyOnAnswer = optionalBool(ctx.body, 'notify_on_answer');
  sendJson(ctx.res, 200, { responding: true, subscribed: rec.notifyOnAnswer !== false });
});

route('GET', '/questions/:questionId/answers.json', (ctx) => {
  const q = getRecordingOr404(paramId(ctx.params, 'questionId'), 'Questionnaire::Question');
  requireRecordingAccess(ctx.person, q);
  const list = Array.from(db.recordings.values()).filter((r) => r.type === 'Questionnaire::Answer' && r.parent_id === q.id && r.status === 'active');
  const page = paginate(list, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(questionAnswerProjection));
});

route('POST', '/questions/:questionId/answers.json', (ctx) => {
  const q = getRecordingOr404(paramId(ctx.params, 'questionId'), 'Questionnaire::Question');
  requireRecordingAccess(ctx.person, q);
  const content = requireString(ctx.body, 'content');
  const answer = createRecording('Questionnaire::Answer', {
    title: `${db.people.get(ctx.person.id).name}'s answer`,
    content,
    group_on: optionalString(ctx.body, 'group_on') || dateOnly(new Date()),
    bucket_id: q.bucket_id,
    parent_id: q.id,
    creator_id: ctx.person.id,
  });
  recordEvent(answer.id, 'created', ctx.person.id);
  sendJson(ctx.res, 201, questionAnswerProjection(answer));
});

route('GET', '/questions/:questionId/answers/by.json', (ctx) => {
  const q = getRecordingOr404(paramId(ctx.params, 'questionId'), 'Questionnaire::Question');
  requireRecordingAccess(ctx.person, q);
  const answers = Array.from(db.recordings.values()).filter((r) => r.type === 'Questionnaire::Answer' && r.parent_id === q.id && r.status === 'active');
  const people = new Map();
  answers.forEach((a) => { if (!people.has(a.creator_id)) people.set(a.creator_id, db.people.get(a.creator_id)); });
  sendJson(ctx.res, 200, Array.from(people.values()).filter(Boolean).map(personProjection));
});

route('GET', '/questions/:questionId/answers/by/:personId', (ctx) => {
  const q = getRecordingOr404(paramId(ctx.params, 'questionId'), 'Questionnaire::Question');
  requireRecordingAccess(ctx.person, q);
  const personId = paramId(ctx.params, 'personId');
  const answers = Array.from(db.recordings.values()).filter((r) => r.type === 'Questionnaire::Answer' && r.parent_id === q.id && r.creator_id === personId && r.status === 'active');
  sendJson(ctx.res, 200, answers.map(questionAnswerProjection));
});

route('GET', '/question_answers/:answerId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'answerId'), 'Questionnaire::Answer');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, questionAnswerProjection(rec));
});

route('PUT', '/question_answers/:answerId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'answerId'), 'Questionnaire::Answer');
  requireRecordingAccess(ctx.person, rec);
  requireOwnVoiceOrAdmin(ctx.person, rec);
  rec.content = requireString(ctx.body, 'content');
  const groupOn = optionalString(ctx.body, 'group_on');
  if (groupOn !== undefined) rec.group_on = groupOn;
  touch(rec);
  sendNoContent(ctx.res);
});

// ============================================================================
// Email Forwards (Inbox) — read-only: inbound email is out of scope (no SMTP)
// ============================================================================

function inboxProjection(rec) {
  const forwards = Array.from(db.recordings.values()).filter((r) => r.type === 'Inbox::Forward' && r.parent_id === rec.id && r.status === 'active');
  return Object.assign(recordingEnvelope(rec), {
    forwards_count: forwards.length,
    forwards_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/inboxes/${rec.id}/forwards.json`,
  });
}

function forwardProjection(rec) {
  const env = recordingEnvelope(rec);
  env.subject = rec.subject;
  env.from = rec.from || '';
  env.replies_count = Array.from(db.recordings.values()).filter((r) => r.type === 'Inbox::Reply' && r.parent_id === rec.id && r.status === 'active').length;
  env.replies_url = `${PUBLIC_BASE_URL}/${CONFIG.accountId}/inbox_forwards/${rec.id}/replies.json`;
  return env;
}

function forwardReplyProjection(rec) {
  return recordingEnvelope(rec);
}

TOOL_FACTORIES['Inbox'] = function createInboxTool(project, creator, title) {
  const inbox = createRecording('Inbox', {
    title: title || 'Email Forwards',
    bucket_id: project.id,
    creator_id: creator.id,
  });
  addToDock(project, inbox);
  return inbox;
};

route('GET', '/inboxes/:inboxId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'inboxId'), 'Inbox');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, inboxProjection(rec));
});

route('GET', '/inboxes/:inboxId/forwards.json', (ctx) => {
  const inbox = getRecordingOr404(paramId(ctx.params, 'inboxId'), 'Inbox');
  requireRecordingAccess(ctx.person, inbox);
  const list = Array.from(db.recordings.values()).filter((r) => r.type === 'Inbox::Forward' && r.parent_id === inbox.id && r.status === 'active');
  const page = paginate(list, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(forwardProjection));
});

route('GET', '/inbox_forwards/:forwardId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'forwardId'), 'Inbox::Forward');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, forwardProjection(rec));
});

route('GET', '/inbox_forwards/:forwardId/replies.json', (ctx) => {
  const fwd = getRecordingOr404(paramId(ctx.params, 'forwardId'), 'Inbox::Forward');
  requireRecordingAccess(ctx.person, fwd);
  const list = Array.from(db.recordings.values()).filter((r) => r.type === 'Inbox::Reply' && r.parent_id === fwd.id && r.status === 'active');
  const page = paginate(list, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(forwardReplyProjection));
});

route('POST', '/inbox_forwards/:forwardId/replies.json', (ctx) => {
  const fwd = getRecordingOr404(paramId(ctx.params, 'forwardId'), 'Inbox::Forward');
  requireRecordingAccess(ctx.person, fwd);
  const content = requireString(ctx.body, 'content');
  const reply = createRecording('Inbox::Reply', {
    title: 'Reply',
    content,
    bucket_id: fwd.bucket_id,
    parent_id: fwd.id,
    creator_id: ctx.person.id,
  });
  recordEvent(reply.id, 'created', ctx.person.id);
  sendJson(ctx.res, 201, forwardReplyProjection(reply));
});

route('GET', '/inbox_forwards/:forwardId/replies/:replyId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'replyId'), 'Inbox::Reply');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, forwardReplyProjection(rec));
});

// ============================================================================
// Client features — read-only surfaces (no create endpoint in the SDK contract)
// ============================================================================

function clientApprovalProjection(rec) {
  const env = recordingEnvelope(rec);
  env.subject = rec.subject;
  env.due_on = rec.due_on || null;
  env.approval_status = rec.approval_status || 'pending';
  env.approver = personProjection(db.people.get(rec.approver_id));
  env.replies_count = 0;
  env.replies_url = `${PUBLIC_BASE_URL}/${CONFIG.accountId}/client/recordings/${rec.id}/replies.json`;
  env.responses = [];
  return env;
}
function clientCorrespondenceProjection(rec) {
  const env = recordingEnvelope(rec);
  env.subject = rec.subject;
  env.replies_count = 0;
  env.replies_url = `${PUBLIC_BASE_URL}/${CONFIG.accountId}/client/recordings/${rec.id}/replies.json`;
  return env;
}
function clientReplyProjection(rec) {
  return recordingEnvelope(rec);
}

route('GET', '/client/approvals.json', (ctx) => {
  const list = Array.from(db.recordings.values()).filter((r) => r.type === 'Client::Approval' && canAccessProject(ctx.person, projectOf(r)));
  const page = paginate(list, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(clientApprovalProjection));
});
route('GET', '/client/approvals/:approvalId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'approvalId'), 'Client::Approval');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, clientApprovalProjection(rec));
});
route('GET', '/client/correspondences.json', (ctx) => {
  const list = Array.from(db.recordings.values()).filter((r) => r.type === 'Client::Correspondence' && canAccessProject(ctx.person, projectOf(r)));
  const page = paginate(list, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(clientCorrespondenceProjection));
});
route('GET', '/client/correspondences/:correspondenceId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'correspondenceId'), 'Client::Correspondence');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, clientCorrespondenceProjection(rec));
});
route('GET', '/client/recordings/:recordingId/replies.json', (ctx) => {
  const parent = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, parent);
  const list = Array.from(db.recordings.values()).filter((r) => r.type === 'Client::Reply' && r.parent_id === parent.id);
  const page = paginate(list, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(clientReplyProjection));
});
route('GET', '/client/recordings/:recordingId/replies/:replyId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'replyId'), 'Client::Reply');
  requireRecordingAccess(ctx.person, rec);
  sendJson(ctx.res, 200, clientReplyProjection(rec));
});

// ============================================================================
// Reports
// ============================================================================

route('GET', '/reports/progress.json', (ctx) => {
  const accessibleProjectIds = new Set(visibleProjects(ctx.person).map((p) => p.id));
  const events = db.events
    .filter((e) => {
      const rec = db.recordings.get(e.recording_id);
      return rec && accessibleProjectIds.has(rec.bucket_id);
    })
    .sort((a, b) => b.created_at - a.created_at)
    .slice(0, 200)
    .map((e) => timelineEventProjection(e, db.projects.get(db.recordings.get(e.recording_id).bucket_id)));
  sendJson(ctx.res, 200, events);
});

route('GET', '/reports/schedules/upcoming.json', (ctx) => {
  const windowStart = ctx.url.searchParams.get('window_starts_on') || dateOnly(new Date());
  const windowEnd = ctx.url.searchParams.get('window_ends_on') || dateOnly(fromT(30 * DAY));
  const accessibleProjectIds = new Set(visibleProjects(ctx.person).map((p) => p.id));
  const entries = Array.from(db.recordings.values()).filter(
    (r) => r.type === 'Schedule::Entry' && r.status === 'active' && accessibleProjectIds.has(r.bucket_id) && dateOnly(r.starts_at) >= windowStart && dateOnly(r.starts_at) <= windowEnd
  );
  const dueTodos = Array.from(db.recordings.values()).filter(
    (r) => r.type === 'Todo' && r.status === 'active' && !r.completed && r.due_on && accessibleProjectIds.has(r.bucket_id) && r.due_on >= windowStart && r.due_on <= windowEnd
  );
  sendJson(ctx.res, 200, {
    schedule_entries: entries.map(scheduleEntryProjection),
    recurring_schedule_entry_occurrences: [],
    assignables: dueTodos.map((t) => ({
      id: t.id,
      title: t.title,
      type: t.type,
      url: jsonUrlFor(t),
      app_url: appUrlFor(t),
      bucket: recordingBucket(t.bucket_id),
      parent: recordingParent(t),
      due_on: t.due_on,
      starts_on: t.starts_on || null,
      assignees: peopleByIds(t.assignee_ids).map(personProjection),
    })),
  });
});

route('GET', '/reports/timesheet.json', (ctx) => {
  requireTimesheetAccess(ctx.person);
  const from = ctx.url.searchParams.get('from');
  const to = ctx.url.searchParams.get('to');
  const personId = ctx.url.searchParams.get('person_id');
  const accessibleProjectIds = new Set(visibleProjects(ctx.person).map((p) => p.id));
  let entries = Array.from(db.recordings.values()).filter((r) => r.type === 'Timesheet::Entry' && accessibleProjectIds.has(r.bucket_id));
  if (from) entries = entries.filter((e) => e.date >= from);
  if (to) entries = entries.filter((e) => e.date <= to);
  if (personId) entries = entries.filter((e) => e.person_id === Number(personId));
  sendJson(ctx.res, 200, entries.map(timesheetEntryProjection));
});

route('GET', '/reports/todos/assigned.json', (ctx) => {
  const assignedIds = new Set();
  for (const r of db.recordings.values()) {
    if ((r.type === 'Todo' || r.type === 'Kanban::Card') && r.status === 'active' && !r.completed) {
      (r.assignee_ids || []).forEach((id) => assignedIds.add(id));
    }
  }
  sendJson(ctx.res, 200, peopleByIds(Array.from(assignedIds)).map(personProjection));
});

route('GET', '/reports/todos/assigned/:personId', (ctx) => {
  const personId = paramId(ctx.params, 'personId');
  const person = db.people.get(personId);
  if (!person) throw Errors.notFound('Person not found');
  const groupBy = ctx.url.searchParams.get('group_by') || 'project';
  const accessibleProjectIds = new Set(visibleProjects(ctx.person).map((p) => p.id));
  const todos = Array.from(db.recordings.values()).filter(
    (r) => r.type === 'Todo' && r.status === 'active' && !r.completed && (r.assignee_ids || []).includes(personId) && accessibleProjectIds.has(r.bucket_id)
  );
  sendJson(ctx.res, 200, { person: personProjection(person), grouped_by: groupBy, todos: todos.map(todoProjection) });
});

route('GET', '/reports/todos/overdue.json', (ctx) => {
  const today = dateOnly(new Date());
  const accessibleProjectIds = new Set(visibleProjects(ctx.person).map((p) => p.id));
  const overdue = Array.from(db.recordings.values()).filter(
    (r) => r.type === 'Todo' && r.status === 'active' && !r.completed && r.due_on && r.due_on < today && accessibleProjectIds.has(r.bucket_id)
  );
  const daysLate = (t) => Math.floor((Date.now() - new Date(t.due_on).getTime()) / DAY);
  sendJson(ctx.res, 200, {
    under_a_week_late: overdue.filter((t) => daysLate(t) <= 7).map(todoProjection),
    over_a_week_late: overdue.filter((t) => daysLate(t) > 7 && daysLate(t) <= 30).map(todoProjection),
    over_a_month_late: overdue.filter((t) => daysLate(t) > 30 && daysLate(t) <= 90).map(todoProjection),
    over_three_months_late: overdue.filter((t) => daysLate(t) > 90).map(todoProjection),
  });
});

route('GET', '/reports/users/progress/:personIdJson', (ctx) => {
  const personId = Number(String(ctx.params.personIdJson).replace(/\.json$/, ''));
  if (!Number.isInteger(personId)) throw Errors.badRequest('Invalid personId');
  const person = db.people.get(personId);
  if (!person) throw Errors.notFound('Person not found');
  const accessibleProjectIds = new Set(visibleProjects(ctx.person).map((p) => p.id));
  const events = db.events
    .filter((e) => e.creator_id === personId)
    .filter((e) => {
      const rec = db.recordings.get(e.recording_id);
      return rec && accessibleProjectIds.has(rec.bucket_id);
    })
    .sort((a, b) => b.created_at - a.created_at)
    .slice(0, 200)
    .map((e) => timelineEventProjection(e, db.projects.get(db.recordings.get(e.recording_id).bucket_id)));
  sendJson(ctx.res, 200, { person: personProjection(person), events });
});

// ============================================================================
// Timesheet entries (standalone) + per-recording time logging
// ============================================================================

route('GET', '/timesheet_entries/:entryId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'entryId'), 'Timesheet::Entry');
  requireRecordingAccess(ctx.person, rec);
  requireTimesheetAccess(ctx.person);
  sendJson(ctx.res, 200, timesheetEntryProjection(rec));
});

route('PUT', '/timesheet_entries/:entryId', (ctx) => {
  const rec = getRecordingOr404(paramId(ctx.params, 'entryId'), 'Timesheet::Entry');
  requireRecordingAccess(ctx.person, rec);
  requireTimesheetAccess(ctx.person);
  const date = optionalString(ctx.body, 'date');
  if (date !== undefined) rec.date = date;
  const hours = optionalString(ctx.body, 'hours');
  if (hours !== undefined) rec.hours = hours;
  const description = optionalString(ctx.body, 'description');
  if (description !== undefined) rec.description = description;
  if (ctx.body.person_id !== undefined) rec.person_id = Number(ctx.body.person_id);
  touch(rec);
  sendJson(ctx.res, 200, timesheetEntryProjection(rec));
});

route('GET', '/recordings/:recordingId/timesheet.json', (ctx) => {
  const target = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, target);
  requireTimesheetAccess(ctx.person);
  const entries = Array.from(db.recordings.values()).filter((r) => r.type === 'Timesheet::Entry' && r.parent_id === target.id);
  sendJson(ctx.res, 200, entries.map(timesheetEntryProjection));
});

route('POST', '/recordings/:recordingId/timesheet/entries.json', (ctx) => {
  const target = getRecordingOr404(paramId(ctx.params, 'recordingId'));
  requireRecordingAccess(ctx.person, target);
  requireTimesheetAccess(ctx.person);
  const date = requireString(ctx.body, 'date');
  const hours = requireString(ctx.body, 'hours');
  const personId = ctx.body.person_id ? Number(ctx.body.person_id) : ctx.person.id;
  const entry = createRecording('Timesheet::Entry', {
    title: `${hours}h on ${date}`,
    bucket_id: target.bucket_id,
    parent_id: target.id,
    creator_id: ctx.person.id,
    date,
    hours,
    description: optionalString(ctx.body, 'description') || '',
    person_id: personId,
  });
  sendJson(ctx.res, 201, timesheetEntryProjection(entry));
});

// ============================================================================
// Lineup markers (account-level)
// ============================================================================

function lineupMarkerProjection(m) {
  return { id: m.id, name: m.name, date: m.date, created_at: isoAt(m.created_at), updated_at: isoAt(m.updated_at) };
}

route('GET', '/lineup/markers.json', (ctx) => {
  sendJson(ctx.res, 200, Array.from(db.lineupMarkers.values()).map(lineupMarkerProjection));
});

route('POST', '/lineup/markers.json', (ctx) => {
  const name = requireString(ctx.body, 'name');
  const date = requireDateString(ctx.body, 'date');
  const marker = { id: nextId(), name, date, created_at: new Date(), updated_at: new Date() };
  db.lineupMarkers.set(marker.id, marker);
  sendJson(ctx.res, 201, lineupMarkerProjection(marker));
});

route('PUT', '/lineup/markers/:markerId', (ctx) => {
  const marker = db.lineupMarkers.get(paramId(ctx.params, 'markerId'));
  if (!marker) throw Errors.notFound('Marker not found');
  const name = optionalString(ctx.body, 'name');
  if (name !== undefined) marker.name = name;
  const date = optionalString(ctx.body, 'date');
  if (date !== undefined) marker.date = date;
  marker.updated_at = new Date();
  sendJson(ctx.res, 200, lineupMarkerProjection(marker));
});

route('DELETE', '/lineup/markers/:markerId', (ctx) => {
  const id = paramId(ctx.params, 'markerId');
  if (!db.lineupMarkers.has(id)) throw Errors.notFound('Marker not found');
  db.lineupMarkers.delete(id);
  sendNoContent(ctx.res);
});

// ============================================================================
// Templates
// ============================================================================

function templateProjection(t) {
  return {
    id: t.id,
    status: t.status,
    created_at: isoAt(t.created_at),
    updated_at: isoAt(t.updated_at),
    name: t.name,
    description: t.description || '',
    url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/templates/${t.id}.json`,
    app_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/templates/${t.id}`,
    dock: t.dock || [],
  };
}

route('GET', '/templates.json', (ctx) => {
  const list = Array.from(db.templates.values()).filter((t) => t.status !== 'trashed');
  const page = paginate(list, ctx.req, ctx.res);
  sendJson(ctx.res, 200, page.map(templateProjection));
});

route('POST', '/templates.json', (ctx) => {
  requireEmployee(ctx.person);
  const name = requireString(ctx.body, 'name');
  const t = { id: nextId(), status: 'active', name, description: optionalString(ctx.body, 'description') || '', dock: [], created_at: new Date(), updated_at: new Date() };
  db.templates.set(t.id, t);
  sendJson(ctx.res, 201, templateProjection(t));
});

route('GET', '/templates/:templateId', (ctx) => {
  const t = db.templates.get(paramId(ctx.params, 'templateId'));
  if (!t || t.status === 'trashed') throw Errors.notFound('Template not found');
  sendJson(ctx.res, 200, templateProjection(t));
});

route('PUT', '/templates/:templateId', (ctx) => {
  const t = db.templates.get(paramId(ctx.params, 'templateId'));
  if (!t) throw Errors.notFound('Template not found');
  requireEmployee(ctx.person);
  const name = optionalString(ctx.body, 'name');
  if (name !== undefined) t.name = name;
  const description = optionalString(ctx.body, 'description');
  if (description !== undefined) t.description = description;
  t.updated_at = new Date();
  sendJson(ctx.res, 200, templateProjection(t));
});

route('DELETE', '/templates/:templateId', (ctx) => {
  const t = db.templates.get(paramId(ctx.params, 'templateId'));
  if (!t) throw Errors.notFound('Template not found');
  requireEmployee(ctx.person);
  t.status = 'trashed';
  sendNoContent(ctx.res);
});

route('POST', '/templates/:templateId/project_constructions.json', (ctx) => {
  const t = db.templates.get(paramId(ctx.params, 'templateId'));
  if (!t || t.status === 'trashed') throw Errors.notFound('Template not found');
  requireEmployee(ctx.person);
  const name = requireString(ctx.body, 'name');
  const project = {
    id: nextId(),
    status: 'active',
    name,
    description: optionalString(ctx.body, 'description') || '',
    purpose: 'topic',
    clientsEnabled: false,
    allAccess: false,
    memberIds: [ctx.person.id],
    dock: [],
    created_at: new Date(),
    updated_at: new Date(),
  };
  db.projects.set(project.id, project);
  const construction = { id: nextId(), status: 'completed', project_id: project.id };
  db.projectConstructions.set(construction.id, construction);
  sendJson(ctx.res, 201, projectConstructionProjection(construction));
});

function projectConstructionProjection(c) {
  return {
    id: c.id,
    status: c.status,
    url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/templates/project_constructions/${c.id}.json`,
    project: c.status === 'completed' ? projectProjection(db.projects.get(c.project_id)) : undefined,
  };
}

route('GET', '/templates/:templateId/project_constructions/:constructionId', (ctx) => {
  const c = db.projectConstructions.get(paramId(ctx.params, 'constructionId'));
  if (!c) throw Errors.notFound('Project construction not found');
  sendJson(ctx.res, 200, projectConstructionProjection(c));
});

// ============================================================================
// Webhooks
// ============================================================================

function webhookProjection(w) {
  return {
    id: w.id,
    active: w.active,
    created_at: isoAt(w.created_at),
    updated_at: isoAt(w.updated_at),
    payload_url: w.payload_url,
    types: w.types,
    url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/webhooks/${w.id}.json`,
    app_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/buckets/${w.bucket_id}/webhooks/${w.id}`,
    recent_deliveries: db.webhookDeliveries.get(w.id) || [],
  };
}

const MAX_WEBHOOKS_PER_BUCKET = 20;

route('GET', '/buckets/:bucketId/webhooks.json', (ctx) => {
  const project = db.projects.get(paramId(ctx.params, 'bucketId'));
  if (!project) throw Errors.notFound('Project not found');
  requireProjectAccess(ctx.person, project);
  const list = Array.from(db.webhooks.values()).filter((w) => w.bucket_id === project.id);
  sendJson(ctx.res, 200, list.map(webhookProjection));
});

route('POST', '/buckets/:bucketId/webhooks.json', (ctx) => {
  const project = db.projects.get(paramId(ctx.params, 'bucketId'));
  if (!project) throw Errors.notFound('Project not found');
  requireProjectAccess(ctx.person, project);
  requireEmployee(ctx.person);
  const existing = Array.from(db.webhooks.values()).filter((w) => w.bucket_id === project.id);
  if (existing.length >= MAX_WEBHOOKS_PER_BUCKET) {
    throw new ApiError(507, 'api_error', `A project may have at most ${MAX_WEBHOOKS_PER_BUCKET} webhooks`);
  }
  const payloadUrl = requireString(ctx.body, 'payload_url');
  let parsed;
  try { parsed = new URL(payloadUrl); } catch { throw Errors.badRequest('`payload_url` must be a valid URL'); }
  if (parsed.protocol !== 'https:') throw Errors.badRequest('`payload_url` must use HTTPS');
  const types = ctx.body.types;
  if (!Array.isArray(types) || types.length === 0) throw Errors.validation('`types` must be a non-empty array');
  const w = {
    id: nextId(),
    bucket_id: project.id,
    active: optionalBool(ctx.body, 'active') !== false,
    payload_url: payloadUrl,
    types,
    created_at: new Date(),
    updated_at: new Date(),
  };
  db.webhooks.set(w.id, w);
  sendJson(ctx.res, 201, webhookProjection(w));
});

route('GET', '/webhooks/:webhookId', (ctx) => {
  const w = db.webhooks.get(paramId(ctx.params, 'webhookId'));
  if (!w) throw Errors.notFound('Webhook not found');
  requireProjectAccess(ctx.person, db.projects.get(w.bucket_id));
  sendJson(ctx.res, 200, webhookProjection(w));
});

route('PUT', '/webhooks/:webhookId', (ctx) => {
  const w = db.webhooks.get(paramId(ctx.params, 'webhookId'));
  if (!w) throw Errors.notFound('Webhook not found');
  requireProjectAccess(ctx.person, db.projects.get(w.bucket_id));
  requireEmployee(ctx.person);
  const payloadUrl = optionalString(ctx.body, 'payload_url');
  if (payloadUrl !== undefined) w.payload_url = payloadUrl;
  if (ctx.body.types !== undefined) w.types = ctx.body.types;
  const active = optionalBool(ctx.body, 'active');
  if (active !== undefined) w.active = active;
  w.updated_at = new Date();
  sendJson(ctx.res, 200, webhookProjection(w));
});

route('DELETE', '/webhooks/:webhookId', (ctx) => {
  const w = db.webhooks.get(paramId(ctx.params, 'webhookId'));
  if (!w) throw Errors.notFound('Webhook not found');
  requireProjectAccess(ctx.person, db.projects.get(w.bucket_id));
  requireEmployee(ctx.person);
  db.webhooks.delete(w.id);
  db.webhookDeliveries.delete(w.id);
  sendNoContent(ctx.res);
});

// ============================================================================
// Search
// ============================================================================

function searchResultProjection(rec) {
  const env = recordingEnvelope(rec);
  return {
    id: env.id,
    status: env.status,
    visible_to_clients: env.visible_to_clients,
    created_at: env.created_at,
    updated_at: env.updated_at,
    title: env.title,
    inherits_status: env.inherits_status,
    type: env.type,
    url: env.url,
    app_url: env.app_url,
    bookmark_url: env.bookmark_url,
    parent: env.parent,
    bucket: env.bucket,
    creator: env.creator,
    content: rec.content || '',
    description: rec.description || '',
    subject: rec.subject || rec.summary || '',
  };
}

route('GET', '/search.json', (ctx) => {
  const q = ctx.url.searchParams.get('q');
  if (!q) throw Errors.validation('`q` query parameter is required');
  const needle = q.toLowerCase();
  const accessibleProjectIds = new Set(visibleProjects(ctx.person).map((p) => p.id));
  const results = Array.from(db.recordings.values()).filter((r) => {
    if (r.status !== 'active') return false;
    if (!accessibleProjectIds.has(r.bucket_id)) return false;
    if (ctx.person.client && !r.visible_to_clients) return false;
    const haystack = `${r.title || ''} ${r.content || ''} ${r.subject || ''}`.toLowerCase();
    return haystack.includes(needle);
  });
  sendJson(ctx.res, 200, results.map(searchResultProjection));
});

route('GET', '/searches/metadata.json', (ctx) => {
  const projects = visibleProjects(ctx.person).map((p) => ({ id: p.id, name: p.name }));
  sendJson(ctx.res, 200, { projects });
});

// ============================================================================
// Sample seed — "Launch the new website" (INIT.md §3)
//
// Materialized once at boot (or on /_reset). Deterministic: every timestamp
// is a fixed offset from the boot instant T. Readings are left clean for the
// bearer of the owner token — nothing here fans out notifications to them.
// ============================================================================

function addCategory(name, icon) {
  const mt = { id: nextId(), name, icon, created_at: BOOT_T, updated_at: BOOT_T };
  db.messageTypes.set(mt.id, mt);
  return mt;
}

function seedAll() {
  if (db.seeded) return;

  const owner = createPerson({
    name: 'Jordan Blake', email_address: 'jordan@example.com', title: 'Head of Operations',
    employee: true, admin: true, owner: true, can_access_timesheet: true, can_access_hill_charts: true,
  });
  const clientPerson = createPerson({
    name: 'Casey Clientson', email_address: 'casey@clientco.example', title: 'Client stakeholder',
    employee: false, client: true, can_ping: false,
  });

  const maya = createPerson({ name: 'Maya Chen', email_address: 'maya@example.com', title: 'Project Lead', sample: true, can_access_hill_charts: true });
  const sam = createPerson({ name: 'Sam Whitaker', email_address: 'sam@example.com', title: 'Writer', sample: true });
  const omar = createPerson({ name: 'Omar Haddad', email_address: 'omar@example.com', title: 'Designer', sample: true });
  const priya = createPerson({ name: 'Priya Nair', email_address: 'priya@example.com', title: 'Developer', sample: true });
  const lena = createPerson({ name: 'Lena Kowalski', email_address: 'lena@example.com', title: 'Marketing', sample: true });
  const diego = createPerson({ name: 'Diego Ramos', email_address: 'diego@example.com', title: 'Community', sample: true });
  const grace = createPerson({ name: 'Grace Okafor', email_address: 'grace@example.com', title: 'QA', sample: true });
  const felix = createPerson({ name: 'Felix Berg', email_address: 'felix@example.com', title: 'Ops', sample: true });
  const cast = [maya, sam, omar, priya, lena, diego, grace, felix];

  db.account = {
    id: CONFIG.accountId,
    name: CONFIG.accountName,
    owner_name: owner.name,
    created_at: fromT(-90 * DAY),
    updated_at: BOOT_T,
    logo_url: null,
  };

  seedTokenLog.set('Jordan Blake (owner, employee, admin)', issueToken(owner.id));
  seedTokenLog.set('Casey Clientson (client)', issueToken(clientPerson.id));
  cast.forEach((p) => seedTokenLog.set(`${p.name} (sample)`, issueToken(p.id)));

  // ---- Project ----------------------------------------------------------
  const project = {
    id: nextId(),
    status: 'active',
    name: 'Launch the new website',
    description:
      '👋 This is a sample project that shows how a team works together here. Poke around, click into things — and delete this project whenever you’re ready.',
    purpose: 'topic',
    clientsEnabled: true,
    allAccess: true, // real user can browse without being a listed member, matching B5 sample behavior
    memberIds: [owner.id, clientPerson.id, ...cast.map((p) => p.id)],
    dock: [],
    clientCompanyId: null,
    created_at: fromT(-20 * DAY),
    updated_at: BOOT_T,
    sample: true,
  };
  db.projects.set(project.id, project);

  // ---- Message categories (account-wide) ---------------------------------
  const catFYI = addCategory('FYI', '✨');
  const catAnnouncement = addCategory('Announcement', '📣');
  const catPitch = addCategory('Pitch', '💡');
  const catHeartbeat = addCategory('Heartbeat', '❤️');
  addCategory('Question', '👋');

  // ---- Dock: Message Board, To-dos, Card Table, Docs & Files, Chat, Calendar
  const board = TOOL_FACTORIES['Message::Board'](project, maya, 'Message Board');
  const todoset = TOOL_FACTORIES['Todoset'](project, maya, 'To-dos');
  const cardTable = TOOL_FACTORIES['Kanban::Board'](project, maya, 'Card Table');
  const vault = TOOL_FACTORIES['Vault'](project, maya, 'Docs & Files');
  const chat = TOOL_FACTORIES['Chat::Transcript'](project, maya, 'Chat');
  const schedule = TOOL_FACTORIES['Schedule'](project, maya, 'Schedule');

  // =========================================================================
  // Message Board — 5 posts
  // =========================================================================

  const kickoff = createRecording('Message', {
    title: 'Kickoff: the plan',
    subject: 'Kickoff: the plan',
    content:
      '<p>Hey team — we are officially underway! Quick rundown of who’s driving what:</p>' +
      `<p>@${sam.name} is leading copy, @${omar.name} is leading design, @${priya.name} is on DNS + infra, and @${lena.name} is running launch marketing.</p>` +
      '<p>Let’s make this the best site we’ve shipped. Ask questions here if anything is unclear!</p>',
    status: 'active',
    creator_id: maya.id,
    bucket_id: project.id,
    parent_id: board.id,
    category_id: catAnnouncement.id,
    pinned: true,
    visible_to_clients: true,
    created_at: fromT(-4 * DAY + 9 * HOUR),
    updated_at: fromT(-4 * DAY + 9 * HOUR),
  });
  setSubscribers(kickoff.id, [maya.id, sam.id, omar.id, priya.id, lena.id]);
  recordEvent(kickoff.id, 'created', maya.id, { notified_recipient_ids: [sam.id, omar.id, priya.id, lena.id] });
  [sam, omar, priya, lena, diego, grace, felix].forEach((p) => createBoost(`recording:${kickoff.id}`, { id: kickoff.id, title: kickoff.title, type: 'Message', url: jsonUrlFor(kickoff), app_url: appUrlFor(kickoff) }, '👏', p.id));
  const kickoffComments = [
    [sam, 'Copy pitch is already in motion — posting it today.'],
    [priya, 'DNS plan is drafted, will need review from Maya before we cut over.'],
    [omar, 'First logo concepts incoming this week.'],
    [lena, 'Excited to get the heartbeat posts going once we have traffic data.'],
  ];
  kickoffComments.forEach(([person, text], i) => {
    const c = createRecording('Comment', { title: 'Comment', content: `<p>${text}</p>`, creator_id: person.id, bucket_id: project.id, parent_id: kickoff.id, visible_to_clients: true, created_at: fromT(-4 * DAY + (10 + i) * HOUR) });
    if (i < 2) createBoost(`recording:${c.id}`, { id: c.id, title: '', type: 'Comment', url: jsonUrlFor(c), app_url: appUrlFor(c) }, '👍', maya.id);
  });

  const pitch = createRecording('Message', {
    title: 'Pitch: trim the homepage copy',
    subject: 'Pitch: trim the homepage copy',
    content: '<p>I think we can cut the homepage word count by half and still land the pitch. Draft attached below — thoughts?</p>',
    status: 'active',
    creator_id: sam.id,
    bucket_id: project.id,
    parent_id: board.id,
    category_id: catPitch.id,
    visible_to_clients: true,
    created_at: fromT(-4 * DAY + 10 * HOUR),
    updated_at: fromT(-4 * DAY + 10 * HOUR),
  });
  recordEvent(pitch.id, 'created', sam.id);
  const pitchThread = [maya, omar, priya, lena, diego, grace, maya];
  pitchThread.forEach((person, i) => {
    createRecording('Comment', { title: 'Comment', content: `<p>${['Strong cut, agreed.', 'Can we keep one line about pricing?', 'Works for engineering too — nothing technical lost.', 'Marketing-approved!', 'Reads a lot cleaner now.', 'Nice, shipping this version.', 'Let’s lock this in.'][i]}</p>`, creator_id: person.id, bucket_id: project.id, parent_id: pitch.id, visible_to_clients: true, created_at: fromT(-4 * DAY + (11 + i) * HOUR) });
  });

  const beta = createRecording('Message', {
    title: 'Nice note from a beta tester',
    subject: 'Nice note from a beta tester',
    content: '<p>Got this in from one of our beta testers:</p><blockquote>"Honestly the fastest site of its kind I’ve used all year — and it looks great too."</blockquote>',
    status: 'active',
    creator_id: diego.id,
    bucket_id: project.id,
    parent_id: board.id,
    category_id: catFYI.id,
    visible_to_clients: true,
    created_at: fromT(-4 * DAY + 13 * HOUR),
    updated_at: fromT(-4 * DAY + 13 * HOUR),
  });
  recordEvent(beta.id, 'created', diego.id);

  const traffic = createRecording('Message', {
    title: 'Traffic this week',
    subject: 'Traffic this week',
    content: '<p>Early traffic on the staging preview is trending up nicely week over week:</p><p>[chart: staging preview sessions, Mon–Fri]</p>',
    status: 'active',
    creator_id: lena.id,
    bucket_id: project.id,
    parent_id: board.id,
    category_id: catHeartbeat.id,
    visible_to_clients: false,
    created_at: fromT(-4 * DAY + 14 * HOUR),
    updated_at: fromT(-4 * DAY + 14 * HOUR),
  });
  recordEvent(traffic.id, 'created', lena.id);

  const press = createRecording('Message', {
    title: 'Local press opportunity',
    subject: 'Local press opportunity',
    content: '<p>A local tech reporter reached out about covering the launch. I think it’s worth a short call.</p>',
    status: 'active',
    creator_id: maya.id,
    bucket_id: project.id,
    parent_id: board.id,
    category_id: catHeartbeat.id,
    visible_to_clients: false,
    created_at: fromT(-4 * DAY + 15 * HOUR),
    updated_at: fromT(-4 * DAY + 15 * HOUR),
  });
  recordEvent(press.id, 'created', maya.id);
  createRecording('Comment', { title: 'Comment', content: '<p>Worth doing — I can join the call.</p>', creator_id: lena.id, bucket_id: project.id, parent_id: press.id, created_at: fromT(-4 * DAY + 16 * HOUR) });

  // =========================================================================
  // To-dos — 2 lists
  // =========================================================================

  const preLaunch = createTodolist(todoset, maya, { name: 'Pre-launch checklist', description: '' });
  const setUpAnalytics = createRecording('Todo', {
    title: 'Set up analytics',
    content: 'Set up analytics',
    description: '<p>Wire up the site analytics snippet on every page. Subtask: verify events fire on the staging preview.</p>',
    bucket_id: project.id,
    parent_id: preLaunch.id,
    creator_id: maya.id,
    completed: false,
    assignee_ids: [priya.id],
    completion_subscriber_ids: [],
    due_on: null,
    starts_on: null,
    position: 1,
  });
  subscribe(setUpAnalytics.id, priya.id);
  const pointDns = createRecording('Todo', {
    title: 'Point DNS at the new host',
    content: 'Point DNS at the new host',
    description: '',
    bucket_id: project.id,
    parent_id: preLaunch.id,
    creator_id: maya.id,
    completed: false,
    assignee_ids: [priya.id],
    completion_subscriber_ids: [maya.id],
    due_on: null,
    starts_on: null,
    position: 2,
  });
  createRecording('Comment', { title: 'Comment', content: '<p>Waiting on registrar access, should land tomorrow.</p>', creator_id: priya.id, bucket_id: project.id, parent_id: pointDns.id, created_at: fromT(-2 * DAY) });
  ['Buy domain name', 'Draft privacy policy'].forEach((title, i) => {
    createRecording('Todo', { title, content: title, description: '', bucket_id: project.id, parent_id: preLaunch.id, creator_id: maya.id, completed: true, assignee_ids: [maya.id], completion_subscriber_ids: [], due_on: null, starts_on: null, position: 3 + i, updated_at: fromT(-10 * DAY + i * DAY) });
  });

  const launchWeek = createTodolist(todoset, maya, { name: 'Launch week: content', description: 'Everything we need published and queued for launch day.' });
  const newsletter = createRecording('Todo', {
    title: 'Email newsletter',
    content: 'Email newsletter',
    description: '',
    bucket_id: project.id,
    parent_id: launchWeek.id,
    creator_id: maya.id,
    completed: false,
    assignee_ids: [sam.id],
    completion_subscriber_ids: [maya.id],
    due_on: dateOnly(fromT(3 * DAY)),
    starts_on: null,
    position: 1,
  });
  subscribe(newsletter.id, sam.id);
  [
    ['Schedule social posts', lena.id],
    ['Write launch-day blog post', sam.id],
    ['Brief the support team', grace.id],
    ['Prep customer FAQ', grace.id],
  ].forEach(([title, assigneeId], i) => {
    createRecording('Todo', { title, content: title, description: '', bucket_id: project.id, parent_id: launchWeek.id, creator_id: maya.id, completed: false, assignee_ids: [assigneeId], completion_subscriber_ids: [], due_on: null, starts_on: null, position: 2 + i });
  });
  createRecording('Todo', { title: 'Reserve launch day war room', content: 'Reserve launch day war room', description: '', bucket_id: project.id, parent_id: launchWeek.id, creator_id: maya.id, completed: true, assignee_ids: [maya.id], completion_subscriber_ids: [], due_on: null, starts_on: null, position: 6, updated_at: fromT(-1 * DAY) });

  // =========================================================================
  // Card Table
  // =========================================================================

  const triage = Array.from(db.recordings.values()).find((r) => r.type === 'Kanban::Column' && r.parent_id === cardTable.id && r.kind === 'triage');
  const notNow = Array.from(db.recordings.values()).find((r) => r.type === 'Kanban::Column' && r.parent_id === cardTable.id && r.kind === 'not_now');
  const done = Array.from(db.recordings.values()).find((r) => r.type === 'Kanban::Column' && r.parent_id === cardTable.id && r.kind === 'done');
  triage.title = 'Page ideas';
  setSubscribers(triage.id, [maya.id, omar.id, grace.id]);

  const writing = createCardColumn(cardTable, maya, { title: 'Writing', kind: 'column', position: 1, color: 'blue' });
  const design = createCardColumn(cardTable, maya, { title: 'Design', kind: 'column', position: 2, color: 'purple' });
  const review = createCardColumn(cardTable, maya, { title: 'Review', kind: 'column', position: 3 });
  const ready = createCardColumn(cardTable, maya, { title: 'Ready', kind: 'column', position: 4 });

  function seedCard(column, title, opts) {
    opts = opts || {};
    const card = createRecording('Kanban::Card', {
      title,
      content: opts.content || '',
      due_on: opts.due_on || null,
      completed: column.kind === 'done',
      completed_at: column.kind === 'done' ? (opts.completed_at || BOOT_T) : null,
      bucket_id: project.id,
      parent_id: column.id,
      creator_id: maya.id,
      assignee_ids: opts.assignee_ids || [],
      completion_subscriber_ids: [],
      position: opts.position || 1,
      created_at: fromT(-20 * DAY),
      updated_at: opts.updated_at || fromT(-20 * DAY),
    });
    (opts.steps || []).forEach((stepTitle, i) => {
      createRecording('Kanban::Step', { title: stepTitle, due_on: null, completed: !!opts.stepsCompleted, bucket_id: project.id, parent_id: card.id, creator_id: maya.id, assignee_ids: [], position: i + 1 });
    });
    (opts.comments || []).forEach((c) => {
      createRecording('Comment', { title: 'Comment', content: `<p>${c.text}</p>`, creator_id: c.person.id, bucket_id: project.id, parent_id: card.id, created_at: fromT(-19 * DAY) });
    });
    return card;
  }

  seedCard(triage, 'Add a customer logos section', { position: 1, assignee_ids: [omar.id] });
  seedCard(triage, 'Consider a dark mode toggle', { position: 2, steps: ['Sketch two directions'] });

  seedCard(writing, 'Draft the about page', { position: 1, assignee_ids: [sam.id] });
  seedCard(writing, 'Rewrite pricing page copy', {
    position: 2,
    assignee_ids: [sam.id],
    steps: ['Outline sections', 'First draft', 'Internal review', 'Final polish'],
    comments: [{ person: maya, text: 'Take your time on this one — it matters a lot for conversion.' }],
  });
  // "on hold" sub-lane on Writing, with the pricing-copy card's sibling moved into it
  {
    const onHold = createRecording('Kanban::Column', { title: 'Writing: On hold', bucket_id: project.id, parent_id: writing.id, creator_id: maya.id, kind: 'on_hold', onHoldParentId: writing.id });
    writing.onHoldId = onHold.id;
  }

  seedCard(review, 'New nav structure', { position: 1, assignee_ids: [omar.id, priya.id] });
  seedCard(ready, 'Updated favicon + touch icons', { position: 1, assignee_ids: [omar.id] });

  ['Homepage hero redesign', 'New button styles', 'Footer refresh', 'Launch banner', 'Update team photos'].forEach((title, i) => {
    seedCard(done, title, { position: i + 1, assignee_ids: [omar.id], completed_at: fromT(-1 * DAY + i * HOUR), updated_at: fromT(-1 * DAY + i * HOUR) });
  });

  ['Animated hero video', 'Custom illustration set'].forEach((title, i) => {
    seedCard(notNow, title, { position: i + 1, updated_at: fromT(-4 * DAY) });
  });

  writing.color = 'blue';
  design.color = 'purple';

  // =========================================================================
  // Docs & Files
  // =========================================================================

  const homepageDoc = createRecording('Document', {
    title: 'Homepage copy — draft',
    content: '<h1>Launch the new website</h1><p>A faster, friendlier home for the whole team.</p><h2>Why now</h2><p>Our current site hasn’t kept up with the product. This draft resets the story.</p>',
    status: 'active',
    bucket_id: project.id,
    parent_id: vault.id,
    creator_id: sam.id,
    visible_to_clients: true,
    created_at: fromT(-3 * DAY),
    updated_at: fromT(-3 * DAY),
  });
  createRecording('Comment', { title: 'Comment', content: '<p>Love the new opening line.</p>', creator_id: omar.id, bucket_id: project.id, parent_id: homepageDoc.id, created_at: fromT(-3 * DAY + 2 * HOUR) });

  const logoSgid = `gid://sample/Upload/${nextId()}`;
  db.attachments.set(logoSgid, { contentType: 'image/png', data: Buffer.from('89504e470d0a1a0a', 'hex'), filename: 'logo-concepts.png' });
  const logoUpload = createRecording('Upload', {
    title: 'logo-concepts.png',
    description: 'Three logo directions from Omar',
    bucket_id: project.id,
    parent_id: vault.id,
    creator_id: omar.id,
    content_type: 'image/png',
    byte_size: 8,
    filename: 'logo-concepts.png',
    download_url: `${PUBLIC_BASE_URL}/${CONFIG.accountId}/attachments/${encodeURIComponent(logoSgid)}`,
    visible_to_clients: true,
    created_at: fromT(-3 * DAY + 1 * HOUR),
    updated_at: fromT(-3 * DAY + 1 * HOUR),
  });
  db.uploadVersions.set(logoUpload.id, [uploadProjection(logoUpload)]);

  // Cloud link (Google Sheet), represented as an Upload pointing at an external placeholder URL.
  createRecording('Upload', {
    title: 'Content calendar',
    description: 'Cloud link — Google Sheets',
    bucket_id: project.id,
    parent_id: vault.id,
    creator_id: lena.id,
    content_type: 'application/vnd.google-apps.spreadsheet',
    byte_size: 0,
    filename: 'Content calendar',
    download_url: 'https://docs.google.com/spreadsheets/d/placeholder-content-calendar',
    created_at: fromT(-3 * DAY + 2 * HOUR),
    updated_at: fromT(-3 * DAY + 2 * HOUR),
  });

  // =========================================================================
  // Calendar
  // =========================================================================

  createRecording('Schedule::Entry', {
    title: 'Launch day 🚀',
    summary: 'Launch day 🚀',
    description: 'The new site goes live.',
    all_day: true,
    starts_at: fromT(7 * 7 * DAY),
    ends_at: fromT(7 * 7 * DAY),
    bucket_id: project.id,
    parent_id: schedule.id,
    creator_id: maya.id,
    participant_ids: cast.map((p) => p.id),
    visible_to_clients: true,
  });
  createRecording('Schedule::Entry', {
    title: 'Content review call',
    summary: 'Content review call',
    description: 'Walk through final homepage + launch email copy.',
    all_day: false,
    starts_at: fromT(2 * 7 * DAY + 10 * HOUR),
    ends_at: fromT(2 * 7 * DAY + 11 * HOUR),
    bucket_id: project.id,
    parent_id: schedule.id,
    creator_id: maya.id,
    participant_ids: [maya.id, sam.id, priya.id],
  });

  // =========================================================================
  // Chat — one day, ~16 lines, 5 people
  // =========================================================================

  const chatDay = -4 * DAY;
  function chatLine(person, text, offsetMin) {
    return createRecording('Chat::Line', { title: 'Chat line', content: `<p>${text}</p>`, bucket_id: project.id, parent_id: chat.id, creator_id: person.id, attachments: [], created_at: fromT(chatDay + 9 * HOUR + offsetMin * MIN), updated_at: fromT(chatDay + 9 * HOUR + offsetMin * MIN) });
  }
  const opener = chatLine(maya, 'Morning all — big week ahead 🙌', 0);
  createBoost(`recording:${opener.id}`, { id: opener.id, title: '', type: 'Chat::Line', url: '', app_url: '' }, '🙌', sam.id);
  chatLine(maya, 'Been thinking about this project a lot this weekend.', 2);
  chatLine(maya, 'Really proud of how the team has come together around it — feels different this time.', 3);
  const linkLine = chatLine(sam, 'Here’s the doc I mentioned: https://example.com/homepage-draft', 20);
  createBoost(`recording:${linkLine.id}`, { id: linkLine.id, title: '', type: 'Chat::Line', url: '', app_url: '' }, 'nice', omar.id);
  chatLine(priya, 'heads up, deploying now', 45);
  chatLine(priya, 'should be about 5 minutes', 46);
  const q1 = chatLine(grace, 'is staging supposed to be down right now?', 50);
  const a1 = chatLine(priya, 'yep, that’s the deploy 👍', 51);
  createBoost(`recording:${q1.id}`, { id: q1.id, title: '', type: 'Chat::Line', url: '', app_url: '' }, '🙏', grace.id);
  chatLine(priya, 'back up now', 56);
  chatLine(diego, 'a customer just told me: "this is the best version of the site yet"', 90);
  chatLine(diego, 'Awww! 💖', 91);
  chatLine(lena, 'love that', 92);
  chatLine(felix, '👍', 93);
  chatLine(omar, 'logo concepts are up in Docs & Files if anyone wants to weigh in', 120);
  chatLine(omar, 'no pressure, just curious what resonates', 121);
  chatLine(felix, '👍', 130);

  db.seeded = true;
  log('info', 'seed_complete', {
    project_id: project.id,
    people: db.people.size,
    recordings: db.recordings.size,
  });
}

// ============================================================================
// Bootstrap
// ============================================================================

seedAll();

const server = http.createServer((req, res) => {
  handleRequest(req, res).catch((err) => {
    log('error', 'handler_crashed', { error: String((err && err.stack) || err) });
    if (!res.headersSent) sendError(res, Errors.internal(), req.headers['x-request-id']);
    else res.end();
  });
});

server.on('clientError', (err, socket) => {
  if (socket.writable) socket.end('HTTP/1.1 400 Bad Request\r\n\r\n');
});

server.listen(CONFIG.port, CONFIG.host, () => {
  log('info', 'server_started', {
    host: CONFIG.host,
    port: CONFIG.port,
    account_id: CONFIG.accountId,
    public_base_url: PUBLIC_BASE_URL,
    allow_reset: CONFIG.allowReset,
  });
  if (CONFIG.allowReset) {
    log('warn', 'debug_endpoints_enabled', {
      note: 'POST /_reset and GET /_seed/tokens are enabled. Set ALLOW_RESET=false to disable for a hardened deployment.',
    });
  }
  for (const [name, token] of seedTokenLog) {
    log('info', 'seed_credential', { person: name, token });
  }
});

function shutdown(signal) {
  log('info', 'shutting_down', { signal });
  server.close(() => {
    log('info', 'shutdown_complete', {});
    process.exit(0);
  });
  // Force-exit if connections don't drain in time.
  setTimeout(() => {
    log('warn', 'shutdown_forced', {});
    process.exit(1);
  }, 10000).unref();
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

process.on('unhandledRejection', (reason) => {
  log('error', 'unhandled_rejection', { error: String((reason && reason.stack) || reason) });
});
process.on('uncaughtException', (err) => {
  log('error', 'uncaught_exception', { error: String((err && err.stack) || err) });
});

module.exports = { server, CONFIG };
