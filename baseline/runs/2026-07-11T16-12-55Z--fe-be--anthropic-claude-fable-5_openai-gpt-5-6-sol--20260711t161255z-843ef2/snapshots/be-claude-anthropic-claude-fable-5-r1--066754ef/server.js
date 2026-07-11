#!/usr/bin/env node
/* eslint-disable max-lines */
/**
 * Basecamp 5 API server — single file, zero dependencies, Node.js >= 18.
 *
 * Implements the Basecamp SDK API contract (reference/basecamp-sdk/openapi.json,
 * API version 2026-03-23: 131 path templates / 203 operations) on top of the
 * Recording domain model described in INIT.md §4, seeded with the deterministic
 * "Launch the new website" sample project from INIT.md §3.
 *
 * Run:
 *   node server.js                 # listens on :3000, prints seed tokens
 *   PORT=8080 node server.js
 *
 * Authentication:
 *   Every /{accountId}/... request requires `Authorization: Bearer <token>`.
 *   Tokens are stored as SHA-256 digests only. On boot the seed issues one
 *   token per person; tokens are printed to stderr unless disabled. Set
 *   BASECAMP_TOKEN_SECRET to derive stable tokens across restarts
 *   (HMAC-SHA256(secret, email) — for test rigs, not shared deployments).
 *
 * Environment:
 *   PORT (3000)                        HOST (0.0.0.0)
 *   NODE_ENV                           LOG_LEVEL (debug|info|warn|error)
 *   BASECAMP_ACCOUNT_ID (5624304)      BASECAMP_BASE_URL   (http://localhost:PORT)
 *   BASECAMP_APP_BASE_URL (=BASE_URL)  BASECAMP_CORS_ORIGIN (*)
 *   BASECAMP_TOKEN_SECRET              BASECAMP_PRINT_TOKENS (1; forced 0 if production+no secret? no — see below)
 *   BASECAMP_SEED (1)                  BASECAMP_SEED_EPOCH (ISO timestamp, default boot time)
 *   BASECAMP_PAGE_SIZE (50)            BASECAMP_RATE_LIMIT (50 req / 10s per token, 0 = off)
 *   BASECAMP_TEST_ENDPOINTS (on outside production)
 *   BASECAMP_WEBHOOK_DELIVERY (1)      BASECAMP_MAX_UPLOAD_MB (50)
 *
 * Contract behavior implemented:
 *   - Exact OpenAPI path templates (`.json` suffixes included); a trailing
 *     `.json` on a bare-{id} template is also accepted for hand-testing.
 *   - Success codes per operation (200/201/204), Rails-style error envelopes
 *     ({error, message}, plus retry_after on 429), X-Request-Id everywhere.
 *   - Pagination via `page` query param, `Link: <...>; rel="next"` and
 *     X-Total-Count headers on list endpoints.
 *   - ETag/If-None-Match on GETs (SDK response caching).
 *   - Rate limiting (429 + Retry-After), method-not-allowed 405 with Allow.
 *   - Webhook delivery: real HTTP POSTs with recent_deliveries history.
 *
 * Additive (non-spec, documented) endpoints:
 *   GET  /up                                        health check (no auth)
 *   GET  /                                          service card (no auth)
 *   GET  /{acct}/avatars/{personId}.svg             generated avatars (no auth)
 *   GET  /{acct}/attachments/{sgid}/download/{name} attachment bytes (auth)
 *   POST /integrations/{botKey}/buckets/{b}/chats/{id}/lines.json
 *                                                   chatbot line ingestion (bot key auth)
 *   POST /__test__/reset                            re-seed; owner token; disabled in production
 *
 * Honest scope notes (INIT §5.9 / §8.2 "scope honesty"):
 *   - Storage is in-memory and process-lifetime by design (spec excludes
 *     databases); state survives for the life of the process.
 *   - Schedule-entry recurrence is not implemented: entries occur once, and
 *     GET /schedule_entries/{id}/occurrences/{date} returns the entry only on
 *     dates it actually spans (404 otherwise).
 *   - Inbound email is out of scope, so no API creates Forwards; the
 *     inbox/forward/reply read+reply endpoints are fully functional but the
 *     seed contains no forwards.
 *   - Client approvals/correspondences have no create API (matches the spec);
 *     endpoints work and return empty/404 against the seed.
 *   - The INIT §3 "cloud link" seed row is represented as an Upload with a
 *     Google-Sheets content type: this OpenAPI surface has no CloudFile type.
 *   - Steps exist on cards only (matching the OpenAPI surface, which has no
 *     todo-step endpoints); the INIT §3 to-do "subtask" is seeded as a
 *     description note on the to-do instead.
 */
'use strict';

const http = require('node:http');
const crypto = require('node:crypto');
const { URL } = require('node:url');

const VERSION = '1.0.0';
const API_VERSION = '2026-03-23';

/* ------------------------------------------------------------------------ *
 * Configuration
 * ------------------------------------------------------------------------ */

function envInt(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n)) {
    process.stderr.write(`fatal: ${name}=${JSON.stringify(raw)} is not an integer\n`);
    process.exit(1);
  }
  return n;
}

function envBool(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  return !['0', 'false', 'no', 'off'].includes(raw.toLowerCase());
}

const IS_PROD = (process.env.NODE_ENV || 'development') === 'production';

const CONFIG = (() => {
  const port = envInt('PORT', 3000);
  const baseUrl = (process.env.BASECAMP_BASE_URL || `http://localhost:${port}`).replace(/\/+$/, '');
  const cfg = {
    env: process.env.NODE_ENV || 'development',
    port,
    host: process.env.HOST || '0.0.0.0',
    accountId: envInt('BASECAMP_ACCOUNT_ID', 5624304),
    baseUrl,
    appBaseUrl: (process.env.BASECAMP_APP_BASE_URL || baseUrl).replace(/\/+$/, ''),
    logLevel: (process.env.LOG_LEVEL || 'info').toLowerCase(),
    corsOrigin: process.env.BASECAMP_CORS_ORIGIN || '*',
    tokenSecret: process.env.BASECAMP_TOKEN_SECRET || null,
    printTokens: envBool('BASECAMP_PRINT_TOKENS', true),
    seed: envBool('BASECAMP_SEED', true),
    seedEpoch: process.env.BASECAMP_SEED_EPOCH || null,
    pageSize: Math.max(1, envInt('BASECAMP_PAGE_SIZE', 50)),
    rateLimit: envInt('BASECAMP_RATE_LIMIT', 50), // requests per 10s window per token; 0 disables
    rateWindowMs: 10_000,
    testEndpoints: envBool('BASECAMP_TEST_ENDPOINTS', !IS_PROD),
    webhookDelivery: envBool('BASECAMP_WEBHOOK_DELIVERY', true),
    webhookAllowPrivate: envBool('BASECAMP_WEBHOOK_ALLOW_PRIVATE', false),
    maxJsonBody: 1024 * 1024,
    maxUploadBody: Math.max(1, envInt('BASECAMP_MAX_UPLOAD_MB', 50)) * 1024 * 1024,
    shutdownGraceMs: 10_000,
  };
  if (cfg.seedEpoch && Number.isNaN(Date.parse(cfg.seedEpoch))) {
    process.stderr.write(`fatal: BASECAMP_SEED_EPOCH=${JSON.stringify(cfg.seedEpoch)} is not a parseable timestamp\n`);
    process.exit(1);
  }
  if (cfg.accountId <= 0) {
    process.stderr.write('fatal: BASECAMP_ACCOUNT_ID must be a positive integer\n');
    process.exit(1);
  }
  return cfg;
})();

/* ------------------------------------------------------------------------ *
 * Logging — single-line structured logs, secrets never logged.
 * ------------------------------------------------------------------------ */

const LOG_LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };
const LOG_THRESHOLD = LOG_LEVELS[CONFIG.logLevel] ?? LOG_LEVELS.info;

function log(level, msg, fields) {
  if ((LOG_LEVELS[level] ?? 20) < LOG_THRESHOLD) return;
  let line = `${new Date().toISOString()} ${level.padEnd(5)} ${msg}`;
  if (fields) {
    for (const [k, v] of Object.entries(fields)) {
      if (v === undefined || v === null) continue;
      line += ` ${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`;
    }
  }
  process.stderr.write(line + '\n');
}

/* ------------------------------------------------------------------------ *
 * Small utilities
 * ------------------------------------------------------------------------ */

function sha256Hex(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function hmacHex(key, value) {
  return crypto.createHmac('sha256', key).update(value).digest('hex');
}

function randomToken() {
  return 'bc5at-' + crypto.randomBytes(30).toString('base64url');
}

function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, (m) => m); // full ISO with ms
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function isDateString(v) {
  if (typeof v !== 'string' || !DATE_RE.test(v)) return false;
  const t = Date.parse(v + 'T00:00:00Z');
  return Number.isFinite(t);
}

function isDateTimeString(v) {
  return typeof v === 'string' && v.length >= 10 && Number.isFinite(Date.parse(v));
}

function dateOnly(iso) {
  return iso.slice(0, 10);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function stripHtml(s) {
  return String(s || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
}

function excerpt(s, max = 120) {
  const plain = stripHtml(s);
  return plain.length <= max ? plain : plain.slice(0, max - 1).trimEnd() + '…';
}

function compact(obj) {
  const out = {};
  for (const [k, v] of Object.entries(obj)) if (v !== undefined) out[k] = v;
  return out;
}

function parseIntStrict(raw) {
  if (typeof raw === 'number' && Number.isInteger(raw)) return raw;
  if (typeof raw === 'string' && /^\d+$/.test(raw)) return Number.parseInt(raw, 10);
  return null;
}

/** Signed global IDs (attachments, readings) — opaque, HMAC-protected. */
const SGID_KEY = CONFIG.tokenSecret || crypto.randomBytes(16).toString('hex');

function makeSgid(kind, id) {
  const payload = `${kind}/${id}`;
  const sig = hmacHex(SGID_KEY, payload).slice(0, 16);
  return Buffer.from(`${payload}/${sig}`).toString('base64url');
}

function parseSgid(sgid) {
  if (typeof sgid !== 'string' || sgid.length === 0 || sgid.length > 512) return null;
  let decoded;
  try {
    decoded = Buffer.from(sgid, 'base64url').toString('utf8');
  } catch {
    return null;
  }
  const parts = decoded.split('/');
  if (parts.length !== 3) return null;
  const [kind, id, sig] = parts;
  if (hmacHex(SGID_KEY, `${kind}/${id}`).slice(0, 16) !== sig) return null;
  return { kind, id };
}

/* ------------------------------------------------------------------------ *
 * API errors — mapped to the OpenAPI error envelopes.
 * ------------------------------------------------------------------------ */

class ApiError extends Error {
  constructor(status, error, message, extra) {
    super(message || error);
    this.status = status;
    this.error = error;
    this.detail = message;
    this.extra = extra;
  }
  body() {
    return compact({ error: this.error, message: this.detail, ...(this.extra || {}) });
  }
}

const err = {
  badRequest: (msg) => new ApiError(400, 'Bad Request', msg),
  unauthorized: (msg) => new ApiError(401, 'Unauthorized', msg || 'A valid Bearer token is required.'),
  forbidden: (msg) => new ApiError(403, 'Forbidden', msg || 'You do not have permission to perform this action.'),
  notFound: (msg) => new ApiError(404, 'Not Found', msg || 'Resource not found.'),
  methodNotAllowed: (allow) => {
    const e = new ApiError(405, 'Method Not Allowed', `Allowed methods: ${allow.join(', ')}.`);
    e.headers = { Allow: allow.join(', ') };
    return e;
  },
  unprocessable: (msg) => new ApiError(422, 'Unprocessable Entity', msg),
  tooManyRequests: (retryAfter) => {
    const e = new ApiError(429, 'Too Many Requests', 'Rate limit exceeded. Slow down and retry.', { retry_after: retryAfter });
    e.headers = { 'Retry-After': String(retryAfter) };
    return e;
  },
  webhookLimit: (msg) => new ApiError(507, 'Insufficient Storage', msg),
};

/* ------------------------------------------------------------------------ *
 * Request body readers
 * ------------------------------------------------------------------------ */

function readBody(req, maxBytes) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let done = false;
    const fail = (e) => { if (!done) { done = true; reject(e); } };
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > maxBytes) {
        req.pause();
        fail(new ApiError(413, 'Payload Too Large', `Request body exceeds ${maxBytes} bytes.`));
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => { if (!done) { done = true; resolve(Buffer.concat(chunks)); } });
    req.on('error', fail);
  });
}

async function readJsonBody(ctx) {
  const type = (ctx.req.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
  const raw = await readBody(ctx.req, CONFIG.maxJsonBody);
  if (raw.length === 0) return {};
  if (type && type !== 'application/json') {
    throw err.badRequest(`Expected Content-Type: application/json, got ${type}.`);
  }
  try {
    const parsed = JSON.parse(raw.toString('utf8'));
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw err.badRequest('Request body must be a JSON object.');
    }
    return parsed;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    throw err.badRequest('Request body is not valid JSON.');
  }
}

/**
 * Minimal multipart/form-data parser: extracts the first file part. Only used
 * by PUT /account/logo.json, which sends a single field.
 */
function parseMultipart(buffer, contentType) {
  const m = /boundary="?([^";]+)"?/i.exec(contentType || '');
  if (!m) return null;
  const boundary = Buffer.from('--' + m[1]);
  const parts = [];
  let idx = buffer.indexOf(boundary);
  while (idx !== -1) {
    const next = buffer.indexOf(boundary, idx + boundary.length);
    if (next === -1) break;
    let part = buffer.subarray(idx + boundary.length, next);
    if (part[0] === 0x0d && part[1] === 0x0a) part = part.subarray(2);
    const headerEnd = part.indexOf('\r\n\r\n');
    if (headerEnd !== -1) {
      const headers = part.subarray(0, headerEnd).toString('utf8');
      let body = part.subarray(headerEnd + 4);
      if (body.length >= 2 && body[body.length - 2] === 0x0d) body = body.subarray(0, body.length - 2);
      const nameMatch = /name="([^"]*)"/i.exec(headers);
      const fileMatch = /filename="([^"]*)"/i.exec(headers);
      const typeMatch = /content-type:\s*([^\r\n]+)/i.exec(headers);
      parts.push({
        name: nameMatch ? nameMatch[1] : null,
        filename: fileMatch ? fileMatch[1] : null,
        contentType: typeMatch ? typeMatch[1].trim() : 'application/octet-stream',
        data: body,
      });
    }
    idx = next;
  }
  return parts;
}

/* ------------------------------------------------------------------------ *
 * Validation helpers — all failures are 422 unless noted.
 * ------------------------------------------------------------------------ */

function vRequireString(body, field, { max = 20_000, label } = {}) {
  const v = body[field];
  if (typeof v !== 'string' || v.trim().length === 0) {
    throw err.unprocessable(`${label || field} is required and must be a non-empty string.`);
  }
  if (v.length > max) throw err.unprocessable(`${label || field} exceeds ${max} characters.`);
  return v;
}

function vOptString(body, field, { max = 100_000 } = {}) {
  const v = body[field];
  if (v === undefined || v === null) return undefined;
  if (typeof v !== 'string') throw err.unprocessable(`${field} must be a string.`);
  if (v.length > max) throw err.unprocessable(`${field} exceeds ${max} characters.`);
  return v;
}

function vOptBool(body, field) {
  const v = body[field];
  if (v === undefined || v === null) return undefined;
  if (typeof v !== 'boolean') throw err.unprocessable(`${field} must be a boolean.`);
  return v;
}

function vOptDate(body, field) {
  const v = body[field];
  if (v === undefined || v === null) return undefined;
  if (v === '') return null; // explicit clear
  if (!isDateString(v)) throw err.unprocessable(`${field} must be a date in YYYY-MM-DD format.`);
  return v;
}

function vOptDateTime(body, field) {
  const v = body[field];
  if (v === undefined || v === null) return undefined;
  if (!isDateTimeString(v)) throw err.unprocessable(`${field} must be an ISO 8601 timestamp.`);
  return new Date(v).toISOString();
}

function vOptIdArray(body, field) {
  const v = body[field];
  if (v === undefined || v === null) return undefined;
  if (!Array.isArray(v)) throw err.unprocessable(`${field} must be an array of numeric ids.`);
  return v.map((item) => {
    const id = parseIntStrict(item);
    if (id === null) throw err.unprocessable(`${field} must contain only numeric ids.`);
    return id;
  });
}

function vOptInt(body, field, { min, max } = {}) {
  const v = body[field];
  if (v === undefined || v === null) return undefined;
  const n = parseIntStrict(v);
  if (n === null) throw err.unprocessable(`${field} must be an integer.`);
  if (min !== undefined && n < min) throw err.unprocessable(`${field} must be >= ${min}.`);
  if (max !== undefined && n > max) throw err.unprocessable(`${field} must be <= ${max}.`);
  return n;
}

function vEnumQuery(ctx, name, allowed, fallback) {
  const raw = ctx.query.get(name);
  if (raw === null || raw === '') return fallback;
  if (!allowed.includes(raw)) {
    throw err.badRequest(`Invalid ${name} parameter: expected one of ${allowed.join(', ')}.`);
  }
  return raw;
}

/* ------------------------------------------------------------------------ *
 * In-memory store
 *
 * Everything content-shaped is a Recording (INIT §4.1): one table, one status
 * lifecycle, one parent tree. Tool roots (message board, todoset, vault,
 * schedule, chat, card table, questionnaire, inbox) are recordings too; the
 * project dock is an ordered list of tool recording ids.
 * ------------------------------------------------------------------------ */

function freshDb() {
  return {
    idCounter: 1_000_000_000,
    account: null,
    people: new Map(),            // id -> person
    tokens: new Map(),            // sha256(token) -> personId
    projects: new Map(),          // id -> project
    recs: new Map(),              // id -> recording
    children: new Map(),          // parentId -> id[] (insertion/position order)
    events: new Map(),            // recordingId -> event[] (oldest first)
    eventsById: new Map(),        // eventId -> event
    boosts: new Map(),            // id -> boost
    recBoosts: new Map(),         // recordingId -> boostId[]
    eventBoosts: new Map(),       // eventId -> boostId[]
    subs: new Map(),              // recordingId -> Set(personId)
    readings: new Map(),          // personId -> reading[] (newest first)
    messageTypes: new Map(),      // id -> {id, name, icon, created_at, updated_at}
    lineupMarkers: new Map(),     // id -> {id, name, date, created_at, updated_at}
    templates: new Map(),         // id -> template
    constructions: new Map(),     // id -> {id, status, templateId, projectId}
    webhooks: new Map(),          // id -> webhook
    chatbots: new Map(),          // id -> {id, campfireId, service_name, command_url, key, ...}
    attachments: new Map(),       // attachmentId -> {id, sgid, name, content_type, bytes, ...}
    outOfOffice: new Map(),       // personId -> {start_date, end_date}
    preferences: new Map(),       // personId -> {time_zone_name, first_week_day, time_format}
    questionNotifSettings: new Map(), // `${personId}:${questionId}` -> settings
    seedTokens: [],               // [{person, token}] — raw tokens kept only for boot banner
  };
}

let db = freshDb();

function nextId() {
  db.idCounter += 1;
  return db.idCounter;
}

/* --------------------------------- people -------------------------------- */

function createPerson(attrs) {
  const at = attrs.created_at || nowIso();
  const person = {
    id: nextId(),
    name: attrs.name,
    email_address: attrs.email_address,
    title: attrs.title || null,
    bio: attrs.bio || null,
    location: attrs.location || null,
    admin: !!attrs.admin,
    owner: !!attrs.owner,
    client: !!attrs.client,
    employee: attrs.employee !== undefined ? !!attrs.employee : !attrs.client,
    sample: !!attrs.sample,
    time_zone: attrs.time_zone || 'America/Chicago',
    company_name: attrs.company_name || null,
    active: true,
    created_at: at,
    updated_at: at,
  };
  db.people.set(person.id, person);
  return person;
}

function issueToken(person, explicitToken) {
  const token = explicitToken
    || (CONFIG.tokenSecret
      ? 'bc5at-' + Buffer.from(hmacHex(CONFIG.tokenSecret, person.email_address), 'hex').toString('base64url')
      : randomToken());
  db.tokens.set(sha256Hex(token), person.id);
  return token;
}

/* ------------------------------- recordings ------------------------------ */

const TOOL_TYPES = new Set([
  'Message::Board', 'Todoset', 'Vault', 'Schedule', 'Chat::Transcript',
  'Kanban::Board', 'Questionnaire', 'Inbox',
]);

const COLUMN_TYPES = new Set([
  'Kanban::Triage', 'Kanban::Column', 'Kanban::OnHoldColumn', 'Kanban::DoneColumn', 'Kanban::NotNowColumn',
]);

/** Recording types that accept comments. */
const COMMENTABLE = new Set([
  'Message', 'Todo', 'Todolist', 'Todolist::Group', 'Document', 'Upload',
  'Kanban::Card', 'Schedule::Entry', 'Question::Answer', 'Inbox::Forward',
  'Client::Approval', 'Client::Correspondence', 'Gauge::Needle',
]);

/** Recording types that accept boosts. */
const BOOSTABLE = new Set([
  'Message', 'Comment', 'Todo', 'Todolist', 'Todolist::Group', 'Document', 'Upload',
  'Kanban::Card', 'Schedule::Entry', 'Chat::Lines::Text', 'Question::Answer',
  'Inbox::Forward::Reply', 'Gauge::Needle', 'Client::Reply',
]);

/** Recording types with a subscriber set (notified on activity). */
const SUBSCRIBABLE = new Set([
  'Message', 'Todo', 'Todolist', 'Todolist::Group', 'Document', 'Upload', 'Vault',
  'Kanban::Card', 'Kanban::Board', 'Kanban::Triage', 'Kanban::Column',
  'Kanban::OnHoldColumn', 'Kanban::DoneColumn', 'Kanban::NotNowColumn',
  'Schedule::Entry', 'Question', 'Question::Answer', 'Chat::Transcript',
  'Inbox::Forward', 'Campfire', 'Todoset', 'Client::Approval', 'Client::Correspondence',
]);

/** `ListRecordings` type filter — the exact enum from the OpenAPI description. */
const LISTABLE_RECORDING_TYPES = {
  'Comment': 'Comment',
  'Document': 'Document',
  'Kanban::Card': 'Kanban::Card',
  'Kanban::Step': 'Kanban::Step',
  'Message': 'Message',
  'Question::Answer': 'Question::Answer',
  'Schedule::Entry': 'Schedule::Entry',
  'Todo': 'Todo',
  'Todolist': 'Todolist',
  'Upload': 'Upload',
  'Vault': 'Vault',
};

function childIds(parentId) {
  let arr = db.children.get(parentId);
  if (!arr) { arr = []; db.children.set(parentId, arr); }
  return arr;
}

function createRec(attrs) {
  const at = attrs.created_at || nowIso();
  const rec = {
    id: nextId(),
    type: attrs.type,
    bucketId: attrs.bucketId,
    parentId: attrs.parentId ?? null,
    creatorId: attrs.creatorId,
    status: attrs.status || 'active',
    visible_to_clients: attrs.visible_to_clients ?? false,
    inherits_status: attrs.inherits_status ?? true,
    created_at: at,
    updated_at: attrs.updated_at || at,
    ...attrs.fields,
  };
  db.recs.set(rec.id, rec);
  if (rec.parentId !== null) {
    const siblings = childIds(rec.parentId);
    if (attrs.prepend) siblings.unshift(rec.id); else siblings.push(rec.id);
  }
  return rec;
}

function removeFromParent(rec) {
  if (rec.parentId === null) return;
  const siblings = childIds(rec.parentId);
  const i = siblings.indexOf(rec.id);
  if (i !== -1) siblings.splice(i, 1);
}

function reparentRec(rec, newParentId, position) {
  removeFromParent(rec);
  rec.parentId = newParentId;
  const siblings = childIds(newParentId);
  if (position === undefined || position === null || position > siblings.length) {
    siblings.push(rec.id);
  } else {
    siblings.splice(Math.max(0, position - 1), 0, rec.id);
  }
}

function repositionRec(rec, position) {
  reparentRec(rec, rec.parentId, position);
}

function touch(rec, at) {
  rec.updated_at = at || nowIso();
}

/** Effective lifecycle status: own status unless inherited from ancestors. */
function effStatus(rec) {
  let cur = rec;
  for (let depth = 0; cur && depth < 32; depth += 1) {
    if (cur.status !== 'active') return cur.status;
    if (!cur.inherits_status || cur.parentId === null) break;
    cur = db.recs.get(cur.parentId);
  }
  const project = db.projects.get(rec.bucketId);
  if (project && project.status !== 'active') return project.status;
  return 'active';
}

function childrenOf(parentId, types) {
  const wanted = types ? (Array.isArray(types) ? new Set(types) : new Set([types])) : null;
  const out = [];
  for (const id of db.children.get(parentId) || []) {
    const rec = db.recs.get(id);
    if (rec && (!wanted || wanted.has(rec.type))) out.push(rec);
  }
  return out;
}

function descendantsOf(rootId, types, acc = []) {
  for (const id of db.children.get(rootId) || []) {
    const rec = db.recs.get(id);
    if (!rec) continue;
    if (!types || types.has(rec.type)) acc.push(rec);
    descendantsOf(id, types, acc);
  }
  return acc;
}

function positionOf(rec) {
  if (rec.parentId === null) return undefined;
  const i = childIds(rec.parentId).indexOf(rec.id);
  return i === -1 ? undefined : i + 1;
}

/** Walk up to the tool-root recording (message board, todoset, …). */
function toolRootOf(rec) {
  let cur = rec;
  for (let depth = 0; cur && depth < 32; depth += 1) {
    if (TOOL_TYPES.has(cur.type)) return cur;
    cur = cur.parentId === null ? null : db.recs.get(cur.parentId);
  }
  return null;
}

/* ------------------------------- projects -------------------------------- */

const DOCK_ORDER = ['Message::Board', 'Todoset', 'Vault', 'Chat::Transcript', 'Schedule', 'Kanban::Board', 'Questionnaire', 'Inbox'];

const TOOL_DEFAULTS = {
  'Message::Board': { name: 'message_board', title: 'Message Board' },
  'Todoset': { name: 'todoset', title: 'To-dos' },
  'Vault': { name: 'vault', title: 'Docs & Files' },
  'Schedule': { name: 'schedule', title: 'Schedule' },
  'Chat::Transcript': { name: 'chat', title: 'Campfire' },
  'Kanban::Board': { name: 'kanban_board', title: 'Card Table' },
  'Questionnaire': { name: 'questionnaire', title: 'Automatic Check-ins' },
  'Inbox': { name: 'inbox', title: 'Email Forwards' },
};

/**
 * Creates a project with its full dock. Per INIT §4.5 new projects start
 * "empty": every tool exists but is disabled until enabled via the dock API.
 */
function createProject(attrs) {
  const at = attrs.created_at || nowIso();
  const project = {
    id: nextId(),
    name: attrs.name,
    description: attrs.description || null,
    purpose: attrs.purpose || 'topic',
    status: 'active',
    clients_enabled: !!attrs.clients_enabled,
    all_access: !!attrs.all_access,
    admissions: attrs.admissions || 'invite',
    starts_on: attrs.starts_on || null,
    ends_on: attrs.ends_on || null,
    sample: !!attrs.sample,
    creatorId: attrs.creatorId,
    access: new Set(attrs.access || []),
    dock: [],
    created_at: at,
    updated_at: at,
  };
  db.projects.set(project.id, project);
  for (const type of DOCK_ORDER) {
    const tool = createRec({
      type,
      bucketId: project.id,
      parentId: null,
      creatorId: attrs.creatorId,
      created_at: at,
      fields: {
        dockTitle: TOOL_DEFAULTS[type].title,
        enabled: false,
        // Kanban boards own their built-in lanes; created below.
      },
    });
    project.dock.push(tool.id);
    if (type === 'Kanban::Board') {
      const lane = (laneType, title) => createRec({
        type: laneType, bucketId: project.id, parentId: tool.id, creatorId: attrs.creatorId,
        created_at: at, fields: { title },
      });
      lane('Kanban::Triage', 'Triage');
      lane('Kanban::NotNowColumn', 'Not now');
      lane('Kanban::DoneColumn', 'Done');
    }
  }
  return project;
}

function dockToolRecs(project, { enabledOnly = false } = {}) {
  const out = [];
  for (const id of project.dock) {
    const rec = db.recs.get(id);
    if (rec && (!enabledOnly || rec.enabled)) out.push(rec);
  }
  return out;
}

function findDockTool(project, type) {
  return dockToolRecs(project).find((t) => t.type === type) || null;
}

/* ------------------------------ authorization ---------------------------- */

function canSeeProject(person, project) {
  if (!project) return false;
  if (project.status === 'trashed' && !(person.admin || person.owner)) return false;
  if (person.owner) return true;
  if (project.access.has(person.id)) return true;
  if (project.all_access && person.employee) return true;
  return false;
}

function visibleProjects(person, { statuses = ['active'] } = {}) {
  const out = [];
  for (const project of db.projects.values()) {
    if (statuses.includes(project.status) && canSeeProject(person, project)) out.push(project);
  }
  return out;
}

function canSeeRec(person, rec) {
  const project = db.projects.get(rec.bucketId);
  if (!canSeeProject(person, project)) return false;
  if (person.client && !rec.visible_to_clients) return false;
  if (rec.status === 'drafted' && rec.creatorId !== person.id) return false;
  return true;
}

/** Personal-voice recordings: only the creator (or admins/owners) may edit. */
const PERSONAL_VOICE = new Set(['Message', 'Comment', 'Chat::Lines::Text', 'Question::Answer', 'Inbox::Forward::Reply', 'Client::Reply']);

function canModifyRec(person, rec) {
  if (person.owner || person.admin) return true;
  if (PERSONAL_VOICE.has(rec.type)) return rec.creatorId === person.id;
  return !person.client || rec.visible_to_clients;
}

function requireProjectMutable(person, project) {
  if (project.status !== 'active') {
    throw err.unprocessable(`This project is ${project.status} and read-only.`);
  }
  void person;
}

/* ------------------------------ subscriptions ---------------------------- */

function subscriberSet(recId) {
  let set = db.subs.get(recId);
  if (!set) { set = new Set(); db.subs.set(recId, set); }
  return set;
}

function subscribe(rec, personId) {
  subscriberSet(rec.id).add(personId);
}

function unsubscribe(rec, personId) {
  subscriberSet(rec.id).delete(personId);
}

/** Extract `data-mention-person-id` chips from rich text (INIT §5.4). */
function mentionedPersonIds(content) {
  const ids = new Set();
  if (typeof content !== 'string') return ids;
  const re = /data-mention-person-id="(\d+)"/g;
  let m;
  while ((m = re.exec(content)) !== null) {
    const id = Number.parseInt(m[1], 10);
    if (db.people.has(id)) ids.add(id);
  }
  return ids;
}

function applyMentions(rec, content, actorId) {
  for (const pid of mentionedPersonIds(content)) {
    if (pid !== actorId) subscribe(rec, pid);
  }
}

/* --------------------------- events + notifications ---------------------- */

function snakeType(type) {
  return type.toLowerCase().replace(/::/g, '_');
}

/**
 * Records an Event on a recording (the audit trail behind Activity, progress
 * reports and webhooks), fans out notifications to subscribers, and queues
 * webhook deliveries. Drafts are silent (INIT §4.3).
 */
function recordEvent(rec, actorId, action, { details, at, notify = true, excerptText } = {}) {
  if (rec.status === 'drafted') return null;
  const event = {
    id: nextId(),
    recordingId: rec.id,
    action,
    details: details || {},
    created_at: at || nowIso(),
    creatorId: actorId,
  };
  let list = db.events.get(rec.id);
  if (!list) { list = []; db.events.set(rec.id, list); }
  list.push(event);
  db.eventsById.set(event.id, event);
  if (notify) notifySubscribers(rec, event, actorId, excerptText);
  queueWebhooks(rec, event);
  return event;
}

function readingsOf(personId) {
  let list = db.readings.get(personId);
  if (!list) { list = []; db.readings.set(personId, list); }
  return list;
}

function notifySubscribers(rec, event, actorId, excerptText) {
  const targets = new Set(subscriberSet(rec.id));
  // Comments/boost-less children notify the parent's subscribers too.
  if (rec.type === 'Comment' && rec.parentId !== null) {
    for (const pid of subscriberSet(rec.parentId)) targets.add(pid);
  }
  const notified = [];
  for (const personId of targets) {
    if (personId === actorId) continue;
    const person = db.people.get(personId);
    if (!person || !canSeeRec(person, rec)) continue;
    const reading = {
      id: nextId(),
      personId,
      recordingId: rec.id,
      eventId: event.id,
      created_at: event.created_at,
      updated_at: event.created_at,
      unread_at: event.created_at,
      read_at: null,
      excerpt: excerptText || excerpt(rec.content || rec.title || '', 160),
    };
    readingsOf(personId).unshift(reading);
    notified.push(personId);
  }
  if (notified.length > 0) event.details.notified_recipient_ids = notified;
}

/* ------------------------------- webhooks -------------------------------- */

const MAX_WEBHOOKS_PER_BUCKET = 25;
const WEBHOOK_TIMEOUT_MS = 5_000;
const WEBHOOK_HISTORY = 5;

function bucketWebhooks(bucketId) {
  return [...db.webhooks.values()].filter((w) => w.bucketId === bucketId);
}

function webhookMatches(webhook, rec) {
  if (!webhook.active) return false;
  if (!webhook.types || webhook.types.length === 0) return true;
  return webhook.types.includes('all') || webhook.types.includes('all_events') || webhook.types.includes(rec.type);
}

function queueWebhooks(rec, event) {
  if (!CONFIG.webhookDelivery) return;
  for (const webhook of bucketWebhooks(rec.bucketId)) {
    if (webhookMatches(webhook, rec)) {
      deliverWebhook(webhook, rec, event).catch((e) => {
        log('warn', 'webhook delivery error', { webhook: webhook.id, error: e.message });
      });
    }
  }
}

async function deliverWebhook(webhook, rec, event) {
  const payload = {
    id: event.id,
    kind: `${snakeType(rec.type)}_${event.action}`,
    details: event.details,
    created_at: event.created_at,
    recording: serializeRec(rec),
    creator: serializePerson(db.people.get(event.creatorId)),
  };
  const body = JSON.stringify(payload);
  const requestHeaders = {
    'Content-Type': 'application/json',
    'User-Agent': `basecamp5-clone/${VERSION} (api:${API_VERSION})`,
  };
  const delivery = {
    id: nextId(),
    created_at: nowIso(),
    request: { headers: requestHeaders, body: payload },
    response: { headers: {}, code: 0, message: '' },
  };
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), WEBHOOK_TIMEOUT_MS);
    const res = await fetch(webhook.payload_url, {
      method: 'POST', headers: requestHeaders, body, signal: controller.signal, redirect: 'error',
    });
    clearTimeout(timer);
    delivery.response.code = res.status;
    delivery.response.message = res.statusText;
    for (const [k, v] of res.headers.entries()) {
      if (Object.keys(delivery.response.headers).length < 16) delivery.response.headers[k] = v;
    }
  } catch (e) {
    delivery.response.code = 0;
    delivery.response.message = e.name === 'AbortError' ? 'timeout' : String(e.message || e).slice(0, 200);
  }
  webhook.deliveries.unshift(delivery);
  webhook.deliveries.length = Math.min(webhook.deliveries.length, WEBHOOK_HISTORY);
  log('debug', 'webhook delivered', { webhook: webhook.id, code: delivery.response.code });
}

/* ------------------------------ rate limiting ----------------------------- */

const rateBuckets = new Map(); // key -> number[] (request timestamps)

function checkRateLimit(key) {
  if (CONFIG.rateLimit <= 0) return null;
  const now = Date.now();
  let stamps = rateBuckets.get(key);
  if (!stamps) { stamps = []; rateBuckets.set(key, stamps); }
  while (stamps.length > 0 && stamps[0] <= now - CONFIG.rateWindowMs) stamps.shift();
  if (stamps.length >= CONFIG.rateLimit) {
    return Math.max(1, Math.ceil((stamps[0] + CONFIG.rateWindowMs - now) / 1000));
  }
  stamps.push(now);
  return null;
}

setInterval(() => {
  const cutoff = Date.now() - CONFIG.rateWindowMs;
  for (const [key, stamps] of rateBuckets) {
    if (stamps.length === 0 || stamps[stamps.length - 1] <= cutoff) rateBuckets.delete(key);
  }
}, 60_000).unref();

/* ------------------------------------------------------------------------ *
 * Serializers — one envelope for every Recording (INIT §4.1), thin per-type
 * extenders on top. Field sets match the OpenAPI component schemas.
 * ------------------------------------------------------------------------ */

function apiUrl(path) {
  return `${CONFIG.baseUrl}/${CONFIG.accountId}${path}`;
}

function appUrl(path) {
  return `${CONFIG.appBaseUrl}/${CONFIG.accountId}${path}`;
}

/** API path for the canonical GET of each recording type. */
function recUrl(rec) {
  switch (rec.type) {
    case 'Message::Board': return apiUrl(`/message_boards/${rec.id}`);
    case 'Message': return apiUrl(`/messages/${rec.id}`);
    case 'Comment': return apiUrl(`/comments/${rec.id}`);
    case 'Todoset': return apiUrl(`/todosets/${rec.id}`);
    case 'Todolist':
    case 'Todolist::Group': return apiUrl(`/todolists/${rec.id}`);
    case 'Todo': return apiUrl(`/todos/${rec.id}`);
    case 'Vault': return apiUrl(`/vaults/${rec.id}`);
    case 'Document': return apiUrl(`/documents/${rec.id}`);
    case 'Upload': return apiUrl(`/uploads/${rec.id}`);
    case 'Chat::Transcript': return apiUrl(`/chats/${rec.id}`);
    case 'Chat::Lines::Text': return apiUrl(`/chats/${rec.parentId}/lines/${rec.id}`);
    case 'Kanban::Board': return apiUrl(`/card_tables/${rec.id}`);
    case 'Kanban::Triage':
    case 'Kanban::Column':
    case 'Kanban::OnHoldColumn':
    case 'Kanban::DoneColumn':
    case 'Kanban::NotNowColumn': return apiUrl(`/card_tables/columns/${rec.id}`);
    case 'Kanban::Card': return apiUrl(`/card_tables/cards/${rec.id}`);
    case 'Kanban::Step': return apiUrl(`/card_tables/steps/${rec.id}`);
    case 'Schedule': return apiUrl(`/schedules/${rec.id}`);
    case 'Schedule::Entry': return apiUrl(`/schedule_entries/${rec.id}`);
    case 'Questionnaire': return apiUrl(`/questionnaires/${rec.id}`);
    case 'Question': return apiUrl(`/questions/${rec.id}`);
    case 'Question::Answer': return apiUrl(`/question_answers/${rec.id}`);
    case 'Inbox': return apiUrl(`/inboxes/${rec.id}`);
    case 'Inbox::Forward': return apiUrl(`/inbox_forwards/${rec.id}`);
    case 'Inbox::Forward::Reply': return apiUrl(`/inbox_forwards/${rec.parentId}/replies/${rec.id}`);
    case 'Gauge': return apiUrl(`/projects/${rec.bucketId}/gauge/needles.json`);
    case 'Gauge::Needle': return apiUrl(`/gauge_needles/${rec.id}`);
    case 'Timesheet::Entry': return apiUrl(`/timesheet_entries/${rec.id}`);
    case 'Client::Approval': return apiUrl(`/client/approvals/${rec.id}`);
    case 'Client::Correspondence': return apiUrl(`/client/correspondences/${rec.id}`);
    case 'Client::Reply': return apiUrl(`/client/recordings/${rec.parentId}/replies/${rec.id}`);
    default: return apiUrl(`/recordings/${rec.id}`);
  }
}

/** HTML-app style URL (INIT §4.5 "URL shapes"). */
function recAppUrl(rec) {
  const b = rec.bucketId;
  const slugMap = {
    'Message::Board': 'message_boards', 'Message': 'messages', 'Comment': 'comments',
    'Todoset': 'todosets', 'Todolist': 'todolists', 'Todolist::Group': 'todolists', 'Todo': 'todos',
    'Vault': 'vaults', 'Document': 'documents', 'Upload': 'uploads',
    'Chat::Transcript': 'chats', 'Chat::Lines::Text': 'chats',
    'Kanban::Board': 'card_tables', 'Kanban::Card': 'card_tables/cards', 'Kanban::Step': 'card_tables/steps',
    'Kanban::Triage': 'card_tables/lists', 'Kanban::Column': 'card_tables/lists',
    'Kanban::OnHoldColumn': 'card_tables/lists', 'Kanban::DoneColumn': 'card_tables/lists',
    'Kanban::NotNowColumn': 'card_tables/lists',
    'Schedule': 'schedules', 'Schedule::Entry': 'schedule_entries',
    'Questionnaire': 'questionnaires', 'Question': 'questions', 'Question::Answer': 'question_answers',
    'Inbox': 'inboxes', 'Inbox::Forward': 'inbox_forwards', 'Inbox::Forward::Reply': 'inbox_forwards',
    'Gauge': 'gauges', 'Gauge::Needle': 'gauge_needles', 'Timesheet::Entry': 'timesheet_entries',
    'Client::Approval': 'client/approvals', 'Client::Correspondence': 'client/correspondences',
    'Client::Reply': 'client/replies',
  };
  if (rec.type === 'Comment') {
    const parent = db.recs.get(rec.parentId);
    if (parent) return `${recAppUrl(parent)}#__recording_${rec.id}`;
  }
  if (rec.type === 'Chat::Lines::Text') return appUrl(`/buckets/${b}/chats/${rec.parentId}#line_${rec.id}`);
  return appUrl(`/buckets/${b}/${slugMap[rec.type] || 'recordings'}/${rec.id}`);
}

function recTitle(rec) {
  switch (rec.type) {
    case 'Message': return rec.subject || '';
    case 'Todo': return stripHtml(rec.content || '');
    case 'Todolist':
    case 'Todolist::Group': return rec.name || '';
    case 'Chat::Lines::Text': return excerpt(rec.content || '', 64);
    case 'Schedule::Entry': return rec.summary || '';
    case 'Upload': return uploadFilename(rec);
    case 'Question::Answer': {
      const q = db.recs.get(rec.parentId);
      return q ? q.title : 'Answer';
    }
    case 'Timesheet::Entry': return rec.description || 'Time entry';
    case 'Gauge::Needle': return rec.description ? excerpt(rec.description, 64) : 'Progress update';
    default: return rec.title || rec.dockTitle || rec.subject || rec.name || '';
  }
}

function uploadFilename(rec) {
  const ext = rec.extension ? `.${rec.extension}` : '';
  return `${rec.base_name || 'file'}${ext}`;
}

function commentsCount(rec) {
  return childrenOf(rec.id, 'Comment').filter((c) => effStatus(c) === 'active').length;
}

function boostsCount(rec) {
  return (db.recBoosts.get(rec.id) || []).length;
}

function bucketRef(rec) {
  const project = db.projects.get(rec.bucketId);
  return { id: rec.bucketId, name: project ? project.name : 'Unknown', type: 'Project' };
}

function parentRef(rec) {
  if (rec.parentId === null) return undefined;
  const parent = db.recs.get(rec.parentId);
  if (!parent) return undefined;
  return { id: parent.id, title: recTitle(parent), type: parent.type, url: recUrl(parent), app_url: recAppUrl(parent) };
}

function serializePerson(person) {
  if (!person) return undefined;
  return compact({
    id: person.id,
    attachable_sgid: makeSgid('person', person.id),
    name: person.name,
    email_address: person.email_address,
    personable_type: person.client ? 'Client' : 'User',
    title: person.title ?? undefined,
    bio: person.bio ?? undefined,
    location: person.location ?? undefined,
    created_at: person.created_at,
    updated_at: person.updated_at,
    admin: person.admin,
    owner: person.owner,
    client: person.client,
    employee: person.employee,
    time_zone: person.time_zone,
    avatar_url: apiUrl(`/avatars/${person.id}.svg`),
    company: person.company_name ? { id: CONFIG.accountId, name: person.company_name } : undefined,
    can_manage_projects: person.employee,
    can_manage_people: person.admin || person.owner,
    can_ping: !person.client,
    can_access_timesheet: person.employee,
    can_access_hill_charts: person.employee,
  });
}

/** Shared recording envelope. */
function recEnvelope(rec) {
  const out = {
    id: rec.id,
    status: effStatus(rec),
    visible_to_clients: rec.visible_to_clients,
    created_at: rec.created_at,
    updated_at: rec.updated_at,
    title: recTitle(rec),
    inherits_status: rec.inherits_status,
    type: rec.type,
    url: recUrl(rec),
    app_url: recAppUrl(rec),
    bookmark_url: apiUrl(`/my/bookmarks/${makeSgid('bookmark', rec.id)}`),
  };
  if (SUBSCRIBABLE.has(rec.type)) out.subscription_url = apiUrl(`/recordings/${rec.id}/subscription.json`);
  if (COMMENTABLE.has(rec.type)) {
    out.comments_count = commentsCount(rec);
    out.comments_url = apiUrl(`/recordings/${rec.id}/comments.json`);
  }
  if (BOOSTABLE.has(rec.type)) {
    out.boosts_count = boostsCount(rec);
    out.boosts_url = apiUrl(`/recordings/${rec.id}/boosts.json`);
  }
  const parent = parentRef(rec);
  if (parent) out.parent = parent;
  out.bucket = bucketRef(rec);
  out.creator = serializePerson(db.people.get(rec.creatorId));
  const pos = positionOf(rec);
  if (pos !== undefined) out.position = pos;
  return out;
}

function peopleByIds(ids) {
  return (ids || []).map((id) => serializePerson(db.people.get(id))).filter(Boolean);
}

function completionStats(container) {
  // container: Todolist, Todolist::Group or Todoset (counts nested todos).
  let done = 0;
  let total = 0;
  const visit = (parentId) => {
    for (const child of childrenOf(parentId)) {
      if (child.type === 'Todo' && effStatus(child) === 'active') {
        total += 1;
        if (child.completed) done += 1;
      } else if (child.type === 'Todolist' || child.type === 'Todolist::Group') {
        if (effStatus(child) === 'active') visit(child.id);
      }
    }
  };
  visit(container.id);
  return { done, total };
}

const TYPE_SERIALIZERS = {
  'Message::Board': (rec) => {
    const msgs = childrenOf(rec.id, 'Message').filter((m) => effStatus(m) === 'active' && m.status !== 'drafted');
    return {
      ...recEnvelope(rec),
      messages_count: msgs.length,
      messages_url: apiUrl(`/message_boards/${rec.id}/messages.json`),
      app_messages_url: appUrl(`/buckets/${rec.bucketId}/message_boards/${rec.id}/messages`),
    };
  },
  'Message': (rec) => compact({
    ...recEnvelope(rec),
    subject: rec.subject,
    content: rec.content || '',
    category: rec.categoryId !== undefined && rec.categoryId !== null ? serializeMessageType(db.messageTypes.get(rec.categoryId)) : undefined,
    pinned: rec.pinned || false, // additive: pin state is otherwise unobservable
  }),
  'Comment': (rec) => ({
    ...recEnvelope(rec),
    content: rec.content || '',
  }),
  'Todoset': (rec) => {
    const lists = childrenOf(rec.id, 'Todolist').filter((l) => effStatus(l) === 'active');
    const { done, total } = completionStats(rec);
    return {
      ...recEnvelope(rec),
      name: rec.dockTitle || 'To-dos',
      todolists_count: lists.length,
      todolists_url: apiUrl(`/todosets/${rec.id}/todolists.json`),
      app_todolists_url: appUrl(`/buckets/${rec.bucketId}/todosets/${rec.id}/todolists`),
      completed_ratio: `${done}/${total}`,
      completed: total > 0 && done === total,
    };
  },
  'Todolist': (rec) => {
    const { done, total } = completionStats(rec);
    return compact({
      ...recEnvelope(rec),
      description: rec.description ?? '',
      name: rec.name,
      completed: total > 0 && done === total,
      completed_ratio: `${done}/${total}`,
      todos_url: apiUrl(`/todolists/${rec.id}/todos.json`),
      groups_url: apiUrl(`/todolists/${rec.id}/groups.json`),
      app_todos_url: appUrl(`/buckets/${rec.bucketId}/todolists/${rec.id}`),
    });
  },
  'Todolist::Group': (rec) => {
    const { done, total } = completionStats(rec);
    return compact({
      ...recEnvelope(rec),
      name: rec.name,
      completed: total > 0 && done === total,
      completed_ratio: `${done}/${total}`,
      todos_url: apiUrl(`/todolists/${rec.id}/todos.json`),
      app_todos_url: appUrl(`/buckets/${rec.bucketId}/todolists/${rec.id}`),
    });
  },
  'Todo': (rec) => compact({
    ...recEnvelope(rec),
    description: rec.description ?? '',
    completed: rec.completed || false,
    content: rec.content,
    starts_on: rec.starts_on ?? null,
    due_on: rec.due_on ?? null,
    assignees: peopleByIds(rec.assigneeIds),
    completion_subscribers: peopleByIds(rec.completionSubscriberIds),
    completion_url: apiUrl(`/todos/${rec.id}/completion.json`),
  }),
  'Vault': (rec) => {
    const docs = childrenOf(rec.id, 'Document').filter((c) => effStatus(c) === 'active');
    const uploads = childrenOf(rec.id, 'Upload').filter((c) => effStatus(c) === 'active');
    const vaults = childrenOf(rec.id, 'Vault').filter((c) => effStatus(c) === 'active');
    return {
      ...recEnvelope(rec),
      title: rec.title || rec.dockTitle,
      documents_count: docs.length,
      documents_url: apiUrl(`/vaults/${rec.id}/documents.json`),
      uploads_count: uploads.length,
      uploads_url: apiUrl(`/vaults/${rec.id}/uploads.json`),
      vaults_count: vaults.length,
      vaults_url: apiUrl(`/vaults/${rec.id}/vaults.json`),
    };
  },
  'Document': (rec) => ({
    ...recEnvelope(rec),
    content: rec.content || '',
  }),
  'Upload': (rec) => {
    const att = rec.attachmentId ? db.attachments.get(rec.attachmentId) : null;
    return compact({
      ...recEnvelope(rec),
      description: rec.description ?? '',
      content_type: att ? att.content_type : rec.content_type,
      byte_size: att ? att.byte_size : rec.byte_size || 0,
      width: (att && att.width) || rec.width || undefined,
      height: (att && att.height) || rec.height || undefined,
      download_url: att ? attachmentDownloadUrl(att, uploadFilename(rec)) : undefined,
      filename: uploadFilename(rec),
    });
  },
  'Chat::Transcript': (rec) => ({
    ...recEnvelope(rec),
    title: rec.dockTitle || 'Campfire',
    topic: rec.topic || null,
    lines_url: apiUrl(`/chats/${rec.id}/lines.json`),
    files_url: apiUrl(`/chats/${rec.id}/uploads.json`),
  }),
  'Chat::Lines::Text': (rec) => compact({
    ...recEnvelope(rec),
    content: rec.content || '',
    attachments: (rec.attachmentIds || []).map((attId) => {
      const att = db.attachments.get(attId);
      if (!att) return null;
      return {
        title: att.name,
        url: attachmentDownloadUrl(att, att.name),
        filename: att.name,
        content_type: att.content_type,
        byte_size: att.byte_size,
        download_url: attachmentDownloadUrl(att, att.name),
      };
    }).filter(Boolean),
  }),
  'Kanban::Board': (rec) => ({
    ...recEnvelope(rec),
    title: rec.dockTitle || 'Card Table',
    subscribers: peopleByIds([...subscriberSet(rec.id)]),
    lists: cardTableLanes(rec).map(serializeColumn),
  }),
  'Kanban::Card': (rec) => compact({
    ...recEnvelope(rec),
    content: rec.content || '',
    description: rec.content || '',
    due_on: rec.due_on ?? null,
    completed: rec.completed || false,
    completed_at: rec.completed_at ?? undefined,
    completion_url: apiUrl(`/card_tables/cards/${rec.id}`),
    completer: rec.completerId ? serializePerson(db.people.get(rec.completerId)) : undefined,
    assignees: peopleByIds(rec.assigneeIds),
    completion_subscribers: peopleByIds(rec.completionSubscriberIds),
    steps: childrenOf(rec.id, 'Kanban::Step').filter((s) => effStatus(s) === 'active').map(serializeRec),
  }),
  'Kanban::Step': (rec) => compact({
    ...recEnvelope(rec),
    due_on: rec.due_on ?? null,
    completed: rec.completed || false,
    completed_at: rec.completed_at ?? undefined,
    completer: rec.completerId ? serializePerson(db.people.get(rec.completerId)) : undefined,
    assignees: peopleByIds(rec.assigneeIds),
    completion_url: apiUrl(`/card_tables/steps/${rec.id}/completions.json`),
  }),
  'Schedule': (rec) => {
    const entries = childrenOf(rec.id, 'Schedule::Entry').filter((e) => effStatus(e) === 'active');
    return {
      ...recEnvelope(rec),
      title: rec.dockTitle || 'Schedule',
      include_due_assignments: rec.include_due_assignments ?? true,
      entries_count: entries.length,
      entries_url: apiUrl(`/schedules/${rec.id}/entries.json`),
    };
  },
  'Schedule::Entry': (rec) => compact({
    ...recEnvelope(rec),
    summary: rec.summary,
    description: rec.description ?? '',
    all_day: rec.all_day || false,
    starts_at: rec.starts_at,
    ends_at: rec.ends_at,
    participants: peopleByIds(rec.participantIds),
  }),
  'Questionnaire': (rec) => {
    const questions = childrenOf(rec.id, 'Question').filter((q) => effStatus(q) === 'active');
    return {
      ...recEnvelope(rec),
      name: rec.dockTitle || 'Automatic Check-ins',
      questions_count: questions.length,
      questions_url: apiUrl(`/questionnaires/${rec.id}/questions.json`),
    };
  },
  'Question': (rec) => {
    const answers = childrenOf(rec.id, 'Question::Answer').filter((a) => effStatus(a) === 'active');
    return compact({
      ...recEnvelope(rec),
      paused: rec.paused || false,
      schedule: rec.schedule,
      answers_count: answers.length,
      answers_url: apiUrl(`/questions/${rec.id}/answers.json`),
    });
  },
  'Question::Answer': (rec) => compact({
    ...recEnvelope(rec),
    content: rec.content || '',
    group_on: rec.group_on,
  }),
  'Inbox': (rec) => {
    const forwards = childrenOf(rec.id, 'Inbox::Forward').filter((f) => effStatus(f) === 'active');
    return {
      ...recEnvelope(rec),
      title: rec.dockTitle || 'Email Forwards',
      forwards_count: forwards.length,
      forwards_url: apiUrl(`/inboxes/${rec.id}/forwards.json`),
    };
  },
  'Inbox::Forward': (rec) => {
    const replies = childrenOf(rec.id, 'Inbox::Forward::Reply').filter((r) => effStatus(r) === 'active');
    return compact({
      ...recEnvelope(rec),
      content: rec.content || '',
      subject: rec.subject,
      from: rec.from,
      replies_count: replies.length,
      replies_url: apiUrl(`/inbox_forwards/${rec.id}/replies.json`),
    });
  },
  'Inbox::Forward::Reply': (rec) => ({
    ...recEnvelope(rec),
    content: rec.content || '',
  }),
  'Gauge': (rec) => compact({
    ...recEnvelope(rec),
    title: rec.dockTitle || 'Gauge',
    description: rec.description ?? '',
    enabled: rec.enabled || false,
    last_needle_color: rec.lastNeedle ? rec.lastNeedle.color : undefined,
    last_needle_position: rec.lastNeedle ? rec.lastNeedle.position : undefined,
    previous_needle_position: rec.previousNeedle ? rec.previousNeedle.position : undefined,
  }),
  'Gauge::Needle': (rec) => compact({
    ...recEnvelope(rec),
    description: rec.description ?? '',
    color: rec.color,
    position: rec.needlePosition,
  }),
  'Timesheet::Entry': (rec) => compact({
    ...recEnvelope(rec),
    date: rec.date,
    description: rec.description ?? '',
    hours: rec.hours,
    person: serializePerson(db.people.get(rec.personId)),
  }),
  'Client::Approval': (rec) => compact({
    ...recEnvelope(rec),
    content: rec.content || '',
    subject: rec.subject,
    due_on: rec.due_on ?? null,
    replies_count: 0,
    replies_url: apiUrl(`/client/recordings/${rec.id}/replies.json`),
    approval_status: rec.approval_status || 'pending',
    approver: rec.approverId ? serializePerson(db.people.get(rec.approverId)) : undefined,
    responses: [],
  }),
  'Client::Correspondence': (rec) => {
    const replies = childrenOf(rec.id, 'Client::Reply').filter((r) => effStatus(r) === 'active');
    return compact({
      ...recEnvelope(rec),
      content: rec.content || '',
      subject: rec.subject,
      replies_count: replies.length,
      replies_url: apiUrl(`/client/recordings/${rec.id}/replies.json`),
    });
  },
  'Client::Reply': (rec) => ({
    ...recEnvelope(rec),
    content: rec.content || '',
  }),
};

/** Column ordering for CardTable.lists: Triage, user columns, Not now, Done. */
function cardTableLanes(board) {
  const kids = childrenOf(board.id);
  const triage = kids.filter((k) => k.type === 'Kanban::Triage');
  const cols = kids.filter((k) => k.type === 'Kanban::Column' && effStatus(k) === 'active');
  const notNow = kids.filter((k) => k.type === 'Kanban::NotNowColumn');
  const done = kids.filter((k) => k.type === 'Kanban::DoneColumn');
  return [...triage, ...cols, ...notNow, ...done];
}

function serializeColumn(rec) {
  const cards = childrenOf(rec.id, 'Kanban::Card').filter((c) => effStatus(c) === 'active');
  const out = compact({
    ...recEnvelope(rec),
    color: rec.color ?? null,
    description: rec.description ?? '',
    cards_count: cards.length,
    comments_count: 0,
    cards_url: apiUrl(`/card_tables/lists/${rec.id}/cards.json`),
    subscribers: peopleByIds([...subscriberSet(rec.id)]),
  });
  if (rec.type === 'Kanban::Column' && rec.onHoldId) {
    const hold = db.recs.get(rec.onHoldId);
    if (hold) {
      const holdCards = childrenOf(hold.id, 'Kanban::Card').filter((c) => effStatus(c) === 'active');
      out.on_hold = {
        id: hold.id,
        status: effStatus(hold),
        inherits_status: hold.inherits_status,
        title: hold.title || 'On Hold',
        created_at: hold.created_at,
        updated_at: hold.updated_at,
        cards_count: holdCards.length,
        cards_url: apiUrl(`/card_tables/lists/${hold.id}/cards.json`),
      };
    }
  }
  return out;
}

function serializeRec(rec) {
  const fn = TYPE_SERIALIZERS[rec.type];
  if (COLUMN_TYPES.has(rec.type)) return serializeColumn(rec);
  return fn ? fn(rec) : { ...recEnvelope(rec), content: rec.content ?? undefined };
}

function serializeMessageType(mt) {
  if (!mt) return undefined;
  return { id: mt.id, name: mt.name, icon: mt.icon, created_at: mt.created_at, updated_at: mt.updated_at };
}

function attachmentDownloadUrl(att, filename) {
  return apiUrl(`/attachments/${att.sgid}/download/${encodeURIComponent(filename || att.name)}`);
}

function serializeBoost(boost) {
  const rec = db.recs.get(boost.recordingId);
  return compact({
    id: boost.id,
    content: boost.content,
    created_at: boost.created_at,
    booster: serializePerson(db.people.get(boost.boosterId)),
    recording: rec ? { id: rec.id, title: recTitle(rec), type: rec.type, url: recUrl(rec), app_url: recAppUrl(rec) } : undefined,
  });
}

function serializeEvent(event) {
  return compact({
    id: event.id,
    recording_id: event.recordingId,
    action: event.action,
    details: event.details,
    created_at: event.created_at,
    creator: serializePerson(db.people.get(event.creatorId)),
    boosts_count: (db.eventBoosts.get(event.id) || []).length,
    boosts_url: apiUrl(`/recordings/${event.recordingId}/events/${event.id}/boosts.json`),
  });
}

function serializeTimelineEvent(event) {
  const rec = db.recs.get(event.recordingId);
  if (!rec) return null;
  return compact({
    id: event.id,
    created_at: event.created_at,
    kind: `${snakeType(rec.type)}_${event.action}`,
    parent_recording_id: rec.parentId ?? undefined,
    url: recUrl(rec),
    app_url: recAppUrl(rec),
    creator: serializePerson(db.people.get(event.creatorId)),
    action: event.action,
    target: recTitle(rec),
    title: recTitle(rec),
    summary_excerpt: excerpt(rec.content || recTitle(rec), 140),
    bucket: bucketRef(rec),
  });
}

function serializeSubscription(rec, person) {
  const set = subscriberSet(rec.id);
  return {
    subscribed: set.has(person.id),
    count: set.size,
    url: apiUrl(`/recordings/${rec.id}/subscription.json`),
    subscribers: peopleByIds([...set]),
  };
}

function serializeNotification(reading) {
  const rec = db.recs.get(reading.recordingId);
  const event = db.eventsById.get(reading.eventId);
  const creator = event ? db.people.get(event.creatorId) : null;
  return compact({
    id: reading.id,
    created_at: reading.created_at,
    updated_at: reading.updated_at,
    section: 'new_for_you',
    unread_count: reading.read_at ? 0 : 1,
    unread_at: reading.unread_at ?? undefined,
    read_at: reading.read_at ?? undefined,
    readable_sgid: makeSgid('reading', reading.id),
    readable_identifier: `reading:${reading.id}`,
    title: rec ? recTitle(rec) : 'Removed content',
    type: rec ? rec.type : 'Recording',
    bucket_name: rec ? bucketRef(rec).name : undefined,
    creator: serializePerson(creator),
    content_excerpt: reading.excerpt,
    app_url: rec ? recAppUrl(rec) : undefined,
    unread_url: apiUrl('/my/unreads.json'),
    bookmark_url: rec ? apiUrl(`/my/bookmarks/${makeSgid('bookmark', rec.id)}`) : undefined,
    subscription_url: rec && SUBSCRIBABLE.has(rec.type) ? apiUrl(`/recordings/${rec.id}/subscription.json`) : undefined,
    subscribed: rec ? subscriberSet(rec.id).has(reading.personId) : false,
    previewable_attachments: [],
    participants: [],
    named: true,
  });
}

function serializeDockItem(tool) {
  return {
    id: tool.id,
    title: tool.dockTitle,
    name: TOOL_DEFAULTS[tool.type] ? TOOL_DEFAULTS[tool.type].name : snakeType(tool.type),
    enabled: !!tool.enabled,
    position: positionOfDock(tool),
    url: recUrl(tool),
    app_url: recAppUrl(tool),
  };
}

function positionOfDock(tool) {
  const project = db.projects.get(tool.bucketId);
  if (!project) return 1;
  const enabled = project.dock.filter((id) => (db.recs.get(id) || {}).enabled);
  const i = enabled.indexOf(tool.id);
  return i === -1 ? project.dock.indexOf(tool.id) + 1 : i + 1;
}

function serializeTool(tool) {
  return {
    id: tool.id,
    status: effStatus(tool),
    created_at: tool.created_at,
    updated_at: tool.updated_at,
    title: tool.dockTitle,
    name: TOOL_DEFAULTS[tool.type] ? TOOL_DEFAULTS[tool.type].name : snakeType(tool.type),
    enabled: !!tool.enabled,
    position: positionOfDock(tool),
    url: recUrl(tool),
    app_url: recAppUrl(tool),
    bucket: bucketRef(tool),
  };
}

function serializeProject(project, person) {
  return compact({
    id: project.id,
    status: project.status,
    created_at: project.created_at,
    updated_at: project.updated_at,
    name: project.name,
    description: project.description,
    purpose: project.purpose,
    clients_enabled: project.clients_enabled,
    bookmark_url: apiUrl(`/my/bookmarks/${makeSgid('bookmark-project', project.id)}`),
    url: apiUrl(`/projects/${project.id}`),
    app_url: appUrl(`/projects/${project.id}`),
    dock: dockToolRecs(project).map(serializeDockItem),
    bookmarked: false,
  });
}

function serializeAccount() {
  const a = db.account;
  return {
    id: CONFIG.accountId,
    name: a.name,
    owner_name: a.owner_name,
    active: true,
    created_at: a.created_at,
    updated_at: a.updated_at,
    trial: false,
    frozen: false,
    paused: false,
    limits: { can_create_projects: true, can_pin_projects: true, can_create_users: true, can_upload_files: true },
    subscription: {
      short_name: 'unlimited', proper_name: 'Basecamp Unlimited', project_limit: 0,
      teams: true, clients: true, templates: true, logo: true, timesheet: true,
    },
    settings: { company_hq_enabled: true, teams_enabled: true, projects_enabled: true },
    logo: a.logo ? { url: apiUrl(`/attachments/${a.logo.sgid}/download/${encodeURIComponent(a.logo.name)}`) } : { url: null },
  };
}

function serializeTemplate(template) {
  return compact({
    id: template.id,
    status: template.status,
    created_at: template.created_at,
    updated_at: template.updated_at,
    name: template.name,
    description: template.description ?? '',
    url: apiUrl(`/templates/${template.id}`),
    app_url: appUrl(`/templates/${template.id}`),
    dock: [],
  });
}

function serializeWebhook(webhook) {
  return {
    id: webhook.id,
    active: webhook.active,
    created_at: webhook.created_at,
    updated_at: webhook.updated_at,
    payload_url: webhook.payload_url,
    types: webhook.types,
    url: apiUrl(`/webhooks/${webhook.id}`),
    app_url: appUrl(`/buckets/${webhook.bucketId}/webhooks/${webhook.id}`),
    recent_deliveries: webhook.deliveries,
  };
}

function serializeChatbot(bot) {
  return compact({
    id: bot.id,
    created_at: bot.created_at,
    updated_at: bot.updated_at,
    service_name: bot.service_name,
    command_url: bot.command_url ?? undefined,
    url: apiUrl(`/chats/${bot.campfireId}/integrations/${bot.id}`),
    app_url: appUrl(`/buckets/${bot.bucketId}/chats/${bot.campfireId}/integrations/${bot.id}`),
    lines_url: `${CONFIG.baseUrl}/integrations/${bot.key}/buckets/${bot.bucketId}/chats/${bot.campfireId}/lines.json`,
  });
}

function serializePreferences(person) {
  const prefs = db.preferences.get(person.id) || {};
  return {
    url: apiUrl('/my/preferences.json'),
    app_url: appUrl('/my/preferences'),
    time_zone_name: prefs.time_zone_name || person.time_zone,
    first_week_day: prefs.first_week_day || 'Monday',
    time_format: prefs.time_format || '12h',
  };
}

function serializeOutOfOffice(person) {
  const ooo = db.outOfOffice.get(person.id);
  const today = dateOnly(nowIso());
  return {
    person: { id: person.id, name: person.name },
    enabled: !!ooo,
    ongoing: !!ooo && ooo.start_date <= today && today <= ooo.end_date,
    start_date: ooo ? ooo.start_date : null,
    end_date: ooo ? ooo.end_date : null,
  };
}

function serializeSearchResult(rec) {
  return compact({
    ...recEnvelope(rec),
    content: rec.content ?? undefined,
    description: rec.description ?? undefined,
    subject: rec.subject ?? undefined,
  });
}

function assignmentParentRef(rec) {
  const parent = rec.parentId === null ? null : db.recs.get(rec.parentId);
  if (!parent) return undefined;
  return { id: parent.id, title: recTitle(parent), app_url: recAppUrl(parent) };
}

function serializeMyAssignment(rec) {
  const project = db.projects.get(rec.bucketId);
  return compact({
    id: rec.id,
    app_url: recAppUrl(rec),
    content: recTitle(rec),
    starts_on: rec.starts_on ?? null,
    due_on: rec.due_on ?? null,
    bucket: project ? { id: project.id, name: project.name, app_url: appUrl(`/projects/${project.id}`) } : undefined,
    completed: rec.completed || false,
    type: rec.type,
    assignees: (rec.assigneeIds || []).map((id) => {
      const p = db.people.get(id);
      return p ? { id: p.id, name: p.name, avatar_url: apiUrl(`/avatars/${p.id}.svg`) } : null;
    }).filter(Boolean),
    comments_count: COMMENTABLE.has(rec.type) ? commentsCount(rec) : 0,
    has_description: !!(rec.description && rec.description.length > 0),
    parent: assignmentParentRef(rec),
    children: [],
  });
}

function serializeAssignable(rec) {
  return compact({
    id: rec.id,
    title: recTitle(rec),
    type: rec.type,
    url: recUrl(rec),
    app_url: recAppUrl(rec),
    bucket: bucketRef(rec),
    parent: parentRef(rec),
    due_on: rec.due_on ?? null,
    starts_on: rec.starts_on ?? null,
    assignees: peopleByIds(rec.assigneeIds),
  });
}

/* ------------------------------------------------------------------------ *
 * Router — exact OpenAPI path templates. A trailing `.json` is tolerated on
 * bare-{param} tails so hand-typed production-style URLs also work.
 * ------------------------------------------------------------------------ */

const ROUTES = [];

function route(method, template, opId, handler) {
  ROUTES.push({ method, opId, handler, segs: template.split('/').filter(Boolean) });
}

const PARAM_SEGMENT_RE = /^\{(\w+)\}(\.json)?$/;

function matchSegment(templateSeg, actualSeg, params) {
  const paramMatch = PARAM_SEGMENT_RE.exec(templateSeg);
  if (paramMatch) {
    const name = paramMatch[1];
    let value = actualSeg;
    // Templates like {personId}.json require the suffix; bare {id} tails
    // tolerate an optional .json for hand-typed production-style URLs.
    if (value.endsWith('.json')) value = value.slice(0, -5);
    else if (paramMatch[2]) return false;
    if (name === 'date') {
      if (!isDateString(value)) return false;
      params[name] = value;
      return true;
    }
    const n = parseIntStrict(value);
    if (n === null) return false;
    params[name] = n;
    return true;
  }
  return templateSeg === actualSeg;
}

function findRoute(method, pathSegs) {
  const allowed = new Set();
  let hit = null;
  for (const r of ROUTES) {
    if (r.segs.length !== pathSegs.length) continue;
    const params = {};
    let ok = true;
    for (let i = 0; i < r.segs.length; i += 1) {
      if (!matchSegment(r.segs[i], pathSegs[i], params)) { ok = false; break; }
    }
    if (!ok) continue;
    allowed.add(r.method);
    if (r.method === method && !hit) hit = { route: r, params };
  }
  return { hit, allowed: [...allowed] };
}

/* ----------------------------- response helpers --------------------------- */

const ok = (body, headers) => ({ status: 200, body, headers });
const created = (body) => ({ status: 201, body });
const noContent = () => ({ status: 204 });

function pageParam(ctx) {
  const raw = ctx.query.get('page');
  if (raw === null || raw === '') return 1;
  const n = parseIntStrict(raw);
  if (n === null || n < 1) throw err.badRequest('page must be a positive integer.');
  return n;
}

function listOk(ctx, items, serialize) {
  const total = items.length;
  const page = pageParam(ctx);
  const per = CONFIG.pageSize;
  const slice = items.slice((page - 1) * per, page * per);
  const headers = { 'X-Total-Count': String(total) };
  if (page * per < total) {
    const nextQuery = new URLSearchParams(ctx.query);
    nextQuery.set('page', String(page + 1));
    headers.Link = `<${CONFIG.baseUrl}${ctx.path}?${nextQuery.toString()}>; rel="next"`;
  }
  return ok(slice.map(serialize), headers);
}

function sortItems(ctx, items, { sort = 'created_at', direction = 'asc' } = {}) {
  const field = vEnumQuery(ctx, 'sort', ['created_at', 'updated_at'], sort);
  const dir = vEnumQuery(ctx, 'direction', ['asc', 'desc'], direction);
  const mul = dir === 'asc' ? 1 : -1;
  return [...items].sort((a, b) => {
    if (a[field] < b[field]) return -1 * mul;
    if (a[field] > b[field]) return 1 * mul;
    return (a.id - b.id) * mul;
  });
}

function filterByStatusQuery(ctx, items) {
  const status = vEnumQuery(ctx, 'status', ['active', 'archived', 'trashed'], 'active');
  return items.filter((rec) => effStatus(rec) === status && rec.status !== 'drafted');
}

/* ------------------------------ domain lookups ---------------------------- */

function getProjectOr404(ctx, projectId, { allowInactive = true } = {}) {
  const project = db.projects.get(projectId);
  if (!project || !canSeeProject(ctx.person, project)) throw err.notFound('Project not found.');
  if (!allowInactive && project.status !== 'active') throw err.notFound('Project not found.');
  return project;
}

function getRecOr404(ctx, recId, types) {
  const rec = db.recs.get(recId);
  if (!rec || !canSeeRec(ctx.person, rec)) throw err.notFound('Recording not found.');
  if (types) {
    const set = Array.isArray(types) ? new Set(types) : new Set([types]);
    if (!set.has(rec.type)) throw err.notFound('Recording not found.');
  }
  return rec;
}

function projectOfRec(rec) {
  return db.projects.get(rec.bucketId);
}

function requireActive(rec, label = 'recording') {
  const status = effStatus(rec);
  if (status !== 'active') throw err.unprocessable(`This ${label} is ${status} and cannot be modified.`);
}

function requireEmployee(ctx, action = 'perform this action') {
  if (!ctx.person.employee) throw err.forbidden(`Only employees can ${action}.`);
}

function requireAdmin(ctx, action = 'perform this action') {
  if (!(ctx.person.admin || ctx.person.owner)) throw err.forbidden(`Only administrators can ${action}.`);
}

function validAssignees(ids, project) {
  if (ids === undefined) return undefined;
  for (const id of ids) {
    const person = db.people.get(id);
    if (!person) throw err.unprocessable(`Unknown person id ${id}.`);
    if (!canSeeProject(person, project)) throw err.unprocessable(`Person ${id} does not have access to this project.`);
  }
  return ids;
}

/* ------------------------------------------------------------------------ *
 * Handlers: Account
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/account.json', 'GetAccount', async () => ok(serializeAccount()));

route('PUT', '/{accountId}/account/name.json', 'UpdateAccountName', async (ctx) => {
  requireAdmin(ctx, 'rename the account');
  const body = await readJsonBody(ctx);
  if (typeof body.name !== 'string' || body.name.trim().length === 0) {
    throw err.badRequest('name is required.'); // this operation declares 400, not 422
  }
  db.account.name = body.name.trim();
  db.account.updated_at = nowIso();
  return ok(serializeAccount());
});

route('PUT', '/{accountId}/account/logo.json', 'UpdateAccountLogo', async (ctx) => {
  requireAdmin(ctx, 'change the account logo');
  const raw = await readBody(ctx.req, CONFIG.maxUploadBody);
  const contentType = ctx.req.headers['content-type'] || '';
  let name = 'logo';
  let bytes = raw;
  let type = contentType.split(';')[0].trim() || 'application/octet-stream';
  if (type === 'multipart/form-data') {
    const parts = parseMultipart(raw, contentType) || [];
    const filePart = parts.find((p) => p.filename) || parts[0];
    if (!filePart || filePart.data.length === 0) throw err.unprocessable('logo file is required.');
    bytes = filePart.data;
    name = filePart.filename || 'logo';
    type = filePart.contentType;
  }
  if (bytes.length === 0) throw err.unprocessable('logo file is required.');
  const att = storeAttachment(name, type, bytes);
  db.account.logo = { sgid: att.sgid, name: att.name };
  db.account.updated_at = nowIso();
  return noContent();
});

route('DELETE', '/{accountId}/account/logo.json', 'RemoveAccountLogo', async (ctx) => {
  requireAdmin(ctx, 'remove the account logo');
  db.account.logo = null;
  db.account.updated_at = nowIso();
  return noContent();
});

/* ------------------------------------------------------------------------ *
 * Handlers: People, profile, preferences, out-of-office
 * ------------------------------------------------------------------------ */

function activePeople() {
  return [...db.people.values()].filter((p) => p.active).sort((a, b) => a.name.localeCompare(b.name));
}

route('GET', '/{accountId}/people.json', 'ListPeople', async (ctx) => listOk(ctx, activePeople(), serializePerson));

route('GET', '/{accountId}/people/{personId}', 'GetPerson', async (ctx) => {
  const person = db.people.get(ctx.params.personId);
  if (!person || !person.active) throw err.notFound('Person not found.');
  return ok(serializePerson(person));
});

route('GET', '/{accountId}/circles/people.json', 'ListPingablePeople', async (ctx) => {
  const people = activePeople().filter((p) => p.id !== ctx.person.id && !p.client);
  return listOk(ctx, people, serializePerson);
});

route('GET', '/{accountId}/projects/{projectId}/people.json', 'ListProjectPeople', async (ctx) => {
  const project = getProjectOr404(ctx, ctx.params.projectId);
  const members = [...project.access].map((id) => db.people.get(id)).filter((p) => p && p.active)
    .sort((a, b) => a.name.localeCompare(b.name));
  return listOk(ctx, members, serializePerson);
});

route('PUT', '/{accountId}/projects/{projectId}/people/users.json', 'UpdateProjectAccess', async (ctx) => {
  const project = getProjectOr404(ctx, ctx.params.projectId);
  requireEmployee(ctx, 'manage project access');
  requireProjectMutable(ctx.person, project);
  const body = await readJsonBody(ctx);
  const grant = vOptIdArray(body, 'grant') || [];
  const revoke = vOptIdArray(body, 'revoke') || [];
  const create = body.create === undefined ? [] : body.create;
  if (!Array.isArray(create)) throw err.unprocessable('create must be an array of person payloads.');

  const granted = [];
  const revoked = [];
  for (const id of grant) {
    const person = db.people.get(id);
    if (!person) throw err.unprocessable(`Unknown person id ${id}.`);
    if (!project.access.has(id)) { project.access.add(id); granted.push(person); }
  }
  for (const payload of create) {
    if (payload === null || typeof payload !== 'object') throw err.unprocessable('create entries must be objects.');
    const name = vRequireString(payload, 'name');
    const email = vRequireString(payload, 'email_address');
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw err.unprocessable('email_address is not a valid email.');
    const existing = [...db.people.values()].find((p) => p.email_address.toLowerCase() === email.toLowerCase());
    const person = existing || createPerson({
      name, email_address: email,
      title: vOptString(payload, 'title'),
      company_name: vOptString(payload, 'company_name'),
      employee: false, client: false,
    });
    if (!project.access.has(person.id)) { project.access.add(person.id); granted.push(person); }
  }
  for (const id of revoke) {
    const person = db.people.get(id);
    if (person && project.access.has(id)) { project.access.delete(id); revoked.push(person); }
  }
  project.updated_at = nowIso();
  return ok({ granted: granted.map(serializePerson), revoked: revoked.map(serializePerson) });
});

route('GET', '/{accountId}/my/profile.json', 'GetMyProfile', async (ctx) => ok(serializePerson(ctx.person)));

route('PUT', '/{accountId}/my/profile.json', 'UpdateMyProfile', async (ctx) => {
  const body = await readJsonBody(ctx);
  const p = ctx.person;
  const name = vOptString(body, 'name', { max: 200 });
  const email = vOptString(body, 'email_address', { max: 200 });
  if (name !== undefined) {
    if (name.trim().length === 0) throw err.unprocessable('name cannot be blank.');
    p.name = name.trim();
  }
  if (email !== undefined) {
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw err.unprocessable('email_address is not a valid email.');
    p.email_address = email;
  }
  for (const field of ['title', 'bio', 'location']) {
    const v = vOptString(body, field, { max: 2000 });
    if (v !== undefined) p[field] = v;
  }
  const tz = vOptString(body, 'time_zone_name', { max: 100 });
  if (tz !== undefined) p.time_zone = tz;
  const prefs = db.preferences.get(p.id) || {};
  const fwd = vOptString(body, 'first_week_day', { max: 20 });
  if (fwd !== undefined) {
    const days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    if (!days.includes(fwd)) throw err.unprocessable(`first_week_day must be one of ${days.join(', ')}.`);
    prefs.first_week_day = fwd;
  }
  const tf = vOptString(body, 'time_format', { max: 10 });
  if (tf !== undefined) prefs.time_format = tf;
  db.preferences.set(p.id, prefs);
  p.updated_at = nowIso();
  return noContent();
});

route('GET', '/{accountId}/my/preferences.json', 'GetMyPreferences', async (ctx) => ok(serializePreferences(ctx.person)));

route('PUT', '/{accountId}/my/preferences.json', 'UpdateMyPreferences', async (ctx) => {
  const body = await readJsonBody(ctx);
  if (body.person === null || typeof body.person !== 'object') {
    throw err.unprocessable('person payload is required.');
  }
  const payload = body.person;
  const prefs = db.preferences.get(ctx.person.id) || {};
  const tz = vOptString(payload, 'time_zone_name', { max: 100 });
  if (tz !== undefined) prefs.time_zone_name = tz;
  const fwd = vOptString(payload, 'first_week_day', { max: 20 });
  if (fwd !== undefined) prefs.first_week_day = fwd;
  const tf = vOptString(payload, 'time_format', { max: 10 });
  if (tf !== undefined) prefs.time_format = tf;
  db.preferences.set(ctx.person.id, prefs);
  return ok(serializePreferences(ctx.person));
});

route('GET', '/{accountId}/people/{personId}/out_of_office.json', 'GetOutOfOffice', async (ctx) => {
  const person = db.people.get(ctx.params.personId);
  if (!person || !person.active) throw err.notFound('Person not found.');
  return ok(serializeOutOfOffice(person));
});

route('POST', '/{accountId}/people/{personId}/out_of_office.json', 'EnableOutOfOffice', async (ctx) => {
  const person = db.people.get(ctx.params.personId);
  if (!person || !person.active) throw err.notFound('Person not found.');
  if (person.id !== ctx.person.id && !(ctx.person.admin || ctx.person.owner)) {
    throw err.forbidden('You can only set out-of-office for yourself.');
  }
  const body = await readJsonBody(ctx);
  const payload = body.out_of_office;
  if (payload === null || typeof payload !== 'object') throw err.unprocessable('out_of_office payload is required.');
  const start = payload.start_date;
  const end = payload.end_date;
  if (!isDateString(start)) throw err.unprocessable('out_of_office.start_date must be a YYYY-MM-DD date.');
  if (!isDateString(end)) throw err.unprocessable('out_of_office.end_date must be a YYYY-MM-DD date.');
  if (end < start) throw err.unprocessable('end_date must be on or after start_date.');
  db.outOfOffice.set(person.id, { start_date: start, end_date: end });
  return ok(serializeOutOfOffice(person));
});

route('DELETE', '/{accountId}/people/{personId}/out_of_office.json', 'DisableOutOfOffice', async (ctx) => {
  const person = db.people.get(ctx.params.personId);
  if (!person || !person.active) throw err.notFound('Person not found.');
  if (person.id !== ctx.person.id && !(ctx.person.admin || ctx.person.owner)) {
    throw err.forbidden('You can only clear out-of-office for yourself.');
  }
  db.outOfOffice.delete(person.id);
  return noContent();
});

/* ------------------------------------------------------------------------ *
 * Handlers: Projects
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/projects.json', 'ListProjects', async (ctx) => {
  const status = vEnumQuery(ctx, 'status', ['active', 'archived', 'trashed'], 'active');
  const projects = visibleProjects(ctx.person, { statuses: [status] })
    .sort((a, b) => a.created_at < b.created_at ? -1 : 1);
  return listOk(ctx, projects, (p) => serializeProject(p, ctx.person));
});

route('POST', '/{accountId}/projects.json', 'CreateProject', async (ctx) => {
  requireEmployee(ctx, 'create projects');
  const body = await readJsonBody(ctx);
  const name = vRequireString(body, 'name', { max: 255 });
  const project = createProject({
    name: name.trim(),
    description: vOptString(body, 'description', { max: 10_000 }) || null,
    creatorId: ctx.person.id,
    access: [ctx.person.id],
  });
  log('info', 'project created', { project: project.id, by: ctx.person.id });
  return created(serializeProject(project, ctx.person));
});

route('GET', '/{accountId}/projects/{projectId}', 'GetProject', async (ctx) => {
  const project = getProjectOr404(ctx, ctx.params.projectId);
  return ok(serializeProject(project, ctx.person));
});

route('PUT', '/{accountId}/projects/{projectId}', 'UpdateProject', async (ctx) => {
  const project = getProjectOr404(ctx, ctx.params.projectId);
  requireEmployee(ctx, 'edit projects');
  requireProjectMutable(ctx.person, project);
  const body = await readJsonBody(ctx);
  const name = vRequireString(body, 'name', { max: 255 });
  project.name = name.trim();
  const description = vOptString(body, 'description', { max: 10_000 });
  if (description !== undefined) project.description = description;
  const admissions = vOptString(body, 'admissions', { max: 30 });
  if (admissions !== undefined) {
    const allowed = ['invite', 'employee', 'team'];
    if (!allowed.includes(admissions)) throw err.unprocessable(`admissions must be one of ${allowed.join(', ')}.`);
    project.admissions = admissions;
    project.all_access = admissions === 'employee';
  }
  if (body.schedule_attributes !== undefined) {
    const sa = body.schedule_attributes;
    if (sa === null || typeof sa !== 'object') throw err.unprocessable('schedule_attributes must be an object.');
    const start = vOptDate(sa, 'start_date');
    const end = vOptDate(sa, 'end_date');
    if (start !== undefined) project.starts_on = start;
    if (end !== undefined) project.ends_on = end;
    if (project.starts_on && project.ends_on && project.ends_on < project.starts_on) {
      throw err.unprocessable('end_date must be on or after start_date.');
    }
  }
  project.updated_at = nowIso();
  return ok(serializeProject(project, ctx.person));
});

route('DELETE', '/{accountId}/projects/{projectId}', 'TrashProject', async (ctx) => {
  const project = getProjectOr404(ctx, ctx.params.projectId);
  requireEmployee(ctx, 'trash projects');
  project.status = 'trashed';
  project.updated_at = nowIso();
  log('info', 'project trashed', { project: project.id, by: ctx.person.id });
  return noContent();
});

/* ------------------------------------------------------------------------ *
 * Handlers: Dock tools
 * ------------------------------------------------------------------------ */

function getToolOr404(ctx, toolId) {
  const rec = db.recs.get(toolId);
  if (!rec || !TOOL_TYPES.has(rec.type)) throw err.notFound('Tool not found.');
  getProjectOr404(ctx, rec.bucketId);
  return rec;
}

route('GET', '/{accountId}/dock/tools/{toolId}', 'GetTool', async (ctx) => ok(serializeTool(getToolOr404(ctx, ctx.params.toolId))));

route('PUT', '/{accountId}/dock/tools/{toolId}', 'UpdateTool', async (ctx) => {
  const tool = getToolOr404(ctx, ctx.params.toolId);
  requireEmployee(ctx, 'rename tools');
  const body = await readJsonBody(ctx);
  tool.dockTitle = vRequireString(body, 'title', { max: 100 }).trim();
  touch(tool);
  return ok(serializeTool(tool));
});

route('DELETE', '/{accountId}/dock/tools/{toolId}', 'DeleteTool', async (ctx) => {
  const tool = getToolOr404(ctx, ctx.params.toolId);
  requireEmployee(ctx, 'remove tools');
  const project = projectOfRec(tool);
  project.dock = project.dock.filter((id) => id !== tool.id);
  tool.status = 'trashed';
  touch(tool);
  return noContent();
});

route('POST', '/{accountId}/dock/tools.json', 'CloneTool', async (ctx) => {
  const body = await readJsonBody(ctx);
  const sourceId = parseIntStrict(body.source_recording_id);
  if (sourceId === null) throw err.unprocessable('source_recording_id is required and must be an integer.');
  const source = db.recs.get(sourceId);
  if (!source || !TOOL_TYPES.has(source.type)) throw err.notFound('Source tool not found.');
  const project = getProjectOr404(ctx, source.bucketId);
  requireEmployee(ctx, 'add tools');
  requireProjectMutable(ctx.person, project);
  const title = vOptString(body, 'title', { max: 100 });
  const clone = createRec({
    type: source.type, bucketId: project.id, parentId: null, creatorId: ctx.person.id,
    fields: { dockTitle: (title && title.trim()) || `${source.dockTitle} (copy)`, enabled: true },
  });
  project.dock.push(clone.id);
  if (clone.type === 'Kanban::Board') {
    for (const [laneType, laneTitle] of [['Kanban::Triage', 'Triage'], ['Kanban::NotNowColumn', 'Not now'], ['Kanban::DoneColumn', 'Done']]) {
      createRec({ type: laneType, bucketId: project.id, parentId: clone.id, creatorId: ctx.person.id, fields: { title: laneTitle } });
    }
  }
  project.updated_at = nowIso();
  return created(serializeTool(clone));
});

route('POST', '/{accountId}/recordings/{toolId}/position.json', 'EnableTool', async (ctx) => {
  const tool = getToolOr404(ctx, ctx.params.toolId);
  requireEmployee(ctx, 'enable tools');
  requireProjectMutable(ctx.person, projectOfRec(tool));
  tool.enabled = true;
  touch(tool);
  return { status: 201, body: serializeTool(tool) };
});

route('DELETE', '/{accountId}/recordings/{toolId}/position.json', 'DisableTool', async (ctx) => {
  const tool = getToolOr404(ctx, ctx.params.toolId);
  requireEmployee(ctx, 'disable tools');
  tool.enabled = false;
  touch(tool);
  return noContent();
});

route('PUT', '/{accountId}/recordings/{toolId}/position.json', 'RepositionTool', async (ctx) => {
  const tool = getToolOr404(ctx, ctx.params.toolId);
  requireEmployee(ctx, 'reorder tools');
  const body = await readJsonBody(ctx);
  const position = vOptInt(body, 'position', { min: 1 });
  if (position === undefined) throw err.unprocessable('position is required.');
  const project = projectOfRec(tool);
  project.dock = project.dock.filter((id) => id !== tool.id);
  project.dock.splice(Math.min(position - 1, project.dock.length), 0, tool.id);
  project.updated_at = nowIso();
  touch(tool);
  return ok({});
});

/* ------------------------------------------------------------------------ *
 * Handlers: Message types (categories)
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/categories.json', 'ListMessageTypes', async (ctx) => {
  const types = [...db.messageTypes.values()].sort((a, b) => a.name.localeCompare(b.name));
  return listOk(ctx, types, serializeMessageType);
});

route('POST', '/{accountId}/categories.json', 'CreateMessageType', async (ctx) => {
  requireAdmin(ctx, 'manage message categories');
  const body = await readJsonBody(ctx);
  const at = nowIso();
  const mt = {
    id: nextId(),
    name: vRequireString(body, 'name', { max: 60 }).trim(),
    icon: vRequireString(body, 'icon', { max: 16 }),
    created_at: at,
    updated_at: at,
  };
  db.messageTypes.set(mt.id, mt);
  return created(serializeMessageType(mt));
});

route('GET', '/{accountId}/categories/{typeId}', 'GetMessageType', async (ctx) => {
  const mt = db.messageTypes.get(ctx.params.typeId);
  if (!mt) throw err.notFound('Message type not found.');
  return ok(serializeMessageType(mt));
});

route('PUT', '/{accountId}/categories/{typeId}', 'UpdateMessageType', async (ctx) => {
  requireAdmin(ctx, 'manage message categories');
  const mt = db.messageTypes.get(ctx.params.typeId);
  if (!mt) throw err.notFound('Message type not found.');
  const body = await readJsonBody(ctx);
  const name = vOptString(body, 'name', { max: 60 });
  const icon = vOptString(body, 'icon', { max: 16 });
  if (name !== undefined) {
    if (name.trim().length === 0) throw err.unprocessable('name cannot be blank.');
    mt.name = name.trim();
  }
  if (icon !== undefined) mt.icon = icon;
  mt.updated_at = nowIso();
  return ok(serializeMessageType(mt));
});

route('DELETE', '/{accountId}/categories/{typeId}', 'DeleteMessageType', async (ctx) => {
  requireAdmin(ctx, 'manage message categories');
  if (!db.messageTypes.delete(ctx.params.typeId)) throw err.notFound('Message type not found.');
  return noContent();
});

/* ------------------------------------------------------------------------ *
 * Handlers: Lineup markers
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/lineup/markers.json', 'ListLineupMarkers', async (ctx) => {
  const markers = [...db.lineupMarkers.values()].sort((a, b) => a.date < b.date ? -1 : 1);
  return listOk(ctx, markers, (m) => ({ ...m }));
});

route('POST', '/{accountId}/lineup/markers.json', 'CreateLineupMarker', async (ctx) => {
  requireEmployee(ctx, 'manage lineup markers');
  const body = await readJsonBody(ctx);
  const name = vRequireString(body, 'name', { max: 100 }).trim();
  const date = body.date;
  if (!isDateString(date)) throw err.unprocessable('date must be a YYYY-MM-DD date.');
  const at = nowIso();
  const marker = { id: nextId(), name, date, created_at: at, updated_at: at };
  db.lineupMarkers.set(marker.id, marker);
  return { status: 201 }; // spec: 201 with no body
});

route('PUT', '/{accountId}/lineup/markers/{markerId}', 'UpdateLineupMarker', async (ctx) => {
  requireEmployee(ctx, 'manage lineup markers');
  const marker = db.lineupMarkers.get(ctx.params.markerId);
  if (!marker) throw err.notFound('Lineup marker not found.');
  const body = await readJsonBody(ctx);
  const name = vOptString(body, 'name', { max: 100 });
  if (name !== undefined) {
    if (name.trim().length === 0) throw err.unprocessable('name cannot be blank.');
    marker.name = name.trim();
  }
  if (body.date !== undefined) {
    if (!isDateString(body.date)) throw err.unprocessable('date must be a YYYY-MM-DD date.');
    marker.date = body.date;
  }
  marker.updated_at = nowIso();
  return ok({});
});

route('DELETE', '/{accountId}/lineup/markers/{markerId}', 'DeleteLineupMarker', async (ctx) => {
  requireEmployee(ctx, 'manage lineup markers');
  if (!db.lineupMarkers.delete(ctx.params.markerId)) throw err.notFound('Lineup marker not found.');
  return noContent();
});

/* ------------------------------------------------------------------------ *
 * Handlers: Templates & project construction
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/templates.json', 'ListTemplates', async (ctx) => {
  const status = vEnumQuery(ctx, 'status', ['active', 'archived', 'trashed'], 'active');
  const templates = [...db.templates.values()].filter((t) => t.status === status);
  return listOk(ctx, templates, serializeTemplate);
});

route('POST', '/{accountId}/templates.json', 'CreateTemplate', async (ctx) => {
  requireEmployee(ctx, 'create templates');
  const body = await readJsonBody(ctx);
  const at = nowIso();
  const template = {
    id: nextId(),
    status: 'active',
    name: vRequireString(body, 'name', { max: 255 }).trim(),
    description: vOptString(body, 'description', { max: 10_000 }) || null,
    created_at: at,
    updated_at: at,
  };
  db.templates.set(template.id, template);
  return created(serializeTemplate(template));
});

route('GET', '/{accountId}/templates/{templateId}', 'GetTemplate', async (ctx) => {
  const template = db.templates.get(ctx.params.templateId);
  if (!template || template.status === 'trashed') throw err.notFound('Template not found.');
  return ok(serializeTemplate(template));
});

route('PUT', '/{accountId}/templates/{templateId}', 'UpdateTemplate', async (ctx) => {
  requireEmployee(ctx, 'edit templates');
  const template = db.templates.get(ctx.params.templateId);
  if (!template || template.status === 'trashed') throw err.notFound('Template not found.');
  const body = await readJsonBody(ctx);
  const name = vOptString(body, 'name', { max: 255 });
  if (name !== undefined) {
    if (name.trim().length === 0) throw err.unprocessable('name cannot be blank.');
    template.name = name.trim();
  }
  const description = vOptString(body, 'description', { max: 10_000 });
  if (description !== undefined) template.description = description;
  template.updated_at = nowIso();
  return ok(serializeTemplate(template));
});

route('DELETE', '/{accountId}/templates/{templateId}', 'DeleteTemplate', async (ctx) => {
  requireEmployee(ctx, 'delete templates');
  const template = db.templates.get(ctx.params.templateId);
  if (!template || template.status === 'trashed') throw err.notFound('Template not found.');
  template.status = 'trashed';
  template.updated_at = nowIso();
  return noContent();
});

route('POST', '/{accountId}/templates/{templateId}/project_constructions.json', 'CreateProjectFromTemplate', async (ctx) => {
  requireEmployee(ctx, 'create projects');
  const template = db.templates.get(ctx.params.templateId);
  if (!template || template.status === 'trashed') throw err.notFound('Template not found.');
  const body = await readJsonBody(ctx);
  const name = vRequireString(body, 'name', { max: 255 });
  const project = createProject({
    name: name.trim(),
    description: vOptString(body, 'description', { max: 10_000 }) || template.description,
    creatorId: ctx.person.id,
    access: [ctx.person.id],
  });
  // Construction completes synchronously: there is no background worker tier.
  const construction = { id: nextId(), status: 'completed', templateId: template.id, projectId: project.id };
  db.constructions.set(construction.id, construction);
  return created(serializeConstruction(construction, ctx.person));
});

function serializeConstruction(construction, person) {
  const project = db.projects.get(construction.projectId);
  return compact({
    id: construction.id,
    status: construction.status,
    url: apiUrl(`/templates/${construction.templateId}/project_constructions/${construction.id}`),
    project: project ? serializeProject(project, person) : undefined,
  });
}

route('GET', '/{accountId}/templates/{templateId}/project_constructions/{constructionId}', 'GetProjectConstruction', async (ctx) => {
  const construction = db.constructions.get(ctx.params.constructionId);
  if (!construction || construction.templateId !== ctx.params.templateId) throw err.notFound('Project construction not found.');
  return ok(serializeConstruction(construction, ctx.person));
});

/* ------------------------------------------------------------------------ *
 * Handlers: Message board & messages
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/message_boards/{boardId}', 'GetMessageBoard', async (ctx) => {
  const board = getRecOr404(ctx, ctx.params.boardId, 'Message::Board');
  return ok(serializeRec(board));
});

route('GET', '/{accountId}/message_boards/{boardId}/messages.json', 'ListMessages', async (ctx) => {
  const board = getRecOr404(ctx, ctx.params.boardId, 'Message::Board');
  let messages = childrenOf(board.id, 'Message')
    .filter((m) => effStatus(m) === 'active' && m.status !== 'drafted')
    .filter((m) => canSeeRec(ctx.person, m));
  messages = sortItems(ctx, messages, { sort: 'created_at', direction: 'desc' });
  // Pinned messages surface first (INIT §7.3), preserving sort within groups.
  messages = [...messages.filter((m) => m.pinned), ...messages.filter((m) => !m.pinned)];
  return listOk(ctx, messages, serializeRec);
});

route('POST', '/{accountId}/message_boards/{boardId}/messages.json', 'CreateMessage', async (ctx) => {
  const board = getRecOr404(ctx, ctx.params.boardId, 'Message::Board');
  requireActive(board, 'message board');
  requireProjectMutable(ctx.person, projectOfRec(board));
  const body = await readJsonBody(ctx);
  const subject = vRequireString(body, 'subject', { max: 255 });
  const status = body.status === undefined ? 'active' : body.status;
  if (!['active', 'drafted'].includes(status)) throw err.unprocessable("status must be 'active' or 'drafted'.");
  let categoryId;
  if (body.category_id !== undefined && body.category_id !== null) {
    categoryId = parseIntStrict(body.category_id);
    if (categoryId === null || !db.messageTypes.has(categoryId)) throw err.unprocessable('category_id does not match a message type.');
  }
  const message = createRec({
    type: 'Message', bucketId: board.bucketId, parentId: board.id, creatorId: ctx.person.id,
    status,
    fields: {
      subject: subject.trim(),
      content: vOptString(body, 'content') || '',
      categoryId: categoryId ?? null,
      pinned: false,
    },
  });
  subscribe(message, ctx.person.id);
  for (const pid of vOptIdArray(body, 'subscriptions') || []) {
    if (db.people.has(pid)) subscribe(message, pid);
  }
  applyMentions(message, message.content, ctx.person.id);
  recordEvent(message, ctx.person.id, 'created');
  return created(serializeRec(message));
});

route('GET', '/{accountId}/messages/{messageId}', 'GetMessage', async (ctx) => {
  const message = getRecOr404(ctx, ctx.params.messageId, 'Message');
  return ok(serializeRec(message));
});

route('PUT', '/{accountId}/messages/{messageId}', 'UpdateMessage', async (ctx) => {
  const message = getRecOr404(ctx, ctx.params.messageId, 'Message');
  if (!canModifyRec(ctx.person, message)) throw err.forbidden('Only the author can edit this message.');
  if (message.status !== 'drafted') requireActive(message, 'message');
  const body = await readJsonBody(ctx);
  const subject = vOptString(body, 'subject', { max: 255 });
  if (subject !== undefined) {
    if (subject.trim().length === 0) throw err.unprocessable('subject cannot be blank.');
    message.subject = subject.trim();
  }
  const content = vOptString(body, 'content');
  if (content !== undefined) {
    message.content = content;
    applyMentions(message, content, ctx.person.id);
  }
  if (body.category_id !== undefined) {
    if (body.category_id === null) {
      message.categoryId = null;
    } else {
      const categoryId = parseIntStrict(body.category_id);
      if (categoryId === null || !db.messageTypes.has(categoryId)) throw err.unprocessable('category_id does not match a message type.');
      message.categoryId = categoryId;
    }
  }
  if (body.status !== undefined) {
    if (!['active', 'drafted'].includes(body.status)) throw err.unprocessable("status must be 'active' or 'drafted'.");
    const publishing = message.status === 'drafted' && body.status === 'active';
    message.status = body.status;
    if (publishing) {
      touch(message);
      recordEvent(message, ctx.person.id, 'created');
      return ok(serializeRec(message));
    }
  }
  touch(message);
  recordEvent(message, ctx.person.id, 'content_changed');
  return ok(serializeRec(message));
});

route('POST', '/{accountId}/recordings/{messageId}/pin.json', 'PinMessage', async (ctx) => {
  const message = getRecOr404(ctx, ctx.params.messageId, 'Message');
  requireActive(message, 'message');
  message.pinned = true;
  touch(message);
  recordEvent(message, ctx.person.id, 'pinned', { notify: false });
  return noContent();
});

route('DELETE', '/{accountId}/recordings/{messageId}/pin.json', 'UnpinMessage', async (ctx) => {
  const message = getRecOr404(ctx, ctx.params.messageId, 'Message');
  message.pinned = false;
  touch(message);
  recordEvent(message, ctx.person.id, 'unpinned', { notify: false });
  return noContent();
});

/* ------------------------------------------------------------------------ *
 * Handlers: Comments
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/recordings/{recordingId}/comments.json', 'ListComments', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.recordingId);
  const comments = childrenOf(rec.id, 'Comment').filter((c) => effStatus(c) === 'active' && canSeeRec(ctx.person, c));
  return listOk(ctx, comments, serializeRec);
});

route('POST', '/{accountId}/recordings/{recordingId}/comments.json', 'CreateComment', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.recordingId);
  if (!COMMENTABLE.has(rec.type)) throw err.unprocessable(`${rec.type} recordings do not accept comments.`);
  requireActive(rec);
  requireProjectMutable(ctx.person, projectOfRec(rec));
  const body = await readJsonBody(ctx);
  const content = vRequireString(body, 'content');
  const comment = createRec({
    type: 'Comment', bucketId: rec.bucketId, parentId: rec.id, creatorId: ctx.person.id,
    visible_to_clients: rec.visible_to_clients,
    fields: { content },
  });
  subscribe(rec, ctx.person.id); // commenting subscribes you to the thread
  applyMentions(rec, content, ctx.person.id);
  recordEvent(comment, ctx.person.id, 'created', { excerptText: excerpt(content, 160) });
  return created(serializeRec(comment));
});

route('GET', '/{accountId}/comments/{commentId}', 'GetComment', async (ctx) => {
  const comment = getRecOr404(ctx, ctx.params.commentId, 'Comment');
  return ok(serializeRec(comment));
});

route('PUT', '/{accountId}/comments/{commentId}', 'UpdateComment', async (ctx) => {
  const comment = getRecOr404(ctx, ctx.params.commentId, 'Comment');
  if (!canModifyRec(ctx.person, comment)) throw err.forbidden('Only the author can edit this comment.');
  requireActive(comment, 'comment');
  const body = await readJsonBody(ctx);
  comment.content = vRequireString(body, 'content');
  touch(comment);
  recordEvent(comment, ctx.person.id, 'content_changed', { notify: false });
  return ok(serializeRec(comment));
});

/* ------------------------------------------------------------------------ *
 * Handlers: Generic recordings — list, get, lifecycle, client visibility
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/projects/recordings.json', 'ListRecordings', async (ctx) => {
  const typeParam = ctx.query.get('type');
  if (!typeParam) throw err.badRequest('type parameter is required.');
  const type = LISTABLE_RECORDING_TYPES[typeParam];
  if (!type) {
    throw err.badRequest(`Invalid type parameter: expected one of ${Object.keys(LISTABLE_RECORDING_TYPES).join(', ')}.`);
  }
  const status = vEnumQuery(ctx, 'status', ['active', 'archived', 'trashed'], 'active');
  let bucketIds = null;
  const bucketParam = ctx.query.get('bucket');
  if (bucketParam) {
    bucketIds = new Set();
    for (const piece of bucketParam.split(',')) {
      const id = parseIntStrict(piece.trim());
      if (id === null) throw err.badRequest('bucket must be a comma-separated list of project ids.');
      bucketIds.add(id);
    }
  }
  const projects = visibleProjects(ctx.person, { statuses: ['active', 'archived'] })
    .filter((p) => !bucketIds || bucketIds.has(p.id));
  const projectIds = new Set(projects.map((p) => p.id));
  let items = [];
  for (const rec of db.recs.values()) {
    if (rec.type !== type) continue;
    if (!projectIds.has(rec.bucketId)) continue;
    if (effStatus(rec) !== status || rec.status === 'drafted') continue;
    if (!canSeeRec(ctx.person, rec)) continue;
    items.push(rec);
  }
  items = sortItems(ctx, items, { sort: 'created_at', direction: 'desc' });
  return listOk(ctx, items, serializeRec);
});

route('GET', '/{accountId}/recordings/{recordingId}', 'GetRecording', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.recordingId);
  return ok(serializeRec(rec));
});

function lifecycleTransition(ctx, target) {
  const rec = getRecOr404(ctx, ctx.params.recordingId);
  if (TOOL_TYPES.has(rec.type) || COLUMN_TYPES.has(rec.type)) {
    throw err.unprocessable('Tools and card table lanes cannot be archived or trashed directly.');
  }
  if (!canModifyRec(ctx.person, rec)) throw err.forbidden('You do not have permission to change this recording.');
  requireProjectMutable(ctx.person, projectOfRec(rec));
  if (rec.status !== target) {
    rec.status = target;
    touch(rec);
    const action = target === 'active' ? 'unarchived' : target;
    recordEvent(rec, ctx.person.id, action === 'archived' ? 'archived' : action === 'trashed' ? 'trashed' : 'unarchived');
  }
  return noContent();
}

route('PUT', '/{accountId}/recordings/{recordingId}/status/active.json', 'UnarchiveRecording', async (ctx) => lifecycleTransition(ctx, 'active'));
route('PUT', '/{accountId}/recordings/{recordingId}/status/archived.json', 'ArchiveRecording', async (ctx) => lifecycleTransition(ctx, 'archived'));
route('PUT', '/{accountId}/recordings/{recordingId}/status/trashed.json', 'TrashRecording', async (ctx) => lifecycleTransition(ctx, 'trashed'));

route('PUT', '/{accountId}/recordings/{recordingId}/client_visibility.json', 'SetClientVisibility', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.recordingId);
  requireEmployee(ctx, 'change client visibility');
  const body = await readJsonBody(ctx);
  if (typeof body.visible_to_clients !== 'boolean') throw err.unprocessable('visible_to_clients must be a boolean.');
  rec.visible_to_clients = body.visible_to_clients;
  touch(rec);
  recordEvent(rec, ctx.person.id, body.visible_to_clients ? 'made_visible_to_clients' : 'hidden_from_clients', { notify: false });
  return ok(serializeRec(rec));
});

/* ------------------------------------------------------------------------ *
 * Handlers: Events
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/recordings/{recordingId}/events.json', 'ListEvents', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.recordingId);
  const events = [...(db.events.get(rec.id) || [])].reverse(); // newest first
  return listOk(ctx, events, serializeEvent);
});

/* ------------------------------------------------------------------------ *
 * Handlers: Boosts
 * ------------------------------------------------------------------------ */

const MAX_BOOST_LENGTH = 64;

route('GET', '/{accountId}/recordings/{recordingId}/boosts.json', 'ListRecordingBoosts', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.recordingId);
  const boosts = (db.recBoosts.get(rec.id) || []).map((id) => db.boosts.get(id)).filter(Boolean);
  return listOk(ctx, boosts, serializeBoost);
});

route('POST', '/{accountId}/recordings/{recordingId}/boosts.json', 'CreateRecordingBoost', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.recordingId);
  if (!BOOSTABLE.has(rec.type)) throw err.unprocessable(`${rec.type} recordings do not accept boosts.`);
  requireActive(rec);
  const body = await readJsonBody(ctx);
  const content = vRequireString(body, 'content', { max: MAX_BOOST_LENGTH });
  const boost = {
    id: nextId(),
    content,
    created_at: nowIso(),
    boosterId: ctx.person.id,
    recordingId: rec.id,
    eventId: null,
  };
  db.boosts.set(boost.id, boost);
  let list = db.recBoosts.get(rec.id);
  if (!list) { list = []; db.recBoosts.set(rec.id, list); }
  list.push(boost.id);
  return created(serializeBoost(boost));
});

route('GET', '/{accountId}/recordings/{recordingId}/events/{eventId}/boosts.json', 'ListEventBoosts', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.recordingId);
  const event = db.eventsById.get(ctx.params.eventId);
  if (!event || event.recordingId !== rec.id) throw err.notFound('Event not found.');
  const boosts = (db.eventBoosts.get(event.id) || []).map((id) => db.boosts.get(id)).filter(Boolean);
  return listOk(ctx, boosts, serializeBoost);
});

route('POST', '/{accountId}/recordings/{recordingId}/events/{eventId}/boosts.json', 'CreateEventBoost', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.recordingId);
  const event = db.eventsById.get(ctx.params.eventId);
  if (!event || event.recordingId !== rec.id) throw err.notFound('Event not found.');
  const body = await readJsonBody(ctx);
  const content = vRequireString(body, 'content', { max: MAX_BOOST_LENGTH });
  const boost = {
    id: nextId(),
    content,
    created_at: nowIso(),
    boosterId: ctx.person.id,
    recordingId: rec.id,
    eventId: event.id,
  };
  db.boosts.set(boost.id, boost);
  let list = db.eventBoosts.get(event.id);
  if (!list) { list = []; db.eventBoosts.set(event.id, list); }
  list.push(boost.id);
  return created(serializeBoost(boost));
});

route('GET', '/{accountId}/boosts/{boostId}', 'GetBoost', async (ctx) => {
  const boost = db.boosts.get(ctx.params.boostId);
  if (!boost) throw err.notFound('Boost not found.');
  const rec = db.recs.get(boost.recordingId);
  if (rec && !canSeeRec(ctx.person, rec)) throw err.notFound('Boost not found.');
  return ok(serializeBoost(boost));
});

route('DELETE', '/{accountId}/boosts/{boostId}', 'DeleteBoost', async (ctx) => {
  const boost = db.boosts.get(ctx.params.boostId);
  if (!boost) throw err.notFound('Boost not found.');
  if (boost.boosterId !== ctx.person.id && !(ctx.person.admin || ctx.person.owner)) {
    throw err.forbidden('Only the booster can remove a boost.');
  }
  db.boosts.delete(boost.id);
  const pool = boost.eventId ? db.eventBoosts.get(boost.eventId) : db.recBoosts.get(boost.recordingId);
  if (pool) {
    const i = pool.indexOf(boost.id);
    if (i !== -1) pool.splice(i, 1);
  }
  return noContent();
});

/* ------------------------------------------------------------------------ *
 * Handlers: Subscriptions
 * ------------------------------------------------------------------------ */

/**
 * Non-subscribable types fail per the declared codes: reads/deletes only
 * declare 404, writes declare 422.
 */
function subscribableOr404(ctx, { asNotFound = false } = {}) {
  const rec = getRecOr404(ctx, ctx.params.recordingId);
  if (!SUBSCRIBABLE.has(rec.type)) {
    if (asNotFound) throw err.notFound('This recording has no subscription.');
    throw err.unprocessable(`${rec.type} recordings do not have subscriptions.`);
  }
  return rec;
}

route('GET', '/{accountId}/recordings/{recordingId}/subscription.json', 'GetSubscription', async (ctx) => {
  const rec = subscribableOr404(ctx, { asNotFound: true });
  return ok(serializeSubscription(rec, ctx.person));
});

route('POST', '/{accountId}/recordings/{recordingId}/subscription.json', 'Subscribe', async (ctx) => {
  const rec = subscribableOr404(ctx);
  subscribe(rec, ctx.person.id);
  return ok(serializeSubscription(rec, ctx.person));
});

route('DELETE', '/{accountId}/recordings/{recordingId}/subscription.json', 'Unsubscribe', async (ctx) => {
  const rec = subscribableOr404(ctx, { asNotFound: true });
  unsubscribe(rec, ctx.person.id);
  return noContent();
});

route('PUT', '/{accountId}/recordings/{recordingId}/subscription.json', 'UpdateSubscription', async (ctx) => {
  const rec = subscribableOr404(ctx);
  const body = await readJsonBody(ctx);
  const add = vOptIdArray(body, 'subscriptions') || [];
  const remove = vOptIdArray(body, 'unsubscriptions') || [];
  for (const id of add) {
    if (!db.people.has(id)) throw err.unprocessable(`Unknown person id ${id}.`);
    subscribe(rec, id);
  }
  for (const id of remove) unsubscribe(rec, id);
  return ok(serializeSubscription(rec, ctx.person));
});

/* ------------------------------------------------------------------------ *
 * Handlers: Notifications (readings) — INIT §4.5: sidebar rows are Readings.
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/my/readings.json', 'GetMyNotifications', async (ctx) => {
  const rows = readingsOf(ctx.person.id);
  const unreads = rows.filter((r) => !r.read_at);
  const reads = rows.filter((r) => r.read_at);
  const page = pageParam(ctx);
  const per = CONFIG.pageSize;
  const readSlice = reads.slice((page - 1) * per, page * per);
  const headers = { 'X-Total-Count': String(rows.length) };
  if (page * per < reads.length) {
    const nextQuery = new URLSearchParams(ctx.query);
    nextQuery.set('page', String(page + 1));
    headers.Link = `<${CONFIG.baseUrl}${ctx.path}?${nextQuery.toString()}>; rel="next"`;
  }
  return ok({
    unreads: unreads.map(serializeNotification),
    reads: readSlice.map(serializeNotification),
    memories: [],
  }, headers);
});

route('PUT', '/{accountId}/my/unreads.json', 'MarkAsRead', async (ctx) => {
  const body = await readJsonBody(ctx);
  if (!Array.isArray(body.readables)) throw err.unprocessable('readables must be an array of readable sgids.');
  const rows = readingsOf(ctx.person.id);
  const at = nowIso();
  let marked = 0;
  for (const sgid of body.readables) {
    const parsed = parseSgid(sgid);
    if (!parsed || parsed.kind !== 'reading') continue;
    const readingId = parseIntStrict(parsed.id);
    const row = rows.find((r) => r.id === readingId);
    if (row && !row.read_at) {
      row.read_at = at;
      row.updated_at = at;
      row.unread_at = null;
      marked += 1;
    }
  }
  log('debug', 'notifications marked read', { person: ctx.person.id, count: marked });
  return ok({});
});

/* ------------------------------------------------------------------------ *
 * Handlers: To-dos (todoset → todolists → groups → todos)
 * ------------------------------------------------------------------------ */

/** Direct notification fan-out to an explicit person list (completion notify). */
function notifyPeopleDirectly(rec, event, personIds, actorId, excerptText) {
  if (!event) return;
  for (const pid of personIds || []) {
    if (pid === actorId) continue;
    const person = db.people.get(pid);
    if (!person || !canSeeRec(person, rec)) continue;
    readingsOf(pid).unshift({
      id: nextId(),
      personId: pid,
      recordingId: rec.id,
      eventId: event.id,
      created_at: event.created_at,
      updated_at: event.created_at,
      unread_at: event.created_at,
      read_at: null,
      excerpt: excerptText || excerpt(rec.content || recTitle(rec), 160),
    });
  }
}

route('GET', '/{accountId}/todosets/{todosetId}', 'GetTodoset', async (ctx) => {
  const todoset = getRecOr404(ctx, ctx.params.todosetId, 'Todoset');
  return ok(serializeRec(todoset));
});

route('GET', '/{accountId}/todosets/{todosetId}/todolists.json', 'ListTodolists', async (ctx) => {
  const todoset = getRecOr404(ctx, ctx.params.todosetId, 'Todoset');
  let lists = childrenOf(todoset.id, 'Todolist').filter((l) => canSeeRec(ctx.person, l));
  lists = filterByStatusQuery(ctx, lists);
  return listOk(ctx, lists, serializeRec);
});

route('POST', '/{accountId}/todosets/{todosetId}/todolists.json', 'CreateTodolist', async (ctx) => {
  const todoset = getRecOr404(ctx, ctx.params.todosetId, 'Todoset');
  requireActive(todoset, 'to-do set');
  requireProjectMutable(ctx.person, projectOfRec(todoset));
  const body = await readJsonBody(ctx);
  const todolist = createRec({
    type: 'Todolist', bucketId: todoset.bucketId, parentId: todoset.id, creatorId: ctx.person.id,
    fields: {
      name: vRequireString(body, 'name', { max: 255 }).trim(),
      description: vOptString(body, 'description', { max: 10_000 }) || '',
    },
  });
  subscribe(todolist, ctx.person.id);
  recordEvent(todolist, ctx.person.id, 'created');
  return created(serializeRec(todolist));
});

/** The polymorphic todolist endpoint returns a discriminated union wrapper. */
function todolistOrGroupUnion(rec) {
  return rec.type === 'Todolist' ? { todolist: serializeRec(rec) } : { group: serializeRec(rec) };
}

route('GET', '/{accountId}/todolists/{id}', 'GetTodolistOrGroup', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.id, ['Todolist', 'Todolist::Group']);
  return ok(todolistOrGroupUnion(rec));
});

route('PUT', '/{accountId}/todolists/{id}', 'UpdateTodolistOrGroup', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.id, ['Todolist', 'Todolist::Group']);
  requireActive(rec, 'to-do list');
  requireProjectMutable(ctx.person, projectOfRec(rec));
  const body = await readJsonBody(ctx);
  const name = vOptString(body, 'name', { max: 255 });
  if (name !== undefined) {
    if (name.trim().length === 0) throw err.unprocessable('name cannot be blank.');
    rec.name = name.trim();
  }
  const description = vOptString(body, 'description', { max: 10_000 });
  if (description !== undefined && rec.type === 'Todolist') rec.description = description;
  touch(rec);
  recordEvent(rec, ctx.person.id, 'content_changed', { notify: false });
  return ok(todolistOrGroupUnion(rec));
});

route('GET', '/{accountId}/todolists/{todolistId}/groups.json', 'ListTodolistGroups', async (ctx) => {
  const todolist = getRecOr404(ctx, ctx.params.todolistId, 'Todolist');
  const groups = childrenOf(todolist.id, 'Todolist::Group').filter((g) => effStatus(g) === 'active' && canSeeRec(ctx.person, g));
  return listOk(ctx, groups, serializeRec);
});

route('POST', '/{accountId}/todolists/{todolistId}/groups.json', 'CreateTodolistGroup', async (ctx) => {
  const todolist = getRecOr404(ctx, ctx.params.todolistId, 'Todolist');
  requireActive(todolist, 'to-do list');
  requireProjectMutable(ctx.person, projectOfRec(todolist));
  const body = await readJsonBody(ctx);
  const group = createRec({
    type: 'Todolist::Group', bucketId: todolist.bucketId, parentId: todolist.id, creatorId: ctx.person.id,
    fields: { name: vRequireString(body, 'name', { max: 255 }).trim() },
  });
  recordEvent(group, ctx.person.id, 'created', { notify: false });
  return created(serializeRec(group));
});

route('PUT', '/{accountId}/todolists/{groupId}/position.json', 'RepositionTodolistGroup', async (ctx) => {
  const group = getRecOr404(ctx, ctx.params.groupId, 'Todolist::Group');
  requireActive(group, 'group');
  const body = await readJsonBody(ctx);
  const position = vOptInt(body, 'position', { min: 1 });
  if (position === undefined) throw err.unprocessable('position is required.');
  repositionRec(group, position);
  touch(group);
  return ok({});
});

route('GET', '/{accountId}/todolists/{todolistId}/todos.json', 'ListTodos', async (ctx) => {
  const container = getRecOr404(ctx, ctx.params.todolistId, ['Todolist', 'Todolist::Group']);
  let todos = childrenOf(container.id, 'Todo').filter((t) => canSeeRec(ctx.person, t));
  const status = vEnumQuery(ctx, 'status', ['active', 'archived', 'trashed'], 'active');
  todos = todos.filter((t) => effStatus(t) === status);
  const completedParam = ctx.query.get('completed');
  if (completedParam !== null && completedParam !== '') {
    if (!['true', 'false'].includes(completedParam)) throw err.badRequest('completed must be true or false.');
    const wantCompleted = completedParam === 'true';
    todos = todos.filter((t) => !!t.completed === wantCompleted);
  } else {
    todos = todos.filter((t) => !t.completed);
  }
  return listOk(ctx, todos, serializeRec);
});

async function todoPayload(ctx, project, { requireContent }) {
  const body = await readJsonBody(ctx);
  const content = requireContent ? vRequireString(body, 'content', { max: 1000 }) : vOptString(body, 'content', { max: 1000 });
  if (content !== undefined && content.trim().length === 0) throw err.unprocessable('content cannot be blank.');
  const due = vOptDate(body, 'due_on');
  const starts = vOptDate(body, 'starts_on');
  if (due && starts && due < starts) throw err.unprocessable('due_on must be on or after starts_on.');
  return {
    content: content === undefined ? undefined : content.trim(),
    description: vOptString(body, 'description', { max: 10_000 }),
    assigneeIds: validAssignees(vOptIdArray(body, 'assignee_ids'), project),
    completionSubscriberIds: validAssignees(vOptIdArray(body, 'completion_subscriber_ids'), project),
    notify: vOptBool(body, 'notify'),
    due_on: due,
    starts_on: starts,
  };
}

route('POST', '/{accountId}/todolists/{todolistId}/todos.json', 'CreateTodo', async (ctx) => {
  const container = getRecOr404(ctx, ctx.params.todolistId, ['Todolist', 'Todolist::Group']);
  requireActive(container, 'to-do list');
  const project = projectOfRec(container);
  requireProjectMutable(ctx.person, project);
  const p = await todoPayload(ctx, project, { requireContent: true });
  const todo = createRec({
    type: 'Todo', bucketId: container.bucketId, parentId: container.id, creatorId: ctx.person.id,
    fields: {
      content: p.content,
      description: p.description || '',
      assigneeIds: p.assigneeIds || [],
      completionSubscriberIds: p.completionSubscriberIds || [],
      completed: false,
      due_on: p.due_on ?? null,
      starts_on: p.starts_on ?? null,
    },
  });
  subscribe(todo, ctx.person.id);
  const event = recordEvent(todo, ctx.person.id, 'created');
  if (p.notify) notifyPeopleDirectly(todo, event, todo.assigneeIds, ctx.person.id);
  return created(serializeRec(todo));
});

route('GET', '/{accountId}/todos/{todoId}', 'GetTodo', async (ctx) => {
  const todo = getRecOr404(ctx, ctx.params.todoId, 'Todo');
  return ok(serializeRec(todo));
});

route('PUT', '/{accountId}/todos/{todoId}', 'UpdateTodo', async (ctx) => {
  const todo = getRecOr404(ctx, ctx.params.todoId, 'Todo');
  requireActive(todo, 'to-do');
  const project = projectOfRec(todo);
  requireProjectMutable(ctx.person, project);
  const p = await todoPayload(ctx, project, { requireContent: false });
  if (p.content !== undefined) todo.content = p.content;
  if (p.description !== undefined) todo.description = p.description;
  if (p.assigneeIds !== undefined) todo.assigneeIds = p.assigneeIds;
  if (p.completionSubscriberIds !== undefined) todo.completionSubscriberIds = p.completionSubscriberIds;
  if (p.due_on !== undefined) todo.due_on = p.due_on;
  if (p.starts_on !== undefined) todo.starts_on = p.starts_on;
  touch(todo);
  recordEvent(todo, ctx.person.id, 'content_changed', { notify: false });
  return ok(serializeRec(todo));
});

route('DELETE', '/{accountId}/todos/{todoId}', 'TrashTodo', async (ctx) => {
  const todo = getRecOr404(ctx, ctx.params.todoId, 'Todo');
  if (!canModifyRec(ctx.person, todo)) throw err.forbidden('You do not have permission to trash this to-do.');
  requireProjectMutable(ctx.person, projectOfRec(todo));
  todo.status = 'trashed';
  touch(todo);
  recordEvent(todo, ctx.person.id, 'trashed');
  return noContent();
});

route('POST', '/{accountId}/todos/{todoId}/completion.json', 'CompleteTodo', async (ctx) => {
  const todo = getRecOr404(ctx, ctx.params.todoId, 'Todo');
  requireActive(todo, 'to-do');
  requireProjectMutable(ctx.person, projectOfRec(todo));
  if (!todo.completed) {
    todo.completed = true;
    todo.completed_at = nowIso();
    todo.completerId = ctx.person.id;
    touch(todo);
    const event = recordEvent(todo, ctx.person.id, 'completed');
    notifyPeopleDirectly(todo, event, todo.completionSubscriberIds, ctx.person.id);
  }
  return noContent();
});

route('DELETE', '/{accountId}/todos/{todoId}/completion.json', 'UncompleteTodo', async (ctx) => {
  const todo = getRecOr404(ctx, ctx.params.todoId, 'Todo');
  requireActive(todo, 'to-do');
  if (todo.completed) {
    todo.completed = false;
    todo.completed_at = null;
    todo.completerId = null;
    touch(todo);
    recordEvent(todo, ctx.person.id, 'uncompleted');
  }
  return noContent();
});

route('PUT', '/{accountId}/todos/{todoId}/position.json', 'RepositionTodo', async (ctx) => {
  const todo = getRecOr404(ctx, ctx.params.todoId, 'Todo');
  requireActive(todo, 'to-do');
  const body = await readJsonBody(ctx);
  const position = vOptInt(body, 'position', { min: 1 });
  if (position === undefined) throw err.unprocessable('position is required.');
  if (body.parent_id !== undefined && body.parent_id !== null) {
    const parentId = parseIntStrict(body.parent_id);
    if (parentId === null) throw err.unprocessable('parent_id must be an integer.');
    const parent = getRecOr404(ctx, parentId, ['Todolist', 'Todolist::Group']);
    if (parent.bucketId !== todo.bucketId) throw err.unprocessable('parent_id must be a list in the same project.');
    reparentRec(todo, parent.id, position);
  } else {
    repositionRec(todo, position);
  }
  touch(todo);
  return ok({});
});

/* ----------------------------- hill chart -------------------------------- */

route('GET', '/{accountId}/todosets/{todosetId}/hill.json', 'GetHillChart', async (ctx) => {
  const todoset = getRecOr404(ctx, ctx.params.todosetId, 'Todoset');
  return ok(serializeHillChart(todoset));
});

function serializeHillChart(todoset) {
  const tracked = (todoset.hillTracked || []).map((id) => db.recs.get(id)).filter((l) => l && effStatus(l) === 'active');
  return {
    enabled: tracked.length > 0,
    stale: false,
    updated_at: todoset.hillUpdatedAt || todoset.updated_at,
    app_update_url: appUrl(`/buckets/${todoset.bucketId}/todosets/${todoset.id}/hill/update`),
    app_versions_url: appUrl(`/buckets/${todoset.bucketId}/todosets/${todoset.id}/hill/versions`),
    dots: tracked.map((list) => ({
      id: list.id,
      label: list.name,
      color: list.hillColor || 'blue',
      position: list.hillPosition || 0,
      url: recUrl(list),
      app_url: recAppUrl(list),
    })),
  };
}

route('PUT', '/{accountId}/todosets/{todosetId}/hills/settings.json', 'UpdateHillChartSettings', async (ctx) => {
  const todoset = getRecOr404(ctx, ctx.params.todosetId, 'Todoset');
  requireActive(todoset, 'to-do set');
  const body = await readJsonBody(ctx);
  const tracked = vOptIdArray(body, 'tracked') || [];
  const untracked = vOptIdArray(body, 'untracked') || [];
  const current = new Set(todoset.hillTracked || []);
  for (const id of tracked) {
    const list = db.recs.get(id);
    if (!list || list.type !== 'Todolist' || list.parentId !== todoset.id) {
      throw err.unprocessable(`tracked id ${id} is not a to-do list in this to-do set.`);
    }
    current.add(id);
  }
  for (const id of untracked) current.delete(id);
  todoset.hillTracked = [...current];
  todoset.hillUpdatedAt = nowIso();
  touch(todoset);
  return ok(serializeHillChart(todoset));
});

/* ------------------------------------------------------------------------ *
 * Handlers: Card table (INIT §4.5: Triage + columns + Not now/Done edges;
 * On Hold is a per-column sub-lane)
 * ------------------------------------------------------------------------ */

const CARD_COLORS = ['white', 'red', 'orange', 'yellow', 'green', 'blue', 'aqua', 'purple', 'gray', 'pink', 'brown'];

function boardOfLane(lane) {
  let cur = lane;
  for (let depth = 0; cur && depth < 8; depth += 1) {
    if (cur.type === 'Kanban::Board') return cur;
    cur = cur.parentId === null ? null : db.recs.get(cur.parentId);
  }
  return null;
}

function getLaneOr404(ctx, columnId, types) {
  return getRecOr404(ctx, columnId, types || [...COLUMN_TYPES]);
}

route('GET', '/{accountId}/card_tables/{cardTableId}', 'GetCardTable', async (ctx) => {
  const board = getRecOr404(ctx, ctx.params.cardTableId, 'Kanban::Board');
  return ok(serializeRec(board));
});

route('POST', '/{accountId}/card_tables/{cardTableId}/columns.json', 'CreateCardColumn', async (ctx) => {
  const board = getRecOr404(ctx, ctx.params.cardTableId, 'Kanban::Board');
  requireActive(board, 'card table');
  requireProjectMutable(ctx.person, projectOfRec(board));
  const body = await readJsonBody(ctx);
  const column = createRec({
    type: 'Kanban::Column', bucketId: board.bucketId, parentId: board.id, creatorId: ctx.person.id,
    fields: {
      title: vRequireString(body, 'title', { max: 100 }).trim(),
      description: vOptString(body, 'description', { max: 5000 }) || '',
      color: null,
      onHoldId: null,
    },
  });
  // Keep user columns ahead of the built-in Not now / Done edge lanes.
  const siblings = childIds(board.id);
  const idx = siblings.indexOf(column.id);
  if (idx !== -1) siblings.splice(idx, 1);
  const firstEdge = siblings.findIndex((id) => {
    const t = (db.recs.get(id) || {}).type;
    return t === 'Kanban::NotNowColumn' || t === 'Kanban::DoneColumn';
  });
  siblings.splice(firstEdge === -1 ? siblings.length : firstEdge, 0, column.id);
  recordEvent(column, ctx.person.id, 'created', { notify: false });
  return created(serializeColumn(column));
});

route('POST', '/{accountId}/card_tables/{cardTableId}/moves.json', 'MoveCardColumn', async (ctx) => {
  const board = getRecOr404(ctx, ctx.params.cardTableId, 'Kanban::Board');
  requireActive(board, 'card table');
  const body = await readJsonBody(ctx);
  const sourceId = parseIntStrict(body.source_id);
  const targetId = parseIntStrict(body.target_id);
  if (sourceId === null || targetId === null) throw err.unprocessable('source_id and target_id are required integers.');
  const column = db.recs.get(sourceId);
  if (!column || column.type !== 'Kanban::Column' || boardOfLane(column)?.id !== board.id) {
    throw err.unprocessable('source_id must be a column on this card table.');
  }
  if (targetId !== board.id) throw err.unprocessable('target_id must be this card table.');
  const position = vOptInt(body, 'position', { min: 1 });
  // Position is relative to user columns; triage stays first, edges stay last.
  const siblings = childIds(board.id);
  const userCols = siblings.filter((id) => (db.recs.get(id) || {}).type === 'Kanban::Column' && id !== column.id);
  const insertAt = position === undefined ? userCols.length : Math.min(position - 1, userCols.length);
  userCols.splice(insertAt, 0, column.id);
  const triage = siblings.filter((id) => (db.recs.get(id) || {}).type === 'Kanban::Triage');
  const edges = siblings.filter((id) => {
    const t = (db.recs.get(id) || {}).type;
    return t === 'Kanban::NotNowColumn' || t === 'Kanban::DoneColumn';
  });
  const rest = siblings.filter((id) => !triage.includes(id) && !edges.includes(id) && !userCols.includes(id));
  db.children.set(board.id, [...triage, ...userCols, ...edges, ...rest]);
  touch(column);
  return noContent();
});

route('GET', '/{accountId}/card_tables/columns/{columnId}', 'GetCardColumn', async (ctx) => {
  const lane = getLaneOr404(ctx, ctx.params.columnId);
  return ok(serializeColumn(lane));
});

route('PUT', '/{accountId}/card_tables/columns/{columnId}', 'UpdateCardColumn', async (ctx) => {
  const lane = getLaneOr404(ctx, ctx.params.columnId, ['Kanban::Column', 'Kanban::Triage']);
  requireActive(lane, 'column');
  requireProjectMutable(ctx.person, projectOfRec(lane));
  const body = await readJsonBody(ctx);
  const title = vOptString(body, 'title', { max: 100 });
  if (title !== undefined) {
    if (title.trim().length === 0) throw err.unprocessable('title cannot be blank.');
    lane.title = title.trim();
  }
  const description = vOptString(body, 'description', { max: 5000 });
  if (description !== undefined) lane.description = description;
  touch(lane);
  return ok(serializeColumn(lane));
});

route('PUT', '/{accountId}/buckets/{bucketId}/card_tables/columns/{columnId}/color.json', 'SetCardColumnColor', async (ctx) => {
  getProjectOr404(ctx, ctx.params.bucketId);
  const lane = getLaneOr404(ctx, ctx.params.columnId, ['Kanban::Column', 'Kanban::Triage']);
  if (lane.bucketId !== ctx.params.bucketId) throw err.notFound('Column not found in this project.');
  requireActive(lane, 'column');
  const body = await readJsonBody(ctx);
  const color = vRequireString(body, 'color', { max: 20 }).toLowerCase();
  if (!CARD_COLORS.includes(color) && !/^#[0-9a-f]{6}$/.test(color)) {
    throw err.unprocessable(`color must be one of ${CARD_COLORS.join(', ')} or a #rrggbb value.`);
  }
  lane.color = color;
  touch(lane);
  return ok(serializeColumn(lane));
});

route('POST', '/{accountId}/buckets/{bucketId}/card_tables/columns/{columnId}/on_hold.json', 'EnableCardColumnOnHold', async (ctx) => {
  getProjectOr404(ctx, ctx.params.bucketId);
  const column = getLaneOr404(ctx, ctx.params.columnId, 'Kanban::Column');
  if (column.bucketId !== ctx.params.bucketId) throw err.notFound('Column not found in this project.');
  requireActive(column, 'column');
  if (!column.onHoldId) {
    const hold = createRec({
      type: 'Kanban::OnHoldColumn', bucketId: column.bucketId, parentId: column.id, creatorId: ctx.person.id,
      fields: { title: `${column.title}: On hold` },
    });
    column.onHoldId = hold.id;
  }
  touch(column);
  return ok(serializeColumn(column));
});

route('DELETE', '/{accountId}/buckets/{bucketId}/card_tables/columns/{columnId}/on_hold.json', 'DisableCardColumnOnHold', async (ctx) => {
  getProjectOr404(ctx, ctx.params.bucketId);
  const column = getLaneOr404(ctx, ctx.params.columnId, 'Kanban::Column');
  if (column.bucketId !== ctx.params.bucketId) throw err.notFound('Column not found in this project.');
  if (column.onHoldId) {
    const hold = db.recs.get(column.onHoldId);
    if (hold) {
      for (const card of childrenOf(hold.id, 'Kanban::Card')) reparentRec(card, column.id);
      hold.status = 'trashed';
    }
    column.onHoldId = null;
    touch(column);
  }
  return ok(serializeColumn(column));
});

route('GET', '/{accountId}/card_tables/lists/{columnId}/cards.json', 'ListCards', async (ctx) => {
  const lane = getLaneOr404(ctx, ctx.params.columnId);
  const cards = childrenOf(lane.id, 'Kanban::Card').filter((c) => effStatus(c) === 'active' && canSeeRec(ctx.person, c));
  return listOk(ctx, cards, serializeRec);
});

route('POST', '/{accountId}/card_tables/lists/{columnId}/cards.json', 'CreateCard', async (ctx) => {
  const lane = getLaneOr404(ctx, ctx.params.columnId, ['Kanban::Triage', 'Kanban::Column', 'Kanban::OnHoldColumn']);
  requireActive(lane, 'column');
  requireProjectMutable(ctx.person, projectOfRec(lane));
  const body = await readJsonBody(ctx);
  const card = createRec({
    type: 'Kanban::Card', bucketId: lane.bucketId, parentId: lane.id, creatorId: ctx.person.id,
    fields: {
      title: vRequireString(body, 'title', { max: 255 }).trim(),
      content: vOptString(body, 'content', { max: 100_000 }) || '',
      due_on: vOptDate(body, 'due_on') ?? null,
      assigneeIds: [],
      completionSubscriberIds: [],
      completed: false,
    },
  });
  subscribe(card, ctx.person.id);
  const event = recordEvent(card, ctx.person.id, 'created');
  if (vOptBool(body, 'notify')) notifyPeopleDirectly(card, event, [...subscriberSet(lane.id)], ctx.person.id);
  return created(serializeRec(card));
});

route('GET', '/{accountId}/card_tables/cards/{cardId}', 'GetCard', async (ctx) => {
  const card = getRecOr404(ctx, ctx.params.cardId, 'Kanban::Card');
  return ok(serializeRec(card));
});

route('PUT', '/{accountId}/card_tables/cards/{cardId}', 'UpdateCard', async (ctx) => {
  const card = getRecOr404(ctx, ctx.params.cardId, 'Kanban::Card');
  requireActive(card, 'card');
  const project = projectOfRec(card);
  requireProjectMutable(ctx.person, project);
  const body = await readJsonBody(ctx);
  const title = vOptString(body, 'title', { max: 255 });
  if (title !== undefined) {
    if (title.trim().length === 0) throw err.unprocessable('title cannot be blank.');
    card.title = title.trim();
  }
  const content = vOptString(body, 'content', { max: 100_000 });
  if (content !== undefined) card.content = content;
  const due = vOptDate(body, 'due_on');
  if (due !== undefined) card.due_on = due;
  const assignees = validAssignees(vOptIdArray(body, 'assignee_ids'), project);
  if (assignees !== undefined) card.assigneeIds = assignees;
  touch(card);
  recordEvent(card, ctx.person.id, 'content_changed', { notify: false });
  return ok(serializeRec(card));
});

route('POST', '/{accountId}/card_tables/cards/{cardId}/moves.json', 'MoveCard', async (ctx) => {
  const card = getRecOr404(ctx, ctx.params.cardId, 'Kanban::Card');
  requireActive(card, 'card');
  requireProjectMutable(ctx.person, projectOfRec(card));
  const body = await readJsonBody(ctx);
  const columnId = parseIntStrict(body.column_id);
  if (columnId === null) throw err.unprocessable('column_id is required and must be an integer.');
  const target = db.recs.get(columnId);
  const sourceLane = db.recs.get(card.parentId);
  if (!target || !COLUMN_TYPES.has(target.type)) throw err.unprocessable('column_id must be a card table lane.');
  if (!sourceLane || boardOfLane(target)?.id !== boardOfLane(sourceLane)?.id) {
    throw err.unprocessable('Cards can only move within their own card table.');
  }
  const position = vOptInt(body, 'position', { min: 1 });
  reparentRec(card, target.id, position);
  const wasCompleted = card.completed;
  card.completed = target.type === 'Kanban::DoneColumn';
  if (card.completed && !wasCompleted) {
    card.completed_at = nowIso();
    card.completerId = ctx.person.id;
    const event = recordEvent(card, ctx.person.id, 'completed');
    notifyPeopleDirectly(card, event, card.completionSubscriberIds, ctx.person.id);
  } else if (!card.completed && wasCompleted) {
    card.completed_at = null;
    card.completerId = null;
    recordEvent(card, ctx.person.id, 'uncompleted');
  } else {
    recordEvent(card, ctx.person.id, 'moved', {
      details: { from: sourceLane.title, to: target.title }, notify: false,
    });
  }
  touch(card);
  return noContent();
});

/* ------------------------------- card steps ------------------------------- */

route('POST', '/{accountId}/card_tables/cards/{cardId}/steps.json', 'CreateCardStep', async (ctx) => {
  const card = getRecOr404(ctx, ctx.params.cardId, 'Kanban::Card');
  requireActive(card, 'card');
  const project = projectOfRec(card);
  requireProjectMutable(ctx.person, project);
  const body = await readJsonBody(ctx);
  const step = createRec({
    type: 'Kanban::Step', bucketId: card.bucketId, parentId: card.id, creatorId: ctx.person.id,
    fields: {
      title: vRequireString(body, 'title', { max: 255 }).trim(),
      due_on: vOptDate(body, 'due_on') ?? null,
      assigneeIds: validAssignees(vOptIdArray(body, 'assignee_ids'), project) || [],
      completed: false,
    },
  });
  touch(card);
  return created(serializeRec(step));
});

route('POST', '/{accountId}/card_tables/cards/{cardId}/positions.json', 'RepositionCardStep', async (ctx) => {
  const card = getRecOr404(ctx, ctx.params.cardId, 'Kanban::Card');
  requireActive(card, 'card');
  const body = await readJsonBody(ctx);
  const sourceId = parseIntStrict(body.source_id);
  const position = vOptInt(body, 'position', { min: 1 });
  if (sourceId === null || position === undefined) throw err.unprocessable('source_id and position are required.');
  const step = db.recs.get(sourceId);
  if (!step || step.type !== 'Kanban::Step' || step.parentId !== card.id) {
    throw err.unprocessable('source_id must be a step on this card.');
  }
  repositionRec(step, position);
  touch(step);
  return ok({});
});

route('GET', '/{accountId}/card_tables/steps/{stepId}', 'GetCardStep', async (ctx) => {
  const step = getRecOr404(ctx, ctx.params.stepId, 'Kanban::Step');
  return ok(serializeRec(step));
});

route('PUT', '/{accountId}/card_tables/steps/{stepId}', 'UpdateCardStep', async (ctx) => {
  const step = getRecOr404(ctx, ctx.params.stepId, 'Kanban::Step');
  requireActive(step, 'step');
  const project = projectOfRec(step);
  requireProjectMutable(ctx.person, project);
  const body = await readJsonBody(ctx);
  const title = vOptString(body, 'title', { max: 255 });
  if (title !== undefined) {
    if (title.trim().length === 0) throw err.unprocessable('title cannot be blank.');
    step.title = title.trim();
  }
  const due = vOptDate(body, 'due_on');
  if (due !== undefined) step.due_on = due;
  const assignees = validAssignees(vOptIdArray(body, 'assignee_ids'), project);
  if (assignees !== undefined) step.assigneeIds = assignees;
  touch(step);
  return ok(serializeRec(step));
});

route('PUT', '/{accountId}/card_tables/steps/{stepId}/completions.json', 'SetCardStepCompletion', async (ctx) => {
  const step = getRecOr404(ctx, ctx.params.stepId, 'Kanban::Step');
  requireActive(step, 'step');
  requireProjectMutable(ctx.person, projectOfRec(step));
  const body = await readJsonBody(ctx);
  const completion = vRequireString(body, 'completion', { max: 10 });
  if (!['on', 'off'].includes(completion)) throw err.unprocessable("completion must be 'on' or 'off'.");
  const completing = completion === 'on';
  if (completing !== !!step.completed) {
    step.completed = completing;
    step.completed_at = completing ? nowIso() : null;
    step.completerId = completing ? ctx.person.id : null;
    touch(step);
  }
  return ok(serializeRec(step));
});

/* ------------------------- column watch subscriptions --------------------- */

route('POST', '/{accountId}/card_tables/lists/{columnId}/subscription.json', 'SubscribeToCardColumn', async (ctx) => {
  const lane = getLaneOr404(ctx, ctx.params.columnId);
  subscribe(lane, ctx.person.id);
  return noContent();
});

route('DELETE', '/{accountId}/card_tables/lists/{columnId}/subscription.json', 'UnsubscribeFromCardColumn', async (ctx) => {
  const lane = getLaneOr404(ctx, ctx.params.columnId);
  unsubscribe(lane, ctx.person.id);
  return noContent();
});

/* ------------------------------------------------------------------------ *
 * Handlers: Attachments, vaults, documents, uploads
 * ------------------------------------------------------------------------ */

function storeAttachment(name, contentType, bytes) {
  const id = nextId();
  // Filenames reach Content-Disposition and JSON payloads: strip control
  // characters and path separators, cap length.
  const safeName = String(name || 'file')
    .replace(/[\u0000-\u001f\u007f/\\]/g, '')
    .slice(0, 255) || 'file';
  const att = {
    id,
    sgid: makeSgid('attachment', id),
    name: safeName,
    content_type: contentType || 'application/octet-stream',
    bytes,
    byte_size: bytes.length,
    width: null,
    height: null,
    created_at: nowIso(),
  };
  db.attachments.set(id, att);
  return att;
}

function attachmentBySgid(sgid) {
  const parsed = parseSgid(sgid);
  if (!parsed || parsed.kind !== 'attachment') return null;
  return db.attachments.get(parseIntStrict(parsed.id)) || null;
}

route('POST', '/{accountId}/attachments.json', 'CreateAttachment', async (ctx) => {
  const name = ctx.query.get('name');
  if (!name) throw err.badRequest('name query parameter is required.');
  const bytes = await readBody(ctx.req, CONFIG.maxUploadBody);
  if (bytes.length === 0) throw err.unprocessable('Attachment body cannot be empty.');
  const contentType = (ctx.req.headers['content-type'] || 'application/octet-stream').split(';')[0].trim();
  const att = storeAttachment(name, contentType, bytes);
  log('debug', 'attachment stored', { id: att.id, bytes: att.byte_size, type: att.content_type });
  return created({ attachable_sgid: att.sgid });
});

route('GET', '/{accountId}/vaults/{vaultId}', 'GetVault', async (ctx) => {
  const vault = getRecOr404(ctx, ctx.params.vaultId, 'Vault');
  return ok(serializeRec(vault));
});

route('PUT', '/{accountId}/vaults/{vaultId}', 'UpdateVault', async (ctx) => {
  const vault = getRecOr404(ctx, ctx.params.vaultId, 'Vault');
  requireActive(vault, 'folder');
  requireProjectMutable(ctx.person, projectOfRec(vault));
  const body = await readJsonBody(ctx);
  const title = vOptString(body, 'title', { max: 255 });
  if (title !== undefined) {
    if (title.trim().length === 0) throw err.unprocessable('title cannot be blank.');
    if (vault.parentId === null) vault.dockTitle = title.trim(); else vault.title = title.trim();
  }
  touch(vault);
  return ok(serializeRec(vault));
});

route('GET', '/{accountId}/vaults/{vaultId}/vaults.json', 'ListVaults', async (ctx) => {
  const vault = getRecOr404(ctx, ctx.params.vaultId, 'Vault');
  const vaults = childrenOf(vault.id, 'Vault').filter((v) => effStatus(v) === 'active' && canSeeRec(ctx.person, v));
  return listOk(ctx, vaults, serializeRec);
});

route('POST', '/{accountId}/vaults/{vaultId}/vaults.json', 'CreateVault', async (ctx) => {
  const vault = getRecOr404(ctx, ctx.params.vaultId, 'Vault');
  requireActive(vault, 'folder');
  requireProjectMutable(ctx.person, projectOfRec(vault));
  const body = await readJsonBody(ctx);
  const child = createRec({
    type: 'Vault', bucketId: vault.bucketId, parentId: vault.id, creatorId: ctx.person.id,
    fields: { title: vRequireString(body, 'title', { max: 255 }).trim() },
  });
  recordEvent(child, ctx.person.id, 'created', { notify: false });
  return created(serializeRec(child));
});

route('GET', '/{accountId}/vaults/{vaultId}/documents.json', 'ListDocuments', async (ctx) => {
  const vault = getRecOr404(ctx, ctx.params.vaultId, 'Vault');
  const docs = childrenOf(vault.id, 'Document')
    .filter((d) => effStatus(d) === 'active' && d.status !== 'drafted' && canSeeRec(ctx.person, d));
  return listOk(ctx, docs, serializeRec);
});

route('POST', '/{accountId}/vaults/{vaultId}/documents.json', 'CreateDocument', async (ctx) => {
  const vault = getRecOr404(ctx, ctx.params.vaultId, 'Vault');
  requireActive(vault, 'folder');
  requireProjectMutable(ctx.person, projectOfRec(vault));
  const body = await readJsonBody(ctx);
  const status = body.status === undefined ? 'active' : body.status;
  if (!['active', 'drafted'].includes(status)) throw err.unprocessable("status must be 'active' or 'drafted'.");
  const doc = createRec({
    type: 'Document', bucketId: vault.bucketId, parentId: vault.id, creatorId: ctx.person.id,
    status,
    fields: {
      title: vRequireString(body, 'title', { max: 255 }).trim(),
      content: vOptString(body, 'content') || '',
    },
  });
  subscribe(doc, ctx.person.id);
  for (const pid of vOptIdArray(body, 'subscriptions') || []) {
    if (db.people.has(pid)) subscribe(doc, pid);
  }
  applyMentions(doc, doc.content, ctx.person.id);
  recordEvent(doc, ctx.person.id, 'created');
  return created(serializeRec(doc));
});

route('GET', '/{accountId}/documents/{documentId}', 'GetDocument', async (ctx) => {
  const doc = getRecOr404(ctx, ctx.params.documentId, 'Document');
  return ok(serializeRec(doc));
});

route('PUT', '/{accountId}/documents/{documentId}', 'UpdateDocument', async (ctx) => {
  const doc = getRecOr404(ctx, ctx.params.documentId, 'Document');
  if (doc.status !== 'drafted') requireActive(doc, 'document');
  requireProjectMutable(ctx.person, projectOfRec(doc));
  const body = await readJsonBody(ctx);
  const title = vOptString(body, 'title', { max: 255 });
  if (title !== undefined) {
    if (title.trim().length === 0) throw err.unprocessable('title cannot be blank.');
    doc.title = title.trim();
  }
  const content = vOptString(body, 'content');
  if (content !== undefined) {
    doc.content = content;
    applyMentions(doc, content, ctx.person.id);
  }
  touch(doc);
  recordEvent(doc, ctx.person.id, 'content_changed', { notify: false });
  return ok(serializeRec(doc));
});

route('GET', '/{accountId}/vaults/{vaultId}/uploads.json', 'ListUploads', async (ctx) => {
  const vault = getRecOr404(ctx, ctx.params.vaultId, 'Vault');
  const uploads = childrenOf(vault.id, 'Upload').filter((u) => effStatus(u) === 'active' && canSeeRec(ctx.person, u));
  return listOk(ctx, uploads, serializeRec);
});

function splitFilename(name) {
  const i = name.lastIndexOf('.');
  if (i <= 0 || i === name.length - 1) return { base: name, ext: null };
  return { base: name.slice(0, i), ext: name.slice(i + 1) };
}

route('POST', '/{accountId}/vaults/{vaultId}/uploads.json', 'CreateUpload', async (ctx) => {
  const vault = getRecOr404(ctx, ctx.params.vaultId, 'Vault');
  requireActive(vault, 'folder');
  requireProjectMutable(ctx.person, projectOfRec(vault));
  const body = await readJsonBody(ctx);
  const sgid = vRequireString(body, 'attachable_sgid', { max: 512 });
  const att = attachmentBySgid(sgid);
  if (!att) throw err.unprocessable('attachable_sgid does not reference an uploaded attachment.');
  const { base, ext } = splitFilename(att.name);
  const baseName = vOptString(body, 'base_name', { max: 255 });
  const upload = createRec({
    type: 'Upload', bucketId: vault.bucketId, parentId: vault.id, creatorId: ctx.person.id,
    fields: {
      attachmentId: att.id,
      base_name: (baseName && baseName.trim()) || base,
      extension: ext,
      description: vOptString(body, 'description', { max: 10_000 }) || '',
    },
  });
  subscribe(upload, ctx.person.id);
  for (const pid of vOptIdArray(body, 'subscriptions') || []) {
    if (db.people.has(pid)) subscribe(upload, pid);
  }
  recordEvent(upload, ctx.person.id, 'created');
  return created(serializeRec(upload));
});

route('GET', '/{accountId}/uploads/{uploadId}', 'GetUpload', async (ctx) => {
  const upload = getRecOr404(ctx, ctx.params.uploadId, 'Upload');
  return ok(serializeRec(upload));
});

route('PUT', '/{accountId}/uploads/{uploadId}', 'UpdateUpload', async (ctx) => {
  const upload = getRecOr404(ctx, ctx.params.uploadId, 'Upload');
  requireActive(upload, 'upload');
  requireProjectMutable(ctx.person, projectOfRec(upload));
  const body = await readJsonBody(ctx);
  const description = vOptString(body, 'description', { max: 10_000 });
  if (description !== undefined) upload.description = description;
  const baseName = vOptString(body, 'base_name', { max: 255 });
  if (baseName !== undefined) {
    if (baseName.trim().length === 0) throw err.unprocessable('base_name cannot be blank.');
    upload.base_name = baseName.trim();
  }
  touch(upload);
  recordEvent(upload, ctx.person.id, 'content_changed', { notify: false });
  return ok(serializeRec(upload));
});

route('GET', '/{accountId}/uploads/{uploadId}/versions.json', 'ListUploadVersions', async (ctx) => {
  const upload = getRecOr404(ctx, ctx.params.uploadId, 'Upload');
  // Versioning is not modeled: the current file is the only version.
  return listOk(ctx, [upload], serializeRec);
});

/* ------------------------------------------------------------------------ *
 * Handlers: Campfire (chat)
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/chats.json', 'ListCampfires', async (ctx) => {
  const chats = [];
  for (const project of visibleProjects(ctx.person)) {
    for (const tool of dockToolRecs(project, { enabledOnly: true })) {
      if (tool.type === 'Chat::Transcript' && canSeeRec(ctx.person, tool)) chats.push(tool);
    }
  }
  return listOk(ctx, chats, serializeRec);
});

route('GET', '/{accountId}/chats/{campfireId}', 'GetCampfire', async (ctx) => {
  const chat = getRecOr404(ctx, ctx.params.campfireId, 'Chat::Transcript');
  return ok(serializeRec(chat));
});

route('GET', '/{accountId}/chats/{campfireId}/lines.json', 'ListCampfireLines', async (ctx) => {
  const chat = getRecOr404(ctx, ctx.params.campfireId, 'Chat::Transcript');
  let lines = childrenOf(chat.id, 'Chat::Lines::Text').filter((l) => effStatus(l) === 'active' && canSeeRec(ctx.person, l));
  lines = sortItems(ctx, lines, { sort: 'created_at', direction: 'desc' });
  return listOk(ctx, lines, serializeRec);
});

function createChatLine(chat, creatorId, content, { attachmentIds, at } = {}) {
  const line = createRec({
    type: 'Chat::Lines::Text', bucketId: chat.bucketId, parentId: chat.id, creatorId,
    created_at: at,
    fields: { content, attachmentIds: attachmentIds || [] },
  });
  recordEvent(line, creatorId, 'created', { at, notify: false });
  return line;
}

route('POST', '/{accountId}/chats/{campfireId}/lines.json', 'CreateCampfireLine', async (ctx) => {
  const chat = getRecOr404(ctx, ctx.params.campfireId, 'Chat::Transcript');
  requireActive(chat, 'chat');
  requireProjectMutable(ctx.person, projectOfRec(chat));
  const body = await readJsonBody(ctx);
  const content = vRequireString(body, 'content', { max: 20_000 });
  const line = createChatLine(chat, ctx.person.id, content);
  return created(serializeRec(line));
});

route('GET', '/{accountId}/chats/{campfireId}/lines/{lineId}', 'GetCampfireLine', async (ctx) => {
  const chat = getRecOr404(ctx, ctx.params.campfireId, 'Chat::Transcript');
  const line = getRecOr404(ctx, ctx.params.lineId, 'Chat::Lines::Text');
  if (line.parentId !== chat.id) throw err.notFound('Chat line not found.');
  return ok(serializeRec(line));
});

route('DELETE', '/{accountId}/chats/{campfireId}/lines/{lineId}', 'DeleteCampfireLine', async (ctx) => {
  const chat = getRecOr404(ctx, ctx.params.campfireId, 'Chat::Transcript');
  const line = getRecOr404(ctx, ctx.params.lineId, 'Chat::Lines::Text');
  if (line.parentId !== chat.id) throw err.notFound('Chat line not found.');
  if (!canModifyRec(ctx.person, line)) throw err.forbidden('Only the author can delete a chat line.');
  line.status = 'trashed';
  touch(line);
  return noContent();
});

route('GET', '/{accountId}/chats/{campfireId}/uploads.json', 'ListCampfireUploads', async (ctx) => {
  const chat = getRecOr404(ctx, ctx.params.campfireId, 'Chat::Transcript');
  let lines = childrenOf(chat.id, 'Chat::Lines::Text')
    .filter((l) => effStatus(l) === 'active' && (l.attachmentIds || []).length > 0 && canSeeRec(ctx.person, l));
  lines = sortItems(ctx, lines, { sort: 'created_at', direction: 'desc' });
  return listOk(ctx, lines, serializeRec);
});

route('POST', '/{accountId}/chats/{campfireId}/uploads.json', 'CreateCampfireUpload', async (ctx) => {
  const chat = getRecOr404(ctx, ctx.params.campfireId, 'Chat::Transcript');
  requireActive(chat, 'chat');
  requireProjectMutable(ctx.person, projectOfRec(chat));
  const name = ctx.query.get('name');
  if (!name) throw err.badRequest('name query parameter is required.');
  const bytes = await readBody(ctx.req, CONFIG.maxUploadBody);
  if (bytes.length === 0) throw err.unprocessable('Upload body cannot be empty.');
  const contentType = (ctx.req.headers['content-type'] || 'application/octet-stream').split(';')[0].trim();
  const att = storeAttachment(name, contentType, bytes);
  const line = createChatLine(chat, ctx.person.id, '', { attachmentIds: [att.id] });
  return created(serializeRec(line));
});

/* -------------------------------- chatbots -------------------------------- */

route('GET', '/{accountId}/chats/{campfireId}/integrations.json', 'ListChatbots', async (ctx) => {
  const chat = getRecOr404(ctx, ctx.params.campfireId, 'Chat::Transcript');
  const bots = [...db.chatbots.values()].filter((b) => b.campfireId === chat.id);
  return listOk(ctx, bots, serializeChatbot);
});

route('POST', '/{accountId}/chats/{campfireId}/integrations.json', 'CreateChatbot', async (ctx) => {
  const chat = getRecOr404(ctx, ctx.params.campfireId, 'Chat::Transcript');
  requireEmployee(ctx, 'manage chatbots');
  const body = await readJsonBody(ctx);
  const serviceName = vRequireString(body, 'service_name', { max: 60 }).trim();
  if (!/^[a-z0-9_-]+$/i.test(serviceName)) {
    throw err.unprocessable('service_name may only contain letters, numbers, dashes and underscores.');
  }
  const commandUrl = vOptString(body, 'command_url', { max: 2000 });
  if (commandUrl && !/^https?:\/\//.test(commandUrl)) throw err.unprocessable('command_url must be an http(s) URL.');
  const at = nowIso();
  const bot = {
    id: nextId(),
    campfireId: chat.id,
    bucketId: chat.bucketId,
    service_name: serviceName,
    command_url: commandUrl || null,
    key: crypto.randomBytes(16).toString('hex'),
    created_at: at,
    updated_at: at,
  };
  db.chatbots.set(bot.id, bot);
  return created(serializeChatbot(bot));
});

function getChatbotOr404(ctx) {
  const chat = getRecOr404(ctx, ctx.params.campfireId, 'Chat::Transcript');
  const bot = db.chatbots.get(ctx.params.chatbotId);
  if (!bot || bot.campfireId !== chat.id) throw err.notFound('Chatbot not found.');
  return bot;
}

route('GET', '/{accountId}/chats/{campfireId}/integrations/{chatbotId}', 'GetChatbot', async (ctx) => ok(serializeChatbot(getChatbotOr404(ctx))));

route('PUT', '/{accountId}/chats/{campfireId}/integrations/{chatbotId}', 'UpdateChatbot', async (ctx) => {
  requireEmployee(ctx, 'manage chatbots');
  const bot = getChatbotOr404(ctx);
  const body = await readJsonBody(ctx);
  const serviceName = vRequireString(body, 'service_name', { max: 60 }).trim();
  if (!/^[a-z0-9_-]+$/i.test(serviceName)) {
    throw err.unprocessable('service_name may only contain letters, numbers, dashes and underscores.');
  }
  bot.service_name = serviceName;
  const commandUrl = vOptString(body, 'command_url', { max: 2000 });
  if (commandUrl !== undefined) {
    if (commandUrl && !/^https?:\/\//.test(commandUrl)) throw err.unprocessable('command_url must be an http(s) URL.');
    bot.command_url = commandUrl || null;
  }
  bot.updated_at = nowIso();
  return ok(serializeChatbot(bot));
});

route('DELETE', '/{accountId}/chats/{campfireId}/integrations/{chatbotId}', 'DeleteChatbot', async (ctx) => {
  requireEmployee(ctx, 'manage chatbots');
  const bot = getChatbotOr404(ctx);
  db.chatbots.delete(bot.id);
  return noContent();
});

/* ------------------------------------------------------------------------ *
 * Handlers: Schedule
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/schedules/{scheduleId}', 'GetSchedule', async (ctx) => {
  const schedule = getRecOr404(ctx, ctx.params.scheduleId, 'Schedule');
  return ok(serializeRec(schedule));
});

route('PUT', '/{accountId}/schedules/{scheduleId}', 'UpdateScheduleSettings', async (ctx) => {
  const schedule = getRecOr404(ctx, ctx.params.scheduleId, 'Schedule');
  requireActive(schedule, 'schedule');
  const body = await readJsonBody(ctx);
  if (typeof body.include_due_assignments !== 'boolean') {
    throw err.unprocessable('include_due_assignments must be a boolean.');
  }
  schedule.include_due_assignments = body.include_due_assignments;
  touch(schedule);
  return ok(serializeRec(schedule));
});

route('GET', '/{accountId}/schedules/{scheduleId}/entries.json', 'ListScheduleEntries', async (ctx) => {
  const schedule = getRecOr404(ctx, ctx.params.scheduleId, 'Schedule');
  let entries = childrenOf(schedule.id, 'Schedule::Entry').filter((e) => canSeeRec(ctx.person, e));
  entries = filterByStatusQuery(ctx, entries);
  entries.sort((a, b) => (a.starts_at < b.starts_at ? -1 : 1));
  return listOk(ctx, entries, serializeRec);
});

async function scheduleEntryPayload(ctx, project, { requireCore }) {
  const body = await readJsonBody(ctx);
  const summary = requireCore ? vRequireString(body, 'summary', { max: 255 }) : vOptString(body, 'summary', { max: 255 });
  if (summary !== undefined && summary.trim().length === 0) throw err.unprocessable('summary cannot be blank.');
  const allDay = vOptBool(body, 'all_day');
  let starts = body.starts_at;
  let ends = body.ends_at;
  if (requireCore && (starts === undefined || ends === undefined)) {
    throw err.unprocessable('starts_at and ends_at are required.');
  }
  const parseWhen = (v, field) => {
    if (v === undefined) return undefined;
    if (isDateString(v)) return new Date(v + 'T00:00:00Z').toISOString();
    if (isDateTimeString(v)) return new Date(v).toISOString();
    throw err.unprocessable(`${field} must be an ISO 8601 timestamp or YYYY-MM-DD date.`);
  };
  starts = parseWhen(starts, 'starts_at');
  ends = parseWhen(ends, 'ends_at');
  if (starts && ends && ends < starts) throw err.unprocessable('ends_at must be at or after starts_at.');
  return {
    summary: summary === undefined ? undefined : summary.trim(),
    description: vOptString(body, 'description', { max: 10_000 }),
    participantIds: validAssignees(vOptIdArray(body, 'participant_ids'), project),
    all_day: allDay,
    starts_at: starts,
    ends_at: ends,
    notify: vOptBool(body, 'notify'),
    subscriptions: vOptIdArray(body, 'subscriptions'),
  };
}

route('POST', '/{accountId}/schedules/{scheduleId}/entries.json', 'CreateScheduleEntry', async (ctx) => {
  const schedule = getRecOr404(ctx, ctx.params.scheduleId, 'Schedule');
  requireActive(schedule, 'schedule');
  const project = projectOfRec(schedule);
  requireProjectMutable(ctx.person, project);
  const p = await scheduleEntryPayload(ctx, project, { requireCore: true });
  const entry = createRec({
    type: 'Schedule::Entry', bucketId: schedule.bucketId, parentId: schedule.id, creatorId: ctx.person.id,
    fields: {
      summary: p.summary,
      description: p.description || '',
      all_day: p.all_day || false,
      starts_at: p.starts_at,
      ends_at: p.ends_at,
      participantIds: p.participantIds || [],
    },
  });
  subscribe(entry, ctx.person.id);
  for (const pid of p.subscriptions || []) {
    if (db.people.has(pid)) subscribe(entry, pid);
  }
  const event = recordEvent(entry, ctx.person.id, 'created');
  if (p.notify) notifyPeopleDirectly(entry, event, entry.participantIds, ctx.person.id);
  return created(serializeRec(entry));
});

route('GET', '/{accountId}/schedule_entries/{entryId}', 'GetScheduleEntry', async (ctx) => {
  const entry = getRecOr404(ctx, ctx.params.entryId, 'Schedule::Entry');
  return ok(serializeRec(entry));
});

route('PUT', '/{accountId}/schedule_entries/{entryId}', 'UpdateScheduleEntry', async (ctx) => {
  const entry = getRecOr404(ctx, ctx.params.entryId, 'Schedule::Entry');
  requireActive(entry, 'schedule entry');
  const project = projectOfRec(entry);
  requireProjectMutable(ctx.person, project);
  const p = await scheduleEntryPayload(ctx, project, { requireCore: false });
  if (p.summary !== undefined) entry.summary = p.summary;
  if (p.description !== undefined) entry.description = p.description;
  if (p.all_day !== undefined) entry.all_day = p.all_day;
  if (p.starts_at !== undefined) entry.starts_at = p.starts_at;
  if (p.ends_at !== undefined) entry.ends_at = p.ends_at;
  if (entry.ends_at < entry.starts_at) throw err.unprocessable('ends_at must be at or after starts_at.');
  if (p.participantIds !== undefined) entry.participantIds = p.participantIds;
  touch(entry);
  const event = recordEvent(entry, ctx.person.id, 'content_changed', { notify: false });
  if (p.notify) notifyPeopleDirectly(entry, event, entry.participantIds, ctx.person.id);
  return ok(serializeRec(entry));
});

route('GET', '/{accountId}/schedule_entries/{entryId}/occurrences/{date}', 'GetScheduleEntryOccurrence', async (ctx) => {
  const entry = getRecOr404(ctx, ctx.params.entryId, 'Schedule::Entry');
  // Recurrence is not modeled (scope note in the header): an entry occurs on
  // the calendar days it spans, and only those days resolve.
  const date = ctx.params.date;
  if (dateOnly(entry.starts_at) > date || date > dateOnly(entry.ends_at)) {
    throw err.notFound('No occurrence of this entry on that date.');
  }
  return ok(serializeRec(entry));
});

/* ------------------------------------------------------------------------ *
 * Handlers: Automatic check-ins (questionnaire → questions → answers)
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/questionnaires/{questionnaireId}', 'GetQuestionnaire', async (ctx) => {
  const questionnaire = getRecOr404(ctx, ctx.params.questionnaireId, 'Questionnaire');
  return ok(serializeRec(questionnaire));
});

route('GET', '/{accountId}/questionnaires/{questionnaireId}/questions.json', 'ListQuestions', async (ctx) => {
  const questionnaire = getRecOr404(ctx, ctx.params.questionnaireId, 'Questionnaire');
  const questions = childrenOf(questionnaire.id, 'Question').filter((q) => effStatus(q) === 'active' && canSeeRec(ctx.person, q));
  return listOk(ctx, questions, serializeRec);
});

const QUESTION_FREQUENCIES = ['every_day', 'every_week', 'every_other_week', 'every_month', 'once'];

function validQuestionSchedule(raw) {
  if (raw === null || typeof raw !== 'object') throw err.unprocessable('schedule payload is required.');
  const schedule = {};
  if (raw.frequency !== undefined) {
    if (!QUESTION_FREQUENCIES.includes(raw.frequency)) {
      throw err.unprocessable(`schedule.frequency must be one of ${QUESTION_FREQUENCIES.join(', ')}.`);
    }
    schedule.frequency = raw.frequency;
  } else {
    schedule.frequency = 'every_day';
  }
  if (raw.days !== undefined) {
    if (!Array.isArray(raw.days) || raw.days.some((d) => parseIntStrict(d) === null || d < 0 || d > 6)) {
      throw err.unprocessable('schedule.days must be an array of weekday numbers 0-6.');
    }
    schedule.days = raw.days.map(Number);
  }
  for (const f of ['hour', 'minute', 'week_instance', 'week_interval', 'month_interval']) {
    if (raw[f] !== undefined) {
      const n = parseIntStrict(raw[f]);
      if (n === null || n < 0) throw err.unprocessable(`schedule.${f} must be a non-negative integer.`);
      schedule[f] = n;
    }
  }
  if (raw.start_date !== undefined) {
    if (!isDateString(raw.start_date)) throw err.unprocessable('schedule.start_date must be a YYYY-MM-DD date.');
    schedule.start_date = raw.start_date;
  }
  if (raw.end_date !== undefined && raw.end_date !== null) {
    if (!isDateString(raw.end_date)) throw err.unprocessable('schedule.end_date must be a YYYY-MM-DD date.');
    schedule.end_date = raw.end_date;
  }
  if (schedule.hour === undefined) schedule.hour = 9;
  if (schedule.minute === undefined) schedule.minute = 0;
  return schedule;
}

route('POST', '/{accountId}/questionnaires/{questionnaireId}/questions.json', 'CreateQuestion', async (ctx) => {
  const questionnaire = getRecOr404(ctx, ctx.params.questionnaireId, 'Questionnaire');
  requireActive(questionnaire, 'questionnaire');
  requireProjectMutable(ctx.person, projectOfRec(questionnaire));
  const body = await readJsonBody(ctx);
  const question = createRec({
    type: 'Question', bucketId: questionnaire.bucketId, parentId: questionnaire.id, creatorId: ctx.person.id,
    fields: {
      title: vRequireString(body, 'title', { max: 500 }).trim(),
      schedule: validQuestionSchedule(body.schedule),
      paused: false,
    },
  });
  subscribe(question, ctx.person.id);
  recordEvent(question, ctx.person.id, 'created', { notify: false });
  return created(serializeRec(question));
});

route('GET', '/{accountId}/questions/{questionId}', 'GetQuestion', async (ctx) => {
  const question = getRecOr404(ctx, ctx.params.questionId, 'Question');
  return ok(serializeRec(question));
});

route('PUT', '/{accountId}/questions/{questionId}', 'UpdateQuestion', async (ctx) => {
  const question = getRecOr404(ctx, ctx.params.questionId, 'Question');
  requireActive(question, 'question');
  requireProjectMutable(ctx.person, projectOfRec(question));
  const body = await readJsonBody(ctx);
  const title = vOptString(body, 'title', { max: 500 });
  if (title !== undefined) {
    if (title.trim().length === 0) throw err.unprocessable('title cannot be blank.');
    question.title = title.trim();
  }
  if (body.schedule !== undefined) question.schedule = validQuestionSchedule(body.schedule);
  const paused = vOptBool(body, 'paused');
  if (paused !== undefined) question.paused = paused;
  touch(question);
  return ok(serializeRec(question));
});

route('POST', '/{accountId}/questions/{questionId}/pause.json', 'PauseQuestion', async (ctx) => {
  const question = getRecOr404(ctx, ctx.params.questionId, 'Question');
  question.paused = true;
  touch(question);
  return ok({ paused: true });
});

route('DELETE', '/{accountId}/questions/{questionId}/pause.json', 'ResumeQuestion', async (ctx) => {
  const question = getRecOr404(ctx, ctx.params.questionId, 'Question');
  question.paused = false;
  touch(question);
  return ok({ paused: false });
});

route('GET', '/{accountId}/questions/{questionId}/answers.json', 'ListAnswers', async (ctx) => {
  const question = getRecOr404(ctx, ctx.params.questionId, 'Question');
  const answers = childrenOf(question.id, 'Question::Answer').filter((a) => effStatus(a) === 'active' && canSeeRec(ctx.person, a));
  return listOk(ctx, answers, serializeRec);
});

route('POST', '/{accountId}/questions/{questionId}/answers.json', 'CreateAnswer', async (ctx) => {
  const question = getRecOr404(ctx, ctx.params.questionId, 'Question');
  requireActive(question, 'question');
  requireProjectMutable(ctx.person, projectOfRec(question));
  const body = await readJsonBody(ctx);
  const content = vRequireString(body, 'content');
  const groupOn = body.group_on === undefined ? dateOnly(nowIso()) : body.group_on;
  if (!isDateString(groupOn)) throw err.unprocessable('group_on must be a YYYY-MM-DD date.');
  const answer = createRec({
    type: 'Question::Answer', bucketId: question.bucketId, parentId: question.id, creatorId: ctx.person.id,
    fields: { content, group_on: groupOn },
  });
  subscribe(answer, ctx.person.id);
  applyMentions(answer, content, ctx.person.id);
  // Question subscribers hear about new answers.
  const event = recordEvent(answer, ctx.person.id, 'created');
  notifyPeopleDirectly(answer, event, [...subscriberSet(question.id)], ctx.person.id);
  return created(serializeRec(answer));
});

route('GET', '/{accountId}/question_answers/{answerId}', 'GetAnswer', async (ctx) => {
  const answer = getRecOr404(ctx, ctx.params.answerId, 'Question::Answer');
  return ok(serializeRec(answer));
});

route('PUT', '/{accountId}/question_answers/{answerId}', 'UpdateAnswer', async (ctx) => {
  const answer = getRecOr404(ctx, ctx.params.answerId, 'Question::Answer');
  if (!canModifyRec(ctx.person, answer)) throw err.forbidden('Only the author can edit this answer.');
  requireActive(answer, 'answer');
  const body = await readJsonBody(ctx);
  answer.content = vRequireString(body, 'content');
  if (body.group_on !== undefined) {
    if (!isDateString(body.group_on)) throw err.unprocessable('group_on must be a YYYY-MM-DD date.');
    answer.group_on = body.group_on;
  }
  touch(answer);
  return noContent();
});

route('GET', '/{accountId}/questions/{questionId}/answers/by.json', 'ListQuestionAnswerers', async (ctx) => {
  const question = getRecOr404(ctx, ctx.params.questionId, 'Question');
  const seen = new Set();
  const people = [];
  for (const answer of childrenOf(question.id, 'Question::Answer')) {
    if (effStatus(answer) !== 'active' || seen.has(answer.creatorId)) continue;
    seen.add(answer.creatorId);
    const person = db.people.get(answer.creatorId);
    if (person) people.push(person);
  }
  return listOk(ctx, people, serializePerson);
});

route('GET', '/{accountId}/questions/{questionId}/answers/by/{personId}', 'GetAnswersByPerson', async (ctx) => {
  const question = getRecOr404(ctx, ctx.params.questionId, 'Question');
  if (!db.people.has(ctx.params.personId)) throw err.notFound('Person not found.');
  const answers = childrenOf(question.id, 'Question::Answer')
    .filter((a) => effStatus(a) === 'active' && a.creatorId === ctx.params.personId && canSeeRec(ctx.person, a));
  return listOk(ctx, answers, serializeRec);
});

route('PUT', '/{accountId}/questions/{questionId}/notification_settings.json', 'UpdateQuestionNotificationSettings', async (ctx) => {
  const question = getRecOr404(ctx, ctx.params.questionId, 'Question');
  const body = await readJsonBody(ctx);
  const key = `${ctx.person.id}:${question.id}`;
  const settings = db.questionNotifSettings.get(key) || { responding: true, subscribed: false };
  const notifyOnAnswer = vOptBool(body, 'notify_on_answer');
  if (notifyOnAnswer !== undefined) {
    settings.subscribed = notifyOnAnswer;
    if (notifyOnAnswer) subscribe(question, ctx.person.id); else unsubscribe(question, ctx.person.id);
  }
  const digest = vOptBool(body, 'digest_include_unanswered');
  if (digest !== undefined) settings.digest_include_unanswered = digest;
  db.questionNotifSettings.set(key, settings);
  return ok({ responding: settings.responding !== false, subscribed: !!settings.subscribed });
});

route('GET', '/{accountId}/my/question_reminders.json', 'GetQuestionReminders', async (ctx) => {
  const reminders = [];
  for (const project of visibleProjects(ctx.person)) {
    const questionnaire = findDockTool(project, 'Questionnaire');
    if (!questionnaire || !questionnaire.enabled) continue;
    for (const question of childrenOf(questionnaire.id, 'Question')) {
      if (effStatus(question) !== 'active' || question.paused) continue;
      const sched = question.schedule || {};
      const next = new Date();
      next.setUTCHours(sched.hour ?? 9, sched.minute ?? 0, 0, 0);
      if (next.getTime() < Date.now()) next.setUTCDate(next.getUTCDate() + 1);
      reminders.push({
        reminder_id: question.id,
        remind_at: next.toISOString(),
        group_on: dateOnly(next.toISOString()),
        question: serializeRec(question),
      });
    }
  }
  return listOk(ctx, reminders, (r) => r);
});

/* ------------------------------------------------------------------------ *
 * Handlers: Email forwards (inbound email is out of scope; endpoints operate
 * on whatever forwards exist — the seed contains none)
 * ------------------------------------------------------------------------ */

route('GET', '/{accountId}/inboxes/{inboxId}', 'GetInbox', async (ctx) => {
  const inbox = getRecOr404(ctx, ctx.params.inboxId, 'Inbox');
  return ok(serializeRec(inbox));
});

route('GET', '/{accountId}/inboxes/{inboxId}/forwards.json', 'ListForwards', async (ctx) => {
  const inbox = getRecOr404(ctx, ctx.params.inboxId, 'Inbox');
  let forwards = childrenOf(inbox.id, 'Inbox::Forward').filter((f) => effStatus(f) === 'active' && canSeeRec(ctx.person, f));
  forwards = sortItems(ctx, forwards, { sort: 'created_at', direction: 'desc' });
  return listOk(ctx, forwards, serializeRec);
});

route('GET', '/{accountId}/inbox_forwards/{forwardId}', 'GetForward', async (ctx) => {
  const forward = getRecOr404(ctx, ctx.params.forwardId, 'Inbox::Forward');
  return ok(serializeRec(forward));
});

route('GET', '/{accountId}/inbox_forwards/{forwardId}/replies.json', 'ListForwardReplies', async (ctx) => {
  const forward = getRecOr404(ctx, ctx.params.forwardId, 'Inbox::Forward');
  const replies = childrenOf(forward.id, 'Inbox::Forward::Reply').filter((r) => effStatus(r) === 'active' && canSeeRec(ctx.person, r));
  return listOk(ctx, replies, serializeRec);
});

route('POST', '/{accountId}/inbox_forwards/{forwardId}/replies.json', 'CreateForwardReply', async (ctx) => {
  const forward = getRecOr404(ctx, ctx.params.forwardId, 'Inbox::Forward');
  requireActive(forward, 'forward');
  requireProjectMutable(ctx.person, projectOfRec(forward));
  const body = await readJsonBody(ctx);
  const reply = createRec({
    type: 'Inbox::Forward::Reply', bucketId: forward.bucketId, parentId: forward.id, creatorId: ctx.person.id,
    fields: { content: vRequireString(body, 'content') },
  });
  subscribe(forward, ctx.person.id);
  recordEvent(reply, ctx.person.id, 'created');
  return created(serializeRec(reply));
});

route('GET', '/{accountId}/inbox_forwards/{forwardId}/replies/{replyId}', 'GetForwardReply', async (ctx) => {
  const forward = getRecOr404(ctx, ctx.params.forwardId, 'Inbox::Forward');
  const reply = getRecOr404(ctx, ctx.params.replyId, 'Inbox::Forward::Reply');
  if (reply.parentId !== forward.id) throw err.notFound('Reply not found.');
  return ok(serializeRec(reply));
});

/* ------------------------------------------------------------------------ *
 * Handlers: Client features (read-only surface per the OpenAPI contract)
 * ------------------------------------------------------------------------ */

function clientRecordings(ctx, type) {
  const out = [];
  for (const project of visibleProjects(ctx.person)) {
    if (!project.clients_enabled) continue;
    for (const rec of descendantsOfProject(project.id)) {
      if (rec.type === type && effStatus(rec) === 'active' && canSeeRec(ctx.person, rec)) out.push(rec);
    }
  }
  return out;
}

function descendantsOfProject(projectId) {
  const out = [];
  for (const rec of db.recs.values()) {
    if (rec.bucketId === projectId) out.push(rec);
  }
  return out;
}

route('GET', '/{accountId}/client/approvals.json', 'ListClientApprovals', async (ctx) => {
  const approvals = sortItems(ctx, clientRecordings(ctx, 'Client::Approval'), { sort: 'created_at', direction: 'desc' });
  return listOk(ctx, approvals, serializeRec);
});

route('GET', '/{accountId}/client/approvals/{approvalId}', 'GetClientApproval', async (ctx) => {
  const approval = getRecOr404(ctx, ctx.params.approvalId, 'Client::Approval');
  return ok(serializeRec(approval));
});

route('GET', '/{accountId}/client/correspondences.json', 'ListClientCorrespondences', async (ctx) => {
  const rows = sortItems(ctx, clientRecordings(ctx, 'Client::Correspondence'), { sort: 'created_at', direction: 'desc' });
  return listOk(ctx, rows, serializeRec);
});

route('GET', '/{accountId}/client/correspondences/{correspondenceId}', 'GetClientCorrespondence', async (ctx) => {
  const row = getRecOr404(ctx, ctx.params.correspondenceId, 'Client::Correspondence');
  return ok(serializeRec(row));
});

route('GET', '/{accountId}/client/recordings/{recordingId}/replies.json', 'ListClientReplies', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.recordingId, ['Client::Approval', 'Client::Correspondence']);
  const replies = childrenOf(rec.id, 'Client::Reply').filter((r) => effStatus(r) === 'active');
  return listOk(ctx, replies, serializeRec);
});

route('GET', '/{accountId}/client/recordings/{recordingId}/replies/{replyId}', 'GetClientReply', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.recordingId, ['Client::Approval', 'Client::Correspondence']);
  const reply = getRecOr404(ctx, ctx.params.replyId, 'Client::Reply');
  if (reply.parentId !== rec.id) throw err.notFound('Reply not found.');
  return ok(serializeRec(reply));
});

/* ------------------------------------------------------------------------ *
 * Handlers: Gauges (project progress pill)
 * ------------------------------------------------------------------------ */

function gaugeOfProject(project, creatorId) {
  let gauge = [...db.recs.values()].find((r) => r.type === 'Gauge' && r.bucketId === project.id);
  if (!gauge) {
    gauge = createRec({
      type: 'Gauge', bucketId: project.id, parentId: null, creatorId: creatorId || project.creatorId,
      fields: { dockTitle: 'Gauge', enabled: false, description: '' },
    });
  }
  return gauge;
}

function refreshGaugeNeedles(gauge) {
  const needles = childrenOf(gauge.id, 'Gauge::Needle').filter((n) => effStatus(n) === 'active');
  gauge.lastNeedle = needles.length > 0 ? { color: needles[needles.length - 1].color, position: needles[needles.length - 1].needlePosition } : null;
  gauge.previousNeedle = needles.length > 1 ? { position: needles[needles.length - 2].needlePosition } : null;
}

route('PUT', '/{accountId}/projects/{projectId}/gauge.json', 'ToggleGauge', async (ctx) => {
  const project = getProjectOr404(ctx, ctx.params.projectId);
  requireEmployee(ctx, 'configure the gauge');
  const body = await readJsonBody(ctx);
  if (body.gauge === null || typeof body.gauge !== 'object' || typeof body.gauge.enabled !== 'boolean') {
    throw err.unprocessable('gauge.enabled must be a boolean.');
  }
  const gauge = gaugeOfProject(project, ctx.person.id);
  gauge.enabled = body.gauge.enabled;
  touch(gauge);
  return ok({});
});

route('GET', '/{accountId}/projects/{projectId}/gauge/needles.json', 'ListGaugeNeedles', async (ctx) => {
  const project = getProjectOr404(ctx, ctx.params.projectId);
  const gauge = gaugeOfProject(project);
  const needles = childrenOf(gauge.id, 'Gauge::Needle').filter((n) => effStatus(n) === 'active' && canSeeRec(ctx.person, n));
  return listOk(ctx, [...needles].reverse(), serializeRec);
});

route('POST', '/{accountId}/projects/{projectId}/gauge/needles.json', 'CreateGaugeNeedle', async (ctx) => {
  const project = getProjectOr404(ctx, ctx.params.projectId);
  requireEmployee(ctx, 'post gauge updates');
  requireProjectMutable(ctx.person, project);
  const body = await readJsonBody(ctx);
  const payload = body.gauge_needle;
  if (payload === null || typeof payload !== 'object') throw err.unprocessable('gauge_needle payload is required.');
  const position = parseIntStrict(payload.position);
  if (position === null || position < 0 || position > 100) {
    throw err.unprocessable('gauge_needle.position is required and must be an integer 0-100.');
  }
  const color = vOptString(payload, 'color', { max: 20 }) || 'green';
  if (!['green', 'yellow', 'red'].includes(color)) throw err.unprocessable("gauge_needle.color must be 'green', 'yellow' or 'red'.");
  const gauge = gaugeOfProject(project, ctx.person.id);
  const needle = createRec({
    type: 'Gauge::Needle', bucketId: project.id, parentId: gauge.id, creatorId: ctx.person.id,
    fields: {
      needlePosition: position,
      color,
      description: vOptString(payload, 'description', { max: 10_000 }) || '',
    },
  });
  refreshGaugeNeedles(gauge);
  subscribe(needle, ctx.person.id);
  for (const pid of vOptIdArray(body, 'subscriptions') || []) {
    if (db.people.has(pid)) subscribe(needle, pid);
  }
  recordEvent(needle, ctx.person.id, 'created');
  return created(serializeRec(needle));
});

route('GET', '/{accountId}/gauge_needles/{needleId}', 'GetGaugeNeedle', async (ctx) => {
  const needle = getRecOr404(ctx, ctx.params.needleId, 'Gauge::Needle');
  return ok(serializeRec(needle));
});

route('PUT', '/{accountId}/gauge_needles/{needleId}', 'UpdateGaugeNeedle', async (ctx) => {
  const needle = getRecOr404(ctx, ctx.params.needleId, 'Gauge::Needle');
  requireActive(needle, 'gauge update');
  const body = await readJsonBody(ctx);
  if (body.gauge_needle !== undefined) {
    if (body.gauge_needle === null || typeof body.gauge_needle !== 'object') {
      throw err.unprocessable('gauge_needle payload must be an object.');
    }
    const description = vOptString(body.gauge_needle, 'description', { max: 10_000 });
    if (description !== undefined) needle.description = description;
  }
  touch(needle);
  return ok(serializeRec(needle));
});

route('DELETE', '/{accountId}/gauge_needles/{needleId}', 'DestroyGaugeNeedle', async (ctx) => {
  const needle = getRecOr404(ctx, ctx.params.needleId, 'Gauge::Needle');
  if (!canModifyRec(ctx.person, needle)) throw err.forbidden('You do not have permission to delete this update.');
  needle.status = 'trashed';
  touch(needle);
  const gauge = db.recs.get(needle.parentId);
  if (gauge) refreshGaugeNeedles(gauge);
  return noContent();
});

route('GET', '/{accountId}/reports/gauges.json', 'ListGauges', async (ctx) => {
  const bucketParam = ctx.query.get('bucket_ids');
  let projects = visibleProjects(ctx.person);
  if (bucketParam) {
    const order = [];
    for (const piece of bucketParam.split(',')) {
      const id = parseIntStrict(piece.trim());
      if (id === null) throw err.badRequest('bucket_ids must be a comma-separated list of project ids.');
      order.push(id);
    }
    const byId = new Map(projects.map((p) => [p.id, p]));
    projects = order.map((id) => byId.get(id)).filter(Boolean);
  }
  const gauges = projects.map((p) => gaugeOfProject(p)).filter((g) => g.enabled);
  return listOk(ctx, gauges, serializeRec);
});

/* ------------------------------------------------------------------------ *
 * Handlers: Timesheet
 * ------------------------------------------------------------------------ */

function timesheetFilters(ctx) {
  const from = ctx.query.get('from');
  const to = ctx.query.get('to');
  if (from && !isDateString(from)) throw err.badRequest('from must be a YYYY-MM-DD date.');
  if (to && !isDateString(to)) throw err.badRequest('to must be a YYYY-MM-DD date.');
  let personId = null;
  const rawPerson = ctx.query.get('person_id');
  if (rawPerson) {
    personId = parseIntStrict(rawPerson);
    if (personId === null) throw err.badRequest('person_id must be an integer.');
  }
  return { from, to, personId };
}

function filterTimesheet(entries, { from, to, personId }) {
  return entries.filter((e) => {
    if (from && e.date < from) return false;
    if (to && e.date > to) return false;
    if (personId && e.personId !== personId) return false;
    return true;
  }).sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : a.id - b.id));
}

function timesheetEntriesIn(predicate) {
  const out = [];
  for (const rec of db.recs.values()) {
    if (rec.type === 'Timesheet::Entry' && effStatus(rec) === 'active' && predicate(rec)) out.push(rec);
  }
  return out;
}

route('POST', '/{accountId}/recordings/{recordingId}/timesheet/entries.json', 'CreateTimesheetEntry', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.recordingId);
  requireActive(rec);
  requireEmployee(ctx, 'track time');
  requireProjectMutable(ctx.person, projectOfRec(rec));
  const body = await readJsonBody(ctx);
  if (!isDateString(body.date)) throw err.unprocessable('date is required and must be a YYYY-MM-DD date.');
  const hours = body.hours;
  if (typeof hours !== 'string' && typeof hours !== 'number') throw err.unprocessable('hours is required.');
  const hoursNum = Number(hours);
  if (!Number.isFinite(hoursNum) || hoursNum <= 0 || hoursNum > 24) {
    throw err.unprocessable('hours must be a number between 0 and 24.');
  }
  let personId = ctx.person.id;
  if (body.person_id !== undefined && body.person_id !== null) {
    personId = parseIntStrict(body.person_id);
    if (personId === null || !db.people.has(personId)) throw err.unprocessable('person_id does not match a person.');
  }
  const entry = createRec({
    type: 'Timesheet::Entry', bucketId: rec.bucketId, parentId: rec.id, creatorId: ctx.person.id,
    fields: {
      date: body.date,
      hours: String(hours),
      description: vOptString(body, 'description', { max: 10_000 }) || '',
      personId,
    },
  });
  return created(serializeRec(entry));
});

route('GET', '/{accountId}/timesheet_entries/{entryId}', 'GetTimesheetEntry', async (ctx) => {
  const entry = getRecOr404(ctx, ctx.params.entryId, 'Timesheet::Entry');
  return ok(serializeRec(entry));
});

route('PUT', '/{accountId}/timesheet_entries/{entryId}', 'UpdateTimesheetEntry', async (ctx) => {
  const entry = getRecOr404(ctx, ctx.params.entryId, 'Timesheet::Entry');
  requireActive(entry, 'timesheet entry');
  if (entry.personId !== ctx.person.id && entry.creatorId !== ctx.person.id && !(ctx.person.admin || ctx.person.owner)) {
    throw err.forbidden('You can only edit your own time entries.');
  }
  const body = await readJsonBody(ctx);
  if (body.date !== undefined) {
    if (!isDateString(body.date)) throw err.unprocessable('date must be a YYYY-MM-DD date.');
    entry.date = body.date;
  }
  if (body.hours !== undefined) {
    const hoursNum = Number(body.hours);
    if (!Number.isFinite(hoursNum) || hoursNum <= 0 || hoursNum > 24) {
      throw err.unprocessable('hours must be a number between 0 and 24.');
    }
    entry.hours = String(body.hours);
  }
  const description = vOptString(body, 'description', { max: 10_000 });
  if (description !== undefined) entry.description = description;
  if (body.person_id !== undefined && body.person_id !== null) {
    const personId = parseIntStrict(body.person_id);
    if (personId === null || !db.people.has(personId)) throw err.unprocessable('person_id does not match a person.');
    entry.personId = personId;
  }
  touch(entry);
  return ok(serializeRec(entry));
});

route('GET', '/{accountId}/projects/{projectId}/timesheet.json', 'GetProjectTimesheet', async (ctx) => {
  const project = getProjectOr404(ctx, ctx.params.projectId);
  const filters = timesheetFilters(ctx);
  const entries = filterTimesheet(timesheetEntriesIn((e) => e.bucketId === project.id && canSeeRec(ctx.person, e)), filters);
  return listOk(ctx, entries, serializeRec);
});

route('GET', '/{accountId}/recordings/{recordingId}/timesheet.json', 'GetRecordingTimesheet', async (ctx) => {
  const rec = getRecOr404(ctx, ctx.params.recordingId);
  const filters = timesheetFilters(ctx);
  const entries = filterTimesheet(timesheetEntriesIn((e) => e.parentId === rec.id && canSeeRec(ctx.person, e)), filters);
  return listOk(ctx, entries, serializeRec);
});

route('GET', '/{accountId}/reports/timesheet.json', 'GetTimesheetReport', async (ctx) => {
  const filters = timesheetFilters(ctx);
  const visible = new Set(visibleProjects(ctx.person).map((p) => p.id));
  const entries = filterTimesheet(timesheetEntriesIn((e) => visible.has(e.bucketId) && canSeeRec(ctx.person, e)), filters);
  return listOk(ctx, entries, serializeRec);
});

/* ------------------------------------------------------------------------ *
 * Handlers: Search
 * ------------------------------------------------------------------------ */

const SEARCHABLE_TYPES = new Set([
  'Message', 'Comment', 'Todo', 'Todolist', 'Document', 'Upload', 'Vault',
  'Kanban::Card', 'Schedule::Entry', 'Question::Answer', 'Chat::Lines::Text',
]);

route('GET', '/{accountId}/search.json', 'Search', async (ctx) => {
  const q = ctx.query.get('q');
  if (!q) throw err.badRequest('q parameter is required.');
  const sort = vEnumQuery(ctx, 'sort', ['best_match', 'created_at'], 'best_match');
  const needle = q.toLowerCase();
  const visible = new Set(visibleProjects(ctx.person).map((p) => p.id));
  const scored = [];
  for (const rec of db.recs.values()) {
    if (!SEARCHABLE_TYPES.has(rec.type)) continue;
    if (!visible.has(rec.bucketId)) continue;
    if (effStatus(rec) !== 'active' || rec.status === 'drafted') continue;
    if (!canSeeRec(ctx.person, rec)) continue;
    const title = recTitle(rec).toLowerCase();
    const content = stripHtml(rec.content || rec.description || '').toLowerCase();
    let score = 0;
    if (title.includes(needle)) score += 2;
    if (content.includes(needle)) score += 1;
    if (score > 0) scored.push({ rec, score });
  }
  if (sort === 'created_at') {
    scored.sort((a, b) => (a.rec.created_at < b.rec.created_at ? 1 : -1));
  } else {
    scored.sort((a, b) => b.score - a.score || (a.rec.created_at < b.rec.created_at ? 1 : -1));
  }
  return listOk(ctx, scored.map((s) => s.rec), serializeSearchResult);
});

route('GET', '/{accountId}/searches/metadata.json', 'GetSearchMetadata', async (ctx) => {
  const projects = visibleProjects(ctx.person).map((p) => ({ id: p.id, name: p.name }));
  return ok({ projects });
});

/* ------------------------------------------------------------------------ *
 * Handlers: Reports & timelines
 * ------------------------------------------------------------------------ */

function assignedRecordings(personId, visibleSet, viewer) {
  const out = [];
  for (const rec of db.recs.values()) {
    if (rec.type !== 'Todo' && rec.type !== 'Kanban::Card' && rec.type !== 'Kanban::Step') continue;
    if (!visibleSet.has(rec.bucketId)) continue;
    if (effStatus(rec) !== 'active') continue;
    if (!(rec.assigneeIds || []).includes(personId)) continue;
    if (viewer && !canSeeRec(viewer, rec)) continue; // honor client visibility
    out.push(rec);
  }
  return out.sort((a, b) => {
    const ad = a.due_on || '9999-12-31';
    const bd = b.due_on || '9999-12-31';
    return ad < bd ? -1 : ad > bd ? 1 : a.id - b.id;
  });
}

function myVisibleSet(ctx) {
  return new Set(visibleProjects(ctx.person).map((p) => p.id));
}

route('GET', '/{accountId}/my/assignments.json', 'GetMyAssignments', async (ctx) => {
  const items = assignedRecordings(ctx.person.id, myVisibleSet(ctx), ctx.person).filter((r) => !r.completed);
  return ok({ priorities: [], non_priorities: items.map(serializeMyAssignment) });
});

route('GET', '/{accountId}/my/assignments/completed.json', 'GetMyCompletedAssignments', async (ctx) => {
  const items = assignedRecordings(ctx.person.id, myVisibleSet(ctx), ctx.person).filter((r) => r.completed)
    .sort((a, b) => ((a.completed_at || '') < (b.completed_at || '') ? 1 : -1));
  return listOk(ctx, items, serializeMyAssignment);
});

const DUE_SCOPES = ['overdue', 'due_today', 'due_tomorrow', 'due_later_this_week', 'due_next_week', 'due_later'];

route('GET', '/{accountId}/my/assignments/due.json', 'GetMyDueAssignments', async (ctx) => {
  const scopeRaw = ctx.query.get('scope');
  if (scopeRaw !== null && scopeRaw !== '' && !DUE_SCOPES.includes(scopeRaw)) {
    throw err.badRequest(`scope must be one of ${DUE_SCOPES.join(', ')}.`);
  }
  const scope = scopeRaw || null;
  const today = dateOnly(nowIso());
  const plus = (days) => dateOnly(new Date(Date.now() + days * 86_400_000).toISOString());
  const items = assignedRecordings(ctx.person.id, myVisibleSet(ctx), ctx.person).filter((r) => !r.completed && r.due_on).filter((r) => {
    switch (scope) {
      case 'overdue': return r.due_on < today;
      case 'due_today': return r.due_on === today;
      case 'due_tomorrow': return r.due_on === plus(1);
      case 'due_later_this_week': return r.due_on > plus(1) && r.due_on <= plus(7);
      case 'due_next_week': return r.due_on > plus(7) && r.due_on <= plus(14);
      case 'due_later': return r.due_on > plus(14);
      default: return true;
    }
  });
  return listOk(ctx, items, serializeMyAssignment);
});

route('GET', '/{accountId}/reports/todos/overdue.json', 'GetOverdueTodos', async (ctx) => {
  const visible = myVisibleSet(ctx);
  const today = Date.parse(dateOnly(nowIso()) + 'T00:00:00Z');
  const buckets = { under_a_week_late: [], over_a_week_late: [], over_a_month_late: [], over_three_months_late: [] };
  for (const rec of db.recs.values()) {
    if (rec.type !== 'Todo' || rec.completed || !rec.due_on) continue;
    if (!visible.has(rec.bucketId) || effStatus(rec) !== 'active' || !canSeeRec(ctx.person, rec)) continue;
    const late = Math.floor((today - Date.parse(rec.due_on + 'T00:00:00Z')) / 86_400_000);
    if (late <= 0) continue;
    if (late > 90) buckets.over_three_months_late.push(rec);
    else if (late > 30) buckets.over_a_month_late.push(rec);
    else if (late > 7) buckets.over_a_week_late.push(rec);
    else buckets.under_a_week_late.push(rec);
  }
  const byDue = (a, b) => (a.due_on < b.due_on ? -1 : 1);
  return ok({
    under_a_week_late: buckets.under_a_week_late.sort(byDue).map(serializeRec),
    over_a_week_late: buckets.over_a_week_late.sort(byDue).map(serializeRec),
    over_a_month_late: buckets.over_a_month_late.sort(byDue).map(serializeRec),
    over_three_months_late: buckets.over_three_months_late.sort(byDue).map(serializeRec),
  });
});

route('GET', '/{accountId}/reports/todos/assigned.json', 'ListAssignablePeople', async (ctx) => {
  const visible = myVisibleSet(ctx);
  const people = activePeople().filter((p) => assignedRecordings(p.id, visible, ctx.person).some((r) => !r.completed));
  return listOk(ctx, people, serializePerson);
});

route('GET', '/{accountId}/reports/todos/assigned/{personId}', 'GetAssignedTodos', async (ctx) => {
  const person = db.people.get(ctx.params.personId);
  if (!person || !person.active) throw err.notFound('Person not found.');
  const groupBy = vEnumQuery(ctx, 'group_by', ['bucket', 'date'], 'bucket');
  const items = assignedRecordings(person.id, myVisibleSet(ctx), ctx.person).filter((r) => r.type === 'Todo' && !r.completed);
  if (groupBy === 'bucket') items.sort((a, b) => a.bucketId - b.bucketId || a.id - b.id);
  return ok({ person: serializePerson(person), grouped_by: groupBy, todos: items.map(serializeRec) });
});

function visibleTimelineEvents(ctx, { bucketIds, personId } = {}) {
  const visible = bucketIds || myVisibleSet(ctx);
  const out = [];
  for (const [recId, events] of db.events) {
    const rec = db.recs.get(recId);
    if (!rec || !visible.has(rec.bucketId)) continue;
    if (!canSeeRec(ctx.person, rec)) continue;
    if (TOOL_TYPES.has(rec.type) || COLUMN_TYPES.has(rec.type)) continue;
    for (const event of events) {
      if (personId && event.creatorId !== personId) continue;
      out.push(event);
    }
  }
  out.sort((a, b) => (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : b.id - a.id));
  return out;
}

route('GET', '/{accountId}/reports/progress.json', 'GetProgressReport', async (ctx) => {
  const events = visibleTimelineEvents(ctx);
  return listOk(ctx, events, serializeTimelineEvent);
});

route('GET', '/{accountId}/projects/{projectId}/timeline.json', 'GetProjectTimeline', async (ctx) => {
  const project = getProjectOr404(ctx, ctx.params.projectId);
  const events = visibleTimelineEvents(ctx, { bucketIds: new Set([project.id]) });
  return listOk(ctx, events, serializeTimelineEvent);
});

route('GET', '/{accountId}/reports/users/progress/{personId}.json', 'GetPersonProgress', async (ctx) => {
  const person = db.people.get(ctx.params.personId);
  if (!person || !person.active) throw err.notFound('Person not found.');
  const events = visibleTimelineEvents(ctx, { personId: person.id });
  const page = pageParam(ctx);
  const per = CONFIG.pageSize;
  const slice = events.slice((page - 1) * per, page * per);
  const headers = { 'X-Total-Count': String(events.length) };
  if (page * per < events.length) {
    const nextQuery = new URLSearchParams(ctx.query);
    nextQuery.set('page', String(page + 1));
    headers.Link = `<${CONFIG.baseUrl}${ctx.path}?${nextQuery.toString()}>; rel="next"`;
  }
  return ok({ person: serializePerson(person), events: slice.map(serializeTimelineEvent).filter(Boolean) }, headers);
});

route('GET', '/{accountId}/reports/schedules/upcoming.json', 'GetUpcomingSchedule', async (ctx) => {
  const startRaw = ctx.query.get('window_starts_on');
  const endRaw = ctx.query.get('window_ends_on');
  if (startRaw && !isDateString(startRaw)) throw err.badRequest('window_starts_on must be a YYYY-MM-DD date.');
  if (endRaw && !isDateString(endRaw)) throw err.badRequest('window_ends_on must be a YYYY-MM-DD date.');
  const start = startRaw || dateOnly(nowIso());
  const end = endRaw || dateOnly(new Date(Date.now() + 14 * 86_400_000).toISOString());
  const visible = myVisibleSet(ctx);
  const entries = [];
  const assignables = [];
  for (const rec of db.recs.values()) {
    if (!visible.has(rec.bucketId) || effStatus(rec) !== 'active' || !canSeeRec(ctx.person, rec)) continue;
    if (rec.type === 'Schedule::Entry') {
      if (dateOnly(rec.ends_at) >= start && dateOnly(rec.starts_at) <= end) entries.push(rec);
    } else if ((rec.type === 'Todo' || rec.type === 'Kanban::Card') && rec.due_on && !rec.completed) {
      const schedule = findDockTool(db.projects.get(rec.bucketId), 'Schedule');
      const includeDue = !schedule || schedule.include_due_assignments !== false;
      if (includeDue && rec.due_on >= start && rec.due_on <= end) assignables.push(rec);
    }
  }
  entries.sort((a, b) => (a.starts_at < b.starts_at ? -1 : 1));
  assignables.sort((a, b) => (a.due_on < b.due_on ? -1 : 1));
  return ok({
    schedule_entries: entries.map(serializeRec),
    recurring_schedule_entry_occurrences: [], // recurrence not modeled
    assignables: assignables.map(serializeAssignable),
  });
});

/* ------------------------------------------------------------------------ *
 * Handlers: Webhooks
 * ------------------------------------------------------------------------ */

const WEBHOOK_TYPES_ALLOWED = new Set(['all', 'all_events', ...Object.keys(LISTABLE_RECORDING_TYPES), 'Chat::Lines::Text', 'Question', 'Client::Approval', 'Client::Correspondence', 'Inbox::Forward', 'Schedule::Entry', 'Todoset', 'Gauge::Needle']);

function isLoopbackHost(host) {
  const h = host.replace(/^\[|\]$/g, '');
  return h === 'localhost' || h === '::1' || h === '::ffff:127.0.0.1' || /^127\./.test(h) || host.endsWith('.localhost');
}

/**
 * Blocks webhook delivery to internal/reserved network ranges (SSRF defense):
 * private RFC 1918, carrier-grade NAT, link-local, and unique-local IPv6.
 * Loopback is intentionally exempt (documented dev carve-out) and can host
 * the local test receiver; set BASECAMP_WEBHOOK_ALLOW_PRIVATE=1 to lift this.
 */
function isBlockedWebhookHost(host) {
  const h = host.replace(/^\[|\]$/g, '').toLowerCase();
  const v4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(h);
  if (v4) {
    const [a, b] = [Number(v4[1]), Number(v4[2])];
    if (a === 10) return true;
    if (a === 172 && b >= 16 && b <= 31) return true;
    if (a === 192 && b === 168) return true;
    if (a === 169 && b === 254) return true; // link-local
    if (a === 100 && b >= 64 && b <= 127) return true; // CGNAT
    if (a === 0) return true;
    return false;
  }
  if (h === '::' || h.startsWith('fc') || h.startsWith('fd') || h.startsWith('fe80:')) return true; // ULA / link-local IPv6
  if (h.startsWith('::ffff:')) return isBlockedWebhookHost(h.slice(7));
  return false;
}

function validWebhookPayloadUrl(raw) {
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    throw err.badRequest('payload_url must be a valid URL.');
  }
  const localhost = isLoopbackHost(parsed.hostname);
  if (parsed.protocol !== 'https:' && !(parsed.protocol === 'http:' && localhost)) {
    throw err.badRequest('payload_url must use HTTPS (plain HTTP is allowed for localhost only).');
  }
  if (!localhost && !CONFIG.webhookAllowPrivate && isBlockedWebhookHost(parsed.hostname)) {
    throw err.badRequest('payload_url must not target a private, link-local, or reserved network address.');
  }
  return parsed.toString();
}

function validWebhookTypes(raw, { required = false } = {}) {
  if (raw === undefined) {
    if (required) throw err.badRequest('types is required (e.g. ["all"] or recording type names).');
    return undefined;
  }
  if (!Array.isArray(raw) || raw.length === 0 || raw.some((t) => typeof t !== 'string')) {
    throw err.badRequest('types must be a non-empty array of recording type names.');
  }
  for (const t of raw) {
    if (!WEBHOOK_TYPES_ALLOWED.has(t)) throw err.badRequest(`Unknown webhook type ${JSON.stringify(t)}.`);
  }
  return raw;
}

route('GET', '/{accountId}/buckets/{bucketId}/webhooks.json', 'ListWebhooks', async (ctx) => {
  const project = getProjectOr404(ctx, ctx.params.bucketId);
  return listOk(ctx, bucketWebhooks(project.id), serializeWebhook);
});

route('POST', '/{accountId}/buckets/{bucketId}/webhooks.json', 'CreateWebhook', async (ctx) => {
  const project = getProjectOr404(ctx, ctx.params.bucketId);
  requireEmployee(ctx, 'manage webhooks');
  if (bucketWebhooks(project.id).length >= MAX_WEBHOOKS_PER_BUCKET) {
    throw err.webhookLimit(`This project already has the maximum of ${MAX_WEBHOOKS_PER_BUCKET} webhooks.`);
  }
  const body = await readJsonBody(ctx);
  if (typeof body.payload_url !== 'string' || body.payload_url.length === 0) {
    throw err.badRequest('payload_url is required.');
  }
  const at = nowIso();
  const webhook = {
    id: nextId(),
    bucketId: project.id,
    payload_url: validWebhookPayloadUrl(body.payload_url),
    types: validWebhookTypes(body.types, { required: true }),
    active: body.active === undefined ? true : !!body.active,
    deliveries: [],
    created_at: at,
    updated_at: at,
  };
  db.webhooks.set(webhook.id, webhook);
  return created(serializeWebhook(webhook));
});

function getWebhookOr404(ctx) {
  const webhook = db.webhooks.get(ctx.params.webhookId);
  if (!webhook) throw err.notFound('Webhook not found.');
  getProjectOr404(ctx, webhook.bucketId);
  return webhook;
}

route('GET', '/{accountId}/webhooks/{webhookId}', 'GetWebhook', async (ctx) => ok(serializeWebhook(getWebhookOr404(ctx))));

route('PUT', '/{accountId}/webhooks/{webhookId}', 'UpdateWebhook', async (ctx) => {
  requireEmployee(ctx, 'manage webhooks');
  const webhook = getWebhookOr404(ctx);
  const body = await readJsonBody(ctx);
  if (body.payload_url !== undefined) {
    if (typeof body.payload_url !== 'string' || body.payload_url.length === 0) throw err.badRequest('payload_url must be a URL.');
    webhook.payload_url = validWebhookPayloadUrl(body.payload_url);
  }
  if (body.types !== undefined) webhook.types = validWebhookTypes(body.types);
  if (body.active !== undefined) {
    if (typeof body.active !== 'boolean') throw err.badRequest('active must be a boolean.');
    webhook.active = body.active;
  }
  webhook.updated_at = nowIso();
  return ok(serializeWebhook(webhook));
});

route('DELETE', '/{accountId}/webhooks/{webhookId}', 'DeleteWebhook', async (ctx) => {
  requireEmployee(ctx, 'manage webhooks');
  const webhook = getWebhookOr404(ctx);
  db.webhooks.delete(webhook.id);
  return noContent();
});

/* ------------------------------------------------------------------------ *
 * HTTP pipeline
 * ------------------------------------------------------------------------ */

function applyCors(res) {
  res.setHeader('Access-Control-Allow-Origin', CONFIG.corsOrigin);
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type, If-None-Match');
  res.setHeader('Access-Control-Expose-Headers', 'X-Request-Id, X-Total-Count, Link, ETag, Retry-After');
}

function sendJson(req, res, status, body, extraHeaders) {
  const headers = { ...(extraHeaders || {}) };
  let payload = null;
  if (body !== undefined) {
    payload = Buffer.from(JSON.stringify(body), 'utf8');
    headers['Content-Type'] = 'application/json; charset=utf-8';
    headers['Content-Length'] = String(payload.length);
    if (req.method === 'GET' || req.method === 'HEAD') {
      const etag = `W/"${sha256Hex(payload).slice(0, 32)}"`;
      headers.ETag = etag;
      const inm = req.headers['if-none-match'];
      if (inm && status === 200 && inm.split(',').map((s) => s.trim()).includes(etag)) {
        res.writeHead(304, { ETag: etag });
        res.end();
        return 304;
      }
    }
  }
  res.writeHead(status, headers);
  if (payload && req.method !== 'HEAD') res.end(payload); else res.end();
  return status;
}

function sendError(req, res, e) {
  const status = e instanceof ApiError ? e.status : 500;
  const body = e instanceof ApiError
    ? e.body()
    : { error: 'Internal Server Error', message: 'Something went wrong on our side.' };
  return sendJson(req, res, status, body, e instanceof ApiError ? e.headers : undefined);
}

function authenticate(req) {
  const header = req.headers.authorization;
  if (!header) throw err.unauthorized('Missing Authorization header.');
  const m = /^Bearer\s+(\S+)$/i.exec(header);
  if (!m) throw err.unauthorized('Authorization header must use the Bearer scheme.');
  const digest = sha256Hex(m[1]);
  const personId = db.tokens.get(digest);
  const person = personId ? db.people.get(personId) : null;
  if (!person || !person.active) throw err.unauthorized('Invalid or revoked access token.');
  return { person, digest };
}

/** Generated identicon-style avatar (additive endpoint; referenced by avatar_url). */
function avatarSvg(person) {
  const palette = ['#3b82f6', '#8b5cf6', '#ec4899', '#f97316', '#10b981', '#06b6d4', '#eab308', '#ef4444'];
  const color = palette[person.id % palette.length];
  const initials = person.name.split(/\s+/).map((w) => w[0]).filter(Boolean).slice(0, 2).join('').toUpperCase();
  return `<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">` +
    `<rect width="96" height="96" rx="48" fill="${color}"/>` +
    `<text x="48" y="60" font-family="system-ui, sans-serif" font-size="36" font-weight="600" fill="#fff" text-anchor="middle">${escapeHtml(initials)}</text></svg>`;
}

/** Routes that skip Bearer auth: health, service card, avatars, signed downloads, bot lines. */
async function handlePublic(req, res, method, segs, query) {
  if (method === 'GET' && segs.length === 1 && segs[0] === 'up') {
    sendJson(req, res, 200, { status: 'ok', version: VERSION, api_version: API_VERSION, uptime_s: Math.floor(process.uptime()) });
    return true;
  }
  if (method === 'GET' && segs.length === 0) {
    sendJson(req, res, 200, {
      name: 'basecamp5-api',
      version: VERSION,
      api_version: API_VERSION,
      account_id: CONFIG.accountId,
      docs: 'See the header comment in server.js. Authenticate with: Authorization: Bearer <token>.',
      health: `${CONFIG.baseUrl}/up`,
    });
    return true;
  }
  // GET /{accountId}/avatars/{personId}.svg
  if (method === 'GET' && segs.length === 3 && segs[0] === String(CONFIG.accountId) && segs[1] === 'avatars' && segs[2].endsWith('.svg')) {
    const personId = parseIntStrict(segs[2].slice(0, -4));
    const person = personId === null ? null : db.people.get(personId);
    if (!person) { sendError(req, res, err.notFound('Person not found.')); return true; }
    const svg = Buffer.from(avatarSvg(person), 'utf8');
    res.writeHead(200, { 'Content-Type': 'image/svg+xml', 'Content-Length': String(svg.length), 'Cache-Control': 'public, max-age=3600' });
    res.end(req.method === 'HEAD' ? undefined : svg);
    return true;
  }
  // GET /{accountId}/attachments/{sgid}/download/{filename} — sgid is a signed capability.
  if (method === 'GET' && segs.length === 5 && segs[0] === String(CONFIG.accountId) && segs[1] === 'attachments' && segs[3] === 'download') {
    const att = attachmentBySgid(decodeURIComponent(segs[2]));
    if (!att) { sendError(req, res, err.notFound('Attachment not found.')); return true; }
    res.writeHead(200, {
      'Content-Type': att.content_type,
      'Content-Length': String(att.byte_size),
      'Content-Disposition': `attachment; filename="${att.name.replace(/["\\]/g, '')}"`,
      'Cache-Control': 'private, max-age=3600',
    });
    res.end(req.method === 'HEAD' ? undefined : att.bytes);
    return true;
  }
  // POST /integrations/{botKey}/buckets/{bucketId}/chats/{chatId}/lines.json — chatbot ingestion.
  if (method === 'POST' && segs.length === 7 && segs[0] === 'integrations' && segs[2] === 'buckets' && segs[4] === 'chats' && segs[6] === 'lines.json') {
    const bot = [...db.chatbots.values()].find((b) => b.key === segs[1]);
    const bucketId = parseIntStrict(segs[3]);
    const chatId = parseIntStrict(segs[5]);
    if (!bot || bot.bucketId !== bucketId || bot.campfireId !== chatId) {
      sendError(req, res, err.notFound('Chatbot endpoint not found.'));
      return true;
    }
    try {
      const raw = await readBody(req, CONFIG.maxJsonBody);
      let content = null;
      try { content = JSON.parse(raw.toString('utf8')).content; } catch { /* fall through */ }
      if (typeof content !== 'string' || content.length === 0) throw err.unprocessable('content is required.');
      const chat = db.recs.get(chatId);
      if (!chat || effStatus(chat) !== 'active') throw err.notFound('Chat not found.');
      if (!bot.personId) {
        const botPerson = createPerson({ name: `${bot.service_name} (bot)`, email_address: `${bot.service_name}-${bot.id}@bots.invalid`, employee: false });
        botPerson.active = false; // hidden from people lists, still serializable
        bot.personId = botPerson.id;
      }
      const line = createChatLine(chat, bot.personId, content);
      sendJson(req, res, 201, serializeRec(line));
    } catch (e) {
      sendError(req, res, e);
    }
    return true;
  }
  // POST /__test__/reset — dev/test convenience (disabled in production by default).
  if (method === 'POST' && segs.length === 2 && segs[0] === '__test__' && segs[1] === 'reset') {
    if (!CONFIG.testEndpoints) { sendError(req, res, err.notFound()); return true; }
    try {
      const { person } = authenticate(req);
      if (!person.owner) throw err.forbidden('Only the account owner can reset the server.');
      resetAndSeed();
      sendJson(req, res, 200, {
        reset: true,
        tokens: db.seedTokens.map(({ person: p, token }) => ({ id: p.id, name: p.name, email: p.email_address, token })),
      });
    } catch (e) {
      sendError(req, res, e);
    }
    return true;
  }
  void query;
  return false;
}

async function handleRequest(req, res) {
  const startedAt = Date.now();
  const requestId = crypto.randomUUID();
  res.setHeader('X-Request-Id', requestId);
  applyCors(res);

  let status = 0;
  let opId = '-';
  let personId = '-';
  let pathname = req.url || '/';
  try {
    if (req.method === 'OPTIONS') {
      res.writeHead(204, { 'Access-Control-Max-Age': '600' });
      res.end();
      status = 204;
      return;
    }
    if ((req.url || '').length > 4096) throw err.badRequest('Request URL is too long.');
    let url;
    try {
      url = new URL(req.url, CONFIG.baseUrl);
    } catch {
      throw err.badRequest('Malformed request URL.');
    }
    try {
      pathname = decodeURIComponent(url.pathname);
    } catch {
      throw err.badRequest('Malformed percent-encoding in request path.');
    }
    const method = req.method === 'HEAD' ? 'GET' : req.method;
    const segs = pathname.split('/').filter(Boolean);

    if (await handlePublic(req, res, method, segs, url.searchParams)) {
      status = res.statusCode;
      return;
    }

    const { person, digest } = authenticate(req);
    personId = String(person.id);

    const retryAfter = checkRateLimit(digest);
    if (retryAfter !== null) throw err.tooManyRequests(retryAfter);

    const { hit, allowed } = findRoute(method, segs);
    if (!hit) {
      if (allowed.length > 0) throw err.methodNotAllowed(allowed);
      throw err.notFound('No such API endpoint. Paths follow the Basecamp OpenAPI templates.');
    }
    if (hit.params.accountId !== undefined && hit.params.accountId !== CONFIG.accountId) {
      throw err.notFound(`Unknown account ${hit.params.accountId}.`);
    }
    opId = hit.route.opId;
    const ctx = {
      req, res,
      method,
      path: pathname,
      query: url.searchParams,
      params: hit.params,
      person,
      requestId,
    };
    const result = await hit.route.handler(ctx);
    status = sendJson(req, res, result.status, result.body, result.headers);
  } catch (e) {
    if (!(e instanceof ApiError)) {
      log('error', 'unhandled error', { requestId, path: pathname, error: e.stack || String(e) });
    }
    if (!res.headersSent) {
      status = sendError(req, res, e);
    } else {
      res.destroy();
      status = res.statusCode;
    }
  } finally {
    if (!status) status = res.statusCode;
    log('info', 'request', {
      requestId,
      method: req.method,
      path: pathname.length > 200 ? pathname.slice(0, 200) + '…' : pathname,
      status,
      op: opId,
      person: personId,
      ms: Date.now() - startedAt,
    });
  }
}

/* ------------------------------------------------------------------------ *
 * Seed — INIT.md §3 "Launch the new website".
 *
 * Deterministic: every timestamp is a fixed offset from T (the seed epoch,
 * BASECAMP_SEED_EPOCH or boot time), IDs are allocated in a fixed order, and
 * no randomness is used for content. Events are written so Activity looks
 * real; readings are NOT fanned out to the account owner (their sidebar
 * starts clean).
 * ------------------------------------------------------------------------ */

/** Tiny PNG encoder (solid color) so image uploads are genuinely valid PNGs. */
function makePng(width, height, [r, g, b]) {
  const zlib = require('node:zlib');
  const crcTable = [];
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    crcTable[n] = c >>> 0;
  }
  const crc32 = (buf) => {
    let c = 0xffffffff;
    for (const byte of buf) c = crcTable[(c ^ byte) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const chunk = (type, data) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(body));
    return Buffer.concat([len, body, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; ihdr[9] = 2; // 8-bit RGB
  const row = Buffer.alloc(1 + width * 3);
  for (let x = 0; x < width; x += 1) {
    row[1 + x * 3] = r; row[2 + x * 3] = g; row[3 + x * 3] = b;
  }
  const raw = Buffer.concat(Array.from({ length: height }, () => row));
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', require('node:zlib').deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

function trafficChartSvg() {
  const bars = [42, 55, 48, 70, 86, 64, 95];
  const rects = bars.map((v, i) =>
    `<rect x="${20 + i * 46}" y="${140 - v}" width="32" height="${v}" rx="4" fill="#3b82f6"/>`).join('');
  return `<svg xmlns="http://www.w3.org/2000/svg" width="360" height="160" viewBox="0 0 360 160">` +
    `<rect width="360" height="160" fill="#f8fafc"/>${rects}` +
    `<text x="20" y="24" font-family="system-ui" font-size="13" fill="#334155">Visits this week (thousands)</text></svg>`;
}

function seedDatabase() {
  const T = CONFIG.seedEpoch ? new Date(CONFIG.seedEpoch) : new Date();
  const dayBase = Date.UTC(T.getUTCFullYear(), T.getUTCMonth(), T.getUTCDate());
  const at = (days, hours = 0, minutes = 0) =>
    new Date(dayBase + days * 86_400_000 + hours * 3_600_000 + minutes * 60_000).toISOString();
  const dateAt = (days) => at(days).slice(0, 10);

  db.account = {
    name: 'Skylight Studio',
    owner_name: 'Alex Rivera',
    created_at: at(-30, 9, 0),
    updated_at: at(-30, 9, 0),
    logo: null,
  };

  // Default message categories (INIT §7.3).
  for (const [name, icon] of [['Announcement', '📣'], ['FYI', '✨'], ['Heartbeat', '❤️'], ['Pitch', '💡'], ['Question', '👋']]) {
    const mt = { id: nextId(), name, icon, created_at: at(-30, 9, 0), updated_at: at(-30, 9, 0) };
    db.messageTypes.set(mt.id, mt);
  }
  const category = (name) => [...db.messageTypes.values()].find((mt) => mt.name === name).id;

  // --- People: the signed-in owner + the 8-person sample cast (INIT §3). ---
  const mkPerson = (attrs) => createPerson({ ...attrs, created_at: at(-30, 9, 0) });
  const owner = mkPerson({ name: 'Alex Rivera', email_address: 'alex@skylight.example', title: 'Founder', admin: true, owner: true, employee: true, company_name: 'Skylight Studio' });
  const maya = mkPerson({ name: 'Maya Chen', email_address: 'maya@skylight.example', title: 'Project lead', admin: true, employee: true, sample: true, company_name: 'Skylight Studio' });
  const sam = mkPerson({ name: 'Sam Whitaker', email_address: 'sam@skylight.example', title: 'Writer', employee: true, sample: true, company_name: 'Skylight Studio' });
  const omar = mkPerson({ name: 'Omar Haddad', email_address: 'omar@skylight.example', title: 'Designer', employee: true, sample: true, company_name: 'Skylight Studio' });
  const priya = mkPerson({ name: 'Priya Nair', email_address: 'priya@skylight.example', title: 'Developer', employee: true, sample: true, company_name: 'Skylight Studio' });
  const lena = mkPerson({ name: 'Lena Kowalski', email_address: 'lena@skylight.example', title: 'Marketing', employee: true, sample: true, company_name: 'Skylight Studio' });
  const diego = mkPerson({ name: 'Diego Ramos', email_address: 'diego@skylight.example', title: 'Community', employee: true, sample: true, company_name: 'Skylight Studio' });
  const grace = mkPerson({ name: 'Grace Okafor', email_address: 'grace@skylight.example', title: 'QA', employee: true, sample: true, company_name: 'Skylight Studio' });
  const felix = mkPerson({ name: 'Felix Berg', email_address: 'felix@skylight.example', title: 'Ops', employee: true, sample: true, company_name: 'Skylight Studio' });
  const cast = [maya, sam, omar, priya, lena, diego, grace, felix];
  db.seedTokens = [owner, ...cast].map((person) => ({ person, token: issueToken(person) }));

  // --- Project ------------------------------------------------------------
  const project = createProject({
    name: 'Launch the new website',
    description: '👋 This is a sample project that shows how a team works together here. Poke around, click into things — and delete this project whenever you’re ready.',
    creatorId: maya.id,
    all_access: true,
    admissions: 'employee',
    sample: true,
    access: cast.map((p) => p.id),
    created_at: at(-21, 9, 0),
  });
  const tools = {};
  for (const type of ['Message::Board', 'Todoset', 'Vault', 'Chat::Transcript', 'Schedule', 'Kanban::Board', 'Questionnaire', 'Inbox']) {
    tools[type] = findDockTool(project, type);
  }
  for (const type of ['Message::Board', 'Todoset', 'Vault', 'Chat::Transcript', 'Schedule', 'Kanban::Board']) {
    tools[type].enabled = true;
  }

  const mention = (person) =>
    `<bc-attachment content-type="application/vnd.basecamp.mention" data-mention-person-id="${person.id}">@${escapeHtml(person.name.split(' ')[0])}</bc-attachment>`;

  const seedBoost = (rec, person, content, when) => {
    const boost = { id: nextId(), content, created_at: when, boosterId: person.id, recordingId: rec.id, eventId: null };
    db.boosts.set(boost.id, boost);
    let list = db.recBoosts.get(rec.id);
    if (!list) { list = []; db.recBoosts.set(rec.id, list); }
    list.push(boost.id);
  };

  // Unlike the CreateComment handler, seeding a comment does not subscribe the
  // commenter — INIT §3 pins exact subscriber counts (kickoff: 5).
  const seedComment = (parent, person, content, when) => {
    const comment = createRec({
      type: 'Comment', bucketId: parent.bucketId, parentId: parent.id, creatorId: person.id,
      created_at: when, fields: { content },
    });
    recordEvent(comment, person.id, 'created', { at: when, excerptText: excerpt(content, 160) });
    return comment;
  };

  /* --- Message Board (5 messages, posted T−4d morning → afternoon) ------- */
  const board = tools['Message::Board'];

  const kickoff = createRec({
    type: 'Message', bucketId: project.id, parentId: board.id, creatorId: maya.id,
    created_at: at(-4, 9, 2),
    fields: {
      subject: 'Kickoff: the plan',
      categoryId: category('Announcement'),
      pinned: true,
      content:
        `<p>Team — the new site ships in seven weeks. Here’s how we’re splitting it up:</p>` +
        `<ul><li>${mention(sam)} owns the copy — homepage first, then the launch announcement.</li>` +
        `<li>${mention(omar)} owns design — logo refresh and page art.</li>` +
        `<li>${mention(priya)} owns the build — DNS, hosting, analytics.</li>` +
        `<li>${mention(lena)} owns the launch push — newsletter, social, press.</li></ul>` +
        `<p>Everything lives in this project. Check the to-dos for your name, and shout in the campfire if anything’s unclear. Let’s make it great. 🚀</p>`,
    },
  });
  for (const p of [maya, sam, omar, priya, lena]) subscribe(kickoff, p.id);
  recordEvent(kickoff, maya.id, 'created', { at: at(-4, 9, 2) });
  const clapAt = [[sam, 9, 10], [omar, 9, 14], [priya, 9, 21], [lena, 9, 30], [diego, 9, 44], [grace, 10, 2], [felix, 10, 15]];
  for (const [p, h, m] of clapAt) seedBoost(kickoff, p, '👏', at(-4, h, m));
  const kc1 = seedComment(kickoff, priya, '<p>On it. Staging environment is already live — I’ll post the URL in chat once DNS is sorted.</p>', at(-4, 10, 15));
  seedComment(kickoff, omar, '<p>Logo concepts land this week. Three directions, one clear winner (you’ll see).</p>', at(-4, 10, 40));
  const kc3 = seedComment(kickoff, lena, '<p>Booking the newsletter slot for launch week now. Draft copy needed by Thursday!</p>', at(-4, 11, 20));
  seedComment(kickoff, diego, '<p>Beta testers are primed — expect some very enthusiastic quotes soon.</p>', at(-4, 13, 5));
  seedBoost(kc1, maya, '💯', at(-4, 10, 22));
  seedBoost(kc3, sam, '👍', at(-4, 11, 31));

  const pitch = createRec({
    type: 'Message', bucketId: project.id, parentId: board.id, creatorId: sam.id,
    created_at: at(-4, 10, 30),
    fields: {
      subject: 'Pitch: trim the homepage copy',
      categoryId: category('Pitch'),
      pinned: false,
      content:
        `<p>Hot take: our homepage says too much. Nobody reads 600 words above the fold.</p>` +
        `<p>Proposal: one promise, three proof points, one button. Everything else moves to the About page. ` +
        `I drafted the trimmed version in Docs &amp; Files (“Homepage copy — draft”) — tear it apart.</p>`,
    },
  });
  subscribe(pitch, sam.id);
  recordEvent(pitch, sam.id, 'created', { at: at(-4, 10, 30) });
  seedComment(pitch, maya, '<p>Strong agree on the single promise. What’s the one sentence, though? That’s the whole game.</p>', at(-4, 10, 45));
  seedComment(pitch, omar, '<p>Less copy gives the art room to breathe. Voting yes.</p>', at(-4, 11, 5));
  seedComment(pitch, sam, '<p>Working sentence: “Your site, live in a week — without the agency runaround.”</p>', at(-4, 11, 30));
  seedComment(pitch, lena, '<p>That sentence works in the newsletter subject line too. Double win.</p>', at(-4, 12, 10));
  seedComment(pitch, diego, '<p>Beta testers literally said “too much text” in three separate calls. Receipts available.</p>', at(-4, 13, 40));
  seedComment(pitch, priya, '<p>Shorter page = faster page. The perf budget thanks you.</p>', at(-4, 14, 20));
  seedComment(pitch, sam, '<p>Consensus! Trimmed draft goes final tomorrow. Thanks all 🙏</p>', at(-4, 15, 0));

  const fyi = createRec({
    type: 'Message', bucketId: project.id, parentId: board.id, creatorId: diego.id,
    created_at: at(-4, 11, 45),
    fields: {
      subject: 'Nice note from a beta tester',
      categoryId: category('FYI'),
      pinned: false,
      content:
        `<p>From this morning’s feedback inbox:</p>` +
        `<blockquote>“I signed up expecting the usual clunky setup, and instead the whole thing just… worked. ` +
        `Whoever wrote the onboarding copy deserves a raise.” — Ines, beta cohort 2</blockquote>` +
        `<p>(Sam, that’s you. No pressure on the homepage.)</p>`,
    },
  });
  subscribe(fyi, diego.id);
  recordEvent(fyi, diego.id, 'created', { at: at(-4, 11, 45) });

  const chartSvg = Buffer.from(trafficChartSvg(), 'utf8');
  const chartAtt = storeAttachment('traffic-chart.svg', 'image/svg+xml', chartSvg);
  const heartbeat = createRec({
    type: 'Message', bucketId: project.id, parentId: board.id, creatorId: lena.id,
    created_at: at(-4, 14, 10),
    fields: {
      subject: 'Traffic this week',
      categoryId: category('Heartbeat'),
      pinned: false,
      content:
        `<p>Weekly numbers, one chart:</p>` +
        `<figure><img src="${attachmentDownloadUrl(chartAtt, 'traffic-chart.svg')}" alt="Bar chart: site visits per day, trending up to 95k" width="360" height="160"/>` +
        `<figcaption>Best day ever on Thursday — the beta invite tweet did the heavy lifting.</figcaption></figure>` +
        `<p>Takeaway: the audience is showing up before we’ve even launched. Let’s not keep them waiting.</p>`,
    },
  });
  subscribe(heartbeat, lena.id);
  recordEvent(heartbeat, lena.id, 'created', { at: at(-4, 14, 10) });

  const press = createRec({
    type: 'Message', bucketId: project.id, parentId: board.id, creatorId: maya.id,
    created_at: at(-4, 15, 40),
    fields: {
      subject: 'Local press opportunity',
      categoryId: category('Heartbeat'),
      pinned: false,
      content:
        `<p>The city business weekly wants a short piece on small teams shipping big redesigns. ` +
        `They’d run it launch week if we can give them 300 words and two screenshots by Friday.</p>` +
        `<p>Lena — worth folding into the press kit?</p>`,
    },
  });
  subscribe(press, maya.id);
  recordEvent(press, maya.id, 'created', { at: at(-4, 15, 40) });
  seedComment(press, sam, '<p>I can draft the 300 words right after the homepage copy is locked. Easy yes.</p>', at(-4, 16, 5));

  /* --- To-dos (2 lists) --------------------------------------------------- */
  const todoset = tools['Todoset'];

  const checklist = createRec({
    type: 'Todolist', bucketId: project.id, parentId: todoset.id, creatorId: maya.id,
    created_at: at(-18, 9, 30),
    fields: { name: 'Pre-launch checklist', description: '' },
  });
  subscribe(checklist, maya.id);
  recordEvent(checklist, maya.id, 'created', { at: at(-18, 9, 30) });

  const seedTodo = (list, creator, attrs, when) => {
    const todo = createRec({
      type: 'Todo', bucketId: project.id, parentId: list.id, creatorId: creator.id,
      created_at: when,
      fields: {
        content: attrs.content,
        description: attrs.description || '',
        assigneeIds: (attrs.assignees || []).map((p) => p.id),
        completionSubscriberIds: (attrs.notifyWhenDone || []).map((p) => p.id),
        completed: false,
        due_on: attrs.due_on || null,
        starts_on: attrs.starts_on || null,
      },
    });
    subscribe(todo, creator.id);
    for (const p of attrs.assignees || []) subscribe(todo, p.id);
    recordEvent(todo, creator.id, 'created', { at: when });
    if (attrs.completedBy) {
      todo.completed = true;
      todo.completed_at = attrs.completedAt;
      todo.completerId = attrs.completedBy.id;
      todo.updated_at = attrs.completedAt;
      recordEvent(todo, attrs.completedBy.id, 'completed', { at: attrs.completedAt });
    }
    return todo;
  };

  seedTodo(checklist, maya, {
    content: 'Set up analytics',
    description: '<p>One sub-step: verify events fire on staging before we ship.</p>',
    assignees: [priya],
  }, at(-18, 9, 35));
  const dnsTodo = seedTodo(checklist, maya, {
    content: 'Point DNS at the new host',
    assignees: [priya],
  }, at(-18, 9, 40));
  seedComment(dnsTodo, priya, '<p>TTL is lowered to 5 minutes — the actual cutover will be painless.</p>', at(-6, 15, 20));
  seedTodo(checklist, maya, {
    content: 'Register the new domain', assignees: [priya],
    completedBy: priya, completedAt: at(-10, 11, 0),
  }, at(-18, 9, 45));
  seedTodo(checklist, maya, {
    content: 'Choose hosting plan', assignees: [maya],
    completedBy: maya, completedAt: at(-9, 16, 30),
  }, at(-18, 9, 50));

  const contentList = createRec({
    type: 'Todolist', bucketId: project.id, parentId: todoset.id, creatorId: sam.id,
    created_at: at(-8, 10, 0),
    fields: { name: 'Launch week: content', description: '<p>Everything that needs to be written, designed and scheduled for launch week.</p>' },
  });
  subscribe(contentList, sam.id);
  recordEvent(contentList, sam.id, 'created', { at: at(-8, 10, 0) });

  seedTodo(contentList, sam, {
    content: 'Email newsletter',
    description: '<p>Subject line comes from the homepage promise. Send Tuesday 9am.</p>',
    assignees: [sam], notifyWhenDone: [maya], due_on: dateAt(3),
  }, at(-8, 10, 5));
  seedTodo(contentList, sam, { content: 'Write launch blog post', assignees: [sam] }, at(-8, 10, 8));
  seedTodo(contentList, lena, { content: 'Social posts for launch day', assignees: [lena] }, at(-8, 10, 12));
  seedTodo(contentList, sam, { content: 'Update team bios', assignees: [sam] }, at(-8, 10, 15));
  seedTodo(contentList, lena, { content: 'Press kit PDF', assignees: [lena, diego] }, at(-8, 10, 18));
  seedTodo(contentList, sam, {
    content: 'Draft announcement outline', assignees: [sam],
    completedBy: sam, completedAt: at(-2, 14, 45),
  }, at(-8, 10, 21));

  /* --- Card Table ---------------------------------------------------------
   * Triage renamed "Page ideas" (watchers: Maya, Omar, Grace) · Writing
   * (on-hold enabled, colored) · Design · Review (colored) · Ready ·
   * Done (5, completed T−1d…T) · Not now (2, moved T−4d). Cards T−20d.
   * ---------------------------------------------------------------------- */
  const boardTable = tools['Kanban::Board'];
  const lanes = childrenOf(boardTable.id);
  const triage = lanes.find((l) => l.type === 'Kanban::Triage');
  const notNow = lanes.find((l) => l.type === 'Kanban::NotNowColumn');
  const done = lanes.find((l) => l.type === 'Kanban::DoneColumn');
  triage.title = 'Page ideas';
  for (const p of [maya, omar, grace]) subscribe(triage, p.id);

  const mkColumn = (title, opts = {}) => {
    const column = createRec({
      type: 'Kanban::Column', bucketId: project.id, parentId: boardTable.id, creatorId: maya.id,
      created_at: at(-20, 9, 0),
      fields: { title, description: '', color: opts.color || null, onHoldId: null },
    });
    // user columns sit between Triage and the Not now / Done edge lanes
    const siblings = childIds(boardTable.id);
    siblings.splice(siblings.indexOf(column.id), 1);
    siblings.splice(siblings.indexOf(notNow.id), 0, column.id);
    return column;
  };
  const writing = mkColumn('Writing', { color: 'blue' });
  const design = mkColumn('Design');
  const review = mkColumn('Review', { color: 'orange' });
  const ready = mkColumn('Ready');
  void design;

  const writingHold = createRec({
    type: 'Kanban::OnHoldColumn', bucketId: project.id, parentId: writing.id, creatorId: maya.id,
    created_at: at(-20, 9, 5),
    fields: { title: 'Writing: On hold' },
  });
  writing.onHoldId = writingHold.id;

  let cardMinute = 0;
  const seedCard = (lane, attrs) => {
    cardMinute += 3;
    const when = attrs.created_at || at(-20, 10, cardMinute);
    const card = createRec({
      type: 'Kanban::Card', bucketId: project.id, parentId: lane.id, creatorId: maya.id,
      created_at: when,
      fields: {
        title: attrs.title,
        content: attrs.content || '',
        due_on: attrs.due_on || null,
        assigneeIds: (attrs.assignees || []).map((p) => p.id),
        completionSubscriberIds: [],
        completed: false,
      },
    });
    subscribe(card, maya.id);
    recordEvent(card, maya.id, 'created', { at: when });
    for (const [i, stepTitle] of (attrs.steps || []).entries()) {
      createRec({
        type: 'Kanban::Step', bucketId: project.id, parentId: card.id, creatorId: maya.id,
        created_at: at(-20, 10, cardMinute + i + 1),
        fields: { title: stepTitle, due_on: null, assigneeIds: [], completed: false },
      });
    }
    if (attrs.completedBy) {
      card.completed = true;
      card.completed_at = attrs.completedAt;
      card.completerId = attrs.completedBy.id;
      card.updated_at = attrs.completedAt;
      recordEvent(card, attrs.completedBy.id, 'completed', { at: attrs.completedAt });
    }
    if (attrs.movedAt) {
      card.updated_at = attrs.movedAt;
      recordEvent(card, maya.id, 'moved', { at: attrs.movedAt, notify: false, details: { to: lane.title } });
    }
    return card;
  };

  seedCard(triage, {
    title: 'Customer stories page',
    content: '<p>Real quotes, real names, real numbers. Two columns max.</p>',
    steps: ['Collect three customer quotes', 'Pick a layout direction'],
  });
  seedCard(triage, { title: 'Interactive pricing calculator', content: '<p>Sliders in, sticker shock out.</p>' });
  seedCard(writing, { title: 'About page rewrite', assignees: [sam] });
  const holdCard = seedCard(writingHold, {
    title: 'Case study: Fern & Co.',
    content: '<p>Waiting on sign-off from their comms team before we quote numbers.</p>',
    steps: ['Interview their ops lead', 'Draft the narrative', 'Get quote approval', 'Final pass + photos'],
  });
  seedComment(holdCard, sam, '<p>Their comms team says two weeks. Parking it here so it doesn’t haunt the board.</p>', at(-7, 9, 40));
  seedCard(review, { title: 'Homepage hero art', assignees: [omar, maya] });
  seedCard(ready, { title: 'Contact page' });

  const doneTitles = ['Navigation cleanup', '404 page', 'Font licensing', 'Color palette audit', 'Alt-text pass'];
  const doneBy = [omar, priya, sam, omar, grace];
  doneTitles.forEach((title, i) => {
    seedCard(done, {
      title,
      completedBy: doneBy[i],
      completedAt: at(-1, 10 + i * 2, 15),
    });
  });
  seedCard(notNow, { title: 'Podcast page', movedAt: at(-4, 16, 10) });
  seedCard(notNow, { title: 'Careers page', movedAt: at(-4, 16, 12) });

  /* --- Docs & Files (created T−3d) ---------------------------------------- */
  const vault = tools['Vault'];

  const homepageDoc = createRec({
    type: 'Document', bucketId: project.id, parentId: vault.id, creatorId: sam.id,
    created_at: at(-3, 9, 15),
    fields: {
      title: 'Homepage copy — draft',
      content:
        `<h1>Your site, live in a week</h1>` +
        `<p>Without the agency runaround. Pick a direction Monday, review Thursday, launch Friday.</p>` +
        `<h2>Why teams pick us</h2>` +
        `<p>We’ve shipped ninety-two sites for teams of two to two hundred. The process is boring on purpose: ` +
        `one call, one doc, one build.</p>` +
        `<h2>What you get</h2>` +
        `<p>Design, copy, and hosting handled. You keep the keys — export everything, any time.</p>` +
        `<p><em>[CTA button: See a live build →]</em></p>`,
    },
  });
  subscribe(homepageDoc, sam.id);
  recordEvent(homepageDoc, sam.id, 'created', { at: at(-3, 9, 15) });
  seedComment(homepageDoc, maya, '<p>This is the tightest version yet. Ship it to Omar for the hero pairing.</p>', at(-3, 11, 50));

  const logoPng = makePng(240, 160, [59, 130, 246]);
  const logoAtt = storeAttachment('logo-concepts.png', 'image/png', logoPng);
  logoAtt.width = 240;
  logoAtt.height = 160;
  const logoUpload = createRec({
    type: 'Upload', bucketId: project.id, parentId: vault.id, creatorId: omar.id,
    created_at: at(-3, 10, 5),
    fields: {
      attachmentId: logoAtt.id,
      base_name: 'logo-concepts',
      extension: 'png',
      description: '<p>Three directions: wordmark, monogram, and the weird one (pick the weird one).</p>',
    },
  });
  subscribe(logoUpload, omar.id);
  recordEvent(logoUpload, omar.id, 'created', { at: at(-3, 10, 5) });

  // INIT §3 lists a cloud link here; this API surface has no CloudFile type,
  // so the seed represents it as an Upload with a Google-Sheets content type.
  const calendarAtt = storeAttachment(
    'Content calendar',
    'application/vnd.google-apps.spreadsheet',
    Buffer.from('Cloud link: https://docs.google.com/spreadsheets/d/PLACEHOLDER-content-calendar\n', 'utf8'),
  );
  const calendarLink = createRec({
    type: 'Upload', bucketId: project.id, parentId: vault.id, creatorId: lena.id,
    created_at: at(-3, 10, 20),
    fields: {
      attachmentId: calendarAtt.id,
      base_name: 'Content calendar',
      extension: null,
      description: '<p>Google Sheet — one row per post, launch week tab first.</p>',
      color: 'yellow',
    },
  });
  subscribe(calendarLink, lena.id);
  recordEvent(calendarLink, lena.id, 'created', { at: at(-3, 10, 20) });

  /* --- Schedule ------------------------------------------------------------ */
  const schedule = tools['Schedule'];
  // Launch day: T+7w, snapped forward to a Saturday.
  const launchBase = new Date(dayBase + 49 * 86_400_000);
  const daysToSaturday = (6 - launchBase.getUTCDay() + 7) % 7;
  const launchDay = new Date(launchBase.getTime() + daysToSaturday * 86_400_000);
  const launchDate = launchDay.toISOString().slice(0, 10);

  const launchEntry = createRec({
    type: 'Schedule::Entry', bucketId: project.id, parentId: schedule.id, creatorId: maya.id,
    created_at: at(-4, 9, 50),
    fields: {
      summary: 'Launch day 🚀',
      description: '<p>Flip DNS in the morning, newsletter at 9, social at 10. Then pancakes.</p>',
      all_day: true,
      starts_at: `${launchDate}T00:00:00.000Z`,
      ends_at: `${launchDate}T23:59:59.000Z`,
      participantIds: [],
    },
  });
  subscribe(launchEntry, maya.id);
  recordEvent(launchEntry, maya.id, 'created', { at: at(-4, 9, 50) });

  const reviewCall = createRec({
    type: 'Schedule::Entry', bucketId: project.id, parentId: schedule.id, creatorId: maya.id,
    created_at: at(-3, 14, 0),
    fields: {
      summary: 'Content review call',
      description: '<p>Walk the homepage copy and hero art together. 60 minutes, cameras optional.</p>',
      all_day: false,
      starts_at: at(14, 10, 0),
      ends_at: at(14, 11, 0),
      participantIds: [maya.id, sam.id, omar.id],
    },
  });
  for (const p of [maya, sam, omar]) subscribe(reviewCall, p.id);
  recordEvent(reviewCall, maya.id, 'created', { at: at(-3, 14, 0) });

  /* --- Chat (one day, T−4d, 16 lines, 5 people) ---------------------------- */
  const chat = tools['Chat::Transcript'];
  const line = (person, h, m, content) => createChatLine(chat, person.id, content, { at: at(-4, h, m) });

  const l1 = line(diego, 8, 58, 'Morning all! Four days into launch prep and the beta list keeps growing 🎉');
  seedBoost(l1, grace, '🙌', at(-4, 9, 0));
  line(maya, 9, 4,
    'Been thinking about why this launch feels different. Last redesign we argued about pixels for a month; this time everyone owns a lane and the arguments are about the work.\n\nKeep it that way — disagree loudly in the threads, decide once, move on.');
  line(maya, 9, 5, 'Also — kickoff notes are up on the message board, with names next to every lane.');
  const l4 = line(grace, 9, 12, 'Found a lovely pattern library for form states: https://patterns.example.dev/forms — stealing the error styles');
  seedBoost(l4, diego, 'so good', at(-4, 9, 14));
  line(diego, 9, 15, 'Bookmarking that one.');
  line(priya, 11, 30, 'Heads up: deploying the staging build now. Two minutes of blips.');
  line(grace, 11, 32, 'Will the contact form work on staging, or does it still swallow submissions?');
  const l8 = line(priya, 11, 35, 'Fixed as of this build — submissions route to the test inbox. Try to break it.');
  seedBoost(l8, grace, '👍', at(-4, 11, 36));
  seedBoost(l8, diego, '🙏', at(-4, 11, 37));
  line(felix, 11, 40, 'Certs renewed and monitoring is green. You are cleared for takeoff. ✅');
  line(grace, 11, 41, 'Smoke test passed on staging — forms, nav, 404, all behaving.');
  line(maya, 12, 5, 'Great. Page speed numbers look solid too — 98 on mobile.');
  line(diego, 15, 20, 'Customer quote of the day: “I rebuilt our whole site during my kid’s nap. NAP TIME. What is this sorcery.”');
  line(maya, 15, 22, 'Awww! 💖');
  line(grace, 15, 23, 'That one goes on the testimonial wall.');
  line(diego, 15, 24, 'Already saved it 😄');
  line(maya, 16, 30, 'Good day, team. Same energy tomorrow.');

  log('info', 'seed complete', {
    epoch: new Date(dayBase).toISOString().slice(0, 10),
    people: db.people.size,
    recordings: db.recs.size,
    project: project.id,
  });
  return project;
}

/** Minimal seed when BASECAMP_SEED=0: account, owner, default categories. */
function seedMinimal() {
  const at = nowIso();
  db.account = { name: 'Basecamp Clone', owner_name: 'Account Owner', created_at: at, updated_at: at, logo: null };
  for (const [name, icon] of [['Announcement', '📣'], ['FYI', '✨'], ['Heartbeat', '❤️'], ['Pitch', '💡'], ['Question', '👋']]) {
    const mt = { id: nextId(), name, icon, created_at: at, updated_at: at };
    db.messageTypes.set(mt.id, mt);
  }
  const owner = createPerson({ name: 'Account Owner', email_address: 'owner@example.com', admin: true, owner: true, employee: true });
  db.seedTokens = [{ person: owner, token: issueToken(owner) }];
}

function resetAndSeed() {
  db = freshDb();
  if (CONFIG.seed) seedDatabase(); else seedMinimal();
}

/* ------------------------------------------------------------------------ *
 * Boot
 * ------------------------------------------------------------------------ */

function printBootBanner(server) {
  const addr = server.address();
  log('info', 'basecamp5 api listening', {
    host: addr.address,
    port: addr.port,
    account: CONFIG.accountId,
    base_url: CONFIG.baseUrl,
    env: CONFIG.env,
    routes: ROUTES.length,
    seed: CONFIG.seed,
  });
  if (CONFIG.printTokens && db.seedTokens.length > 0) {
    const lines = ['', 'Access tokens (stored as digests only; set BASECAMP_PRINT_TOKENS=0 to hide):'];
    for (const { person, token } of db.seedTokens) {
      const role = person.owner ? 'owner' : person.admin ? 'admin' : person.sample ? 'sample' : 'member';
      lines.push(`  ${person.name.padEnd(14)} <${person.email_address}>`.padEnd(48) + ` [${role}]  ${token}`);
    }
    lines.push('', `Try:  curl -H 'Authorization: Bearer <token>' ${CONFIG.baseUrl}/${CONFIG.accountId}/projects.json`, '');
    process.stderr.write(lines.join('\n') + '\n');
  }
}

function main() {
  resetAndSeed();
  const server = http.createServer((req, res) => {
    handleRequest(req, res).catch((e) => {
      log('error', 'request pipeline crashed', { error: e.stack || String(e) });
      if (!res.headersSent) sendError(req, res, e);
    });
  });
  server.requestTimeout = 65_000;
  server.keepAliveTimeout = 5_000;
  server.maxRequestsPerSocket = 0;
  server.listen(CONFIG.port, CONFIG.host, () => printBootBanner(server));
  server.on('error', (e) => {
    log('error', 'server error', { error: e.message });
    process.exit(1);
  });

  let shuttingDown = false;
  const shutdown = (signal) => {
    if (shuttingDown) return;
    shuttingDown = true;
    log('info', 'shutting down', { signal });
    server.close(() => {
      log('info', 'shutdown complete');
      process.exit(0);
    });
    setTimeout(() => {
      log('warn', 'forcing shutdown after grace period');
      server.closeAllConnections();
      process.exit(0);
    }, CONFIG.shutdownGraceMs).unref();
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
}

if (require.main === module) main();

module.exports = { CONFIG, main, resetAndSeed };
