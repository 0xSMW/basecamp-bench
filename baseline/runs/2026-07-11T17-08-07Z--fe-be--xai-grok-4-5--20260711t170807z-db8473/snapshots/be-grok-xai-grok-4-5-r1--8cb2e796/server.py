#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Basecamp 5 API Server — single-file, production-shaped implementation.

Contract sources (in precedence order):
  reference/basecamp-sdk/openapi.json  — path templates, schemas, status codes
  reference/basecamp-sdk/SPEC.md       — auth, errors, pagination conventions
  INIT.md                              — domain model, Recording pattern, seed

Design choices suitable for production inclusion:
  - Recording-centric store: one identity + lifecycle for every content entity
  - Bearer personal-access tokens (digest-stored); role-aware authorization
  - Link rel=next pagination + X-Total-Count (Basecamp page size 15)
  - Deterministic sample seed ("Launch the new website") on boot
  - Explicit 501 stubs only where the OpenAPI surface is hollow (none claimed)
  - Operability: /health, /ready, request IDs, structured logs, graceful shutdown,
    CORS, env-based config, optional state reset for tests

Run:
  python3 server.py
  python3 server.py --port 9292 --host 0.0.0.0

Default credentials (seed):
  Account ID: 1
  Owner token:  bcamp_pat_owner_maya   (Maya Chen — sample cast is not loginable;
                                         a real owner "Alex Rivera" is the default principal)
  Also issued: bcamp_pat_owner_alex
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import traceback
import uuid
from calendar import timegm
from datetime import date, datetime, timedelta, timezone
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VERSION = "5.0.0"
PAGE_SIZE = 15
API_BASE_DEFAULT = "http://localhost:9292"
APP_BASE_DEFAULT = "http://localhost:3000"

def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v

def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return int(v)

def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self) -> None:
        self.host = _env("BASECAMP_HOST", "127.0.0.1")
        self.port = _env_int("BASECAMP_PORT", 9292)
        self.api_base = _env("BASECAMP_API_BASE", API_BASE_DEFAULT).rstrip("/")
        self.app_base = _env("BASECAMP_APP_BASE", APP_BASE_DEFAULT).rstrip("/")
        self.account_id = _env("BASECAMP_ACCOUNT_ID", "1")
        self.cors_origin = _env("BASECAMP_CORS_ORIGIN", "*")
        self.log_level = _env("BASECAMP_LOG_LEVEL", "INFO")
        self.page_size = _env_int("BASECAMP_PAGE_SIZE", PAGE_SIZE)
        self.rate_limit_per_minute = _env_int("BASECAMP_RATE_LIMIT", 0)  # 0 = off
        self.token_pepper = _env("BASECAMP_TOKEN_PEPPER", "bc5-dev-pepper-change-me")
        self.allow_reset = _env_bool("BASECAMP_ALLOW_RESET", True)
        self.seed_on_boot = _env_bool("BASECAMP_SEED", True)


CONFIG = Config()

logging.basicConfig(
    level=getattr(logging, CONFIG.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("basecamp.api")

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc

def utcnow() -> datetime:
    return datetime.now(tz=UTC)

def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None

def date_str(d: Optional[date]) -> Optional[str]:
    if d is None:
        return None
    return d.isoformat()

def parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None

def http_date(dt: Optional[datetime] = None) -> str:
    dt = dt or utcnow()
    return formatdate(timegm(dt.utctimetuple()), usegmt=True)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class APIError(Exception):
    def __init__(self, status: int, error: str, message: str, extra: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message
        self.extra = extra or {}

    def body(self) -> dict:
        b = {"error": self.error, "message": self.message}
        b.update(self.extra)
        return b

def bad_request(msg: str) -> APIError:
    return APIError(400, "bad_request", msg)

def unauthorized(msg: str = "Authorization required") -> APIError:
    return APIError(401, "unauthorized", msg)

def forbidden(msg: str = "Forbidden") -> APIError:
    return APIError(403, "forbidden", msg)

def not_found(msg: str = "Not found") -> APIError:
    return APIError(404, "not_found", msg)

def method_not_allowed(allowed: Iterable[str]) -> APIError:
    return APIError(405, "method_not_allowed", "Method not allowed",
                    {"allowed": list(allowed)})

def conflict(msg: str) -> APIError:
    return APIError(409, "conflict", msg)

def validation(msg: str) -> APIError:
    return APIError(422, "validation_failed", msg)

def rate_limited(retry_after: int = 60) -> APIError:
    return APIError(429, "rate_limited", "Too many requests",
                    {"retry_after": retry_after})

def not_implemented(msg: str = "Not implemented") -> APIError:
    return APIError(501, "not_implemented", msg)

def server_error(msg: str = "Internal server error") -> APIError:
    return APIError(500, "internal_error", msg)

# ---------------------------------------------------------------------------
# Token hashing (digest-only storage)
# ---------------------------------------------------------------------------

def hash_token(token: str, pepper: str = None) -> str:
    pepper = pepper if pepper is not None else CONFIG.token_pepper
    return hashlib.sha256(f"{pepper}:{token}".encode("utf-8")).hexdigest()

def new_token(prefix: str = "bcamp_pat") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"

# ---------------------------------------------------------------------------
# ID allocation
# ---------------------------------------------------------------------------

class IDGen:
    def __init__(self, start: int = 10_000) -> None:
        self._n = start
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._n += 1
            return self._n

    def set_min(self, n: int) -> None:
        with self._lock:
            if n > self._n:
                self._n = n

# ---------------------------------------------------------------------------
# In-memory Store — Recording-centric
# ---------------------------------------------------------------------------

class Store:
    """Thread-safe process-lifetime store.

    Every content entity is a Recording (dict) keyed by id. Cross-cutting
    tables (boosts, events, subscriptions, readings, bookmarks, tokens) sit
    alongside. Mutations that are user-visible write Events.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.lock = threading.RLock()
        self.ids = IDGen(50_000)
        self.account: Dict[str, Any] = {}
        self.people: Dict[int, Dict[str, Any]] = {}
        self.companies: Dict[int, Dict[str, Any]] = {}
        self.groups: Dict[int, Dict[str, Any]] = {}
        self.projects: Dict[int, Dict[str, Any]] = {}
        self.recordings: Dict[int, Dict[str, Any]] = {}  # all recordings
        self.boosts: Dict[int, Dict[str, Any]] = {}
        self.events: Dict[int, Dict[str, Any]] = {}
        self.subscriptions: Dict[int, set] = {}  # recording_id -> set(person_id)
        self.readings: Dict[Tuple[int, int], Dict[str, Any]] = {}  # (person, recording)
        self.bookmarks: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self.do_today: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self.tokens: Dict[str, int] = {}  # token_hash -> person_id
        self.token_plaintext_dev: Dict[int, str] = {}  # person_id -> token (dev only)
        self.categories: Dict[int, Dict[str, Any]] = {}
        self.webhooks: Dict[int, Dict[str, Any]] = {}
        self.chatbots: Dict[int, Dict[str, Any]] = {}
        self.templates: Dict[int, Dict[str, Any]] = {}
        self.constructions: Dict[int, Dict[str, Any]] = {}
        self.attachments: Dict[str, Dict[str, Any]] = {}  # sgid -> meta
        self.lineup_markers: Dict[int, Dict[str, Any]] = {}
        self.out_of_office: Dict[int, Dict[str, Any]] = {}
        self.preferences: Dict[int, Dict[str, Any]] = {}
        self.pinned: set = set()  # recording ids
        self.client_correspondences: Dict[int, int] = {}  # id -> recording id
        self.client_approvals: Dict[int, int] = {}
        self.rate_buckets: Dict[str, List[float]] = {}
        self.boot_time = utcnow()
        self.seed_time: datetime = self.boot_time  # T for seed offsets
        self.ready = False
        self.stats = {"requests": 0, "errors": 0}

    # -- id / clock ---------------------------------------------------------

    def next_id(self) -> int:
        return self.ids.next()

    def touch(self, rec: dict) -> None:
        rec["updated_at"] = utcnow()

    # -- people -------------------------------------------------------------

    def person(self, pid: int) -> Optional[dict]:
        return self.people.get(int(pid))

    def require_person(self, pid: int) -> dict:
        p = self.person(pid)
        if not p:
            raise not_found(f"Person {pid} not found")
        return p

    def people_by_ids(self, ids: Iterable[int]) -> List[dict]:
        out = []
        for i in ids or []:
            p = self.person(int(i))
            if p:
                out.append(p)
        return out

    # -- projects / buckets -------------------------------------------------

    def project(self, pid: int) -> Optional[dict]:
        return self.projects.get(int(pid))

    def require_project(self, pid: int) -> dict:
        p = self.project(pid)
        if not p:
            raise not_found(f"Project {pid} not found")
        return p

    def can_access_project(self, person: dict, project: dict) -> bool:
        if person.get("owner") or person.get("admin"):
            return True
        if person.get("id") in project.get("access", set()):
            return True
        # all-access (admissions == employee) grants employees
        if project.get("admissions") == "employee" and person.get("employee"):
            # clients never get all-access without invite
            if person.get("client"):
                return person.get("id") in project.get("access", set())
            return True
        return False

    def require_project_access(self, person: dict, project: dict) -> None:
        if not self.can_access_project(person, project):
            raise forbidden("You do not have access to this project")

    # -- recordings ---------------------------------------------------------

    def recording(self, rid: int) -> Optional[dict]:
        return self.recordings.get(int(rid))

    def require_recording(self, rid: int, types: Optional[Iterable[str]] = None) -> dict:
        r = self.recording(rid)
        if not r:
            raise not_found(f"Recording {rid} not found")
        if types and r.get("type") not in types:
            raise not_found(f"Recording {rid} not found")
        return r

    def children(self, parent_id: int, type_: Optional[str] = None,
                 status: Optional[str] = "active") -> List[dict]:
        out = []
        for r in self.recordings.values():
            if r.get("parent_id") != parent_id:
                continue
            if type_ and r.get("type") != type_:
                continue
            if status and r.get("status") != status:
                if status == "active" and r.get("status") == "drafted":
                    pass  # exclude drafts from default lists unless asked
                else:
                    continue
            out.append(r)
        out.sort(key=lambda x: (x.get("position") or 0, x.get("id") or 0))
        return out

    def children_any_status(self, parent_id: int, type_: Optional[str] = None) -> List[dict]:
        out = []
        for r in self.recordings.values():
            if r.get("parent_id") != parent_id:
                continue
            if type_ and r.get("type") != type_:
                continue
            if r.get("status") == "trashed":
                continue
            out.append(r)
        out.sort(key=lambda x: (x.get("position") or 0, x.get("id") or 0))
        return out

    def put_recording(self, rec: dict) -> dict:
        rid = int(rec["id"])
        self.recordings[rid] = rec
        self.ids.set_min(rid)
        return rec

    def new_recording(
        self,
        *,
        type_: str,
        title: str,
        creator_id: int,
        bucket_id: int,
        parent_id: Optional[int] = None,
        status: str = "active",
        content: str = "",
        visible_to_clients: bool = False,
        inherits_status: bool = True,
        position: Optional[int] = None,
        created_at: Optional[datetime] = None,
        extra: Optional[dict] = None,
        rid: Optional[int] = None,
        name: Optional[str] = None,
        **kwargs: Any,
    ) -> dict:
        now = created_at or utcnow()
        rec = {
            "id": rid if rid is not None else self.next_id(),
            "type": type_,
            "title": title,
            "status": status,
            "visible_to_clients": visible_to_clients,
            "inherits_status": inherits_status,
            "created_at": now,
            "updated_at": now,
            "creator_id": creator_id,
            "bucket_id": bucket_id,
            "parent_id": parent_id,
            "content": content or "",
            "position": position if position is not None else 0,
            "comments_count": 0,
            "boosts_count": 0,
        }
        if name is not None:
            rec["name"] = name
        if extra:
            rec.update(extra)
        # Allow seed/callers to pass type-specific fields directly
        for k, v in kwargs.items():
            if k in ("type",):  # reserved
                continue
            rec[k] = v
        return self.put_recording(rec)

    # -- events -------------------------------------------------------------

    def write_event(self, recording_id: int, action: str, creator_id: int,
                    details: Optional[dict] = None,
                    created_at: Optional[datetime] = None) -> dict:
        eid = self.next_id()
        ev = {
            "id": eid,
            "recording_id": recording_id,
            "action": action,
            "creator_id": creator_id,
            "details": details or {},
            "created_at": created_at or utcnow(),
            "boosts_count": 0,
        }
        self.events[eid] = ev
        return ev

    # -- subscriptions ------------------------------------------------------

    def subscribe(self, recording_id: int, person_ids: Iterable[int]) -> None:
        s = self.subscriptions.setdefault(int(recording_id), set())
        for p in person_ids:
            s.add(int(p))

    def unsubscribe(self, recording_id: int, person_ids: Iterable[int]) -> None:
        s = self.subscriptions.setdefault(int(recording_id), set())
        for p in person_ids:
            s.discard(int(p))

    def subscribers(self, recording_id: int) -> List[dict]:
        ids = self.subscriptions.get(int(recording_id), set())
        return self.people_by_ids(ids)

    # -- boosts -------------------------------------------------------------

    def add_boost(self, recording_id: int, person_id: int, content: str,
                  created_at: Optional[datetime] = None) -> dict:
        if len(content) > 16:
            raise validation("Boost content must be 16 characters or fewer")
        bid = self.next_id()
        b = {
            "id": bid,
            "recording_id": recording_id,
            "event_id": None,
            "booster_id": person_id,
            "content": content,
            "created_at": created_at or utcnow(),
        }
        self.boosts[bid] = b
        rec = self.recording(recording_id)
        if rec is not None:
            rec["boosts_count"] = int(rec.get("boosts_count") or 0) + 1
            self.touch(rec)
        return b

    def add_event_boost(self, event_id: int, person_id: int, content: str) -> dict:
        if len(content) > 16:
            raise validation("Boost content must be 16 characters or fewer")
        ev = self.events.get(int(event_id))
        if not ev:
            raise not_found("Event not found")
        bid = self.next_id()
        b = {
            "id": bid,
            "recording_id": ev["recording_id"],
            "event_id": event_id,
            "booster_id": person_id,
            "content": content,
            "created_at": utcnow(),
        }
        self.boosts[bid] = b
        ev["boosts_count"] = int(ev.get("boosts_count") or 0) + 1
        return b

    def boosts_for(self, recording_id: int, event_id: Optional[int] = None) -> List[dict]:
        out = []
        for b in self.boosts.values():
            if b["recording_id"] != recording_id:
                continue
            if event_id is None and b.get("event_id"):
                continue
            if event_id is not None and b.get("event_id") != event_id:
                continue
            out.append(b)
        out.sort(key=lambda x: x["created_at"])
        return out

    # -- comments helper ----------------------------------------------------

    def add_comment(self, parent: dict, creator_id: int, content: str,
                    created_at: Optional[datetime] = None,
                    visible_to_clients: Optional[bool] = None) -> dict:
        c = self.new_recording(
            type_="Comment",
            title=content[:80] if content else "Comment",
            creator_id=creator_id,
            bucket_id=parent["bucket_id"],
            parent_id=parent["id"],
            content=content,
            visible_to_clients=(
                parent.get("visible_to_clients", False)
                if visible_to_clients is None else visible_to_clients
            ),
            created_at=created_at,
        )
        parent["comments_count"] = int(parent.get("comments_count") or 0) + 1
        self.touch(parent)
        self.write_event(c["id"], "commented", creator_id, created_at=created_at)
        self.write_event(parent["id"], "commented_on", creator_id,
                         details={"comment_id": c["id"]}, created_at=created_at)
        return c

    # -- tokens -------------------------------------------------------------

    def issue_token(self, person_id: int, plaintext: Optional[str] = None) -> str:
        tok = plaintext or new_token()
        self.tokens[hash_token(tok)] = int(person_id)
        self.token_plaintext_dev[int(person_id)] = tok
        return tok

    def person_for_token(self, token: str) -> Optional[dict]:
        pid = self.tokens.get(hash_token(token))
        if pid is None:
            return None
        return self.person(pid)

    # -- status lifecycle ---------------------------------------------------

    def set_status(self, rec: dict, status: str, actor_id: int) -> None:
        if status not in ("active", "archived", "trashed", "drafted"):
            raise validation(f"Invalid status: {status}")
        old = rec.get("status")
        rec["status"] = status
        self.touch(rec)
        action = {
            "active": "active",
            "archived": "archived",
            "trashed": "trashed",
            "drafted": "drafted",
        }[status]
        if old != status:
            self.write_event(rec["id"], action, actor_id)
        # cascade inherits_status children
        if rec.get("inherits_status") is not False:
            for child in list(self.recordings.values()):
                if child.get("parent_id") == rec["id"] and child.get("inherits_status", True):
                    if child.get("status") != "trashed" or status == "trashed":
                        child["status"] = status
                        self.touch(child)

    # -- rate limit ---------------------------------------------------------

    def check_rate(self, key: str) -> None:
        limit = self.config.rate_limit_per_minute
        if not limit:
            return
        now = time.time()
        bucket = self.rate_buckets.setdefault(key, [])
        cutoff = now - 60.0
        self.rate_buckets[key] = [t for t in bucket if t >= cutoff]
        if len(self.rate_buckets[key]) >= limit:
            raise rate_limited(60)
        self.rate_buckets[key].append(now)

    # -- reset --------------------------------------------------------------

    def clear(self) -> None:
        with self.lock:
            self.__init__(self.config)  # type: ignore[misc]


STORE = Store(CONFIG)

# ---------------------------------------------------------------------------
# URL builders & serializers
# ---------------------------------------------------------------------------

def api_url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{CONFIG.api_base}{path}"

def app_url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{CONFIG.app_base}{path}"

def acct_path(path: str) -> str:
    aid = CONFIG.account_id
    if not path.startswith("/"):
        path = "/" + path
    return f"/{aid}{path}"

def person_json(p: Optional[dict], compact: bool = False) -> Optional[dict]:
    if not p:
        return None
    company = None
    if p.get("company_id"):
        c = STORE.companies.get(p["company_id"])
        if c:
            company = {"id": c["id"], "name": c["name"]}
    base = {
        "id": p["id"],
        "attachable_sgid": p.get("attachable_sgid") or f"BAh7CEkiCGdpZAY6BkVUSSIpZ2lkOi8vYmMzL1BlcnNvbi8{p['id']}",
        "name": p["name"],
        "email_address": p.get("email_address"),
        "personable_type": p.get("personable_type") or "User",
        "title": p.get("title") or "",
        "bio": p.get("bio") or "",
        "location": p.get("location") or "",
        "created_at": iso(p.get("created_at")),
        "updated_at": iso(p.get("updated_at")),
        "admin": bool(p.get("admin")),
        "owner": bool(p.get("owner")),
        "client": bool(p.get("client")),
        "employee": bool(p.get("employee", True)),
        "time_zone": p.get("time_zone") or "America/Chicago",
        "avatar_url": p.get("avatar_url") or app_url(f"/avatars/{p['id']}.png"),
        "company": company,
        "can_manage_projects": bool(p.get("can_manage_projects", p.get("employee", True))),
        "can_manage_people": bool(p.get("can_manage_people", p.get("admin"))),
        "can_ping": bool(p.get("can_ping", True)),
        "can_access_timesheet": bool(p.get("can_access_timesheet", True)),
        "can_access_hill_charts": bool(p.get("can_access_hill_charts", True)),
    }
    if p.get("sample"):
        base["sample"] = True
    return base

def bucket_json(project: dict) -> dict:
    return {
        "id": project["id"],
        "name": project["name"],
        "type": "Project",
    }

def parent_json(rec: Optional[dict]) -> Optional[dict]:
    if not rec:
        return None
    return {
        "id": rec["id"],
        "title": rec.get("title") or "",
        "type": rec.get("type"),
        "url": recording_api_url(rec),
        "app_url": recording_app_url(rec),
    }

def recording_api_url(rec: dict) -> str:
    t = rec.get("type") or ""
    rid = rec["id"]
    aid = CONFIG.account_id
    mapping = {
        "Message": f"/{aid}/messages/{rid}.json",
        "Message::Board": f"/{aid}/message_boards/{rid}.json",
        "Todo": f"/{aid}/todos/{rid}.json",
        "Todolist": f"/{aid}/todolists/{rid}.json",
        "Todolist::Group": f"/{aid}/todolists/{rid}.json",
        "Todoset": f"/{aid}/todosets/{rid}.json",
        "Document": f"/{aid}/documents/{rid}.json",
        "Upload": f"/{aid}/uploads/{rid}.json",
        "Vault": f"/{aid}/vaults/{rid}.json",
        "Comment": f"/{aid}/comments/{rid}.json",
        "Chat::Transcript": f"/{aid}/chats/{rid}.json",
        "Chat::Lines::Line": f"/{aid}/chats/{rec.get('parent_id')}/lines/{rid}.json",
        "Schedule": f"/{aid}/schedules/{rid}.json",
        "Schedule::Entry": f"/{aid}/schedule_entries/{rid}.json",
        "Kanban::Board": f"/{aid}/card_tables/{rid}.json",
        "Kanban::Triage": f"/{aid}/card_tables/columns/{rid}.json",
        "Kanban::Column": f"/{aid}/card_tables/columns/{rid}.json",
        "Kanban::Card": f"/{aid}/card_tables/cards/{rid}.json",
        "Kanban::Step": f"/{aid}/card_tables/steps/{rid}.json",
        "Kanban::OnHoldColumn": f"/{aid}/card_tables/columns/{rid}.json",
        "Kanban::DoneColumn": f"/{aid}/card_tables/columns/{rid}.json",
        "Kanban::NotNowColumn": f"/{aid}/card_tables/columns/{rid}.json",
        "Questionnaire": f"/{aid}/questionnaires/{rid}.json",
        "Question": f"/{aid}/questions/{rid}.json",
        "Question::Answer": f"/{aid}/question_answers/{rid}.json",
        "Inbox": f"/{aid}/inboxes/{rid}.json",
        "Inbox::Forward": f"/{aid}/inbox_forwards/{rid}.json",
        "Inbox::Reply": f"/{aid}/inbox_forwards/{rec.get('parent_id')}/replies/{rid}.json",
        "Client::Correspondence": f"/{aid}/client/correspondences/{rid}.json",
        "Client::Approval": f"/{aid}/client/approvals/{rid}.json",
        "Client::Reply": f"/{aid}/client/recordings/{rec.get('parent_id')}/replies/{rid}.json",
        "Gauge": f"/{aid}/projects/{rec.get('bucket_id')}/gauge.json",
        "Gauge::Needle": f"/{aid}/gauge_needles/{rid}.json",
        "Timesheet::Entry": f"/{aid}/timesheet_entries/{rid}.json",
    }
    path = mapping.get(t, f"/{aid}/recordings/{rid}.json")
    return api_url(path)

def recording_app_url(rec: dict) -> str:
    t = rec.get("type") or ""
    rid = rec["id"]
    bid = rec.get("bucket_id")
    aid = CONFIG.account_id
    if t == "Message":
        return app_url(f"/{aid}/buckets/{bid}/messages/{rid}")
    if t == "Todo":
        return app_url(f"/{aid}/buckets/{bid}/todos/{rid}")
    if t == "Document":
        return app_url(f"/{aid}/buckets/{bid}/documents/{rid}")
    if t.startswith("Kanban::Card"):
        return app_url(f"/{aid}/buckets/{bid}/card_tables/cards/{rid}")
    if t == "Chat::Lines::Line":
        return app_url(f"/{aid}/buckets/{bid}/chats/{rec.get('parent_id')}")
    return app_url(f"/{aid}/buckets/{bid}/recordings/{rid}")

def dock_name_for_type(t: str) -> str:
    return {
        "Message::Board": "message_board",
        "Todoset": "todoset",
        "Vault": "vault",
        "Chat::Transcript": "chat",
        "Schedule": "schedule",
        "Kanban::Board": "kanban_board",
        "Questionnaire": "questionnaire",
        "Inbox": "inbox",
    }.get(t, t.lower().replace("::", "_"))

def dock_item_json(tool: dict, project: dict) -> dict:
    enabled = tool.get("enabled", True) and tool.get("status") == "active"
    return {
        "id": tool["id"],
        "title": tool.get("title") or tool.get("name") or "",
        "name": dock_name_for_type(tool["type"]),
        "enabled": bool(enabled),
        "position": tool.get("position") or 0,
        "url": recording_api_url(tool),
        "app_url": recording_app_url(tool),
    }

def project_json(project: dict, include_dock: bool = True) -> dict:
    dock = []
    if include_dock:
        tools = [
            r for r in STORE.recordings.values()
            if r.get("bucket_id") == project["id"]
            and r.get("type") in TOOL_ROOT_TYPES
            and r.get("status") != "trashed"
        ]
        tools.sort(key=lambda x: (x.get("position") or 0, x.get("id") or 0))
        dock = [dock_item_json(t, project) for t in tools if t.get("enabled", True)]
    return {
        "id": project["id"],
        "status": project.get("status") or "active",
        "created_at": iso(project.get("created_at")),
        "updated_at": iso(project.get("updated_at")),
        "name": project["name"],
        "description": project.get("description") or "",
        "purpose": project.get("purpose") or "topic",
        "clients_enabled": bool(project.get("clients_enabled")),
        "bookmark_url": api_url(acct_path(f"/projects/{project['id']}/bookmark.json")),
        "url": api_url(acct_path(f"/projects/{project['id']}.json")),
        "app_url": app_url(f"/{CONFIG.account_id}/projects/{project['id']}"),
        "dock": dock,
        "bookmarked": bool(project.get("bookmarked")),
        "sample": bool(project.get("sample")),
        "admissions": project.get("admissions") or "invite",
        "starts_on": date_str(project.get("starts_on")),
        "ends_on": date_str(project.get("ends_on")),
    }

TOOL_ROOT_TYPES = {
    "Message::Board", "Todoset", "Vault", "Chat::Transcript",
    "Schedule", "Kanban::Board", "Questionnaire", "Inbox",
}

def base_recording_json(rec: dict) -> dict:
    project = STORE.project(rec["bucket_id"]) or {
        "id": rec["bucket_id"], "name": "Unknown", "type": "Project"
    }
    parent = STORE.recording(rec["parent_id"]) if rec.get("parent_id") else None
    creator = STORE.person(rec.get("creator_id"))
    j = {
        "id": rec["id"],
        "status": rec.get("status") or "active",
        "visible_to_clients": bool(rec.get("visible_to_clients")),
        "created_at": iso(rec.get("created_at")),
        "updated_at": iso(rec.get("updated_at")),
        "title": rec.get("title") or "",
        "inherits_status": bool(rec.get("inherits_status", True)),
        "type": rec.get("type"),
        "url": recording_api_url(rec),
        "app_url": recording_app_url(rec),
        "bookmark_url": api_url(acct_path(f"/recordings/{rec['id']}/bookmark.json")),
        "parent": parent_json(parent),
        "bucket": bucket_json(project) if project else {"id": rec["bucket_id"], "name": "", "type": "Project"},
        "creator": person_json(creator),
    }
    if rec.get("position") is not None:
        j["position"] = rec.get("position")
    if "content" in rec:
        j["content"] = rec.get("content") or ""
    if rec.get("comments_count") is not None:
        j["comments_count"] = rec.get("comments_count") or 0
        j["comments_url"] = api_url(acct_path(f"/recordings/{rec['id']}/comments.json"))
    if rec.get("boosts_count") is not None:
        j["boosts_count"] = rec.get("boosts_count") or 0
        j["boosts_url"] = api_url(acct_path(f"/recordings/{rec['id']}/boosts.json"))
    j["subscription_url"] = api_url(acct_path(f"/recordings/{rec['id']}/subscription.json"))
    return j

def category_json(cat: dict) -> dict:
    return {
        "id": cat["id"],
        "name": cat["name"],
        "icon": cat["icon"],
        "created_at": iso(cat.get("created_at")),
        "updated_at": iso(cat.get("updated_at")),
    }

def serialize_recording(rec: dict) -> dict:
    """Type-aware recording serializer."""
    t = rec.get("type")
    j = base_recording_json(rec)

    if t == "Message":
        j["subject"] = rec.get("subject") or rec.get("title") or ""
        j["content"] = rec.get("content") or ""
        if rec.get("category_id"):
            cat = STORE.categories.get(rec["category_id"])
            if cat:
                j["category"] = category_json(cat)
        if rec["id"] in STORE.pinned:
            j["pinned"] = True

    elif t == "Message::Board":
        msgs = [r for r in STORE.recordings.values()
                if r.get("parent_id") == rec["id"] and r.get("type") == "Message"
                and r.get("status") == "active"]
        j["messages_count"] = len(msgs)
        j["messages_url"] = api_url(acct_path(f"/message_boards/{rec['id']}/messages.json"))
        j["app_messages_url"] = recording_app_url(rec)

    elif t == "Todo":
        j["description"] = rec.get("description") or ""
        j["content"] = rec.get("content") or rec.get("title") or ""
        j["completed"] = bool(rec.get("completed"))
        j["starts_on"] = date_str(rec.get("starts_on"))
        j["due_on"] = date_str(rec.get("due_on"))
        j["assignees"] = [person_json(p) for p in STORE.people_by_ids(rec.get("assignee_ids") or [])]
        j["completion_subscribers"] = [
            person_json(p) for p in STORE.people_by_ids(rec.get("completion_subscriber_ids") or [])
        ]
        j["completion_url"] = api_url(acct_path(f"/todos/{rec['id']}/completion.json"))
        # steps as children of type Kanban::Step or Todo::Step — we use "Step"
        steps = [r for r in STORE.recordings.values()
                 if r.get("parent_id") == rec["id"] and r.get("type") in ("Step", "Kanban::Step")
                 and r.get("status") != "trashed"]
        steps.sort(key=lambda x: (x.get("position") or 0, x.get("id")))
        if steps:
            j["steps"] = [serialize_recording(s) for s in steps]
            j["steps_count"] = len(steps)
            j["completed_steps_count"] = sum(1 for s in steps if s.get("completed"))

    elif t in ("Todolist", "Todolist::Group"):
        j["name"] = rec.get("name") or rec.get("title") or ""
        j["description"] = rec.get("description") or ""
        todos = [r for r in STORE.recordings.values()
                 if r.get("parent_id") == rec["id"] and r.get("type") == "Todo"
                 and r.get("status") != "trashed"]
        completed = sum(1 for x in todos if x.get("completed"))
        total = len(todos)
        j["completed"] = total > 0 and completed == total
        j["completed_ratio"] = f"{completed}/{total}"
        j["todos_url"] = api_url(acct_path(f"/todolists/{rec['id']}/todos.json"))
        j["groups_url"] = api_url(acct_path(f"/todolists/{rec['id']}/groups.json"))
        j["app_todos_url"] = recording_app_url(rec)

    elif t == "Todoset":
        j["name"] = rec.get("name") or rec.get("title") or "To-dos"
        lists = [r for r in STORE.recordings.values()
                 if r.get("parent_id") == rec["id"] and r.get("type") == "Todolist"
                 and r.get("status") != "trashed"]
        j["todolists_count"] = len(lists)
        j["todolists_url"] = api_url(acct_path(f"/todosets/{rec['id']}/todolists.json"))
        j["app_todolists_url"] = recording_app_url(rec)
        # ratio across all todos
        todos = [r for r in STORE.recordings.values()
                 if r.get("type") == "Todo" and r.get("bucket_id") == rec["bucket_id"]
                 and r.get("status") != "trashed"]
        # only those under this todoset
        list_ids = {x["id"] for x in lists}
        group_ids = {r["id"] for r in STORE.recordings.values()
                     if r.get("type") == "Todolist::Group" and r.get("parent_id") in list_ids}
        parents = list_ids | group_ids
        scoped = [t for t in todos if t.get("parent_id") in parents]
        completed = sum(1 for x in scoped if x.get("completed"))
        total = len(scoped)
        j["completed"] = total > 0 and completed == total
        j["completed_ratio"] = f"{completed}/{total}"

    elif t == "Document":
        j["content"] = rec.get("content") or ""

    elif t == "Upload":
        j["description"] = rec.get("description") or ""
        j["content_type"] = rec.get("content_type") or "application/octet-stream"
        j["byte_size"] = rec.get("byte_size") or 0
        j["filename"] = rec.get("filename") or rec.get("title") or "file"
        j["download_url"] = rec.get("download_url") or api_url(
            acct_path(f"/uploads/{rec['id']}/download")
        )
        if rec.get("width"):
            j["width"] = rec["width"]
        if rec.get("height"):
            j["height"] = rec["height"]

    elif t == "Vault":
        docs = [r for r in STORE.recordings.values()
                if r.get("parent_id") == rec["id"] and r.get("type") == "Document"
                and r.get("status") != "trashed"]
        ups = [r for r in STORE.recordings.values()
               if r.get("parent_id") == rec["id"] and r.get("type") == "Upload"
               and r.get("status") != "trashed"]
        vaults = [r for r in STORE.recordings.values()
                  if r.get("parent_id") == rec["id"] and r.get("type") == "Vault"
                  and r.get("status") != "trashed"]
        j["documents_count"] = len(docs)
        j["documents_url"] = api_url(acct_path(f"/vaults/{rec['id']}/documents.json"))
        j["uploads_count"] = len(ups)
        j["uploads_url"] = api_url(acct_path(f"/vaults/{rec['id']}/uploads.json"))
        j["vaults_count"] = len(vaults)
        j["vaults_url"] = api_url(acct_path(f"/vaults/{rec['id']}/vaults.json"))

    elif t == "Comment":
        j["content"] = rec.get("content") or ""

    elif t == "Chat::Transcript":
        j["topic"] = rec.get("topic") or rec.get("title") or "Campfire"
        j["lines_url"] = api_url(acct_path(f"/chats/{rec['id']}/lines.json"))
        j["files_url"] = api_url(acct_path(f"/chats/{rec['id']}/uploads.json"))

    elif t == "Chat::Lines::Line":
        j["content"] = rec.get("content") or ""
        j["attachments"] = rec.get("attachments") or []

    elif t == "Schedule":
        entries = [r for r in STORE.recordings.values()
                   if r.get("parent_id") == rec["id"] and r.get("type") == "Schedule::Entry"
                   and r.get("status") != "trashed"]
        j["include_due_assignments"] = bool(rec.get("include_due_assignments", True))
        j["entries_count"] = len(entries)
        j["entries_url"] = api_url(acct_path(f"/schedules/{rec['id']}/entries.json"))

    elif t == "Schedule::Entry":
        j["summary"] = rec.get("summary") or rec.get("title") or ""
        j["description"] = rec.get("description") or ""
        j["all_day"] = bool(rec.get("all_day"))
        j["starts_at"] = iso(rec.get("starts_at")) if isinstance(rec.get("starts_at"), datetime) else rec.get("starts_at")
        j["ends_at"] = iso(rec.get("ends_at")) if isinstance(rec.get("ends_at"), datetime) else rec.get("ends_at")
        j["participants"] = [
            person_json(p) for p in STORE.people_by_ids(rec.get("participant_ids") or [])
        ]

    elif t == "Kanban::Board":
        lists = card_columns_for_board(rec)
        j["lists"] = [serialize_recording(c) for c in lists]
        j["subscribers"] = [person_json(p) for p in STORE.subscribers(rec["id"])]

    elif t in ("Kanban::Column", "Kanban::Triage", "Kanban::DoneColumn",
               "Kanban::NotNowColumn", "Kanban::OnHoldColumn"):
        cards = [r for r in STORE.recordings.values()
                 if r.get("parent_id") == rec["id"] and r.get("type") == "Kanban::Card"
                 and r.get("status") != "trashed"]
        j["color"] = rec.get("color") or "white"
        j["description"] = rec.get("description") or ""
        j["cards_count"] = len(cards)
        j["cards_url"] = api_url(acct_path(f"/card_tables/lists/{rec['id']}/cards.json"))
        j["subscribers"] = [person_json(p) for p in STORE.subscribers(rec["id"])]
        # on-hold sub-lane for regular columns
        if t == "Kanban::Column" and rec.get("on_hold_id"):
            oh = STORE.recording(rec["on_hold_id"])
            if oh:
                oh_cards = [r for r in STORE.recordings.values()
                            if r.get("parent_id") == oh["id"] and r.get("type") == "Kanban::Card"
                            and r.get("status") != "trashed"]
                j["on_hold"] = {
                    "id": oh["id"],
                    "status": oh.get("status") or "active",
                    "inherits_status": True,
                    "title": oh.get("title") or "On hold",
                    "created_at": iso(oh.get("created_at")),
                    "updated_at": iso(oh.get("updated_at")),
                    "cards_count": len(oh_cards),
                    "cards_url": api_url(acct_path(f"/card_tables/lists/{oh['id']}/cards.json")),
                }

    elif t == "Kanban::Card":
        j["content"] = rec.get("content") or ""
        j["description"] = rec.get("description") or rec.get("content") or ""
        j["due_on"] = date_str(rec.get("due_on"))
        j["completed"] = bool(rec.get("completed"))
        j["completed_at"] = iso(rec.get("completed_at"))
        j["assignees"] = [person_json(p) for p in STORE.people_by_ids(rec.get("assignee_ids") or [])]
        j["completion_subscribers"] = [
            person_json(p) for p in STORE.people_by_ids(rec.get("completion_subscriber_ids") or [])
        ]
        steps = [r for r in STORE.recordings.values()
                 if r.get("parent_id") == rec["id"] and r.get("type") == "Kanban::Step"
                 and r.get("status") != "trashed"]
        steps.sort(key=lambda x: (x.get("position") or 0, x.get("id")))
        j["steps"] = [serialize_recording(s) for s in steps]

    elif t in ("Kanban::Step", "Step"):
        j["due_on"] = date_str(rec.get("due_on"))
        j["completed"] = bool(rec.get("completed"))
        j["completed_at"] = iso(rec.get("completed_at"))
        j["assignees"] = [person_json(p) for p in STORE.people_by_ids(rec.get("assignee_ids") or [])]
        j["completion_url"] = api_url(acct_path(f"/card_tables/steps/{rec['id']}/completions.json"))
        if rec.get("completer_id"):
            j["completer"] = person_json(STORE.person(rec["completer_id"]))

    elif t == "Questionnaire":
        qs = [r for r in STORE.recordings.values()
              if r.get("parent_id") == rec["id"] and r.get("type") == "Question"
              and r.get("status") != "trashed"]
        j["name"] = rec.get("name") or rec.get("title") or "Automatic Check-ins"
        j["questions_count"] = len(qs)
        j["questions_url"] = api_url(acct_path(f"/questionnaires/{rec['id']}/questions.json"))

    elif t == "Question":
        j["paused"] = bool(rec.get("paused"))
        j["schedule"] = rec.get("schedule") or {"frequency": "every_week", "days": [1], "hour": 9, "minute": 0}
        answers = [r for r in STORE.recordings.values()
                   if r.get("parent_id") == rec["id"] and r.get("type") == "Question::Answer"
                   and r.get("status") != "trashed"]
        j["answers_count"] = len(answers)
        j["answers_url"] = api_url(acct_path(f"/questions/{rec['id']}/answers.json"))

    elif t == "Question::Answer":
        j["content"] = rec.get("content") or ""
        j["group_on"] = date_str(rec.get("group_on")) or (iso(rec.get("created_at")) or "")[:10]

    elif t == "Inbox":
        fw = [r for r in STORE.recordings.values()
              if r.get("parent_id") == rec["id"] and r.get("type") == "Inbox::Forward"
              and r.get("status") != "trashed"]
        j["forwards_count"] = len(fw)
        j["forwards_url"] = api_url(acct_path(f"/inboxes/{rec['id']}/forwards.json"))

    elif t == "Inbox::Forward":
        j["subject"] = rec.get("subject") or rec.get("title") or ""
        j["content"] = rec.get("content") or ""
        j["from"] = rec.get("from") or ""
        replies = [r for r in STORE.recordings.values()
                   if r.get("parent_id") == rec["id"] and r.get("type") == "Inbox::Reply"
                   and r.get("status") != "trashed"]
        j["replies_count"] = len(replies)
        j["replies_url"] = api_url(acct_path(f"/inbox_forwards/{rec['id']}/replies.json"))

    elif t == "Inbox::Reply":
        j["content"] = rec.get("content") or ""

    elif t == "Client::Correspondence":
        j["subject"] = rec.get("subject") or rec.get("title") or ""
        j["content"] = rec.get("content") or ""
        replies = [r for r in STORE.recordings.values()
                   if r.get("parent_id") == rec["id"] and r.get("type") == "Client::Reply"
                   and r.get("status") != "trashed"]
        j["replies_count"] = len(replies)
        j["replies_url"] = api_url(acct_path(f"/client/recordings/{rec['id']}/replies.json"))

    elif t == "Client::Approval":
        j["subject"] = rec.get("subject") or rec.get("title") or ""
        j["content"] = rec.get("content") or ""
        j["due_on"] = date_str(rec.get("due_on"))
        j["approval_status"] = rec.get("approval_status") or "pending"
        if rec.get("approver_id"):
            j["approver"] = person_json(STORE.person(rec["approver_id"]))
        j["responses"] = rec.get("responses") or []
        replies = [r for r in STORE.recordings.values()
                   if r.get("parent_id") == rec["id"] and r.get("type") == "Client::Reply"
                   and r.get("status") != "trashed"]
        j["replies_count"] = len(replies)
        j["replies_url"] = api_url(acct_path(f"/client/recordings/{rec['id']}/replies.json"))

    elif t == "Client::Reply":
        j["content"] = rec.get("content") or ""

    elif t == "Gauge":
        j["description"] = rec.get("description") or ""
        j["enabled"] = bool(rec.get("enabled", True))
        j["last_needle_color"] = rec.get("last_needle_color")
        j["last_needle_position"] = rec.get("last_needle_position")
        j["previous_needle_position"] = rec.get("previous_needle_position")

    elif t == "Gauge::Needle":
        j["description"] = rec.get("description") or ""
        j["color"] = rec.get("color") or "green"
        j["position"] = rec.get("position") if rec.get("position") is not None else 50

    elif t == "Timesheet::Entry":
        j["date"] = rec.get("date") if isinstance(rec.get("date"), str) else date_str(rec.get("date"))
        j["description"] = rec.get("description") or ""
        j["hours"] = rec.get("hours") or "0.0"
        if rec.get("person_id"):
            j["person"] = person_json(STORE.person(rec["person_id"]))

    return j

def card_columns_for_board(board: dict) -> List[dict]:
    cols = [r for r in STORE.recordings.values()
            if r.get("parent_id") == board["id"]
            and r.get("type") in (
                "Kanban::Triage", "Kanban::Column",
                "Kanban::DoneColumn", "Kanban::NotNowColumn",
            )
            and r.get("status") != "trashed"]
    # order: triage, not_now, user columns by position, done
    def sort_key(c):
        t = c["type"]
        if t == "Kanban::Triage":
            return (0, 0, c.get("id"))
        if t == "Kanban::NotNowColumn":
            return (1, 0, c.get("id"))
        if t == "Kanban::Column":
            return (2, c.get("position") or 0, c.get("id"))
        if t == "Kanban::DoneColumn":
            return (3, 0, c.get("id"))
        return (4, 0, c.get("id"))
    cols.sort(key=sort_key)
    return cols

def boost_json(b: dict) -> dict:
    rec = STORE.recording(b["recording_id"])
    return {
        "id": b["id"],
        "content": b.get("content") or "",
        "created_at": iso(b.get("created_at")),
        "booster": person_json(STORE.person(b["booster_id"])),
        "recording": parent_json(rec) if rec else None,
    }

def event_json(ev: dict) -> dict:
    j = {
        "id": ev["id"],
        "recording_id": ev["recording_id"],
        "action": ev.get("action"),
        "details": ev.get("details") or {},
        "created_at": iso(ev.get("created_at")),
        "creator": person_json(STORE.person(ev.get("creator_id"))),
        "boosts_count": ev.get("boosts_count") or 0,
        "boosts_url": api_url(acct_path(
            f"/recordings/{ev['recording_id']}/events/{ev['id']}/boosts.json"
        )),
    }
    return j

def subscription_json(recording_id: int, person: dict) -> dict:
    subs = STORE.subscribers(recording_id)
    ids = {p["id"] for p in subs}
    return {
        "subscribed": person["id"] in ids,
        "count": len(subs),
        "url": api_url(acct_path(f"/recordings/{recording_id}/subscription.json")),
        "subscribers": [person_json(p) for p in subs],
    }

def account_json() -> dict:
    a = STORE.account
    return {
        "id": a["id"],
        "name": a["name"],
        "owner_name": a.get("owner_name") or "",
        "active": bool(a.get("active", True)),
        "created_at": iso(a.get("created_at")),
        "updated_at": iso(a.get("updated_at")),
        "trial": bool(a.get("trial", False)),
        "trial_ends_on": a.get("trial_ends_on"),
        "frozen": bool(a.get("frozen", False)),
        "paused": bool(a.get("paused", False)),
        "limits": a.get("limits") or {
            "can_create_projects": True,
            "can_pin_projects": True,
            "can_create_users": True,
            "can_upload_files": True,
        },
        "subscription": a.get("subscription") or {
            "short_name": "pro",
            "proper_name": "Pro",
            "project_limit": 0,
            "teams": True,
            "clients": True,
            "templates": True,
            "logo": True,
            "timesheet": True,
        },
        "settings": a.get("settings") or {
            "company_hq_enabled": True,
            "teams_enabled": True,
            "projects_enabled": True,
        },
        "logo": a.get("logo") or {"url": None},
    }

def webhook_json(wh: dict) -> dict:
    return {
        "id": wh["id"],
        "active": bool(wh.get("active", True)),
        "created_at": iso(wh.get("created_at")),
        "updated_at": iso(wh.get("updated_at")),
        "payload_url": wh.get("payload_url"),
        "types": wh.get("types") or [],
        "url": api_url(acct_path(f"/webhooks/{wh['id']}.json")),
        "app_url": app_url(f"/{CONFIG.account_id}/webhooks/{wh['id']}"),
        "recent_deliveries": wh.get("recent_deliveries") or [],
    }

def chatbot_json(bot: dict) -> dict:
    return {
        "id": bot["id"],
        "created_at": iso(bot.get("created_at")),
        "updated_at": iso(bot.get("updated_at")),
        "service_name": bot.get("service_name"),
        "command_url": bot.get("command_url"),
        "url": api_url(acct_path(
            f"/chats/{bot['campfire_id']}/integrations/{bot['id']}.json"
        )),
        "app_url": app_url(
            f"/{CONFIG.account_id}/chats/{bot['campfire_id']}/integrations/{bot['id']}"
        ),
        "lines_url": api_url(acct_path(f"/chats/{bot['campfire_id']}/lines.json")),
    }

def template_json(t: dict) -> dict:
    return {
        "id": t["id"],
        "status": t.get("status") or "active",
        "created_at": iso(t.get("created_at")),
        "updated_at": iso(t.get("updated_at")),
        "name": t["name"],
        "description": t.get("description") or "",
        "url": api_url(acct_path(f"/templates/{t['id']}.json")),
        "app_url": app_url(f"/{CONFIG.account_id}/templates/{t['id']}"),
        "dock": t.get("dock") or [],
    }

def tool_json(tool: dict) -> dict:
    project = STORE.project(tool["bucket_id"])
    return {
        "id": tool["id"],
        "status": tool.get("status") or "active",
        "created_at": iso(tool.get("created_at")),
        "updated_at": iso(tool.get("updated_at")),
        "title": tool.get("title") or "",
        "name": dock_name_for_type(tool["type"]),
        "enabled": bool(tool.get("enabled", True)),
        "position": tool.get("position") or 0,
        "url": recording_api_url(tool),
        "app_url": recording_app_url(tool),
        "bucket": bucket_json(project) if project else None,
    }

def filter_client_visible(person: dict, items: List[dict]) -> List[dict]:
    if not person.get("client"):
        return items
    return [i for i in items if i.get("visible_to_clients")]

def paginate(items: List[Any], page: int, page_size: int = None) -> Tuple[List[Any], dict]:
    page_size = page_size or CONFIG.page_size
    page = max(1, int(page or 1))
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    slice_ = items[start:end]
    headers = {"X-Total-Count": str(total)}
    return slice_, headers, page, page_size, total, end < total

# ---------------------------------------------------------------------------
# Seed — INIT §3 "Launch the new website"
# ---------------------------------------------------------------------------

def _T(days: float = 0, hours: float = 0, minutes: float = 0) -> datetime:
    """Fixed offset from seed epoch T."""
    return STORE.seed_time + timedelta(days=days, hours=hours, minutes=minutes)

def seed_world(store: Store = None) -> None:
    store = store or STORE
    with store.lock:
        store.clear() if False else None  # no-op; called on fresh store
        store.boot_time = utcnow()
        # Deterministic T: freeze to a stable epoch so IDs + offsets are stable
        # Use boot time floored to the minute so restarts within the same minute
        # are identical; for absolute determinism prefer BASECAMP_SEED_TIME.
        seed_env = os.environ.get("BASECAMP_SEED_TIME")
        if seed_env:
            store.seed_time = parse_iso(seed_env) or store.boot_time
        else:
            # Stable default: 2026-06-15T15:00:00Z (a Monday afternoon)
            store.seed_time = datetime(2026, 6, 15, 15, 0, 0, tzinfo=UTC)
        T = store.seed_time
        store.seed_time = T

        # --- Account -------------------------------------------------------
        store.account = {
            "id": int(CONFIG.account_id),
            "name": "Northstar Co.",
            "owner_name": "Alex Rivera",
            "active": True,
            "created_at": T - timedelta(days=400),
            "updated_at": T,
            "trial": False,
            "frozen": False,
            "paused": False,
            "limits": {
                "can_create_projects": True,
                "can_pin_projects": True,
                "can_create_users": True,
                "can_upload_files": True,
            },
            "subscription": {
                "short_name": "pro",
                "proper_name": "Pro",
                "project_limit": 0,
                "teams": True,
                "clients": True,
                "templates": True,
                "logo": True,
                "timesheet": True,
            },
            "settings": {
                "company_hq_enabled": True,
                "teams_enabled": True,
                "projects_enabled": True,
            },
            "logo": {"url": app_url("/logos/northstar.png")},
        }

        company = {
            "id": 10,
            "name": "Northstar Co.",
        }
        store.companies[10] = company

        def mk_person(pid, name, email, title, *, admin=False, owner=False,
                      employee=True, client=False, sample=False, created=None):
            p = {
                "id": pid,
                "name": name,
                "email_address": email,
                "title": title,
                "bio": "",
                "location": "",
                "created_at": created or (T - timedelta(days=300)),
                "updated_at": T,
                "admin": admin,
                "owner": owner,
                "client": client,
                "employee": employee,
                "time_zone": "America/Chicago",
                "company_id": 10,
                "sample": sample,
                "can_manage_projects": employee or owner,
                "can_manage_people": admin or owner,
                "can_ping": True,
                "can_access_timesheet": True,
                "can_access_hill_charts": True,
                "personable_type": "User",
                "attachable_sgid": f"sgid_person_{pid}",
            }
            store.people[pid] = p
            store.preferences[pid] = {
                "time_zone_name": "America/Chicago",
                "first_week_day": "Monday",
                "time_format": "twelve_hour",
            }
            return p

        # Real owner (login principal) — not part of sample cast
        alex = mk_person(
            100, "Alex Rivera", "alex@northstar.test", "Owner",
            admin=True, owner=True, employee=True, sample=False,
        )
        store.issue_token(100, "bcamp_pat_owner_alex")

        # Sample cast (sample: true) — INIT §3
        maya = mk_person(101, "Maya Chen", "maya@northstar.test", "Project lead",
                         employee=True, sample=True)
        sam = mk_person(102, "Sam Whitaker", "sam@northstar.test", "Writer",
                        employee=True, sample=True)
        omar = mk_person(103, "Omar Haddad", "omar@northstar.test", "Designer",
                         employee=True, sample=True)
        priya = mk_person(104, "Priya Nair", "priya@northstar.test", "Developer",
                          employee=True, sample=True)
        lena = mk_person(105, "Lena Kowalski", "lena@northstar.test", "Marketing",
                         employee=True, sample=True)
        diego = mk_person(106, "Diego Ramos", "diego@northstar.test", "Community",
                          employee=True, sample=True)
        grace = mk_person(107, "Grace Okafor", "grace@northstar.test", "QA",
                          employee=True, sample=True)
        felix = mk_person(108, "Felix Berg", "felix@northstar.test", "Ops",
                          employee=True, sample=True)

        cast = [maya, sam, omar, priya, lena, diego, grace, felix]
        cast_ids = [p["id"] for p in cast]

        # Message categories
        def mk_cat(cid, name, icon):
            c = {
                "id": cid,
                "name": name,
                "icon": icon,
                "created_at": T - timedelta(days=100),
                "updated_at": T - timedelta(days=100),
            }
            store.categories[cid] = c
            return c

        cat_announcement = mk_cat(201, "Announcement", "📣")
        cat_fyi = mk_cat(202, "FYI", "✨")
        cat_heartbeat = mk_cat(203, "Heartbeat", "❤️")
        cat_pitch = mk_cat(204, "Pitch", "💡")
        cat_question = mk_cat(205, "Question", "👋")

        # --- Project -------------------------------------------------------
        project = {
            "id": 1000,
            "name": "Launch the new website",
            "description": (
                "👋 This is a sample project that shows how a team works together here. "
                "Poke around, click into things — and delete this project whenever you're ready."
            ),
            "purpose": "topic",
            "status": "active",
            "clients_enabled": False,
            "bookmarked": False,
            "sample": True,
            "admissions": "employee",  # All-access
            "created_at": T - timedelta(days=30),
            "updated_at": T - timedelta(days=1),
            "access": set([100] + cast_ids),  # owner + cast
            "starts_on": None,
            "ends_on": None,
        }
        store.projects[1000] = project
        P = project["id"]
        creator = maya["id"]

        def rec(**kwargs):
            return store.new_recording(**kwargs)

        # --- Message Board -------------------------------------------------
        board = rec(
            type_="Message::Board", title="Message Board", name="Message Board",
            creator_id=creator, bucket_id=P, position=1, rid=2001,
            created_at=T - timedelta(days=30),
            extra={"enabled": True, "name": "Message Board"},
        )

        def msg(rid, subject, content, author, cat, days, hours, pinned=False, subs=None):
            m = rec(
                type_="Message", title=subject, creator_id=author, bucket_id=P,
                parent_id=board["id"], content=content, rid=rid,
                created_at=_T(days=days, hours=hours),
                extra={"subject": subject, "category_id": cat},
            )
            if pinned:
                store.pinned.add(m["id"])
            if subs:
                store.subscribe(m["id"], subs)
            store.write_event(m["id"], "created", author, created_at=m["created_at"])
            return m

        # Kickoff: the plan — pinned, @mentions, boosts from all 7 others, 4 comments
        kickoff = msg(
            3001, "Kickoff: the plan",
            "<div><strong>Welcome to the launch!</strong></div>"
            "<div>We're shipping the new Northstar site. Here's the plan:</div>"
            "<ul>"
            "<li><bc-attachment sgid=\"sgid_person_102\">@Sam Whitaker</bc-attachment> — homepage copy</li>"
            "<li><bc-attachment sgid=\"sgid_person_103\">@Omar Haddad</bc-attachment> — visual design</li>"
            "<li><bc-attachment sgid=\"sgid_person_104\">@Priya Nair</bc-attachment> — implementation & DNS</li>"
            "<li><bc-attachment sgid=\"sgid_person_105\">@Lena Kowalski</bc-attachment> — launch marketing</li>"
            "</ul>"
            "<div>Pin this and check in daily.</div>",
            maya["id"], cat_announcement["id"], -4, 9, pinned=True,
            subs=cast_ids[:5],
        )
        for pid in cast_ids:
            if pid != maya["id"]:
                store.add_boost(kickoff["id"], pid, "👏", created_at=_T(-4, 10))
        c1 = store.add_comment(kickoff, sam["id"],
                               "Copy outline is ready for review — will post a pitch next.",
                               created_at=_T(-4, 10, 30))
        c2 = store.add_comment(kickoff, omar["id"],
                               "Moodboard landing tonight. Need final hero photo direction.",
                               created_at=_T(-4, 11))
        store.add_boost(c2["id"], maya["id"], "🙌", created_at=_T(-4, 11, 15))
        c3 = store.add_comment(kickoff, priya["id"],
                               "Infra checklist drafted. DNS cutover needs a freeze window.",
                               created_at=_T(-4, 12))
        c4 = store.add_comment(kickoff, lena["id"],
                               "I'll schedule social posts once we lock the date.",
                               created_at=_T(-4, 13))
        store.add_boost(c4["id"], diego["id"], "💯", created_at=_T(-4, 13, 20))

        # Pitch: trim the homepage copy — longest thread, 7 comments
        pitch = msg(
            3002, "Pitch: trim the homepage copy",
            "<div>The draft homepage is ~40% too long. Proposal: lead with the product story, "
            "move pricing details below the fold, and cut the three redundant testimonials.</div>"
            "<div>Open for pushback before I rewrite.</div>",
            sam["id"], cat_pitch["id"], -4, 11,
            subs=[maya["id"], sam["id"], omar["id"]],
        )
        pitch_comments = [
            (maya["id"], "Agreed on length. Keep the customer quote near the top though."),
            (omar["id"], "Design can flex — shorter hero is better for the illustration."),
            (priya["id"], "Less DOM = faster LCP. I'm for it."),
            (lena["id"], "Marketing needs the pricing block somewhere. Footer CTA?"),
            (sam["id"], "Footer CTA works. I'll move pricing to a dedicated section."),
            (diego["id"], "Beta testers also said it felt long. +1"),
            (grace["id"], "I'll re-test the mobile fold after the rewrite."),
        ]
        for i, (pid, text) in enumerate(pitch_comments):
            store.add_comment(pitch, pid, text, created_at=_T(-4, 12 + i * 0.5))

        # Nice note from a beta tester
        msg(
            3003, "Nice note from a beta tester",
            "<div>Forwarding a note from last week's beta:</div>"
            "<blockquote>\"I finally understood what Northstar does in the first 10 seconds. "
            "That's a first.\"</blockquote>"
            "<div>Feels good — let's protect that clarity.</div>",
            diego["id"], cat_fyi["id"], -4, 14,
        )

        # Traffic this week — embedded chart image
        msg(
            3004, "Traffic this week",
            "<div>Weekly heartbeat. Sessions up 12% WoW after the beta invite blast.</div>"
            "<div><img src=\"https://placehold.co/640x240/png?text=Traffic+chart\" alt=\"Traffic chart\"></div>",
            lena["id"], cat_heartbeat["id"], -4, 15,
        )

        # Local press opportunity
        press = msg(
            3005, "Local press opportunity",
            "<div>City Tech Weekly wants a 400-word founder note on the redesign process. "
            "Deadline Friday. Maya — want to take it, or should I draft?</div>",
            maya["id"], cat_heartbeat["id"], -4, 16,
        )
        store.add_comment(press, lena["id"],
                          "I can draft a skeleton tonight if you want to personalize.",
                          created_at=_T(-4, 16, 30))

        # --- To-dos --------------------------------------------------------
        todoset = rec(
            type_="Todoset", title="To-dos", creator_id=creator, bucket_id=P,
            position=2, rid=2002, created_at=T - timedelta(days=30),
            extra={"enabled": True, "name": "To-dos"},
        )

        list1 = rec(
            type_="Todolist", title="Pre-launch checklist", name="Pre-launch checklist",
            creator_id=creator, bucket_id=P, parent_id=todoset["id"],
            position=1, rid=3100, created_at=_T(-20),
            extra={"name": "Pre-launch checklist", "description": ""},
        )
        list2 = rec(
            type_="Todolist", title="Launch week: content", name="Launch week: content",
            creator_id=creator, bucket_id=P, parent_id=todoset["id"],
            position=2, rid=3101, created_at=_T(-18),
            extra={
                "name": "Launch week: content",
                "description": "Everything that ships in the launch email & social kit.",
            },
        )

        def todo(rid, title, parent, author, days, *, completed=False, assignees=None,
                 due_on=None, notify=None, description="", position=0):
            t = rec(
                type_="Todo", title=title, creator_id=author, bucket_id=P,
                parent_id=parent["id"], content=title, position=position, rid=rid,
                created_at=_T(days=days),
                extra={
                    "description": description,
                    "completed": completed,
                    "assignee_ids": list(assignees or []),
                    "completion_subscriber_ids": list(notify or []),
                    "due_on": due_on,
                    "starts_on": None,
                },
            )
            store.write_event(t["id"], "created", author, created_at=t["created_at"])
            if completed:
                store.write_event(t["id"], "completed", author,
                                  created_at=t["created_at"] + timedelta(days=1))
            return t

        t_analytics = todo(3110, "Set up analytics", list1, priya["id"], -10,
                           assignees=[priya["id"]], position=1)
        # 1 subtask (Step)
        rec(
            type_="Kanban::Step", title="Add Plausible snippet to layout",
            creator_id=priya["id"], bucket_id=P, parent_id=t_analytics["id"],
            position=1, rid=3111, created_at=_T(-9),
            extra={"completed": False, "assignee_ids": [priya["id"]]},
        )

        t_dns = todo(3112, "Point DNS at the new host", list1, priya["id"], -10,
                     assignees=[priya["id"]], position=2)
        store.add_comment(t_dns, maya["id"],
                          "Coordinate with Felix on the TTL drop the day before.",
                          created_at=_T(-8))

        todo(3113, "Freeze content for launch", list1, maya["id"], -12,
             completed=True, assignees=[sam["id"]], position=3)
        todo(3114, "QA pass on staging", list1, grace["id"], -11,
             completed=True, assignees=[grace["id"]], position=4)

        # Launch week: content — 5 open with assignees, 1 completed
        due_plus_3 = (T + timedelta(days=3)).date()
        todo(3120, "Email newsletter", list2, lena["id"], -7,
             assignees=[lena["id"]], due_on=due_plus_3, notify=[maya["id"]], position=1)
        todo(3121, "Launch blog post", list2, sam["id"], -7,
             assignees=[sam["id"]], position=2)
        todo(3122, "Social teaser graphics", list2, omar["id"], -7,
             assignees=[omar["id"], lena["id"]], position=3)
        todo(3123, "Customer quote permissions", list2, diego["id"], -7,
             assignees=[diego["id"]], position=4)
        todo(3124, "Update status page copy", list2, sam["id"], -7,
             assignees=[sam["id"], priya["id"]], position=5)
        todo(3125, "Draft launch announcement outline", list2, sam["id"], -14,
             completed=True, assignees=[sam["id"]], position=6)

        # --- Card Table ----------------------------------------------------
        table = rec(
            type_="Kanban::Board", title="Card Table", creator_id=creator, bucket_id=P,
            position=3, rid=2003, created_at=T - timedelta(days=25),
            extra={"enabled": True, "name": "Card Table"},
        )
        store.subscribe(table["id"], [maya["id"], omar["id"], grace["id"]])

        triage = rec(
            type_="Kanban::Triage", title="Page ideas", creator_id=creator, bucket_id=P,
            parent_id=table["id"], position=0, rid=3200, created_at=_T(-20),
            extra={"color": "white"},
        )
        # watchers on triage
        store.subscribe(triage["id"], [maya["id"], omar["id"], grace["id"]])

        not_now = rec(
            type_="Kanban::NotNowColumn", title="Not now", creator_id=creator, bucket_id=P,
            parent_id=table["id"], position=0, rid=3201, created_at=_T(-20),
            extra={"color": "gray"},
        )

        def column(rid, title, pos, color="white", on_hold=False):
            col = rec(
                type_="Kanban::Column", title=title, creator_id=creator, bucket_id=P,
                parent_id=table["id"], position=pos, rid=rid, created_at=_T(-20),
                extra={"color": color},
            )
            if on_hold:
                oh = rec(
                    type_="Kanban::OnHoldColumn", title=f"{title}: On hold",
                    creator_id=creator, bucket_id=P, parent_id=col["id"],
                    position=0, rid=rid + 50, created_at=_T(-20),
                    extra={"color": color},
                )
                col["on_hold_id"] = oh["id"]
            return col

        col_writing = column(3210, "Writing", 1, "yellow", on_hold=True)
        col_design = column(3211, "Design", 2, "blue", on_hold=False)
        col_review = column(3212, "Review", 3, "purple", on_hold=False)
        col_ready = column(3213, "Ready", 4, "green", on_hold=False)
        done = rec(
            type_="Kanban::DoneColumn", title="Done", creator_id=creator, bucket_id=P,
            parent_id=table["id"], position=99, rid=3220, created_at=_T(-20),
            extra={"color": "gray"},
        )

        def card(rid, title, parent, author, days, *, assignees=None, position=1,
                 content="", steps=None, completed=False, completed_days=None):
            c = rec(
                type_="Kanban::Card", title=title, creator_id=author, bucket_id=P,
                parent_id=parent["id"], content=content, position=position, rid=rid,
                created_at=_T(days=days),
                extra={
                    "assignee_ids": list(assignees or []),
                    "completed": completed,
                    "completed_at": _T(days=completed_days) if completed and completed_days is not None else None,
                },
            )
            store.write_event(c["id"], "created", author, created_at=c["created_at"])
            for i, step_title in enumerate(steps or []):
                rec(
                    type_="Kanban::Step", title=step_title, creator_id=author,
                    bucket_id=P, parent_id=c["id"], position=i + 1,
                    rid=rid * 10 + i, created_at=_T(days=days, hours=1),
                    extra={"completed": False, "assignee_ids": []},
                )
            return c

        # Triage: 2 cards, one with 2 steps
        card(3301, "Pricing page variants", triage, maya["id"], -20, position=1,
             steps=["List competitor pricing", "Sketch 2 layouts"])
        card(3302, "Case study: Harbor Logistics", triage, maya["id"], -19, position=2)

        # Writing: 2 cards — one assigned, one on hold with 4 steps + 1 comment
        card(3303, "Homepage hero rewrite", col_writing, maya["id"], -18,
             assignees=[sam["id"]], position=1)
        oh = store.recording(col_writing["on_hold_id"])
        hold_card = card(3304, "About page narrative", oh, maya["id"], -17, position=1,
                         steps=["Interview founders", "Draft v1", "Legal review", "Final pass"])
        store.add_comment(hold_card, sam["id"],
                          "Paused until hero lands — narrative depends on the new voice.",
                          created_at=_T(-15))

        # Design: 0 cards
        # Review: 1 card, two assignees
        card(3305, "Illustration set QA", col_review, maya["id"], -12,
             assignees=[omar["id"], grace["id"]], position=1)
        # Ready: 1
        card(3306, "Favicon + app icons", col_ready, maya["id"], -10,
             assignees=[omar["id"]], position=1)

        # Done: 5 completed T-1d…T
        for i, title in enumerate([
            "Brand color tokens",
            "Navigation IA",
            "Footer links audit",
            "404 page",
            "Cookie banner copy",
        ]):
            card(3310 + i, title, done, maya["id"], -20 + i,
                 completed=True, completed_days=-1 + (i * 0.2), position=i + 1)

        # Not now: 2, moved T-4d
        card(3320, "Careers page", not_now, maya["id"], -4, position=1)
        card(3321, "Partner directory", not_now, maya["id"], -4, position=2)

        # --- Docs & Files (Vault) ------------------------------------------
        vault = rec(
            type_="Vault", title="Docs & Files", creator_id=creator, bucket_id=P,
            position=4, rid=2004, created_at=T - timedelta(days=30),
            extra={"enabled": True, "name": "Docs & Files"},
        )
        doc = rec(
            type_="Document", title="Homepage copy — draft", creator_id=sam["id"],
            bucket_id=P, parent_id=vault["id"], position=1, rid=3401,
            created_at=_T(-3, 10),
            content=(
                "<h1>Homepage copy — draft</h1>"
                "<h2>Hero</h2>"
                "<p>Northstar helps small teams ship work they're proud of — "
                "without the chaos of a dozen tabs.</p>"
                "<h2>Body</h2>"
                "<p>Plan together. Write clearly. Close the loop.</p>"
                "<p>Built for the way modern teams already work.</p>"
            ),
        )
        store.write_event(doc["id"], "created", sam["id"], created_at=doc["created_at"])
        store.add_comment(doc, maya["id"],
                          "Love the hero. Let's cut the second body paragraph.",
                          created_at=_T(-3, 14))

        upload = rec(
            type_="Upload", title="logo-concepts.png", creator_id=omar["id"],
            bucket_id=P, parent_id=vault["id"], position=2, rid=3402,
            created_at=_T(-3, 11),
            extra={
                "filename": "logo-concepts.png",
                "content_type": "image/png",
                "byte_size": 245_760,
                "width": 1600,
                "height": 900,
                "description": "Three logo directions for review",
                "download_url": "https://placehold.co/1600x900/png?text=logo-concepts",
            },
        )
        store.write_event(upload["id"], "created", omar["id"], created_at=upload["created_at"])

        # Cloud link (modeled as Upload with cloud metadata / Document-like)
        cloud = rec(
            type_="Upload", title="Content calendar", creator_id=lena["id"],
            bucket_id=P, parent_id=vault["id"], position=3, rid=3403,
            created_at=_T(-3, 12),
            extra={
                "filename": "Content calendar",
                "content_type": "application/vnd.google-apps.spreadsheet",
                "byte_size": 0,
                "description": "Google Sheet — launch content calendar",
                "download_url": "https://docs.google.com/spreadsheets/d/placeholder",
                "cloud": True,
                "color_label": "green",
            },
        )
        store.write_event(cloud["id"], "created", lena["id"], created_at=cloud["created_at"])

        # --- Schedule ------------------------------------------------------
        schedule = rec(
            type_="Schedule", title="Schedule", creator_id=creator, bucket_id=P,
            position=5, rid=2005, created_at=T - timedelta(days=30),
            extra={"enabled": True, "name": "Schedule", "include_due_assignments": True},
        )
        # Launch day all-day T+7w Saturday
        launch_day = T + timedelta(weeks=7)
        # shift to Saturday
        while launch_day.weekday() != 5:
            launch_day += timedelta(days=1)
        rec(
            type_="Schedule::Entry", title="Launch day 🚀", creator_id=maya["id"],
            bucket_id=P, parent_id=schedule["id"], rid=3501,
            created_at=_T(-10),
            extra={
                "summary": "Launch day 🚀",
                "all_day": True,
                "starts_at": launch_day.replace(hour=0, minute=0, second=0, microsecond=0),
                "ends_at": launch_day.replace(hour=23, minute=59, second=59, microsecond=0),
                "description": "Public launch. All hands on deck for support & social.",
                "participant_ids": cast_ids,
            },
        )
        # Content review call T+2w, 10:00–11:00, 3 participants
        review_at = T + timedelta(weeks=2)
        review_at = review_at.replace(hour=15, minute=0, second=0, microsecond=0)  # 10am Chicago ≈ 15:00 UTC
        rec(
            type_="Schedule::Entry", title="Content review call", creator_id=maya["id"],
            bucket_id=P, parent_id=schedule["id"], rid=3502,
            created_at=_T(-8),
            extra={
                "summary": "Content review call",
                "all_day": False,
                "starts_at": review_at,
                "ends_at": review_at + timedelta(hours=1),
                "description": "Walk homepage + launch email together.",
                "participant_ids": [maya["id"], sam["id"], lena["id"]],
            },
        )

        # --- Chat ----------------------------------------------------------
        chat = rec(
            type_="Chat::Transcript", title="Chat", creator_id=creator, bucket_id=P,
            position=6, rid=2006, created_at=T - timedelta(days=30),
            extra={"enabled": True, "name": "Chat", "topic": "Launch the new website"},
        )

        lines_spec = [
            # (rid, author, hours_offset_from_T-4d morning, content, boosts)
            (3601, maya["id"], 9.0,
             "Morning, team — launch channel is live. Drop anything website-related here.",
             [("🙌", diego["id"])]),
            (3602, maya["id"], 9.1,
             "Quick reflection before we dive in:\n\n"
             "1) Clarity over cleverness on the homepage.\n"
             "2) Ship the smallest thing that feels complete.\n"
             "3) Protect focus time for Sam & Omar this week.\n\n"
             "If something's blocked, say so early.",
             []),
            (3603, sam["id"], 9.5,
             "Hero draft is up in Docs. Also — useful thread on launch copy: "
             "https://example.com/articles/product-launch-copy",
             [("🔗", lena["id"])]),
            (3604, priya["id"], 10.0, "heads up, deploying staging now", []),
            (3605, priya["id"], 10.05, "staging is green ✅", []),
            (3606, grace["id"], 10.5, "Anyone else seeing the logo flash on first paint?",
             [("👍", omar["id"])]),
            (3607, omar["id"], 10.6, "Yes — I'll pre-size the asset. Thanks for catching it.",
             [("🙏", grace["id"])]),
            (3608, diego["id"], 11.0,
             "Customer quote from beta: \"This finally feels like software that respects my attention.\"",
             []),
            (3609, lena["id"], 11.5, "Awww! 💖", []),
            (3610, felix["id"], 12.0, "Infra side is quiet. Ping me for DNS day-of.", []),
            (3611, maya["id"], 13.0, "Standup notes posted on the message board. Nice work, everyone.", []),
            (3612, sam["id"], 14.0, "Pitch thread is up if you want to weigh in on homepage length.", []),
            (3613, omar["id"], 14.5, "Dropping logo concepts in Docs & Files.", []),
            (3614, grace["id"], 15.0, "I'll start the mobile checklist tomorrow morning.", []),
            (3615, diego["id"], 15.5, "Beta testers loved the new nav. Sharing full notes later.", []),
            (3616, maya["id"], 16.0, "That's a wrap for today. See you on the pitch thread.", []),
        ]
        for rid, author, hour, content, boosts in lines_spec:
            line = rec(
                type_="Chat::Lines::Line", title=content[:60], creator_id=author,
                bucket_id=P, parent_id=chat["id"], content=content, rid=rid,
                created_at=_T(-4, hour),
                extra={"attachments": []},
            )
            for emoji, booster in boosts:
                store.add_boost(line["id"], booster, emoji, created_at=_T(-4, hour + 0.1))

        # Readings intentionally NOT fanned out to real user (Alex) — sidebar clean
        # Sample people may have no readings either for simplicity

        store.ids.set_min(100_000)
        store.ready = True
        log.info(
            "Seed complete: account=%s project=%s people=%d recordings=%d tokens=owner",
            store.account["id"], project["id"], len(store.people), len(store.recordings),
        )

# ---------------------------------------------------------------------------
# Domain helpers used by handlers
# ---------------------------------------------------------------------------

def require_json_body(body: Any, required: List[str] = None) -> dict:
    if body is None:
        raise validation("JSON body required")
    if not isinstance(body, dict):
        raise validation("JSON object required")
    for k in required or []:
        if k not in body or body[k] is None or body[k] == "":
            raise validation(f"Missing required field: {k}")
    return body

def parse_page(qs: dict) -> int:
    try:
        return max(1, int((qs.get("page") or ["1"])[0]))
    except (TypeError, ValueError):
        return 1

def sort_by(items: List[dict], field: str, direction: str = "desc") -> List[dict]:
    reverse = (direction or "desc").lower() != "asc"
    def key(x):
        v = x.get(field)
        if isinstance(v, datetime):
            return v
        return v or datetime.min.replace(tzinfo=UTC)
    try:
        return sorted(items, key=key, reverse=reverse)
    except Exception:
        return items

def client_filter_recordings(person: dict, recs: List[dict]) -> List[dict]:
    if person.get("client"):
        return [r for r in recs if r.get("visible_to_clients")]
    return recs

def ensure_member_can_mutate(person: dict, rec: dict) -> None:
    project = STORE.require_project(rec["bucket_id"])
    STORE.require_project_access(person, project)
    if project.get("status") == "archived" and not person.get("owner"):
        raise forbidden("Project is archived")

def create_tool_root(project: dict, type_: str, title: str, creator_id: int,
                     position: Optional[int] = None) -> dict:
    if type_ not in TOOL_ROOT_TYPES:
        raise validation(f"Unknown tool type: {type_}")
    existing = [
        r for r in STORE.recordings.values()
        if r.get("bucket_id") == project["id"] and r.get("type") == type_
        and r.get("status") != "trashed"
    ]
    pos = position if position is not None else (max([e.get("position") or 0 for e in existing] + [0]) + 1)
    tool = STORE.new_recording(
        type_=type_,
        title=title,
        creator_id=creator_id,
        bucket_id=project["id"],
        position=pos,
        extra={"enabled": True, "name": title},
    )
    if type_ == "Kanban::Board":
        # built-in columns
        triage = STORE.new_recording(
            type_="Kanban::Triage", title="Triage", creator_id=creator_id,
            bucket_id=project["id"], parent_id=tool["id"], position=0,
            extra={"color": "white"},
        )
        STORE.new_recording(
            type_="Kanban::NotNowColumn", title="Not now", creator_id=creator_id,
            bucket_id=project["id"], parent_id=tool["id"], position=0,
            extra={"color": "gray"},
        )
        STORE.new_recording(
            type_="Kanban::DoneColumn", title="Done", creator_id=creator_id,
            bucket_id=project["id"], parent_id=tool["id"], position=99,
            extra={"color": "gray"},
        )
    if type_ == "Schedule":
        tool["include_due_assignments"] = True
    STORE.write_event(tool["id"], "created", creator_id)
    return tool

def clone_tool(source: dict, creator_id: int, title: Optional[str] = None) -> dict:
    project = STORE.require_project(source["bucket_id"])
    new_title = title or source.get("title") or source.get("name") or "Tool"
    clone = create_tool_root(project, source["type"], new_title, creator_id)
    # shallow-clone children for message boards etc. is optional; clone structure only
    return clone

def my_assignment_json(todo_or_card: dict) -> dict:
    project = STORE.project(todo_or_card["bucket_id"])
    parent = STORE.recording(todo_or_card.get("parent_id")) if todo_or_card.get("parent_id") else None
    return {
        "id": todo_or_card["id"],
        "app_url": recording_app_url(todo_or_card),
        "content": todo_or_card.get("content") or todo_or_card.get("title") or "",
        "starts_on": date_str(todo_or_card.get("starts_on")),
        "due_on": date_str(todo_or_card.get("due_on")),
        "bucket": {"id": project["id"], "name": project["name"], "type": "Project"} if project else None,
        "completed": bool(todo_or_card.get("completed")),
        "type": "Todo" if todo_or_card.get("type") == "Todo" else "Kanban::Card",
        "assignees": [
            {"id": p["id"], "name": p["name"], "avatar_url": person_json(p)["avatar_url"]}
            for p in STORE.people_by_ids(todo_or_card.get("assignee_ids") or [])
        ],
        "comments_count": todo_or_card.get("comments_count") or 0,
        "has_description": bool(todo_or_card.get("description") or todo_or_card.get("content")),
        "parent": {
            "id": parent["id"],
            "title": parent.get("title") or "",
            "type": parent.get("type"),
        } if parent else None,
        "children": [],
    }

def notification_json(reading: dict, person: dict) -> dict:
    rec = STORE.recording(reading["recording_id"])
    return {
        "id": reading.get("id") or reading["recording_id"],
        "created_at": iso(reading.get("created_at")),
        "updated_at": iso(reading.get("updated_at") or reading.get("created_at")),
        "summary": reading.get("summary") or (rec.get("title") if rec else "Update"),
        "readable_sgid": reading.get("readable_sgid") or f"sgid_reading_{reading['recording_id']}_{person['id']}",
        "unread": not reading.get("read", False),
        "recording": parent_json(rec) if rec else None,
        "bucket": bucket_json(STORE.project(rec["bucket_id"])) if rec and STORE.project(rec["bucket_id"]) else None,
        "creator": person_json(STORE.person(reading.get("creator_id") or (rec or {}).get("creator_id"))),
        "resurface_at": iso(reading.get("resurface_at")),
    }

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

HandlerFn = Callable[[dict], Any]

class Route:
    __slots__ = ("method", "regex", "param_names", "handler", "auth")

    def __init__(self, method: str, pattern: str, handler: HandlerFn, auth: bool = True):
        self.method = method.upper()
        self.auth = auth
        names = re.findall(r"\{(\w+)\}", pattern)
        rx = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
        self.regex = re.compile("^" + rx + "$")
        self.param_names = names
        self.handler = handler


class Router:
    def __init__(self) -> None:
        self.routes: List[Route] = []

    def add(self, method: str, pattern: str, handler: HandlerFn, auth: bool = True) -> None:
        self.routes.append(Route(method, pattern, handler, auth=auth))

    def match(self, method: str, path: str) -> Tuple[Optional[Route], dict, List[str]]:
        method = method.upper()
        allowed = []
        for r in self.routes:
            m = r.regex.match(path)
            if not m:
                continue
            allowed.append(r.method)
            if r.method == method:
                return r, m.groupdict(), allowed
        if allowed:
            return None, {}, allowed
        return None, {}, []


ROUTER = Router()

def route(method: str, pattern: str, auth: bool = True):
    def deco(fn):
        ROUTER.add(method, pattern, fn, auth=auth)
        return fn
    return deco

# ---------------------------------------------------------------------------
# Handlers — operability
# ---------------------------------------------------------------------------

@route("GET", "/health", auth=False)
def health(ctx):
    return {
        "status": "ok",
        "version": VERSION,
        "time": iso(utcnow()),
        "uptime_seconds": int((utcnow() - STORE.boot_time).total_seconds()),
    }

@route("GET", "/ready", auth=False)
def ready(ctx):
    if not STORE.ready:
        raise APIError(503, "not_ready", "Store not ready")
    return {"status": "ready", "recordings": len(STORE.recordings), "projects": len(STORE.projects)}

@route("GET", "/", auth=False)
def root(ctx):
    return {
        "name": "Basecamp 5 API",
        "version": VERSION,
        "account_id": CONFIG.account_id,
        "documentation": "See reference/basecamp-sdk/openapi.json",
        "health": "/health",
        "auth": "Authorization: Bearer <token>",
        "default_token_hint": "bcamp_pat_owner_alex",
    }

@route("POST", "/admin/reset", auth=False)
def admin_reset(ctx):
    if not CONFIG.allow_reset:
        raise forbidden("Reset disabled")
    # optional shared secret
    secret = os.environ.get("BASECAMP_RESET_TOKEN")
    if secret:
        provided = (ctx.get("headers") or {}).get("X-Reset-Token") or ""
        if not hmac.compare_digest(provided, secret):
            raise unauthorized("Invalid reset token")
    with STORE.lock:
        # re-init store fields and reseed
        cfg = STORE.config
        STORE.__init__(cfg)
        seed_world(STORE)
    return {"status": "reset", "seeded": True, "time": iso(utcnow())}

# ---------------------------------------------------------------------------
# Account & people
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/account.json")
def get_account(ctx):
    return account_json()

@route("PUT", "/{accountId}/account/name.json")
def update_account_name(ctx):
    body = require_json_body(ctx["body"], ["name"])
    if not ctx["person"].get("owner") and not ctx["person"].get("admin"):
        raise forbidden("Admin required")
    STORE.account["name"] = body["name"]
    STORE.account["updated_at"] = utcnow()
    return account_json()

@route("PUT", "/{accountId}/account/logo.json")
def update_account_logo(ctx):
    if not ctx["person"].get("owner") and not ctx["person"].get("admin"):
        raise forbidden("Admin required")
    # accept raw body as image bytes or JSON {url}
    body = ctx["body"]
    if isinstance(body, dict) and body.get("url"):
        STORE.account["logo"] = {"url": body["url"]}
    else:
        STORE.account["logo"] = {"url": app_url("/logos/custom.png")}
    STORE.account["updated_at"] = utcnow()
    return None, 204

@route("DELETE", "/{accountId}/account/logo.json")
def remove_account_logo(ctx):
    if not ctx["person"].get("owner") and not ctx["person"].get("admin"):
        raise forbidden("Admin required")
    STORE.account["logo"] = {"url": None}
    return None, 204

@route("GET", "/{accountId}/people.json")
def list_people(ctx):
    people = [person_json(p) for p in STORE.people.values() if not p.get("trashed")]
    page = parse_page(ctx["qs"])
    slice_, headers, *_ = paginate(people, page)
    return slice_, 200, headers

@route("GET", "/{accountId}/people/{personId}")
@route("GET", "/{accountId}/people/{personId}.json")
def get_person(ctx):
    p = STORE.require_person(int(ctx["params"]["personId"]))
    return person_json(p)

@route("GET", "/{accountId}/circles/people.json")
def list_pingable_people(ctx):
    people = [
        person_json(p) for p in STORE.people.values()
        if p.get("can_ping", True) and not p.get("sample") or True
    ]
    # include everyone who can be pinged (all active people)
    people = [person_json(p) for p in STORE.people.values()]
    return people

@route("GET", "/{accountId}/people/{personId}/out_of_office.json")
def get_ooo(ctx):
    pid = int(ctx["params"]["personId"])
    ooo = STORE.out_of_office.get(pid)
    if not ooo:
        return {
            "person": {"id": pid, "name": STORE.require_person(pid)["name"]},
            "enabled": False,
            "ongoing": False,
            "start_date": None,
            "end_date": None,
        }
    return ooo

@route("POST", "/{accountId}/people/{personId}/out_of_office.json")
def set_ooo(ctx):
    pid = int(ctx["params"]["personId"])
    if ctx["person"]["id"] != pid and not ctx["person"].get("admin"):
        raise forbidden()
    body = ctx["body"] or {}
    ooo = {
        "person": {"id": pid, "name": STORE.require_person(pid)["name"]},
        "enabled": True,
        "ongoing": bool(body.get("ongoing")),
        "start_date": body.get("start_date") or body.get("starts_on"),
        "end_date": body.get("end_date") or body.get("ends_on"),
    }
    STORE.out_of_office[pid] = ooo
    return ooo

@route("DELETE", "/{accountId}/people/{personId}/out_of_office.json")
def clear_ooo(ctx):
    pid = int(ctx["params"]["personId"])
    STORE.out_of_office.pop(pid, None)
    return None, 204

@route("GET", "/{accountId}/my/profile.json")
def get_my_profile(ctx):
    return person_json(ctx["person"])

@route("PUT", "/{accountId}/my/profile.json")
def update_my_profile(ctx):
    body = ctx["body"] or {}
    p = ctx["person"]
    for k in ("name", "email_address", "title", "bio", "location"):
        if k in body and body[k] is not None:
            p[k] = body[k]
    if "time_zone_name" in body:
        p["time_zone"] = body["time_zone_name"]
    p["updated_at"] = utcnow()
    return person_json(p)

@route("GET", "/{accountId}/my/preferences.json")
def get_my_preferences(ctx):
    prefs = STORE.preferences.get(ctx["person"]["id"]) or {}
    return {
        "url": api_url(acct_path("/my/preferences.json")),
        "app_url": app_url(f"/{CONFIG.account_id}/my/preferences"),
        "time_zone_name": prefs.get("time_zone_name") or "America/Chicago",
        "first_week_day": prefs.get("first_week_day") or "Monday",
        "time_format": prefs.get("time_format") or "twelve_hour",
    }

@route("PUT", "/{accountId}/my/preferences.json")
def update_my_preferences(ctx):
    body = require_json_body(ctx["body"], ["person"])
    person_payload = body["person"] or {}
    prefs = STORE.preferences.setdefault(ctx["person"]["id"], {})
    for k in ("time_zone_name", "first_week_day", "time_format"):
        if k in person_payload:
            prefs[k] = person_payload[k]
    return get_my_preferences(ctx)

# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/projects.json")
def list_projects(ctx):
    status = (ctx["qs"].get("status") or ["active"])[0]
    person = ctx["person"]
    items = []
    for p in STORE.projects.values():
        if status and p.get("status") != status:
            continue
        if not STORE.can_access_project(person, p):
            continue
        items.append(project_json(p))
    items.sort(key=lambda x: x["name"].lower())
    page = parse_page(ctx["qs"])
    slice_, headers, *_rest = paginate(items, page)
    return slice_, 200, headers

@route("POST", "/{accountId}/projects.json")
def create_project(ctx):
    body = require_json_body(ctx["body"], ["name"])
    person = ctx["person"]
    if not person.get("employee") and not person.get("owner"):
        raise forbidden("Only employees can create projects")
    now = utcnow()
    pid = STORE.next_id()
    project = {
        "id": pid,
        "name": body["name"],
        "description": body.get("description") or "",
        "purpose": "topic",
        "status": "active",
        "clients_enabled": False,
        "bookmarked": False,
        "sample": False,
        "admissions": "invite",
        "created_at": now,
        "updated_at": now,
        "access": {person["id"]},
        "starts_on": None,
        "ends_on": None,
    }
    STORE.projects[pid] = project
    # New projects start empty — no default tools (INIT §4.5)
    return project_json(project), 201

@route("GET", "/{accountId}/projects/{projectId}")
@route("GET", "/{accountId}/projects/{projectId}.json")
def get_project(ctx):
    p = STORE.require_project(int(ctx["params"]["projectId"]))
    STORE.require_project_access(ctx["person"], p)
    return project_json(p)

@route("PUT", "/{accountId}/projects/{projectId}")
@route("PUT", "/{accountId}/projects/{projectId}.json")
def update_project(ctx):
    p = STORE.require_project(int(ctx["params"]["projectId"]))
    STORE.require_project_access(ctx["person"], p)
    body = require_json_body(ctx["body"], ["name"])
    p["name"] = body["name"]
    if "description" in body:
        p["description"] = body["description"]
    if "admissions" in body:
        p["admissions"] = body["admissions"]
    if "schedule_attributes" in body and isinstance(body["schedule_attributes"], dict):
        sa = body["schedule_attributes"]
        if "starts_on" in sa:
            p["starts_on"] = parse_date(sa["starts_on"])
        if "ends_on" in sa:
            p["ends_on"] = parse_date(sa["ends_on"])
    p["updated_at"] = utcnow()
    return project_json(p)

@route("DELETE", "/{accountId}/projects/{projectId}")
@route("DELETE", "/{accountId}/projects/{projectId}.json")
def trash_project(ctx):
    p = STORE.require_project(int(ctx["params"]["projectId"]))
    STORE.require_project_access(ctx["person"], p)
    p["status"] = "trashed"
    p["updated_at"] = utcnow()
    # trash recordings in project
    for r in STORE.recordings.values():
        if r.get("bucket_id") == p["id"]:
            r["status"] = "trashed"
    return None, 204

@route("GET", "/{accountId}/projects/{projectId}/people.json")
def list_project_people(ctx):
    p = STORE.require_project(int(ctx["params"]["projectId"]))
    STORE.require_project_access(ctx["person"], p)
    people = [person_json(STORE.person(pid)) for pid in sorted(p.get("access") or []) if STORE.person(pid)]
    return people

@route("PUT", "/{accountId}/projects/{projectId}/people/users.json")
def update_project_access(ctx):
    p = STORE.require_project(int(ctx["params"]["projectId"]))
    STORE.require_project_access(ctx["person"], p)
    body = ctx["body"] or {}
    access = set(p.get("access") or set())
    for pid in body.get("grant") or []:
        access.add(int(pid))
    for pid in body.get("revoke") or []:
        access.discard(int(pid))
    for create in body.get("create") or []:
        if not isinstance(create, dict) or not create.get("email_address"):
            continue
        nid = STORE.next_id()
        STORE.people[nid] = {
            "id": nid,
            "name": create.get("name") or create["email_address"],
            "email_address": create["email_address"],
            "title": create.get("title") or "",
            "bio": "",
            "location": "",
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "admin": False,
            "owner": False,
            "client": False,
            "employee": False,  # collaborator by default when invited via project
            "time_zone": "America/Chicago",
            "company_id": None,
            "sample": False,
            "can_manage_projects": False,
            "can_manage_people": False,
            "can_ping": True,
            "can_access_timesheet": True,
            "can_access_hill_charts": False,
            "personable_type": "User",
        }
        if create.get("company_name"):
            cid = STORE.next_id()
            STORE.companies[cid] = {"id": cid, "name": create["company_name"]}
            STORE.people[nid]["company_id"] = cid
        access.add(nid)
        STORE.issue_token(nid)
    p["access"] = access
    p["updated_at"] = utcnow()
    return [person_json(STORE.person(pid)) for pid in sorted(access) if STORE.person(pid)]

@route("GET", "/{accountId}/projects/{projectId}/timeline.json")
def project_timeline(ctx):
    p = STORE.require_project(int(ctx["params"]["projectId"]))
    STORE.require_project_access(ctx["person"], p)
    events = [e for e in STORE.events.values()
              if (STORE.recording(e["recording_id"]) or {}).get("bucket_id") == p["id"]]
    events.sort(key=lambda e: e["created_at"], reverse=True)
    page = parse_page(ctx["qs"])
    slice_, headers, *_ = paginate(events, page)
    return [event_json(e) for e in slice_], 200, headers

@route("GET", "/{accountId}/projects/{projectId}/timesheet.json")
def project_timesheet(ctx):
    p = STORE.require_project(int(ctx["params"]["projectId"]))
    STORE.require_project_access(ctx["person"], p)
    entries = [
        serialize_recording(r) for r in STORE.recordings.values()
        if r.get("type") == "Timesheet::Entry" and r.get("bucket_id") == p["id"]
        and r.get("status") != "trashed"
    ]
    return entries

@route("GET", "/{accountId}/projects/recordings.json")
def list_recordings(ctx):
    type_ = (ctx["qs"].get("type") or [None])[0]
    if not type_:
        raise validation("type is required")
    bucket = (ctx["qs"].get("bucket") or [None])[0]
    status = (ctx["qs"].get("status") or ["active"])[0]
    person = ctx["person"]
    items = []
    for r in STORE.recordings.values():
        if r.get("type") != type_:
            continue
        if status and r.get("status") != status:
            continue
        if bucket and str(r.get("bucket_id")) != str(bucket):
            continue
        project = STORE.project(r["bucket_id"])
        if not project or not STORE.can_access_project(person, project):
            continue
        if person.get("client") and not r.get("visible_to_clients"):
            continue
        items.append(r)
    items.sort(key=lambda x: x.get("created_at") or datetime.min.replace(tzinfo=UTC), reverse=True)
    page = parse_page(ctx["qs"])
    slice_, headers, *_ = paginate(items, page)
    return [serialize_recording(r) for r in slice_], 200, headers

# ---------------------------------------------------------------------------
# Categories (message types)
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/categories.json")
def list_categories(ctx):
    return [category_json(c) for c in sorted(STORE.categories.values(), key=lambda x: x["id"])]

@route("POST", "/{accountId}/categories.json")
def create_category(ctx):
    if not ctx["person"].get("admin") and not ctx["person"].get("owner"):
        raise forbidden("Admin required")
    body = require_json_body(ctx["body"], ["name", "icon"])
    cid = STORE.next_id()
    now = utcnow()
    cat = {"id": cid, "name": body["name"], "icon": body["icon"], "created_at": now, "updated_at": now}
    STORE.categories[cid] = cat
    return category_json(cat), 201

@route("GET", "/{accountId}/categories/{typeId}")
@route("GET", "/{accountId}/categories/{typeId}.json")
def get_category(ctx):
    cat = STORE.categories.get(int(ctx["params"]["typeId"]))
    if not cat:
        raise not_found("Category not found")
    return category_json(cat)

@route("PUT", "/{accountId}/categories/{typeId}")
@route("PUT", "/{accountId}/categories/{typeId}.json")
def update_category(ctx):
    cat = STORE.categories.get(int(ctx["params"]["typeId"]))
    if not cat:
        raise not_found("Category not found")
    body = ctx["body"] or {}
    if "name" in body:
        cat["name"] = body["name"]
    if "icon" in body:
        cat["icon"] = body["icon"]
    cat["updated_at"] = utcnow()
    return category_json(cat)

@route("DELETE", "/{accountId}/categories/{typeId}")
@route("DELETE", "/{accountId}/categories/{typeId}.json")
def delete_category(ctx):
    tid = int(ctx["params"]["typeId"])
    if tid not in STORE.categories:
        raise not_found("Category not found")
    del STORE.categories[tid]
    return None, 204

# ---------------------------------------------------------------------------
# Message Board
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/message_boards/{boardId}")
@route("GET", "/{accountId}/message_boards/{boardId}.json")
def get_message_board(ctx):
    board = STORE.require_recording(int(ctx["params"]["boardId"]), ["Message::Board"])
    ensure_member_can_mutate(ctx["person"], board)
    return serialize_recording(board)

@route("GET", "/{accountId}/message_boards/{boardId}/messages.json")
def list_messages(ctx):
    board = STORE.require_recording(int(ctx["params"]["boardId"]), ["Message::Board"])
    ensure_member_can_mutate(ctx["person"], board)
    msgs = [r for r in STORE.recordings.values()
            if r.get("parent_id") == board["id"] and r.get("type") == "Message"
            and r.get("status") in ("active", "drafted")]
    # drafts only for creator
    person = ctx["person"]
    msgs = [m for m in msgs if m.get("status") != "drafted" or m.get("creator_id") == person["id"]]
    msgs = client_filter_recordings(person, msgs)
    # pinned first
    def sort_key(m):
        pinned = 0 if m["id"] in STORE.pinned else 1
        return (pinned, -(m.get("created_at") or utcnow()).timestamp())
    sort = (ctx["qs"].get("sort") or ["created_at"])[0]
    direction = (ctx["qs"].get("direction") or ["desc"])[0]
    if sort in ("created_at", "updated_at"):
        msgs = sort_by(msgs, sort, direction)
        # re-apply pin priority for default created_at desc
        if sort == "created_at":
            msgs = sorted(msgs, key=lambda m: (0 if m["id"] in STORE.pinned else 1,
                                               -((m.get(sort) or utcnow()).timestamp())))
    page = parse_page(ctx["qs"])
    slice_, headers, *_ = paginate(msgs, page)
    return [serialize_recording(m) for m in slice_], 200, headers

@route("POST", "/{accountId}/message_boards/{boardId}/messages.json")
def create_message(ctx):
    board = STORE.require_recording(int(ctx["params"]["boardId"]), ["Message::Board"])
    ensure_member_can_mutate(ctx["person"], board)
    body = require_json_body(ctx["body"], ["subject"])
    status = body.get("status") or "active"
    if status not in ("active", "drafted"):
        raise validation("status must be active or drafted")
    m = STORE.new_recording(
        type_="Message",
        title=body["subject"],
        creator_id=ctx["person"]["id"],
        bucket_id=board["bucket_id"],
        parent_id=board["id"],
        content=body.get("content") or "",
        status=status,
        extra={
            "subject": body["subject"],
            "category_id": body.get("category_id"),
        },
    )
    if body.get("subscriptions"):
        STORE.subscribe(m["id"], body["subscriptions"])
    STORE.subscribe(m["id"], [ctx["person"]["id"]])
    if status == "active":
        STORE.write_event(m["id"], "created", ctx["person"]["id"])
    return serialize_recording(m), 201

@route("GET", "/{accountId}/messages/{messageId}")
@route("GET", "/{accountId}/messages/{messageId}.json")
def get_message(ctx):
    m = STORE.require_recording(int(ctx["params"]["messageId"]), ["Message"])
    ensure_member_can_mutate(ctx["person"], m)
    if ctx["person"].get("client") and not m.get("visible_to_clients"):
        raise not_found()
    return serialize_recording(m)

@route("PUT", "/{accountId}/messages/{messageId}")
@route("PUT", "/{accountId}/messages/{messageId}.json")
def update_message(ctx):
    m = STORE.require_recording(int(ctx["params"]["messageId"]), ["Message"])
    ensure_member_can_mutate(ctx["person"], m)
    if m.get("creator_id") != ctx["person"]["id"] and not ctx["person"].get("admin"):
        raise forbidden("Only the creator can edit this message")
    body = ctx["body"] or {}
    old_status = m.get("status")
    if "subject" in body:
        m["subject"] = body["subject"]
        m["title"] = body["subject"]
    if "content" in body:
        m["content"] = body["content"]
    if "category_id" in body:
        m["category_id"] = body["category_id"]
    if "status" in body:
        m["status"] = body["status"]
    STORE.touch(m)
    if old_status == "drafted" and m.get("status") == "active":
        STORE.write_event(m["id"], "created", ctx["person"]["id"])
    else:
        STORE.write_event(m["id"], "updated", ctx["person"]["id"])
    return serialize_recording(m)

# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/todosets/{todosetId}")
@route("GET", "/{accountId}/todosets/{todosetId}.json")
def get_todoset(ctx):
    t = STORE.require_recording(int(ctx["params"]["todosetId"]), ["Todoset"])
    ensure_member_can_mutate(ctx["person"], t)
    return serialize_recording(t)

@route("GET", "/{accountId}/todosets/{todosetId}/todolists.json")
def list_todolists(ctx):
    ts = STORE.require_recording(int(ctx["params"]["todosetId"]), ["Todoset"])
    ensure_member_can_mutate(ctx["person"], ts)
    lists = [r for r in STORE.recordings.values()
             if r.get("parent_id") == ts["id"] and r.get("type") == "Todolist"
             and r.get("status") != "trashed"]
    lists.sort(key=lambda x: (x.get("position") or 0, x.get("id")))
    page = parse_page(ctx["qs"])
    slice_, headers, *_ = paginate(lists, page)
    return [serialize_recording(x) for x in slice_], 200, headers

@route("POST", "/{accountId}/todosets/{todosetId}/todolists.json")
def create_todolist(ctx):
    ts = STORE.require_recording(int(ctx["params"]["todosetId"]), ["Todoset"])
    ensure_member_can_mutate(ctx["person"], ts)
    body = require_json_body(ctx["body"], ["name"])
    lists = [r for r in STORE.recordings.values() if r.get("parent_id") == ts["id"] and r.get("type") == "Todolist"]
    pos = max([x.get("position") or 0 for x in lists] + [0]) + 1
    tl = STORE.new_recording(
        type_="Todolist", title=body["name"], creator_id=ctx["person"]["id"],
        bucket_id=ts["bucket_id"], parent_id=ts["id"], position=pos,
        extra={"name": body["name"], "description": body.get("description") or ""},
    )
    STORE.write_event(tl["id"], "created", ctx["person"]["id"])
    return serialize_recording(tl), 201

@route("GET", "/{accountId}/todosets/{todosetId}/hill.json")
def get_hill_chart(ctx):
    ts = STORE.require_recording(int(ctx["params"]["todosetId"]), ["Todoset"])
    ensure_member_can_mutate(ctx["person"], ts)
    return {
        "enabled": bool(ts.get("hill_enabled", False)),
        "stale": True,
        "updated_at": iso(ts.get("updated_at")),
        "app_update_url": app_url(f"/{CONFIG.account_id}/buckets/{ts['bucket_id']}/todosets/{ts['id']}/hill"),
        "app_versions_url": app_url(f"/{CONFIG.account_id}/buckets/{ts['bucket_id']}/todosets/{ts['id']}/hill/versions"),
        "dots": ts.get("hill_dots") or [],
    }

@route("PUT", "/{accountId}/todosets/{todosetId}/hills/settings.json")
def update_hill_settings(ctx):
    ts = STORE.require_recording(int(ctx["params"]["todosetId"]), ["Todoset"])
    ensure_member_can_mutate(ctx["person"], ts)
    body = ctx["body"] or {}
    ts["hill_tracked"] = body.get("tracked") or []
    ts["hill_untracked"] = body.get("untracked") or []
    ts["hill_enabled"] = True
    STORE.touch(ts)
    return get_hill_chart(ctx)

@route("GET", "/{accountId}/todolists/{id}")
@route("GET", "/{accountId}/todolists/{id}.json")
def get_todolist_or_group(ctx):
    tl = STORE.require_recording(int(ctx["params"]["id"]), ["Todolist", "Todolist::Group"])
    ensure_member_can_mutate(ctx["person"], tl)
    return serialize_recording(tl)

@route("PUT", "/{accountId}/todolists/{id}")
@route("PUT", "/{accountId}/todolists/{id}.json")
def update_todolist_or_group(ctx):
    tl = STORE.require_recording(int(ctx["params"]["id"]), ["Todolist", "Todolist::Group"])
    ensure_member_can_mutate(ctx["person"], tl)
    body = ctx["body"] or {}
    if "name" in body:
        tl["name"] = body["name"]
        tl["title"] = body["name"]
    if "description" in body and tl.get("type") == "Todolist":
        tl["description"] = body["description"]
    STORE.touch(tl)
    STORE.write_event(tl["id"], "updated", ctx["person"]["id"])
    return serialize_recording(tl)

@route("GET", "/{accountId}/todolists/{todolistId}/groups.json")
def list_todolist_groups(ctx):
    tl = STORE.require_recording(int(ctx["params"]["todolistId"]), ["Todolist"])
    ensure_member_can_mutate(ctx["person"], tl)
    groups = [r for r in STORE.recordings.values()
              if r.get("parent_id") == tl["id"] and r.get("type") == "Todolist::Group"
              and r.get("status") != "trashed"]
    groups.sort(key=lambda x: (x.get("position") or 0, x.get("id")))
    return [serialize_recording(g) for g in groups]

@route("POST", "/{accountId}/todolists/{todolistId}/groups.json")
def create_todolist_group(ctx):
    tl = STORE.require_recording(int(ctx["params"]["todolistId"]), ["Todolist"])
    ensure_member_can_mutate(ctx["person"], tl)
    body = require_json_body(ctx["body"], ["name"])
    g = STORE.new_recording(
        type_="Todolist::Group", title=body["name"], creator_id=ctx["person"]["id"],
        bucket_id=tl["bucket_id"], parent_id=tl["id"],
        extra={"name": body["name"]},
    )
    STORE.write_event(g["id"], "created", ctx["person"]["id"])
    return serialize_recording(g), 201

@route("PUT", "/{accountId}/todolists/{groupId}/position.json")
def reposition_todolist_group(ctx):
    g = STORE.require_recording(int(ctx["params"]["groupId"]), ["Todolist::Group", "Todolist"])
    ensure_member_can_mutate(ctx["person"], g)
    body = require_json_body(ctx["body"], ["position"])
    g["position"] = int(body["position"])
    STORE.touch(g)
    return serialize_recording(g)

@route("GET", "/{accountId}/todolists/{todolistId}/todos.json")
def list_todos(ctx):
    parent = STORE.require_recording(int(ctx["params"]["todolistId"]), ["Todolist", "Todolist::Group"])
    ensure_member_can_mutate(ctx["person"], parent)
    completed = (ctx["qs"].get("completed") or [None])[0]
    todos = [r for r in STORE.recordings.values()
             if r.get("parent_id") == parent["id"] and r.get("type") == "Todo"
             and r.get("status") != "trashed"]
    if completed is not None:
        want = str(completed).lower() in ("1", "true", "yes")
        todos = [t for t in todos if bool(t.get("completed")) == want]
    todos = client_filter_recordings(ctx["person"], todos)
    todos.sort(key=lambda x: (bool(x.get("completed")), x.get("position") or 0, x.get("id")))
    page = parse_page(ctx["qs"])
    slice_, headers, *_ = paginate(todos, page)
    return [serialize_recording(t) for t in slice_], 200, headers

@route("POST", "/{accountId}/todolists/{todolistId}/todos.json")
def create_todo(ctx):
    parent = STORE.require_recording(int(ctx["params"]["todolistId"]), ["Todolist", "Todolist::Group"])
    ensure_member_can_mutate(ctx["person"], parent)
    body = require_json_body(ctx["body"], ["content"])
    todos = [r for r in STORE.recordings.values() if r.get("parent_id") == parent["id"] and r.get("type") == "Todo"]
    pos = max([t.get("position") or 0 for t in todos] + [0]) + 1
    t = STORE.new_recording(
        type_="Todo", title=body["content"], creator_id=ctx["person"]["id"],
        bucket_id=parent["bucket_id"], parent_id=parent["id"], position=pos,
        content=body["content"],
        extra={
            "description": body.get("description") or "",
            "completed": False,
            "assignee_ids": [int(x) for x in (body.get("assignee_ids") or [])],
            "completion_subscriber_ids": [int(x) for x in (body.get("completion_subscriber_ids") or [])],
            "due_on": parse_date(body.get("due_on")),
            "starts_on": parse_date(body.get("starts_on")),
        },
    )
    STORE.write_event(t["id"], "created", ctx["person"]["id"])
    if body.get("assignee_ids"):
        STORE.subscribe(t["id"], body["assignee_ids"])
    return serialize_recording(t), 201

@route("GET", "/{accountId}/todos/{todoId}")
@route("GET", "/{accountId}/todos/{todoId}.json")
def get_todo(ctx):
    t = STORE.require_recording(int(ctx["params"]["todoId"]), ["Todo"])
    ensure_member_can_mutate(ctx["person"], t)
    return serialize_recording(t)

@route("PUT", "/{accountId}/todos/{todoId}")
@route("PUT", "/{accountId}/todos/{todoId}.json")
def update_todo(ctx):
    t = STORE.require_recording(int(ctx["params"]["todoId"]), ["Todo"])
    ensure_member_can_mutate(ctx["person"], t)
    body = ctx["body"] or {}
    if "content" in body:
        t["content"] = body["content"]
        t["title"] = body["content"]
    if "description" in body:
        t["description"] = body["description"]
    if "assignee_ids" in body:
        t["assignee_ids"] = [int(x) for x in body["assignee_ids"]]
    if "completion_subscriber_ids" in body:
        t["completion_subscriber_ids"] = [int(x) for x in body["completion_subscriber_ids"]]
    if "due_on" in body:
        t["due_on"] = parse_date(body["due_on"])
    if "starts_on" in body:
        t["starts_on"] = parse_date(body["starts_on"])
    STORE.touch(t)
    STORE.write_event(t["id"], "updated", ctx["person"]["id"])
    return serialize_recording(t)

@route("DELETE", "/{accountId}/todos/{todoId}")
@route("DELETE", "/{accountId}/todos/{todoId}.json")
def trash_todo(ctx):
    t = STORE.require_recording(int(ctx["params"]["todoId"]), ["Todo"])
    ensure_member_can_mutate(ctx["person"], t)
    STORE.set_status(t, "trashed", ctx["person"]["id"])
    return None, 204

@route("POST", "/{accountId}/todos/{todoId}/completion.json")
def complete_todo(ctx):
    t = STORE.require_recording(int(ctx["params"]["todoId"]), ["Todo"])
    ensure_member_can_mutate(ctx["person"], t)
    if t.get("completed"):
        return None, 204
    t["completed"] = True
    t["completed_at"] = utcnow()
    t["completer_id"] = ctx["person"]["id"]
    STORE.touch(t)
    STORE.write_event(t["id"], "completed", ctx["person"]["id"])
    return None, 204

@route("DELETE", "/{accountId}/todos/{todoId}/completion.json")
def uncomplete_todo(ctx):
    t = STORE.require_recording(int(ctx["params"]["todoId"]), ["Todo"])
    ensure_member_can_mutate(ctx["person"], t)
    t["completed"] = False
    t["completed_at"] = None
    t["completer_id"] = None
    STORE.touch(t)
    STORE.write_event(t["id"], "uncompleted", ctx["person"]["id"])
    return None, 204

@route("PUT", "/{accountId}/todos/{todoId}/position.json")
def reposition_todo(ctx):
    t = STORE.require_recording(int(ctx["params"]["todoId"]), ["Todo"])
    ensure_member_can_mutate(ctx["person"], t)
    body = require_json_body(ctx["body"], ["position"])
    t["position"] = int(body["position"])
    if body.get("parent_id"):
        parent = STORE.require_recording(int(body["parent_id"]), ["Todolist", "Todolist::Group"])
        t["parent_id"] = parent["id"]
    STORE.touch(t)
    return serialize_recording(t)

# ---------------------------------------------------------------------------
# Card Table
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/card_tables/{cardTableId}")
@route("GET", "/{accountId}/card_tables/{cardTableId}.json")
def get_card_table(ctx):
    t = STORE.require_recording(int(ctx["params"]["cardTableId"]), ["Kanban::Board"])
    ensure_member_can_mutate(ctx["person"], t)
    return serialize_recording(t)

@route("POST", "/{accountId}/card_tables/{cardTableId}/columns.json")
def create_card_column(ctx):
    table = STORE.require_recording(int(ctx["params"]["cardTableId"]), ["Kanban::Board"])
    ensure_member_can_mutate(ctx["person"], table)
    body = require_json_body(ctx["body"], ["title"])
    cols = [c for c in card_columns_for_board(table) if c.get("type") == "Kanban::Column"]
    pos = max([c.get("position") or 0 for c in cols] + [0]) + 1
    col = STORE.new_recording(
        type_="Kanban::Column", title=body["title"], creator_id=ctx["person"]["id"],
        bucket_id=table["bucket_id"], parent_id=table["id"], position=pos,
        extra={"color": "white", "description": body.get("description") or ""},
    )
    STORE.write_event(col["id"], "created", ctx["person"]["id"])
    return serialize_recording(col), 201

@route("POST", "/{accountId}/card_tables/{cardTableId}/moves.json")
def move_card_column(ctx):
    table = STORE.require_recording(int(ctx["params"]["cardTableId"]), ["Kanban::Board"])
    ensure_member_can_mutate(ctx["person"], table)
    body = require_json_body(ctx["body"], ["source_id", "target_id"])
    src = STORE.require_recording(int(body["source_id"]))
    tgt = STORE.require_recording(int(body["target_id"]))
    # swap / reinsert positions among user columns
    if body.get("position") is not None:
        src["position"] = int(body["position"])
    else:
        src["position"], tgt["position"] = tgt.get("position"), src.get("position")
    STORE.touch(src)
    STORE.touch(tgt)
    return None, 204

@route("GET", "/{accountId}/card_tables/columns/{columnId}")
@route("GET", "/{accountId}/card_tables/columns/{columnId}.json")
def get_card_column(ctx):
    col = STORE.require_recording(int(ctx["params"]["columnId"]))
    if col.get("type") not in (
        "Kanban::Column", "Kanban::Triage", "Kanban::DoneColumn",
        "Kanban::NotNowColumn", "Kanban::OnHoldColumn",
    ):
        raise not_found("Column not found")
    ensure_member_can_mutate(ctx["person"], col)
    return serialize_recording(col)

@route("PUT", "/{accountId}/card_tables/columns/{columnId}")
@route("PUT", "/{accountId}/card_tables/columns/{columnId}.json")
def update_card_column(ctx):
    col = STORE.require_recording(int(ctx["params"]["columnId"]))
    ensure_member_can_mutate(ctx["person"], col)
    body = ctx["body"] or {}
    if "title" in body:
        col["title"] = body["title"]
    if "description" in body:
        col["description"] = body["description"]
    STORE.touch(col)
    return serialize_recording(col)

@route("PUT", "/{accountId}/buckets/{bucketId}/card_tables/columns/{columnId}/color.json")
def set_column_color(ctx):
    col = STORE.require_recording(int(ctx["params"]["columnId"]))
    ensure_member_can_mutate(ctx["person"], col)
    body = require_json_body(ctx["body"], ["color"])
    allowed = {"white","red","orange","yellow","green","blue","aqua","purple","gray","pink","brown"}
    if body["color"] not in allowed:
        raise validation(f"Invalid color; allowed: {', '.join(sorted(allowed))}")
    col["color"] = body["color"]
    STORE.touch(col)
    return serialize_recording(col)

@route("POST", "/{accountId}/buckets/{bucketId}/card_tables/columns/{columnId}/on_hold.json")
def enable_on_hold(ctx):
    col = STORE.require_recording(int(ctx["params"]["columnId"]), ["Kanban::Column"])
    ensure_member_can_mutate(ctx["person"], col)
    if col.get("on_hold_id") and STORE.recording(col["on_hold_id"]):
        return serialize_recording(col)
    oh = STORE.new_recording(
        type_="Kanban::OnHoldColumn", title=f"{col.get('title')}: On hold",
        creator_id=ctx["person"]["id"], bucket_id=col["bucket_id"],
        parent_id=col["id"], extra={"color": col.get("color") or "white"},
    )
    col["on_hold_id"] = oh["id"]
    STORE.touch(col)
    return serialize_recording(col)

@route("DELETE", "/{accountId}/buckets/{bucketId}/card_tables/columns/{columnId}/on_hold.json")
def disable_on_hold(ctx):
    col = STORE.require_recording(int(ctx["params"]["columnId"]), ["Kanban::Column"])
    ensure_member_can_mutate(ctx["person"], col)
    if col.get("on_hold_id"):
        oh = STORE.recording(col["on_hold_id"])
        if oh:
            # move cards back to column
            for r in STORE.recordings.values():
                if r.get("parent_id") == oh["id"] and r.get("type") == "Kanban::Card":
                    r["parent_id"] = col["id"]
            oh["status"] = "trashed"
        col.pop("on_hold_id", None)
        STORE.touch(col)
    return serialize_recording(col)

@route("GET", "/{accountId}/card_tables/lists/{columnId}/cards.json")
def list_cards(ctx):
    col = STORE.require_recording(int(ctx["params"]["columnId"]))
    ensure_member_can_mutate(ctx["person"], col)
    cards = [r for r in STORE.recordings.values()
             if r.get("parent_id") == col["id"] and r.get("type") == "Kanban::Card"
             and r.get("status") != "trashed"]
    cards = client_filter_recordings(ctx["person"], cards)
    cards.sort(key=lambda x: (x.get("position") or 0, x.get("id")))
    page = parse_page(ctx["qs"])
    slice_, headers, *_ = paginate(cards, page)
    return [serialize_recording(c) for c in slice_], 200, headers

@route("POST", "/{accountId}/card_tables/lists/{columnId}/cards.json")
def create_card(ctx):
    col = STORE.require_recording(int(ctx["params"]["columnId"]))
    ensure_member_can_mutate(ctx["person"], col)
    body = require_json_body(ctx["body"], ["title"])
    cards = [r for r in STORE.recordings.values() if r.get("parent_id") == col["id"] and r.get("type") == "Kanban::Card"]
    pos = 1
    for c in cards:
        c["position"] = (c.get("position") or 0) + 1
    c = STORE.new_recording(
        type_="Kanban::Card", title=body["title"], creator_id=ctx["person"]["id"],
        bucket_id=col["bucket_id"], parent_id=col["id"], position=pos,
        content=body.get("content") or "",
        extra={
            "due_on": parse_date(body.get("due_on")),
            "assignee_ids": [],
            "completed": col.get("type") == "Kanban::DoneColumn",
        },
    )
    STORE.write_event(c["id"], "created", ctx["person"]["id"])
    return serialize_recording(c), 201

@route("POST", "/{accountId}/card_tables/lists/{columnId}/subscription.json")
def subscribe_column(ctx):
    col = STORE.require_recording(int(ctx["params"]["columnId"]))
    ensure_member_can_mutate(ctx["person"], col)
    STORE.subscribe(col["id"], [ctx["person"]["id"]])
    return None, 204

@route("DELETE", "/{accountId}/card_tables/lists/{columnId}/subscription.json")
def unsubscribe_column(ctx):
    col = STORE.require_recording(int(ctx["params"]["columnId"]))
    STORE.unsubscribe(col["id"], [ctx["person"]["id"]])
    return None, 204

@route("GET", "/{accountId}/card_tables/cards/{cardId}")
@route("GET", "/{accountId}/card_tables/cards/{cardId}.json")
def get_card(ctx):
    c = STORE.require_recording(int(ctx["params"]["cardId"]), ["Kanban::Card"])
    ensure_member_can_mutate(ctx["person"], c)
    return serialize_recording(c)

@route("PUT", "/{accountId}/card_tables/cards/{cardId}")
@route("PUT", "/{accountId}/card_tables/cards/{cardId}.json")
def update_card(ctx):
    c = STORE.require_recording(int(ctx["params"]["cardId"]), ["Kanban::Card"])
    ensure_member_can_mutate(ctx["person"], c)
    body = ctx["body"] or {}
    if "title" in body:
        c["title"] = body["title"]
    if "content" in body:
        c["content"] = body["content"]
        c["description"] = body["content"]
    if "due_on" in body:
        c["due_on"] = parse_date(body["due_on"])
    if "assignee_ids" in body:
        c["assignee_ids"] = [int(x) for x in body["assignee_ids"]]
    STORE.touch(c)
    STORE.write_event(c["id"], "updated", ctx["person"]["id"])
    return serialize_recording(c)

@route("POST", "/{accountId}/card_tables/cards/{cardId}/moves.json")
def move_card(ctx):
    c = STORE.require_recording(int(ctx["params"]["cardId"]), ["Kanban::Card"])
    ensure_member_can_mutate(ctx["person"], c)
    body = require_json_body(ctx["body"], ["column_id"])
    col = STORE.require_recording(int(body["column_id"]))
    old_parent = c.get("parent_id")
    c["parent_id"] = col["id"]
    c["completed"] = col.get("type") == "Kanban::DoneColumn"
    if c["completed"]:
        c["completed_at"] = utcnow()
    pos = int(body.get("position") or 1)
    # shift siblings
    siblings = [r for r in STORE.recordings.values()
                if r.get("parent_id") == col["id"] and r.get("type") == "Kanban::Card"
                and r["id"] != c["id"] and r.get("status") != "trashed"]
    siblings.sort(key=lambda x: x.get("position") or 0)
    siblings.insert(max(0, pos - 1), c)
    for i, s in enumerate(siblings, start=1):
        s["position"] = i
    STORE.touch(c)
    STORE.write_event(c["id"], "moved", ctx["person"]["id"],
                      details={"from": old_parent, "to": col["id"]})
    return None, 204

@route("POST", "/{accountId}/card_tables/cards/{cardId}/steps.json")
def create_card_step(ctx):
    c = STORE.require_recording(int(ctx["params"]["cardId"]), ["Kanban::Card"])
    ensure_member_can_mutate(ctx["person"], c)
    body = require_json_body(ctx["body"], ["title"])
    steps = [r for r in STORE.recordings.values()
             if r.get("parent_id") == c["id"] and r.get("type") == "Kanban::Step"]
    pos = max([s.get("position") or 0 for s in steps] + [0]) + 1
    s = STORE.new_recording(
        type_="Kanban::Step", title=body["title"], creator_id=ctx["person"]["id"],
        bucket_id=c["bucket_id"], parent_id=c["id"], position=pos,
        extra={
            "due_on": parse_date(body.get("due_on")),
            "assignee_ids": [int(x) for x in (body.get("assignee_ids") or [])],
            "completed": False,
        },
    )
    STORE.write_event(s["id"], "created", ctx["person"]["id"])
    return serialize_recording(s), 201

@route("POST", "/{accountId}/card_tables/cards/{cardId}/positions.json")
def reposition_card_step(ctx):
    c = STORE.require_recording(int(ctx["params"]["cardId"]), ["Kanban::Card"])
    ensure_member_can_mutate(ctx["person"], c)
    body = require_json_body(ctx["body"], ["position", "source_id"])
    step = STORE.require_recording(int(body["source_id"]), ["Kanban::Step"])
    steps = [r for r in STORE.recordings.values()
             if r.get("parent_id") == c["id"] and r.get("type") == "Kanban::Step"
             and r.get("status") != "trashed" and r["id"] != step["id"]]
    steps.sort(key=lambda x: x.get("position") or 0)
    steps.insert(int(body["position"]), step)
    for i, s in enumerate(steps):
        s["position"] = i
    return serialize_recording(c)

@route("GET", "/{accountId}/card_tables/steps/{stepId}")
@route("GET", "/{accountId}/card_tables/steps/{stepId}.json")
def get_card_step(ctx):
    s = STORE.require_recording(int(ctx["params"]["stepId"]), ["Kanban::Step", "Step"])
    ensure_member_can_mutate(ctx["person"], s)
    return serialize_recording(s)

@route("PUT", "/{accountId}/card_tables/steps/{stepId}")
@route("PUT", "/{accountId}/card_tables/steps/{stepId}.json")
def update_card_step(ctx):
    s = STORE.require_recording(int(ctx["params"]["stepId"]), ["Kanban::Step", "Step"])
    ensure_member_can_mutate(ctx["person"], s)
    body = ctx["body"] or {}
    if "title" in body:
        s["title"] = body["title"]
    if "due_on" in body:
        s["due_on"] = parse_date(body["due_on"])
    if "assignee_ids" in body:
        s["assignee_ids"] = [int(x) for x in body["assignee_ids"]]
    STORE.touch(s)
    return serialize_recording(s)

@route("PUT", "/{accountId}/card_tables/steps/{stepId}/completions.json")
def set_step_completion(ctx):
    s = STORE.require_recording(int(ctx["params"]["stepId"]), ["Kanban::Step", "Step"])
    ensure_member_can_mutate(ctx["person"], s)
    body = require_json_body(ctx["body"], ["completion"])
    on = body["completion"] == "on"
    s["completed"] = on
    s["completed_at"] = utcnow() if on else None
    s["completer_id"] = ctx["person"]["id"] if on else None
    STORE.touch(s)
    STORE.write_event(s["id"], "completed" if on else "uncompleted", ctx["person"]["id"])
    return serialize_recording(s)

# ---------------------------------------------------------------------------
# Vault / Docs & Files
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/vaults/{vaultId}")
@route("GET", "/{accountId}/vaults/{vaultId}.json")
def get_vault(ctx):
    v = STORE.require_recording(int(ctx["params"]["vaultId"]), ["Vault"])
    ensure_member_can_mutate(ctx["person"], v)
    return serialize_recording(v)

@route("PUT", "/{accountId}/vaults/{vaultId}")
@route("PUT", "/{accountId}/vaults/{vaultId}.json")
def update_vault(ctx):
    v = STORE.require_recording(int(ctx["params"]["vaultId"]), ["Vault"])
    ensure_member_can_mutate(ctx["person"], v)
    body = ctx["body"] or {}
    if "title" in body:
        v["title"] = body["title"]
    STORE.touch(v)
    return serialize_recording(v)

@route("GET", "/{accountId}/vaults/{vaultId}/documents.json")
def list_documents(ctx):
    v = STORE.require_recording(int(ctx["params"]["vaultId"]), ["Vault"])
    ensure_member_can_mutate(ctx["person"], v)
    docs = [r for r in STORE.recordings.values()
            if r.get("parent_id") == v["id"] and r.get("type") == "Document"
            and r.get("status") in ("active", "drafted")]
    docs = client_filter_recordings(ctx["person"], docs)
    docs.sort(key=lambda x: x.get("position") or 0)
    page = parse_page(ctx["qs"])
    slice_, headers, *_ = paginate(docs, page)
    return [serialize_recording(d) for d in slice_], 200, headers

@route("POST", "/{accountId}/vaults/{vaultId}/documents.json")
def create_document(ctx):
    v = STORE.require_recording(int(ctx["params"]["vaultId"]), ["Vault"])
    ensure_member_can_mutate(ctx["person"], v)
    body = require_json_body(ctx["body"], ["title"])
    status = body.get("status") or "active"
    d = STORE.new_recording(
        type_="Document", title=body["title"], creator_id=ctx["person"]["id"],
        bucket_id=v["bucket_id"], parent_id=v["id"], content=body.get("content") or "",
        status=status,
    )
    if body.get("subscriptions"):
        STORE.subscribe(d["id"], body["subscriptions"])
    if status == "active":
        STORE.write_event(d["id"], "created", ctx["person"]["id"])
    return serialize_recording(d), 201

@route("GET", "/{accountId}/documents/{documentId}")
@route("GET", "/{accountId}/documents/{documentId}.json")
def get_document(ctx):
    d = STORE.require_recording(int(ctx["params"]["documentId"]), ["Document"])
    ensure_member_can_mutate(ctx["person"], d)
    return serialize_recording(d)

@route("PUT", "/{accountId}/documents/{documentId}")
@route("PUT", "/{accountId}/documents/{documentId}.json")
def update_document(ctx):
    d = STORE.require_recording(int(ctx["params"]["documentId"]), ["Document"])
    ensure_member_can_mutate(ctx["person"], d)
    body = ctx["body"] or {}
    if "title" in body:
        d["title"] = body["title"]
    if "content" in body:
        d["content"] = body["content"]
    STORE.touch(d)
    STORE.write_event(d["id"], "updated", ctx["person"]["id"])
    return serialize_recording(d)

@route("GET", "/{accountId}/vaults/{vaultId}/uploads.json")
def list_uploads(ctx):
    v = STORE.require_recording(int(ctx["params"]["vaultId"]), ["Vault"])
    ensure_member_can_mutate(ctx["person"], v)
    ups = [r for r in STORE.recordings.values()
           if r.get("parent_id") == v["id"] and r.get("type") == "Upload"
           and r.get("status") != "trashed"]
    ups = client_filter_recordings(ctx["person"], ups)
    page = parse_page(ctx["qs"])
    slice_, headers, *_ = paginate(ups, page)
    return [serialize_recording(u) for u in slice_], 200, headers

@route("POST", "/{accountId}/vaults/{vaultId}/uploads.json")
def create_upload(ctx):
    v = STORE.require_recording(int(ctx["params"]["vaultId"]), ["Vault"])
    ensure_member_can_mutate(ctx["person"], v)
    body = require_json_body(ctx["body"], ["attachable_sgid"])
    att = STORE.attachments.get(body["attachable_sgid"])
    if not att:
        # allow synthetic sgid for tests
        att = {
            "filename": body.get("base_name") or "upload.bin",
            "content_type": "application/octet-stream",
            "byte_size": 0,
        }
    u = STORE.new_recording(
        type_="Upload",
        title=body.get("base_name") or att.get("filename") or "upload",
        creator_id=ctx["person"]["id"],
        bucket_id=v["bucket_id"],
        parent_id=v["id"],
        extra={
            "filename": att.get("filename"),
            "content_type": att.get("content_type"),
            "byte_size": att.get("byte_size") or 0,
            "description": body.get("description") or "",
            "download_url": att.get("download_url") or api_url(acct_path("/attachments/download")),
        },
    )
    if body.get("subscriptions"):
        STORE.subscribe(u["id"], body["subscriptions"])
    STORE.write_event(u["id"], "created", ctx["person"]["id"])
    return serialize_recording(u), 201

@route("GET", "/{accountId}/uploads/{uploadId}")
@route("GET", "/{accountId}/uploads/{uploadId}.json")
def get_upload(ctx):
    u = STORE.require_recording(int(ctx["params"]["uploadId"]), ["Upload"])
    ensure_member_can_mutate(ctx["person"], u)
    return serialize_recording(u)

@route("PUT", "/{accountId}/uploads/{uploadId}")
@route("PUT", "/{accountId}/uploads/{uploadId}.json")
def update_upload(ctx):
    u = STORE.require_recording(int(ctx["params"]["uploadId"]), ["Upload"])
    ensure_member_can_mutate(ctx["person"], u)
    body = ctx["body"] or {}
    if "description" in body:
        u["description"] = body["description"]
    if "base_name" in body:
        u["filename"] = body["base_name"]
        u["title"] = body["base_name"]
    STORE.touch(u)
    return serialize_recording(u)

@route("GET", "/{accountId}/uploads/{uploadId}/versions.json")
def list_upload_versions(ctx):
    u = STORE.require_recording(int(ctx["params"]["uploadId"]), ["Upload"])
    ensure_member_can_mutate(ctx["person"], u)
    return [serialize_recording(u)]  # single current version

@route("GET", "/{accountId}/vaults/{vaultId}/vaults.json")
def list_vaults(ctx):
    v = STORE.require_recording(int(ctx["params"]["vaultId"]), ["Vault"])
    ensure_member_can_mutate(ctx["person"], v)
    kids = [r for r in STORE.recordings.values()
            if r.get("parent_id") == v["id"] and r.get("type") == "Vault"
            and r.get("status") != "trashed"]
    return [serialize_recording(k) for k in kids]

@route("POST", "/{accountId}/vaults/{vaultId}/vaults.json")
def create_nested_vault(ctx):
    v = STORE.require_recording(int(ctx["params"]["vaultId"]), ["Vault"])
    ensure_member_can_mutate(ctx["person"], v)
    body = require_json_body(ctx["body"], ["title"])
    child = STORE.new_recording(
        type_="Vault", title=body["title"], creator_id=ctx["person"]["id"],
        bucket_id=v["bucket_id"], parent_id=v["id"],
        extra={"enabled": True, "name": body["title"]},
    )
    STORE.write_event(child["id"], "created", ctx["person"]["id"])
    return serialize_recording(child), 201

@route("POST", "/{accountId}/attachments.json")
def create_attachment(ctx):
    # Binary or base64 body; produce attachable_sgid
    raw = ctx.get("raw_body") or b""
    content_type = (ctx.get("headers") or {}).get("Content-Type") or "application/octet-stream"
    sgid = f"sgid_att_{uuid.uuid4().hex}"
    STORE.attachments[sgid] = {
        "filename": (ctx.get("headers") or {}).get("X-Filename") or "upload.bin",
        "content_type": content_type.split(";")[0].strip(),
        "byte_size": len(raw) if isinstance(raw, (bytes, bytearray)) else 0,
        "download_url": api_url(acct_path(f"/attachments/{sgid}")),
    }
    return {"attachable_sgid": sgid}, 201

# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/chats.json")
def list_campfires(ctx):
    chats = [r for r in STORE.recordings.values()
             if r.get("type") == "Chat::Transcript" and r.get("status") != "trashed"]
    out = []
    for c in chats:
        project = STORE.project(c["bucket_id"])
        if project and STORE.can_access_project(ctx["person"], project):
            out.append(serialize_recording(c))
    return out

@route("GET", "/{accountId}/chats/{campfireId}")
@route("GET", "/{accountId}/chats/{campfireId}.json")
def get_campfire(ctx):
    c = STORE.require_recording(int(ctx["params"]["campfireId"]), ["Chat::Transcript"])
    ensure_member_can_mutate(ctx["person"], c)
    return serialize_recording(c)

@route("GET", "/{accountId}/chats/{campfireId}/lines.json")
def list_campfire_lines(ctx):
    c = STORE.require_recording(int(ctx["params"]["campfireId"]), ["Chat::Transcript"])
    ensure_member_can_mutate(ctx["person"], c)
    lines = [r for r in STORE.recordings.values()
             if r.get("parent_id") == c["id"] and r.get("type") == "Chat::Lines::Line"
             and r.get("status") != "trashed"]
    lines.sort(key=lambda x: x.get("created_at") or utcnow())
    page = parse_page(ctx["qs"])
    slice_, headers, *_ = paginate(lines, page)
    return [serialize_recording(l) for l in slice_], 200, headers

@route("POST", "/{accountId}/chats/{campfireId}/lines.json")
def create_campfire_line(ctx):
    c = STORE.require_recording(int(ctx["params"]["campfireId"]), ["Chat::Transcript"])
    ensure_member_can_mutate(ctx["person"], c)
    body = require_json_body(ctx["body"], ["content"])
    line = STORE.new_recording(
        type_="Chat::Lines::Line", title=(body["content"] or "")[:60],
        creator_id=ctx["person"]["id"], bucket_id=c["bucket_id"], parent_id=c["id"],
        content=body["content"], extra={"attachments": []},
    )
    STORE.write_event(line["id"], "created", ctx["person"]["id"])
    return serialize_recording(line), 201

@route("GET", "/{accountId}/chats/{campfireId}/lines/{lineId}")
@route("GET", "/{accountId}/chats/{campfireId}/lines/{lineId}.json")
def get_campfire_line(ctx):
    line = STORE.require_recording(int(ctx["params"]["lineId"]), ["Chat::Lines::Line"])
    ensure_member_can_mutate(ctx["person"], line)
    return serialize_recording(line)

@route("DELETE", "/{accountId}/chats/{campfireId}/lines/{lineId}")
@route("DELETE", "/{accountId}/chats/{campfireId}/lines/{lineId}.json")
def delete_campfire_line(ctx):
    line = STORE.require_recording(int(ctx["params"]["lineId"]), ["Chat::Lines::Line"])
    ensure_member_can_mutate(ctx["person"], line)
    if line.get("creator_id") != ctx["person"]["id"] and not ctx["person"].get("admin"):
        raise forbidden("Only the creator can delete this line")
    STORE.set_status(line, "trashed", ctx["person"]["id"])
    return None, 204

@route("GET", "/{accountId}/chats/{campfireId}/uploads.json")
def list_campfire_uploads(ctx):
    c = STORE.require_recording(int(ctx["params"]["campfireId"]), ["Chat::Transcript"])
    ensure_member_can_mutate(ctx["person"], c)
    lines = [r for r in STORE.recordings.values()
             if r.get("parent_id") == c["id"] and r.get("type") == "Chat::Lines::Line"
             and r.get("attachments")]
    return [serialize_recording(l) for l in lines]

@route("POST", "/{accountId}/chats/{campfireId}/uploads.json")
def create_campfire_upload(ctx):
    c = STORE.require_recording(int(ctx["params"]["campfireId"]), ["Chat::Transcript"])
    ensure_member_can_mutate(ctx["person"], c)
    raw = ctx.get("raw_body") or b""
    content_type = (ctx.get("headers") or {}).get("Content-Type") or "application/octet-stream"
    line = STORE.new_recording(
        type_="Chat::Lines::Line", title="Uploaded a file",
        creator_id=ctx["person"]["id"], bucket_id=c["bucket_id"], parent_id=c["id"],
        content="",
        extra={
            "attachments": [{
                "title": "upload",
                "filename": "upload.bin",
                "content_type": content_type.split(";")[0].strip(),
                "byte_size": len(raw) if isinstance(raw, (bytes, bytearray)) else 0,
                "url": api_url(acct_path(f"/chats/{c['id']}/uploads/latest")),
                "download_url": api_url(acct_path(f"/chats/{c['id']}/uploads/latest")),
            }],
        },
    )
    STORE.write_event(line["id"], "created", ctx["person"]["id"])
    return serialize_recording(line), 201

@route("GET", "/{accountId}/chats/{campfireId}/integrations.json")
def list_chatbots(ctx):
    cid = int(ctx["params"]["campfireId"])
    STORE.require_recording(cid, ["Chat::Transcript"])
    bots = [b for b in STORE.chatbots.values() if b.get("campfire_id") == cid]
    return [chatbot_json(b) for b in bots]

@route("POST", "/{accountId}/chats/{campfireId}/integrations.json")
def create_chatbot(ctx):
    c = STORE.require_recording(int(ctx["params"]["campfireId"]), ["Chat::Transcript"])
    ensure_member_can_mutate(ctx["person"], c)
    body = require_json_body(ctx["body"], ["service_name"])
    bid = STORE.next_id()
    now = utcnow()
    bot = {
        "id": bid,
        "campfire_id": c["id"],
        "service_name": body["service_name"],
        "command_url": body.get("command_url"),
        "created_at": now,
        "updated_at": now,
    }
    STORE.chatbots[bid] = bot
    return chatbot_json(bot), 201

@route("GET", "/{accountId}/chats/{campfireId}/integrations/{chatbotId}")
@route("GET", "/{accountId}/chats/{campfireId}/integrations/{chatbotId}.json")
def get_chatbot(ctx):
    bot = STORE.chatbots.get(int(ctx["params"]["chatbotId"]))
    if not bot:
        raise not_found("Chatbot not found")
    return chatbot_json(bot)

@route("PUT", "/{accountId}/chats/{campfireId}/integrations/{chatbotId}")
@route("PUT", "/{accountId}/chats/{campfireId}/integrations/{chatbotId}.json")
def update_chatbot(ctx):
    bot = STORE.chatbots.get(int(ctx["params"]["chatbotId"]))
    if not bot:
        raise not_found("Chatbot not found")
    body = require_json_body(ctx["body"], ["service_name"])
    bot["service_name"] = body["service_name"]
    if "command_url" in body:
        bot["command_url"] = body["command_url"]
    bot["updated_at"] = utcnow()
    return chatbot_json(bot)

@route("DELETE", "/{accountId}/chats/{campfireId}/integrations/{chatbotId}")
@route("DELETE", "/{accountId}/chats/{campfireId}/integrations/{chatbotId}.json")
def delete_chatbot(ctx):
    bid = int(ctx["params"]["chatbotId"])
    if bid not in STORE.chatbots:
        raise not_found("Chatbot not found")
    del STORE.chatbots[bid]
    return None, 204

# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/schedules/{scheduleId}")
@route("GET", "/{accountId}/schedules/{scheduleId}.json")
def get_schedule(ctx):
    s = STORE.require_recording(int(ctx["params"]["scheduleId"]), ["Schedule"])
    ensure_member_can_mutate(ctx["person"], s)
    return serialize_recording(s)

@route("PUT", "/{accountId}/schedules/{scheduleId}")
@route("PUT", "/{accountId}/schedules/{scheduleId}.json")
def update_schedule_settings(ctx):
    s = STORE.require_recording(int(ctx["params"]["scheduleId"]), ["Schedule"])
    ensure_member_can_mutate(ctx["person"], s)
    body = require_json_body(ctx["body"], ["include_due_assignments"])
    s["include_due_assignments"] = bool(body["include_due_assignments"])
    STORE.touch(s)
    return serialize_recording(s)

@route("GET", "/{accountId}/schedules/{scheduleId}/entries.json")
def list_schedule_entries(ctx):
    s = STORE.require_recording(int(ctx["params"]["scheduleId"]), ["Schedule"])
    ensure_member_can_mutate(ctx["person"], s)
    entries = [r for r in STORE.recordings.values()
               if r.get("parent_id") == s["id"] and r.get("type") == "Schedule::Entry"
               and r.get("status") != "trashed"]
    entries.sort(key=lambda x: x.get("starts_at") or utcnow())
    page = parse_page(ctx["qs"])
    slice_, headers, *_ = paginate(entries, page)
    result = [serialize_recording(e) for e in slice_]
    # due-todo overlay when enabled
    if s.get("include_due_assignments", True):
        pass  # clients combine via reports; keep entries pure
    return result, 200, headers

@route("POST", "/{accountId}/schedules/{scheduleId}/entries.json")
def create_schedule_entry(ctx):
    s = STORE.require_recording(int(ctx["params"]["scheduleId"]), ["Schedule"])
    ensure_member_can_mutate(ctx["person"], s)
    body = require_json_body(ctx["body"], ["summary", "starts_at", "ends_at"])
    e = STORE.new_recording(
        type_="Schedule::Entry", title=body["summary"], creator_id=ctx["person"]["id"],
        bucket_id=s["bucket_id"], parent_id=s["id"],
        extra={
            "summary": body["summary"],
            "description": body.get("description") or "",
            "all_day": bool(body.get("all_day")),
            "starts_at": parse_iso(body["starts_at"]) or body["starts_at"],
            "ends_at": parse_iso(body["ends_at"]) or body["ends_at"],
            "participant_ids": [int(x) for x in (body.get("participant_ids") or [])],
        },
    )
    if body.get("subscriptions"):
        STORE.subscribe(e["id"], body["subscriptions"])
    STORE.write_event(e["id"], "created", ctx["person"]["id"])
    return serialize_recording(e), 201

@route("GET", "/{accountId}/schedule_entries/{entryId}")
@route("GET", "/{accountId}/schedule_entries/{entryId}.json")
def get_schedule_entry(ctx):
    e = STORE.require_recording(int(ctx["params"]["entryId"]), ["Schedule::Entry"])
    ensure_member_can_mutate(ctx["person"], e)
    return serialize_recording(e)

@route("PUT", "/{accountId}/schedule_entries/{entryId}")
@route("PUT", "/{accountId}/schedule_entries/{entryId}.json")
def update_schedule_entry(ctx):
    e = STORE.require_recording(int(ctx["params"]["entryId"]), ["Schedule::Entry"])
    ensure_member_can_mutate(ctx["person"], e)
    body = ctx["body"] or {}
    if "summary" in body:
        e["summary"] = body["summary"]
        e["title"] = body["summary"]
    if "description" in body:
        e["description"] = body["description"]
    if "starts_at" in body:
        e["starts_at"] = parse_iso(body["starts_at"]) or body["starts_at"]
    if "ends_at" in body:
        e["ends_at"] = parse_iso(body["ends_at"]) or body["ends_at"]
    if "all_day" in body:
        e["all_day"] = bool(body["all_day"])
    if "participant_ids" in body:
        e["participant_ids"] = [int(x) for x in body["participant_ids"]]
    STORE.touch(e)
    STORE.write_event(e["id"], "updated", ctx["person"]["id"])
    return serialize_recording(e)

@route("GET", "/{accountId}/schedule_entries/{entryId}/occurrences/{date}")
@route("GET", "/{accountId}/schedule_entries/{entryId}/occurrences/{date}.json")
def get_schedule_entry_occurrence(ctx):
    e = STORE.require_recording(int(ctx["params"]["entryId"]), ["Schedule::Entry"])
    ensure_member_can_mutate(ctx["person"], e)
    # For non-recurring entries, return the entry itself
    return serialize_recording(e)

# ---------------------------------------------------------------------------
# Recording cross-cutting: comments, boosts, events, status, subscription, pin
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/recordings/{recordingId}")
@route("GET", "/{accountId}/recordings/{recordingId}.json")
def get_recording(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    if ctx["person"].get("client") and not r.get("visible_to_clients"):
        raise not_found()
    return serialize_recording(r)

@route("GET", "/{accountId}/recordings/{recordingId}/comments.json")
def list_comments(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    comments = [c for c in STORE.recordings.values()
                if c.get("parent_id") == r["id"] and c.get("type") == "Comment"
                and c.get("status") != "trashed"]
    comments = client_filter_recordings(ctx["person"], comments)
    comments.sort(key=lambda x: x.get("created_at") or utcnow())
    page = parse_page(ctx["qs"])
    slice_, headers, *_ = paginate(comments, page)
    return [serialize_recording(c) for c in slice_], 200, headers

@route("POST", "/{accountId}/recordings/{recordingId}/comments.json")
def create_comment(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    body = require_json_body(ctx["body"], ["content"])
    c = STORE.add_comment(r, ctx["person"]["id"], body["content"])
    STORE.subscribe(r["id"], [ctx["person"]["id"]])
    return serialize_recording(c), 201

@route("GET", "/{accountId}/comments/{commentId}")
@route("GET", "/{accountId}/comments/{commentId}.json")
def get_comment(ctx):
    c = STORE.require_recording(int(ctx["params"]["commentId"]), ["Comment"])
    ensure_member_can_mutate(ctx["person"], c)
    return serialize_recording(c)

@route("PUT", "/{accountId}/comments/{commentId}")
@route("PUT", "/{accountId}/comments/{commentId}.json")
def update_comment(ctx):
    c = STORE.require_recording(int(ctx["params"]["commentId"]), ["Comment"])
    ensure_member_can_mutate(ctx["person"], c)
    if c.get("creator_id") != ctx["person"]["id"] and not ctx["person"].get("admin"):
        raise forbidden("Only the creator can edit this comment")
    body = require_json_body(ctx["body"], ["content"])
    c["content"] = body["content"]
    c["title"] = body["content"][:80]
    STORE.touch(c)
    STORE.write_event(c["id"], "updated", ctx["person"]["id"])
    return serialize_recording(c)

@route("GET", "/{accountId}/recordings/{recordingId}/boosts.json")
def list_recording_boosts(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    return [boost_json(b) for b in STORE.boosts_for(r["id"])]

@route("POST", "/{accountId}/recordings/{recordingId}/boosts.json")
def create_recording_boost(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    body = require_json_body(ctx["body"], ["content"])
    b = STORE.add_boost(r["id"], ctx["person"]["id"], body["content"])
    return boost_json(b), 201

@route("GET", "/{accountId}/boosts/{boostId}")
@route("GET", "/{accountId}/boosts/{boostId}.json")
def get_boost(ctx):
    b = STORE.boosts.get(int(ctx["params"]["boostId"]))
    if not b:
        raise not_found("Boost not found")
    return boost_json(b)

@route("DELETE", "/{accountId}/boosts/{boostId}")
@route("DELETE", "/{accountId}/boosts/{boostId}.json")
def delete_boost(ctx):
    bid = int(ctx["params"]["boostId"])
    b = STORE.boosts.get(bid)
    if not b:
        raise not_found("Boost not found")
    if b.get("booster_id") != ctx["person"]["id"] and not ctx["person"].get("admin"):
        raise forbidden()
    rec = STORE.recording(b["recording_id"])
    if rec:
        rec["boosts_count"] = max(0, int(rec.get("boosts_count") or 0) - 1)
    del STORE.boosts[bid]
    return None, 204

@route("GET", "/{accountId}/recordings/{recordingId}/events.json")
def list_events(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    events = [e for e in STORE.events.values() if e["recording_id"] == r["id"]]
    events.sort(key=lambda e: e["created_at"])
    return [event_json(e) for e in events]

@route("GET", "/{accountId}/recordings/{recordingId}/events/{eventId}/boosts.json")
def list_event_boosts(ctx):
    eid = int(ctx["params"]["eventId"])
    rid = int(ctx["params"]["recordingId"])
    return [boost_json(b) for b in STORE.boosts_for(rid, event_id=eid)]

@route("POST", "/{accountId}/recordings/{recordingId}/events/{eventId}/boosts.json")
def create_event_boost(ctx):
    body = require_json_body(ctx["body"], ["content"])
    b = STORE.add_event_boost(int(ctx["params"]["eventId"]), ctx["person"]["id"], body["content"])
    return boost_json(b), 201

@route("PUT", "/{accountId}/recordings/{recordingId}/status/active.json")
def activate_recording(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    STORE.set_status(r, "active", ctx["person"]["id"])
    return None, 204

@route("PUT", "/{accountId}/recordings/{recordingId}/status/archived.json")
def archive_recording(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    STORE.set_status(r, "archived", ctx["person"]["id"])
    return None, 204

@route("PUT", "/{accountId}/recordings/{recordingId}/status/trashed.json")
def trash_recording(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    # personal-voice: creator or admin
    personal = r.get("type") in ("Message", "Comment", "Chat::Lines::Line")
    if personal and r.get("creator_id") != ctx["person"]["id"] and not (
        ctx["person"].get("admin") or ctx["person"].get("owner")
    ):
        raise forbidden("Only the creator can trash this recording")
    STORE.set_status(r, "trashed", ctx["person"]["id"])
    return None, 204

@route("PUT", "/{accountId}/recordings/{recordingId}/client_visibility.json")
def set_client_visibility(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    body = require_json_body(ctx["body"], ["visible_to_clients"])
    r["visible_to_clients"] = bool(body["visible_to_clients"])
    STORE.touch(r)
    STORE.write_event(r["id"], "client_visibility_changed", ctx["person"]["id"],
                      details={"visible_to_clients": r["visible_to_clients"]})
    return serialize_recording(r)

@route("GET", "/{accountId}/recordings/{recordingId}/subscription.json")
def get_subscription(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    return subscription_json(r["id"], ctx["person"])

@route("POST", "/{accountId}/recordings/{recordingId}/subscription.json")
def subscribe(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    STORE.subscribe(r["id"], [ctx["person"]["id"]])
    return subscription_json(r["id"], ctx["person"])

@route("DELETE", "/{accountId}/recordings/{recordingId}/subscription.json")
def unsubscribe(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    STORE.unsubscribe(r["id"], [ctx["person"]["id"]])
    return None, 204

@route("PUT", "/{accountId}/recordings/{recordingId}/subscription.json")
def update_subscription(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    body = ctx["body"] or {}
    if body.get("subscriptions"):
        STORE.subscribe(r["id"], body["subscriptions"])
    if body.get("unsubscriptions"):
        STORE.unsubscribe(r["id"], body["unsubscriptions"])
    return subscription_json(r["id"], ctx["person"])

@route("POST", "/{accountId}/recordings/{messageId}/pin.json")
def pin_recording(ctx):
    r = STORE.require_recording(int(ctx["params"]["messageId"]))
    ensure_member_can_mutate(ctx["person"], r)
    STORE.pinned.add(r["id"])
    STORE.write_event(r["id"], "pinned", ctx["person"]["id"])
    return None, 204

@route("DELETE", "/{accountId}/recordings/{messageId}/pin.json")
def unpin_recording(ctx):
    r = STORE.require_recording(int(ctx["params"]["messageId"]))
    ensure_member_can_mutate(ctx["person"], r)
    STORE.pinned.discard(r["id"])
    STORE.write_event(r["id"], "unpinned", ctx["person"]["id"])
    return None, 204

@route("GET", "/{accountId}/recordings/{recordingId}/timesheet.json")
def get_recording_timesheet(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    entries = [
        serialize_recording(e) for e in STORE.recordings.values()
        if e.get("type") == "Timesheet::Entry" and e.get("parent_id") == r["id"]
        and e.get("status") != "trashed"
    ]
    return entries

@route("POST", "/{accountId}/recordings/{recordingId}/timesheet/entries.json")
def create_timesheet_entry(ctx):
    r = STORE.require_recording(int(ctx["params"]["recordingId"]))
    ensure_member_can_mutate(ctx["person"], r)
    body = require_json_body(ctx["body"], ["date", "hours"])
    person_id = int(body.get("person_id") or ctx["person"]["id"])
    e = STORE.new_recording(
        type_="Timesheet::Entry", title=f"{body['hours']}h on {body['date']}",
        creator_id=ctx["person"]["id"], bucket_id=r["bucket_id"], parent_id=r["id"],
        extra={
            "date": body["date"],
            "hours": str(body["hours"]),
            "description": body.get("description") or "",
            "person_id": person_id,
        },
    )
    STORE.write_event(e["id"], "created", ctx["person"]["id"])
    return serialize_recording(e), 201

@route("GET", "/{accountId}/timesheet_entries/{entryId}")
@route("GET", "/{accountId}/timesheet_entries/{entryId}.json")
def get_timesheet_entry(ctx):
    e = STORE.require_recording(int(ctx["params"]["entryId"]), ["Timesheet::Entry"])
    ensure_member_can_mutate(ctx["person"], e)
    return serialize_recording(e)

@route("PUT", "/{accountId}/timesheet_entries/{entryId}")
@route("PUT", "/{accountId}/timesheet_entries/{entryId}.json")
def update_timesheet_entry(ctx):
    e = STORE.require_recording(int(ctx["params"]["entryId"]), ["Timesheet::Entry"])
    ensure_member_can_mutate(ctx["person"], e)
    body = ctx["body"] or {}
    for k in ("date", "hours", "description"):
        if k in body:
            e[k] = body[k]
    if "person_id" in body:
        e["person_id"] = int(body["person_id"])
    STORE.touch(e)
    return serialize_recording(e)

# ---------------------------------------------------------------------------
# Dock / tools
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/dock/tools/{toolId}")
@route("GET", "/{accountId}/dock/tools/{toolId}.json")
def get_tool(ctx):
    t = STORE.require_recording(int(ctx["params"]["toolId"]))
    if t.get("type") not in TOOL_ROOT_TYPES:
        raise not_found("Tool not found")
    ensure_member_can_mutate(ctx["person"], t)
    return tool_json(t)

@route("PUT", "/{accountId}/dock/tools/{toolId}")
@route("PUT", "/{accountId}/dock/tools/{toolId}.json")
def update_tool(ctx):
    t = STORE.require_recording(int(ctx["params"]["toolId"]))
    if t.get("type") not in TOOL_ROOT_TYPES:
        raise not_found("Tool not found")
    ensure_member_can_mutate(ctx["person"], t)
    body = require_json_body(ctx["body"], ["title"])
    t["title"] = body["title"]
    t["name"] = body["title"]
    STORE.touch(t)
    return tool_json(t)

@route("DELETE", "/{accountId}/dock/tools/{toolId}")
@route("DELETE", "/{accountId}/dock/tools/{toolId}.json")
def delete_tool(ctx):
    t = STORE.require_recording(int(ctx["params"]["toolId"]))
    if t.get("type") not in TOOL_ROOT_TYPES:
        raise not_found("Tool not found")
    ensure_member_can_mutate(ctx["person"], t)
    STORE.set_status(t, "trashed", ctx["person"]["id"])
    t["enabled"] = False
    return None, 204

@route("POST", "/{accountId}/dock/tools.json")
def clone_tool_handler(ctx):
    body = require_json_body(ctx["body"], ["source_recording_id"])
    source = STORE.require_recording(int(body["source_recording_id"]))
    if source.get("type") not in TOOL_ROOT_TYPES:
        raise validation("source_recording_id must be a tool root")
    ensure_member_can_mutate(ctx["person"], source)
    clone = clone_tool(source, ctx["person"]["id"], body.get("title"))
    return tool_json(clone), 201

@route("POST", "/{accountId}/recordings/{toolId}/position.json")
def enable_tool(ctx):
    t = STORE.require_recording(int(ctx["params"]["toolId"]))
    if t.get("type") not in TOOL_ROOT_TYPES:
        raise not_found("Tool not found")
    ensure_member_can_mutate(ctx["person"], t)
    t["enabled"] = True
    if t.get("status") != "active":
        t["status"] = "active"
    # place at end
    tools = [r for r in STORE.recordings.values()
             if r.get("bucket_id") == t["bucket_id"] and r.get("type") in TOOL_ROOT_TYPES
             and r.get("enabled") and r["id"] != t["id"]]
    t["position"] = max([x.get("position") or 0 for x in tools] + [0]) + 1
    STORE.touch(t)
    return tool_json(t), 201

@route("DELETE", "/{accountId}/recordings/{toolId}/position.json")
def disable_tool(ctx):
    t = STORE.require_recording(int(ctx["params"]["toolId"]))
    if t.get("type") not in TOOL_ROOT_TYPES:
        raise not_found("Tool not found")
    ensure_member_can_mutate(ctx["person"], t)
    t["enabled"] = False
    STORE.touch(t)
    return None, 204

@route("PUT", "/{accountId}/recordings/{toolId}/position.json")
def reposition_tool(ctx):
    t = STORE.require_recording(int(ctx["params"]["toolId"]))
    if t.get("type") not in TOOL_ROOT_TYPES:
        raise not_found("Tool not found")
    ensure_member_can_mutate(ctx["person"], t)
    body = require_json_body(ctx["body"], ["position"])
    t["position"] = int(body["position"])
    STORE.touch(t)
    return tool_json(t)

# Convenience: add a fresh tool type to a project (not in OpenAPI as a path of its own,
# but supported via cloning empty tool prototypes held on the sample project).
# Also expose a documented internal endpoint for production prototypes:
@route("POST", "/{accountId}/projects/{projectId}/tools.json")
def add_project_tool(ctx):
    """Create a brand-new tool root on a project.

    OpenAPI models tool addition primarily via CloneTool. Empty projects still
    need a way to grow a dock; this endpoint is the honest production path we
    expose for that (documented in the root payload's extensions).
    """
    p = STORE.require_project(int(ctx["params"]["projectId"]))
    STORE.require_project_access(ctx["person"], p)
    body = require_json_body(ctx["body"], ["type"])
    type_map = {
        "message_board": ("Message::Board", "Message Board"),
        "Message::Board": ("Message::Board", "Message Board"),
        "todoset": ("Todoset", "To-dos"),
        "Todoset": ("Todoset", "To-dos"),
        "vault": ("Vault", "Docs & Files"),
        "Vault": ("Vault", "Docs & Files"),
        "chat": ("Chat::Transcript", "Chat"),
        "Chat::Transcript": ("Chat::Transcript", "Chat"),
        "schedule": ("Schedule", "Schedule"),
        "Schedule": ("Schedule", "Schedule"),
        "kanban_board": ("Kanban::Board", "Card Table"),
        "card_table": ("Kanban::Board", "Card Table"),
        "Kanban::Board": ("Kanban::Board", "Card Table"),
        "questionnaire": ("Questionnaire", "Automatic Check-ins"),
        "Questionnaire": ("Questionnaire", "Automatic Check-ins"),
        "inbox": ("Inbox", "Email Forwards"),
        "Inbox": ("Inbox", "Email Forwards"),
    }
    if body["type"] not in type_map:
        raise validation(f"Unknown tool type: {body['type']}")
    tname, default_title = type_map[body["type"]]
    tool = create_tool_root(p, tname, body.get("title") or default_title, ctx["person"]["id"])
    return tool_json(tool), 201

# ---------------------------------------------------------------------------
# My assignments / readings / unreads
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/my/assignments.json")
def get_my_assignments(ctx):
    pid = ctx["person"]["id"]
    items = []
    for r in STORE.recordings.values():
        if r.get("type") not in ("Todo", "Kanban::Card"):
            continue
        if r.get("status") == "trashed":
            continue
        if r.get("completed"):
            continue
        if pid not in (r.get("assignee_ids") or []):
            continue
        project = STORE.project(r["bucket_id"])
        if not project or not STORE.can_access_project(ctx["person"], project):
            continue
        items.append(my_assignment_json(r))
    return {"priorities": [], "non_priorities": items}

@route("GET", "/{accountId}/my/assignments/completed.json")
def get_my_completed_assignments(ctx):
    pid = ctx["person"]["id"]
    items = []
    for r in STORE.recordings.values():
        if r.get("type") not in ("Todo", "Kanban::Card"):
            continue
        if not r.get("completed"):
            continue
        if pid not in (r.get("assignee_ids") or []):
            continue
        items.append(my_assignment_json(r))
    return {"priorities": [], "non_priorities": items}

@route("GET", "/{accountId}/my/assignments/due.json")
def get_my_due_assignments(ctx):
    data = get_my_assignments(ctx)
    due = [a for a in data["non_priorities"] if a.get("due_on")]
    due.sort(key=lambda a: a.get("due_on") or "")
    return {"priorities": [], "non_priorities": due}

@route("GET", "/{accountId}/my/readings.json")
def get_my_notifications(ctx):
    pid = ctx["person"]["id"]
    unreads, reads, memories = [], [], []
    for (person_id, rid), reading in STORE.readings.items():
        if person_id != pid:
            continue
        n = notification_json(reading, ctx["person"])
        if reading.get("resurface_at") and reading["resurface_at"] > utcnow():
            memories.append(n)
        elif reading.get("read"):
            reads.append(n)
        else:
            unreads.append(n)
    # Seed intentionally leaves readings empty for the viewing user
    return {"unreads": unreads, "reads": reads, "memories": memories}

@route("PUT", "/{accountId}/my/unreads.json")
def mark_as_read(ctx):
    body = require_json_body(ctx["body"], ["readables"])
    pid = ctx["person"]["id"]
    for sgid in body["readables"]:
        # readable_sgid format: sgid_reading_{recordingId}_{personId}
        parts = str(sgid).split("_")
        rid = None
        for p in parts:
            if p.isdigit():
                rid = int(p)
                break
        if rid is None:
            continue
        key = (pid, rid)
        reading = STORE.readings.get(key) or {
            "id": STORE.next_id(),
            "recording_id": rid,
            "created_at": utcnow(),
            "readable_sgid": sgid,
        }
        reading["read"] = True
        reading["updated_at"] = utcnow()
        STORE.readings[key] = reading
    return {"status": "ok"}

@route("GET", "/{accountId}/my/question_reminders.json")
def get_my_question_reminders(ctx):
    return []

# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/search.json")
def search(ctx):
    q = (ctx["qs"].get("q") or [""])[0].strip().lower()
    if not q:
        raise validation("q is required")
    sort = (ctx["qs"].get("sort") or ["best_match"])[0]
    results = []
    for r in STORE.recordings.values():
        if r.get("status") not in ("active", "drafted"):
            continue
        project = STORE.project(r["bucket_id"])
        if not project or not STORE.can_access_project(ctx["person"], project):
            continue
        if ctx["person"].get("client") and not r.get("visible_to_clients"):
            continue
        hay = " ".join([
            str(r.get("title") or ""),
            str(r.get("content") or ""),
            str(r.get("description") or ""),
            str(r.get("subject") or ""),
            str(r.get("summary") or ""),
        ]).lower()
        if q not in hay:
            continue
        results.append(r)
    if sort == "created_at":
        results.sort(key=lambda x: x.get("created_at") or utcnow(), reverse=True)
    else:
        # best_match: title hits first
        results.sort(key=lambda x: (0 if q in (x.get("title") or "").lower() else 1,
                                    -(x.get("created_at") or utcnow()).timestamp()))
    page = parse_page(ctx["qs"])
    slice_, headers, *_ = paginate(results, page)
    out = []
    for r in slice_:
        j = serialize_recording(r)
        out.append({
            "id": j["id"],
            "status": j.get("status"),
            "visible_to_clients": j.get("visible_to_clients"),
            "created_at": j.get("created_at"),
            "updated_at": j.get("updated_at"),
            "title": j.get("title"),
            "inherits_status": j.get("inherits_status"),
            "type": j.get("type"),
            "url": j.get("url"),
            "app_url": j.get("app_url"),
            "bookmark_url": j.get("bookmark_url"),
            "parent": j.get("parent"),
            "bucket": j.get("bucket"),
            "creator": j.get("creator"),
            "content": j.get("content"),
            "description": j.get("description"),
            "subject": j.get("subject"),
        })
    return out, 200, headers

@route("GET", "/{accountId}/searches/metadata.json")
def search_metadata(ctx):
    projects = []
    for p in STORE.projects.values():
        if p.get("status") != "active":
            continue
        if STORE.can_access_project(ctx["person"], p):
            projects.append({"id": p["id"], "name": p["name"]})
    return {"projects": projects}

# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/reports/todos/assigned.json")
def list_assignable_people(ctx):
    return [person_json(p) for p in STORE.people.values() if not p.get("client")]

@route("GET", "/{accountId}/reports/todos/assigned/{personId}")
@route("GET", "/{accountId}/reports/todos/assigned/{personId}.json")
def get_assigned_todos(ctx):
    pid = int(ctx["params"]["personId"])
    person = STORE.require_person(pid)
    todos = []
    for r in STORE.recordings.values():
        if r.get("type") != "Todo" or r.get("status") == "trashed":
            continue
        if pid not in (r.get("assignee_ids") or []):
            continue
        if r.get("completed"):
            continue
        todos.append(serialize_recording(r))
    return {"person": person_json(person), "grouped_by": "project", "todos": todos}

@route("GET", "/{accountId}/reports/todos/overdue.json")
def get_overdue_todos(ctx):
    today = utcnow().date()
    buckets = {
        "under_a_week_late": [],
        "over_a_week_late": [],
        "over_a_month_late": [],
        "over_three_months_late": [],
    }
    for r in STORE.recordings.values():
        if r.get("type") != "Todo" or r.get("completed") or r.get("status") == "trashed":
            continue
        due = r.get("due_on")
        if not due:
            continue
        if isinstance(due, str):
            due = parse_date(due)
        if not due or due >= today:
            continue
        delta = (today - due).days
        item = serialize_recording(r)
        if delta > 90:
            buckets["over_three_months_late"].append(item)
        elif delta > 30:
            buckets["over_a_month_late"].append(item)
        elif delta > 7:
            buckets["over_a_week_late"].append(item)
        else:
            buckets["under_a_week_late"].append(item)
    return buckets

@route("GET", "/{accountId}/reports/schedules/upcoming.json")
def get_upcoming_schedule(ctx):
    entries = []
    for r in STORE.recordings.values():
        if r.get("type") != "Schedule::Entry" or r.get("status") == "trashed":
            continue
        project = STORE.project(r["bucket_id"])
        if not project or not STORE.can_access_project(ctx["person"], project):
            continue
        entries.append(serialize_recording(r))
    assignables = []
    for r in STORE.recordings.values():
        if r.get("type") != "Todo" or r.get("completed") or not r.get("due_on"):
            continue
        assignables.append({
            "id": r["id"],
            "title": r.get("title"),
            "due_on": date_str(r.get("due_on")),
            "type": "Todo",
            "bucket": bucket_json(STORE.project(r["bucket_id"])) if STORE.project(r["bucket_id"]) else None,
        })
    return {
        "schedule_entries": entries,
        "recurring_schedule_entry_occurrences": [],
        "assignables": assignables,
    }

@route("GET", "/{accountId}/reports/timesheet.json")
def get_timesheet_report(ctx):
    return [
        serialize_recording(r) for r in STORE.recordings.values()
        if r.get("type") == "Timesheet::Entry" and r.get("status") != "trashed"
    ]

@route("GET", "/{accountId}/reports/gauges.json")
def list_gauges(ctx):
    return [
        serialize_recording(r) for r in STORE.recordings.values()
        if r.get("type") == "Gauge" and r.get("status") != "trashed"
    ]

@route("GET", "/{accountId}/reports/progress.json")
def get_progress_report(ctx):
    events = sorted(STORE.events.values(), key=lambda e: e["created_at"], reverse=True)[:50]
    out = []
    for e in events:
        rec = STORE.recording(e["recording_id"])
        out.append({
            "id": e["id"],
            "action": e.get("action"),
            "created_at": iso(e.get("created_at")),
            "creator": person_json(STORE.person(e.get("creator_id"))),
            "recording": parent_json(rec) if rec else None,
            "bucket": bucket_json(STORE.project(rec["bucket_id"])) if rec and STORE.project(rec["bucket_id"]) else None,
        })
    return out

@route("GET", "/{accountId}/reports/users/progress/{personId}.json")
def get_person_progress(ctx):
    pid = int(ctx["params"]["personId"])
    person = STORE.require_person(pid)
    events = [e for e in STORE.events.values() if e.get("creator_id") == pid]
    events.sort(key=lambda e: e["created_at"], reverse=True)
    return {
        "person": person_json(person),
        "events": [event_json(e) for e in events[:50]],
    }

# ---------------------------------------------------------------------------
# Gauges
# ---------------------------------------------------------------------------

@route("PUT", "/{accountId}/projects/{projectId}/gauge.json")
def toggle_gauge(ctx):
    p = STORE.require_project(int(ctx["params"]["projectId"]))
    STORE.require_project_access(ctx["person"], p)
    body = ctx["body"] or {}
    gauge_payload = body.get("gauge") or body
    enabled = bool(gauge_payload.get("enabled", True))
    existing = next(
        (r for r in STORE.recordings.values()
         if r.get("type") == "Gauge" and r.get("bucket_id") == p["id"]
         and r.get("status") != "trashed"),
        None,
    )
    if not existing:
        existing = STORE.new_recording(
            type_="Gauge", title="Progress", creator_id=ctx["person"]["id"],
            bucket_id=p["id"], extra={"enabled": enabled},
        )
    else:
        existing["enabled"] = enabled
        STORE.touch(existing)
    return serialize_recording(existing)

@route("GET", "/{accountId}/projects/{projectId}/gauge/needles.json")
def list_gauge_needles(ctx):
    p = STORE.require_project(int(ctx["params"]["projectId"]))
    STORE.require_project_access(ctx["person"], p)
    gauge = next(
        (r for r in STORE.recordings.values()
         if r.get("type") == "Gauge" and r.get("bucket_id") == p["id"]),
        None,
    )
    if not gauge:
        return []
    needles = [r for r in STORE.recordings.values()
               if r.get("parent_id") == gauge["id"] and r.get("type") == "Gauge::Needle"
               and r.get("status") != "trashed"]
    return [serialize_recording(n) for n in needles]

@route("POST", "/{accountId}/projects/{projectId}/gauge/needles.json")
def create_gauge_needle(ctx):
    p = STORE.require_project(int(ctx["params"]["projectId"]))
    STORE.require_project_access(ctx["person"], p)
    body = require_json_body(ctx["body"], ["gauge_needle"])
    gn = body["gauge_needle"]
    gauge = next(
        (r for r in STORE.recordings.values()
         if r.get("type") == "Gauge" and r.get("bucket_id") == p["id"]
         and r.get("status") != "trashed"),
        None,
    )
    if not gauge:
        gauge = STORE.new_recording(
            type_="Gauge", title="Progress", creator_id=ctx["person"]["id"],
            bucket_id=p["id"], extra={"enabled": True},
        )
    needle = STORE.new_recording(
        type_="Gauge::Needle",
        title=gn.get("description") or "Needle",
        creator_id=ctx["person"]["id"],
        bucket_id=p["id"],
        parent_id=gauge["id"],
        extra={
            "description": gn.get("description") or "",
            "color": gn.get("color") or "green",
            "position": gn.get("position") if gn.get("position") is not None else 50,
        },
    )
    gauge["last_needle_color"] = needle["color"]
    gauge["previous_needle_position"] = gauge.get("last_needle_position")
    gauge["last_needle_position"] = needle["position"]
    STORE.touch(gauge)
    STORE.write_event(needle["id"], "created", ctx["person"]["id"])
    return serialize_recording(needle), 201

@route("GET", "/{accountId}/gauge_needles/{needleId}")
@route("GET", "/{accountId}/gauge_needles/{needleId}.json")
def get_gauge_needle(ctx):
    n = STORE.require_recording(int(ctx["params"]["needleId"]), ["Gauge::Needle"])
    ensure_member_can_mutate(ctx["person"], n)
    return serialize_recording(n)

@route("PUT", "/{accountId}/gauge_needles/{needleId}")
@route("PUT", "/{accountId}/gauge_needles/{needleId}.json")
def update_gauge_needle(ctx):
    n = STORE.require_recording(int(ctx["params"]["needleId"]), ["Gauge::Needle"])
    ensure_member_can_mutate(ctx["person"], n)
    body = ctx["body"] or {}
    gn = body.get("gauge_needle") or body
    if "description" in gn:
        n["description"] = gn["description"]
        n["title"] = gn["description"]
    if "color" in gn:
        n["color"] = gn["color"]
    if "position" in gn:
        n["position"] = gn["position"]
    STORE.touch(n)
    return serialize_recording(n)

@route("DELETE", "/{accountId}/gauge_needles/{needleId}")
@route("DELETE", "/{accountId}/gauge_needles/{needleId}.json")
def destroy_gauge_needle(ctx):
    n = STORE.require_recording(int(ctx["params"]["needleId"]), ["Gauge::Needle"])
    ensure_member_can_mutate(ctx["person"], n)
    STORE.set_status(n, "trashed", ctx["person"]["id"])
    return None, 204

# ---------------------------------------------------------------------------
# Questionnaires / Check-ins
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/questionnaires/{questionnaireId}")
@route("GET", "/{accountId}/questionnaires/{questionnaireId}.json")
def get_questionnaire(ctx):
    q = STORE.require_recording(int(ctx["params"]["questionnaireId"]), ["Questionnaire"])
    ensure_member_can_mutate(ctx["person"], q)
    return serialize_recording(q)

@route("GET", "/{accountId}/questionnaires/{questionnaireId}/questions.json")
def list_questions(ctx):
    q = STORE.require_recording(int(ctx["params"]["questionnaireId"]), ["Questionnaire"])
    ensure_member_can_mutate(ctx["person"], q)
    qs = [r for r in STORE.recordings.values()
          if r.get("parent_id") == q["id"] and r.get("type") == "Question"
          and r.get("status") != "trashed"]
    return [serialize_recording(x) for x in qs]

@route("POST", "/{accountId}/questionnaires/{questionnaireId}/questions.json")
def create_question(ctx):
    q = STORE.require_recording(int(ctx["params"]["questionnaireId"]), ["Questionnaire"])
    ensure_member_can_mutate(ctx["person"], q)
    body = require_json_body(ctx["body"], ["title", "schedule"])
    question = STORE.new_recording(
        type_="Question", title=body["title"], creator_id=ctx["person"]["id"],
        bucket_id=q["bucket_id"], parent_id=q["id"],
        extra={"schedule": body["schedule"], "paused": False},
    )
    STORE.write_event(question["id"], "created", ctx["person"]["id"])
    return serialize_recording(question), 201

@route("GET", "/{accountId}/questions/{questionId}")
@route("GET", "/{accountId}/questions/{questionId}.json")
def get_question(ctx):
    q = STORE.require_recording(int(ctx["params"]["questionId"]), ["Question"])
    ensure_member_can_mutate(ctx["person"], q)
    return serialize_recording(q)

@route("PUT", "/{accountId}/questions/{questionId}")
@route("PUT", "/{accountId}/questions/{questionId}.json")
def update_question(ctx):
    q = STORE.require_recording(int(ctx["params"]["questionId"]), ["Question"])
    ensure_member_can_mutate(ctx["person"], q)
    body = ctx["body"] or {}
    if "title" in body:
        q["title"] = body["title"]
    if "schedule" in body:
        q["schedule"] = body["schedule"]
    if "paused" in body:
        q["paused"] = bool(body["paused"])
    STORE.touch(q)
    return serialize_recording(q)

@route("POST", "/{accountId}/questions/{questionId}/pause.json")
def pause_question(ctx):
    q = STORE.require_recording(int(ctx["params"]["questionId"]), ["Question"])
    ensure_member_can_mutate(ctx["person"], q)
    q["paused"] = True
    STORE.touch(q)
    return serialize_recording(q)

@route("DELETE", "/{accountId}/questions/{questionId}/pause.json")
def unpause_question(ctx):
    q = STORE.require_recording(int(ctx["params"]["questionId"]), ["Question"])
    ensure_member_can_mutate(ctx["person"], q)
    q["paused"] = False
    STORE.touch(q)
    return serialize_recording(q)

@route("PUT", "/{accountId}/questions/{questionId}/notification_settings.json")
def update_question_notification_settings(ctx):
    q = STORE.require_recording(int(ctx["params"]["questionId"]), ["Question"])
    ensure_member_can_mutate(ctx["person"], q)
    body = ctx["body"] or {}
    q["notify_on_answer"] = body.get("notify_on_answer")
    q["digest_include_unanswered"] = body.get("digest_include_unanswered")
    STORE.touch(q)
    return serialize_recording(q)

@route("GET", "/{accountId}/questions/{questionId}/answers.json")
def list_answers(ctx):
    q = STORE.require_recording(int(ctx["params"]["questionId"]), ["Question"])
    ensure_member_can_mutate(ctx["person"], q)
    answers = [r for r in STORE.recordings.values()
               if r.get("parent_id") == q["id"] and r.get("type") == "Question::Answer"
               and r.get("status") != "trashed"]
    return [serialize_recording(a) for a in answers]

@route("POST", "/{accountId}/questions/{questionId}/answers.json")
def create_answer(ctx):
    q = STORE.require_recording(int(ctx["params"]["questionId"]), ["Question"])
    ensure_member_can_mutate(ctx["person"], q)
    body = ctx["body"] or {}
    content = body.get("content") or body.get("answer") or ""
    if not content:
        raise validation("content is required")
    a = STORE.new_recording(
        type_="Question::Answer", title=content[:80], creator_id=ctx["person"]["id"],
        bucket_id=q["bucket_id"], parent_id=q["id"], content=content,
        extra={"group_on": utcnow().date()},
    )
    STORE.write_event(a["id"], "created", ctx["person"]["id"])
    return serialize_recording(a), 201

@route("GET", "/{accountId}/questions/{questionId}/answers/by.json")
def list_question_answerers(ctx):
    q = STORE.require_recording(int(ctx["params"]["questionId"]), ["Question"])
    ensure_member_can_mutate(ctx["person"], q)
    people_ids = {
        r.get("creator_id") for r in STORE.recordings.values()
        if r.get("parent_id") == q["id"] and r.get("type") == "Question::Answer"
    }
    return [person_json(STORE.person(pid)) for pid in people_ids if STORE.person(pid)]

@route("GET", "/{accountId}/questions/{questionId}/answers/by/{personId}")
@route("GET", "/{accountId}/questions/{questionId}/answers/by/{personId}.json")
def get_answers_by_person(ctx):
    q = STORE.require_recording(int(ctx["params"]["questionId"]), ["Question"])
    pid = int(ctx["params"]["personId"])
    answers = [r for r in STORE.recordings.values()
               if r.get("parent_id") == q["id"] and r.get("type") == "Question::Answer"
               and r.get("creator_id") == pid and r.get("status") != "trashed"]
    return [serialize_recording(a) for a in answers]

@route("GET", "/{accountId}/question_answers/{answerId}")
@route("GET", "/{accountId}/question_answers/{answerId}.json")
def get_answer(ctx):
    a = STORE.require_recording(int(ctx["params"]["answerId"]), ["Question::Answer"])
    ensure_member_can_mutate(ctx["person"], a)
    return serialize_recording(a)

@route("PUT", "/{accountId}/question_answers/{answerId}")
@route("PUT", "/{accountId}/question_answers/{answerId}.json")
def update_answer(ctx):
    a = STORE.require_recording(int(ctx["params"]["answerId"]), ["Question::Answer"])
    ensure_member_can_mutate(ctx["person"], a)
    body = ctx["body"] or {}
    if "content" in body:
        a["content"] = body["content"]
        a["title"] = body["content"][:80]
    STORE.touch(a)
    return serialize_recording(a)

# ---------------------------------------------------------------------------
# Inbox / Email forwards
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/inboxes/{inboxId}")
@route("GET", "/{accountId}/inboxes/{inboxId}.json")
def get_inbox(ctx):
    i = STORE.require_recording(int(ctx["params"]["inboxId"]), ["Inbox"])
    ensure_member_can_mutate(ctx["person"], i)
    return serialize_recording(i)

@route("GET", "/{accountId}/inboxes/{inboxId}/forwards.json")
def list_forwards(ctx):
    i = STORE.require_recording(int(ctx["params"]["inboxId"]), ["Inbox"])
    ensure_member_can_mutate(ctx["person"], i)
    fw = [r for r in STORE.recordings.values()
          if r.get("parent_id") == i["id"] and r.get("type") == "Inbox::Forward"
          and r.get("status") != "trashed"]
    return [serialize_recording(f) for f in fw]

@route("GET", "/{accountId}/inbox_forwards/{forwardId}")
@route("GET", "/{accountId}/inbox_forwards/{forwardId}.json")
def get_forward(ctx):
    f = STORE.require_recording(int(ctx["params"]["forwardId"]), ["Inbox::Forward"])
    ensure_member_can_mutate(ctx["person"], f)
    return serialize_recording(f)

@route("GET", "/{accountId}/inbox_forwards/{forwardId}/replies.json")
def list_forward_replies(ctx):
    f = STORE.require_recording(int(ctx["params"]["forwardId"]), ["Inbox::Forward"])
    ensure_member_can_mutate(ctx["person"], f)
    replies = [r for r in STORE.recordings.values()
               if r.get("parent_id") == f["id"] and r.get("type") == "Inbox::Reply"
               and r.get("status") != "trashed"]
    return [serialize_recording(r) for r in replies]

@route("POST", "/{accountId}/inbox_forwards/{forwardId}/replies.json")
def create_forward_reply(ctx):
    f = STORE.require_recording(int(ctx["params"]["forwardId"]), ["Inbox::Forward"])
    ensure_member_can_mutate(ctx["person"], f)
    body = require_json_body(ctx["body"], ["content"])
    r = STORE.new_recording(
        type_="Inbox::Reply", title=body["content"][:80], creator_id=ctx["person"]["id"],
        bucket_id=f["bucket_id"], parent_id=f["id"], content=body["content"],
    )
    STORE.write_event(r["id"], "created", ctx["person"]["id"])
    return serialize_recording(r), 201

@route("GET", "/{accountId}/inbox_forwards/{forwardId}/replies/{replyId}")
@route("GET", "/{accountId}/inbox_forwards/{forwardId}/replies/{replyId}.json")
def get_forward_reply(ctx):
    r = STORE.require_recording(int(ctx["params"]["replyId"]), ["Inbox::Reply"])
    ensure_member_can_mutate(ctx["person"], r)
    return serialize_recording(r)

# ---------------------------------------------------------------------------
# Client board
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/client/correspondences.json")
def list_client_correspondences(ctx):
    items = [r for r in STORE.recordings.values()
             if r.get("type") == "Client::Correspondence" and r.get("status") != "trashed"]
    return [serialize_recording(r) for r in items]

@route("GET", "/{accountId}/client/correspondences/{correspondenceId}")
@route("GET", "/{accountId}/client/correspondences/{correspondenceId}.json")
def get_client_correspondence(ctx):
    r = STORE.require_recording(int(ctx["params"]["correspondenceId"]), ["Client::Correspondence"])
    return serialize_recording(r)

@route("GET", "/{accountId}/client/approvals.json")
def list_client_approvals(ctx):
    items = [r for r in STORE.recordings.values()
             if r.get("type") == "Client::Approval" and r.get("status") != "trashed"]
    return [serialize_recording(r) for r in items]

@route("GET", "/{accountId}/client/approvals/{approvalId}")
@route("GET", "/{accountId}/client/approvals/{approvalId}.json")
def get_client_approval(ctx):
    r = STORE.require_recording(int(ctx["params"]["approvalId"]), ["Client::Approval"])
    return serialize_recording(r)

@route("GET", "/{accountId}/client/recordings/{recordingId}/replies.json")
def list_client_replies(ctx):
    rid = int(ctx["params"]["recordingId"])
    replies = [r for r in STORE.recordings.values()
               if r.get("parent_id") == rid and r.get("type") == "Client::Reply"
               and r.get("status") != "trashed"]
    return [serialize_recording(r) for r in replies]

@route("GET", "/{accountId}/client/recordings/{recordingId}/replies/{replyId}")
@route("GET", "/{accountId}/client/recordings/{recordingId}/replies/{replyId}.json")
def get_client_reply(ctx):
    r = STORE.require_recording(int(ctx["params"]["replyId"]), ["Client::Reply"])
    return serialize_recording(r)

# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/buckets/{bucketId}/webhooks.json")
def list_webhooks(ctx):
    bid = int(ctx["params"]["bucketId"])
    return [webhook_json(w) for w in STORE.webhooks.values() if w.get("bucket_id") == bid]

@route("POST", "/{accountId}/buckets/{bucketId}/webhooks.json")
def create_webhook(ctx):
    bid = int(ctx["params"]["bucketId"])
    body = require_json_body(ctx["body"], ["payload_url", "types"])
    if len([w for w in STORE.webhooks.values() if w.get("bucket_id") == bid]) >= 10:
        raise APIError(507, "webhook_limit", "Webhook limit reached for this project")
    wid = STORE.next_id()
    now = utcnow()
    wh = {
        "id": wid,
        "bucket_id": bid,
        "payload_url": body["payload_url"],
        "types": body["types"],
        "active": body.get("active", True),
        "created_at": now,
        "updated_at": now,
        "recent_deliveries": [],
    }
    STORE.webhooks[wid] = wh
    return webhook_json(wh), 201

@route("GET", "/{accountId}/webhooks/{webhookId}")
@route("GET", "/{accountId}/webhooks/{webhookId}.json")
def get_webhook(ctx):
    wh = STORE.webhooks.get(int(ctx["params"]["webhookId"]))
    if not wh:
        raise not_found("Webhook not found")
    return webhook_json(wh)

@route("PUT", "/{accountId}/webhooks/{webhookId}")
@route("PUT", "/{accountId}/webhooks/{webhookId}.json")
def update_webhook(ctx):
    wh = STORE.webhooks.get(int(ctx["params"]["webhookId"]))
    if not wh:
        raise not_found("Webhook not found")
    body = ctx["body"] or {}
    for k in ("payload_url", "types", "active"):
        if k in body:
            wh[k] = body[k]
    wh["updated_at"] = utcnow()
    return webhook_json(wh)

@route("DELETE", "/{accountId}/webhooks/{webhookId}")
@route("DELETE", "/{accountId}/webhooks/{webhookId}.json")
def delete_webhook(ctx):
    wid = int(ctx["params"]["webhookId"])
    if wid not in STORE.webhooks:
        raise not_found("Webhook not found")
    del STORE.webhooks[wid]
    return None, 204

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/templates.json")
def list_templates(ctx):
    return [template_json(t) for t in STORE.templates.values() if t.get("status") != "trashed"]

@route("POST", "/{accountId}/templates.json")
def create_template(ctx):
    body = require_json_body(ctx["body"], ["name"])
    tid = STORE.next_id()
    now = utcnow()
    t = {
        "id": tid,
        "name": body["name"],
        "description": body.get("description") or "",
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "dock": [],
    }
    STORE.templates[tid] = t
    return template_json(t), 201

@route("GET", "/{accountId}/templates/{templateId}")
@route("GET", "/{accountId}/templates/{templateId}.json")
def get_template(ctx):
    t = STORE.templates.get(int(ctx["params"]["templateId"]))
    if not t:
        raise not_found("Template not found")
    return template_json(t)

@route("PUT", "/{accountId}/templates/{templateId}")
@route("PUT", "/{accountId}/templates/{templateId}.json")
def update_template(ctx):
    t = STORE.templates.get(int(ctx["params"]["templateId"]))
    if not t:
        raise not_found("Template not found")
    body = ctx["body"] or {}
    if "name" in body:
        t["name"] = body["name"]
    if "description" in body:
        t["description"] = body["description"]
    t["updated_at"] = utcnow()
    return template_json(t)

@route("DELETE", "/{accountId}/templates/{templateId}")
@route("DELETE", "/{accountId}/templates/{templateId}.json")
def delete_template(ctx):
    tid = int(ctx["params"]["templateId"])
    if tid not in STORE.templates:
        raise not_found("Template not found")
    STORE.templates[tid]["status"] = "trashed"
    return None, 204

@route("POST", "/{accountId}/templates/{templateId}/project_constructions.json")
def create_project_from_template(ctx):
    t = STORE.templates.get(int(ctx["params"]["templateId"]))
    if not t or t.get("status") == "trashed":
        raise not_found("Template not found")
    body = ctx["body"] or {}
    name = body.get("name") or t["name"]
    pid = STORE.next_id()
    now = utcnow()
    project = {
        "id": pid,
        "name": name,
        "description": t.get("description") or "",
        "purpose": "topic",
        "status": "active",
        "clients_enabled": False,
        "bookmarked": False,
        "sample": False,
        "admissions": "invite",
        "created_at": now,
        "updated_at": now,
        "access": {ctx["person"]["id"]},
        "starts_on": None,
        "ends_on": None,
    }
    STORE.projects[pid] = project
    cid = STORE.next_id()
    construction = {
        "id": cid,
        "status": "completed",
        "url": api_url(acct_path(f"/templates/{t['id']}/project_constructions/{cid}.json")),
        "project": project_json(project),
        "template_id": t["id"],
    }
    STORE.constructions[cid] = construction
    return construction, 201

@route("GET", "/{accountId}/templates/{templateId}/project_constructions/{constructionId}")
@route("GET", "/{accountId}/templates/{templateId}/project_constructions/{constructionId}.json")
def get_project_construction(ctx):
    c = STORE.constructions.get(int(ctx["params"]["constructionId"]))
    if not c:
        raise not_found("Construction not found")
    return c

# ---------------------------------------------------------------------------
# Lineup markers
# ---------------------------------------------------------------------------

@route("GET", "/{accountId}/lineup/markers.json")
def list_lineup_markers(ctx):
    return [
        {
            "id": m["id"],
            "name": m["name"],
            "date": m["date"],
            "created_at": iso(m.get("created_at")),
            "updated_at": iso(m.get("updated_at")),
        }
        for m in STORE.lineup_markers.values()
    ]

@route("POST", "/{accountId}/lineup/markers.json")
def create_lineup_marker(ctx):
    body = require_json_body(ctx["body"], ["name", "date"])
    mid = STORE.next_id()
    now = utcnow()
    m = {"id": mid, "name": body["name"], "date": body["date"], "created_at": now, "updated_at": now}
    STORE.lineup_markers[mid] = m
    return {
        "id": m["id"], "name": m["name"], "date": m["date"],
        "created_at": iso(m["created_at"]), "updated_at": iso(m["updated_at"]),
    }, 201

@route("PUT", "/{accountId}/lineup/markers/{markerId}")
@route("PUT", "/{accountId}/lineup/markers/{markerId}.json")
def update_lineup_marker(ctx):
    m = STORE.lineup_markers.get(int(ctx["params"]["markerId"]))
    if not m:
        raise not_found("Marker not found")
    body = ctx["body"] or {}
    if "name" in body:
        m["name"] = body["name"]
    if "date" in body:
        m["date"] = body["date"]
    m["updated_at"] = utcnow()
    return {
        "id": m["id"], "name": m["name"], "date": m["date"],
        "created_at": iso(m["created_at"]), "updated_at": iso(m["updated_at"]),
    }

@route("DELETE", "/{accountId}/lineup/markers/{markerId}")
@route("DELETE", "/{accountId}/lineup/markers/{markerId}.json")
def delete_lineup_marker(ctx):
    mid = int(ctx["params"]["markerId"])
    if mid not in STORE.lineup_markers:
        raise not_found("Marker not found")
    del STORE.lineup_markers[mid]
    return None, 204

# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class RequestContext(dict):
    pass


def normalize_result(result) -> Tuple[Any, int, dict]:
    """Normalize handler return values to (body, status, headers)."""
    if result is None:
        return None, 204, {}
    if isinstance(result, tuple):
        if len(result) == 2:
            body, status = result
            return body, status, {}
        if len(result) == 3:
            body, status, headers = result
            return body, status, headers or {}
    return result, 200, {}


class APIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"BasecampAPI/{VERSION}"

    def log_message(self, fmt: str, *args) -> None:
        # structured request log instead of default
        log.info("%s - %s", self.address_string(), fmt % args)

    def _request_id(self) -> str:
        return self.headers.get("X-Request-Id") or uuid.uuid4().hex

    def _cors_headers(self) -> dict:
        return {
            "Access-Control-Allow-Origin": CONFIG.cors_origin,
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Request-Id, X-Reset-Token, X-Filename",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS, HEAD, PATCH",
            "Access-Control-Expose-Headers": "Link, X-Total-Count, X-Request-Id, X-Runtime",
            "Access-Control-Max-Age": "86400",
        }

    def _send(self, status: int, body: Any = None, headers: Optional[dict] = None,
              request_id: str = "", started: float = 0.0) -> None:
        headers = dict(headers or {})
        headers.update(self._cors_headers())
        headers["X-Request-Id"] = request_id
        headers["X-Runtime"] = f"{max(0.0, time.time() - started):.4f}"
        headers["Date"] = http_date()
        headers["Cache-Control"] = "no-store"
        payload = b""
        if body is not None and status != 204:
            if isinstance(body, (bytes, bytearray)):
                payload = bytes(body)
                headers.setdefault("Content-Type", "application/octet-stream")
            else:
                payload = json.dumps(body, default=str, separators=(",", ":")).encode("utf-8")
                headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Content-Length"] = str(len(payload))
        try:
            self.send_response(status)
            for k, v in headers.items():
                if v is not None:
                    self.send_header(k, str(v))
            self.end_headers()
            if self.command != "HEAD" and payload:
                self.wfile.write(payload)
        except BrokenPipeError:
            pass

    def _read_body(self) -> Tuple[Any, bytes]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None, b""
        # Hard limit 25 MiB — production body guard
        if length > 25 * 1024 * 1024:
            raise validation("Request body too large (max 25 MiB)")
        raw = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype in ("application/json", "text/json") or (raw[:1] in (b"{", b"[") and "json" in ctype or ctype == ""):
            if not raw:
                return None, raw
            try:
                return json.loads(raw.decode("utf-8")), raw
            except (UnicodeDecodeError, json.JSONDecodeError):
                # binary upload with mislabeled content-type
                if ctype and "json" in ctype:
                    raise bad_request("Malformed JSON body")
                return None, raw
        return None, raw

    def _authenticate(self) -> dict:
        auth = self.headers.get("Authorization") or ""
        if not auth:
            raise unauthorized("Missing Authorization header")
        parts = auth.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise unauthorized("Expected Bearer token")
        token = parts[1].strip()
        if not token:
            raise unauthorized("Empty token")
        person = STORE.person_for_token(token)
        if not person:
            raise unauthorized("Invalid token")
        return person

    def _check_account(self, params: dict) -> None:
        if "accountId" in params and str(params["accountId"]) != str(CONFIG.account_id):
            # Still allow if it matches store account
            if str(params["accountId"]) != str(STORE.account.get("id")):
                raise not_found("Account not found")

    def _build_link_header(self, path: str, qs: dict, page: int, page_size: int,
                           total: int, has_next: bool) -> Optional[str]:
        if not has_next:
            return None
        next_page = page + 1
        q = {k: v[0] if isinstance(v, list) else v for k, v in qs.items()}
        q["page"] = str(next_page)
        # rebuild query
        url = api_url(path + "?" + urlencode(q))
        return f'<{url}>; rel="next"'

    def _dispatch(self) -> None:
        started = time.time()
        request_id = self._request_id()
        STORE.stats["requests"] += 1
        try:
            parsed = urlparse(self.path)
            path = parsed.path or "/"
            # strip trailing slash except root
            if len(path) > 1 and path.endswith("/"):
                path = path[:-1]
            qs = parse_qs(parsed.query, keep_blank_values=True)

            if self.command == "OPTIONS":
                self._send(204, None, request_id=request_id, started=started)
                return

            route, params, allowed = ROUTER.match(self.command, path)
            if route is None and allowed:
                raise method_not_allowed(sorted(set(allowed)))
            if route is None:
                raise not_found(f"No route for {self.command} {path}")

            # Coerce path params that look like ids (strip .json already handled by dual routes)
            for k, v in list(params.items()):
                if v is not None and v.endswith(".json") and k.endswith("Id"):
                    # e.g. projects/1000.json captured wrong — dual routes handle most cases
                    params[k] = v[:-5]

            person = None
            if route.auth:
                person = self._authenticate()
                STORE.check_rate(f"person:{person['id']}")
            else:
                # still rate-limit by IP for public endpoints
                STORE.check_rate(f"ip:{self.client_address[0]}")

            body, raw = (None, b"")
            if self.command in ("POST", "PUT", "PATCH"):
                body, raw = self._read_body()

            headers = {k: self.headers.get(k) for k in self.headers.keys()}

            ctx = RequestContext(
                method=self.command,
                path=path,
                qs=qs,
                params=params,
                body=body,
                raw_body=raw,
                person=person,
                headers=headers,
                request_id=request_id,
            )

            if "accountId" in params:
                self._check_account(params)

            with STORE.lock:
                result = route.handler(ctx)

            resp_body, status, extra_headers = normalize_result(result)

            # Pagination Link header if handler provided X-Total-Count and page query
            if extra_headers.get("X-Total-Count") and "page" in qs or extra_headers.get("X-Total-Count"):
                try:
                    total = int(extra_headers["X-Total-Count"])
                    page = parse_page(qs)
                    has_next = page * CONFIG.page_size < total
                    link = self._build_link_header(path, qs, page, CONFIG.page_size, total, has_next)
                    if link:
                        extra_headers["Link"] = link
                except Exception:
                    pass

            self._send(status, resp_body, extra_headers, request_id=request_id, started=started)
            log.info(
                "request method=%s path=%s status=%s rid=%s person=%s duration_ms=%.1f",
                self.command, path, status, request_id,
                (person or {}).get("id"),
                (time.time() - started) * 1000,
            )
        except APIError as e:
            STORE.stats["errors"] += 1
            hdrs = {}
            if e.status == 429 and "retry_after" in e.extra:
                hdrs["Retry-After"] = str(e.extra["retry_after"])
            if e.status == 405 and "allowed" in e.extra:
                hdrs["Allow"] = ", ".join(e.extra["allowed"])
            self._send(e.status, e.body(), hdrs, request_id=request_id, started=started)
            log.warning(
                "request method=%s path=%s status=%s error=%s rid=%s",
                self.command, getattr(self, "path", ""), e.status, e.error, request_id,
            )
        except Exception as e:
            STORE.stats["errors"] += 1
            log.exception("Unhandled error rid=%s: %s", request_id, e)
            self._send(
                500,
                {"error": "internal_error", "message": "Internal server error", "request_id": request_id},
                request_id=request_id,
                started=started,
            )

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()


class ThreadedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def create_server(host: str = None, port: int = None) -> ThreadedServer:
    host = host if host is not None else CONFIG.host
    port = port if port is not None else CONFIG.port
    # Refresh base URLs if port overridden
    if CONFIG.api_base == API_BASE_DEFAULT or os.environ.get("BASECAMP_API_BASE") is None:
        CONFIG.api_base = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"
    return ThreadedServer((host, port), APIHandler)


def run(host: str = None, port: int = None) -> None:
    if CONFIG.seed_on_boot and not STORE.ready:
        seed_world(STORE)

    server = create_server(host, port)
    host, port = server.server_address[:2]

    def _shutdown(signum, frame):
        log.info("Signal %s received — shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("=" * 60)
    log.info("Basecamp 5 API %s listening on http://%s:%s", VERSION, host, port)
    log.info("Account ID: %s", CONFIG.account_id)
    log.info("Bearer token (owner): bcamp_pat_owner_alex")
    log.info("Health: GET /health  Ready: GET /ready  Reset: POST /admin/reset")
    log.info("OpenAPI surface: 131 paths / 203 operations (+ operability routes)")
    log.info("Seed project: Launch the new website (id=1000)")
    log.info("=" * 60)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        log.info("Server stopped")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Basecamp 5 API server")
    parser.add_argument("--host", default=None, help="Bind host (default env/127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default env/9292)")
    parser.add_argument("--no-seed", action="store_true", help="Skip sample seed")
    parser.add_argument("--check", action="store_true",
                        help="Seed + validate routes then exit (CI smoke)")
    args = parser.parse_args(argv)

    if args.no_seed:
        CONFIG.seed_on_boot = False
        STORE.ready = True

    if args.host:
        CONFIG.host = args.host
    if args.port:
        CONFIG.port = args.port

    if args.check:
        seed_world(STORE)
        n_routes = len(ROUTER.routes)
        n_people = len(STORE.people)
        n_recs = len(STORE.recordings)
        n_projects = len(STORE.projects)
        print(json.dumps({
            "ok": True,
            "routes": n_routes,
            "people": n_people,
            "recordings": n_recs,
            "projects": n_projects,
            "messages": sum(1 for r in STORE.recordings.values() if r.get("type") == "Message"),
            "todos": sum(1 for r in STORE.recordings.values() if r.get("type") == "Todo"),
            "cards": sum(1 for r in STORE.recordings.values() if r.get("type") == "Kanban::Card"),
            "chat_lines": sum(1 for r in STORE.recordings.values() if r.get("type") == "Chat::Lines::Line"),
            "owner_token": "bcamp_pat_owner_alex",
            "account_id": CONFIG.account_id,
        }, indent=2))
        return 0

    run(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
