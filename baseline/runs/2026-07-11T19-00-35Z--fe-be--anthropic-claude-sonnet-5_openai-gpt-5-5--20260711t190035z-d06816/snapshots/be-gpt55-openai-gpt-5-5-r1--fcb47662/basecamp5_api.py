#!/usr/bin/env python3
"""
Single-file Basecamp 5 API server.

This server is intentionally dependency-free: it runs on Python's stdlib HTTP
stack and persists state in SQLite. The routing table is built from the vendored
Basecamp OpenAPI reference so every documented path/method is known at runtime.

Run:
  python3 basecamp5_api.py

Default account and tokens:
  account_id: 999
  owner:      dev-owner-token
  client:     dev-client-token
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import email.utils
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import signal
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
OPENAPI_PATH = ROOT / "reference" / "basecamp-sdk" / "openapi.json"
BEHAVIOR_PATH = ROOT / "reference" / "basecamp-sdk" / "behavior-model.json"

DEFAULT_ACCOUNT_ID = "999"
DEFAULT_DB_PATH = str(ROOT / "basecamp5.sqlite3")
DEFAULT_BASE_TIME = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
MAX_REQUEST_BYTES = int(os.getenv("BASECAMP5_MAX_REQUEST_BYTES", str(10 * 1024 * 1024)))
MAX_RESPONSE_BYTES = 50 * 1024 * 1024
MAX_ERROR_BYTES = 1024 * 1024
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
SERVER_NAME = "basecamp5-single-file-api"


Json = dict[str, Any]


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        retryable: bool = False,
        headers: dict[str, str] | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message[:500]
        self.hint = hint
        self.retryable = retryable
        self.headers = headers or {}
        self.details = details

    def body(self, request_id: str) -> Json:
        payload: Json = {
            "error": self.message,
            "code": self.code,
            "http_status": self.status,
            "retryable": self.retryable,
            "request_id": request_id,
        }
        if self.hint:
            payload["error_description"] = self.hint
        if self.details is not None:
            payload["details"] = self.details
        return payload


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def date_iso(dt: datetime) -> str:
    return dt.date().isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def json_loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(400, "validation", "Request body is not valid JSON", details={"line": exc.lineno, "column": exc.colno})


def truthy(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ApiError(400, "validation", f"Invalid boolean query value: {value}")


def require_text(body: Json, *names: str) -> str:
    for name in names:
        value = body.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ApiError(422, "validation", f"Missing required field: {' or '.join(names)}")


def maybe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(422, "validation", f"Expected integer, got {value!r}")


def clean_patch(body: Json, blocked: set[str] | None = None) -> Json:
    blocked = blocked or {"id", "url", "app_url", "created_at", "creator", "bucket", "parent"}
    return {k: v for k, v in body.items() if k not in blocked}


def resource_url(account_id: str, segment: str, rid: int | str) -> str:
    return f"/{account_id}/{segment}/{rid}"


def app_url(account_id: str, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"https://3.basecampapi.com/{account_id}{path}"


@dataclasses.dataclass(frozen=True)
class Route:
    method: str
    template: str
    operation_id: str
    tags: tuple[str, ...]
    regex: re.Pattern[str]
    param_names: tuple[str, ...]
    pagination: bool
    request_required: bool
    required_fields: tuple[str, ...]
    behavior: Json


def compile_template(template: str) -> tuple[re.Pattern[str], tuple[str, ...]]:
    names: list[str] = []

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        names.append(name)
        if name.endswith("Id") or name in {"id", "date"}:
            return rf"(?P<{name}>[^/]+)"
        return rf"(?P<{name}>[^/]+)"

    pattern = re.sub(r"\{([^}]+)\}", repl, template)
    return re.compile("^" + pattern + "$"), tuple(names)


def deref_schema(openapi: Json, schema: Json | None) -> Json:
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        return openapi.get("components", {}).get("schemas", {}).get(name, {})
    return schema


def load_openapi() -> Json:
    if not OPENAPI_PATH.exists():
        raise RuntimeError(f"Missing OpenAPI reference: {OPENAPI_PATH}")
    with OPENAPI_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_behavior() -> Json:
    if not BEHAVIOR_PATH.exists():
        return {"operations": {}}
    with BEHAVIOR_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def build_routes(openapi: Json, behavior: Json) -> list[Route]:
    routes: list[Route] = []
    operation_behavior = behavior.get("operations", {})
    for template, path_spec in openapi.get("paths", {}).items():
        if not isinstance(path_spec, dict):
            continue
        for method, op in path_spec.items():
            if method.lower() not in {"get", "post", "put", "delete", "patch", "head"}:
                continue
            regex, params = compile_template(template)
            request_body = op.get("requestBody") or {}
            schema = (
                request_body.get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            resolved = deref_schema(openapi, schema)
            required = tuple(str(x) for x in resolved.get("required", []) if isinstance(x, str))
            routes.append(
                Route(
                    method=method.upper(),
                    template=template,
                    operation_id=op.get("operationId", f"{method.upper()} {template}"),
                    tags=tuple(op.get("tags") or ()),
                    regex=regex,
                    param_names=params,
                    pagination=bool(op.get("x-basecamp-pagination")),
                    request_required=bool(request_body.get("required")),
                    required_fields=required,
                    behavior=operation_behavior.get(op.get("operationId", ""), {}),
                )
            )
    routes.sort(key=lambda r: (r.template.count("{"), -len(r.template)))
    return routes


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self.local = threading.local()

    def connect(self) -> sqlite3.Connection:
        con = getattr(self.local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA busy_timeout = 5000")
            self.local.con = con
        return con

    @contextlib.contextmanager
    def tx(self) -> Any:
        con = self.connect()
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except Exception:
            con.execute("ROLLBACK")
            raise
        else:
            con.execute("COMMIT")

    def init_schema(self) -> None:
        con = self.connect()
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accounts (
              id TEXT PRIMARY KEY,
              data TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tokens (
              token_hash TEXT PRIMARY KEY,
              account_id TEXT NOT NULL,
              person_id INTEGER NOT NULL,
              role TEXT NOT NULL,
              expires_at TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resources (
              account_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              id INTEGER NOT NULL,
              parent_kind TEXT,
              parent_id INTEGER,
              bucket_id INTEGER,
              status TEXT NOT NULL DEFAULT 'active',
              title TEXT,
              position INTEGER NOT NULL DEFAULT 0,
              data TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(account_id, kind, id)
            );
            CREATE INDEX IF NOT EXISTS idx_resources_parent ON resources(account_id, parent_kind, parent_id);
            CREATE INDEX IF NOT EXISTS idx_resources_bucket ON resources(account_id, bucket_id, kind);
            CREATE INDEX IF NOT EXISTS idx_resources_status ON resources(account_id, kind, status);
            CREATE TABLE IF NOT EXISTS events (
              account_id TEXT NOT NULL,
              id INTEGER NOT NULL,
              recording_id INTEGER,
              action TEXT NOT NULL,
              creator_id INTEGER,
              created_at TEXT NOT NULL,
              data TEXT NOT NULL,
              PRIMARY KEY(account_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_recording ON events(account_id, recording_id, created_at);
            CREATE TABLE IF NOT EXISTS boosts (
              account_id TEXT NOT NULL,
              id INTEGER NOT NULL,
              recording_id INTEGER NOT NULL,
              event_id INTEGER,
              content TEXT NOT NULL,
              booster_id INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(account_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_boosts_recording ON boosts(account_id, recording_id);
            CREATE TABLE IF NOT EXISTS comments (
              account_id TEXT NOT NULL,
              id INTEGER NOT NULL,
              recording_id INTEGER NOT NULL,
              creator_id INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'active',
              data TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(account_id, id)
            );
            CREATE INDEX IF NOT EXISTS idx_comments_recording ON comments(account_id, recording_id, created_at);
            CREATE TABLE IF NOT EXISTS subscriptions (
              account_id TEXT NOT NULL,
              recording_id INTEGER NOT NULL,
              person_id INTEGER NOT NULL,
              active INTEGER NOT NULL DEFAULT 1,
              notify_on_comments INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(account_id, recording_id, person_id)
            );
            CREATE TABLE IF NOT EXISTS readings (
              account_id TEXT NOT NULL,
              person_id INTEGER NOT NULL,
              recording_id INTEGER NOT NULL,
              read_at TEXT,
              resurface_at TEXT,
              data TEXT NOT NULL DEFAULT '{}',
              updated_at TEXT NOT NULL,
              PRIMARY KEY(account_id, person_id, recording_id)
            );
            CREATE TABLE IF NOT EXISTS idempotency (
              account_id TEXT NOT NULL,
              actor_id INTEGER NOT NULL,
              key TEXT NOT NULL,
              method TEXT NOT NULL,
              path TEXT NOT NULL,
              body_hash TEXT NOT NULL,
              status INTEGER NOT NULL,
              headers TEXT NOT NULL,
              body TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(account_id, actor_id, key)
            );
            CREATE TABLE IF NOT EXISTS counters (
              account_id TEXT NOT NULL,
              name TEXT NOT NULL,
              value INTEGER NOT NULL,
              PRIMARY KEY(account_id, name)
            );
            """
        )

    def get_meta(self, key: str) -> str | None:
        row = self.connect().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.connect().execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def next_id(self, account_id: str, name: str, start: int = 1000) -> int:
        con = self.connect()
        row = con.execute("SELECT value FROM counters WHERE account_id=? AND name=?", (account_id, name)).fetchone()
        if row is None:
            con.execute("INSERT INTO counters(account_id,name,value) VALUES(?,?,?)", (account_id, name, start))
            return start
        value = int(row["value"]) + 1
        con.execute("UPDATE counters SET value=? WHERE account_id=? AND name=?", (value, account_id, name))
        return value

    def put_account(self, account_id: str, data: Json) -> None:
        now = iso()
        self.connect().execute(
            """
            INSERT INTO accounts(id,data,created_at,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at
            """,
            (account_id, canonical_json(data), data.get("created_at", now), now),
        )

    def get_account(self, account_id: str) -> Json | None:
        row = self.connect().execute("SELECT data FROM accounts WHERE id=?", (account_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def put_token(self, token: str, account_id: str, person_id: int, role: str, expires_at: str | None = None) -> None:
        self.connect().execute(
            """
            INSERT OR REPLACE INTO tokens(token_hash,account_id,person_id,role,expires_at,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (sha256_text(token), account_id, person_id, role, expires_at, iso()),
        )

    def get_token(self, token: str) -> sqlite3.Row | None:
        return self.connect().execute("SELECT * FROM tokens WHERE token_hash=?", (sha256_text(token),)).fetchone()

    def put_resource(
        self,
        account_id: str,
        kind: str,
        rid: int,
        data: Json,
        *,
        parent_kind: str | None = None,
        parent_id: int | None = None,
        bucket_id: int | None = None,
        status: str | None = None,
        title: str | None = None,
        position: int | None = None,
    ) -> Json:
        now = iso()
        existing = self.connect().execute(
            """
            SELECT parent_kind,parent_id,bucket_id,status,title,position,created_at
            FROM resources
            WHERE account_id=? AND kind=? AND id=?
            """,
            (account_id, kind, int(rid)),
        ).fetchone()
        if "created_at" not in data:
            data["created_at"] = existing["created_at"] if existing else now
        data["updated_at"] = data.get("updated_at") or now
        if existing:
            parent_kind = parent_kind if parent_kind is not None else existing["parent_kind"]
            parent_id = parent_id if parent_id is not None else existing["parent_id"]
            bucket_id = bucket_id if bucket_id is not None else existing["bucket_id"]
            status = status or data.get("status") or existing["status"] or "active"
            title = title if title is not None else data.get("title") or data.get("name") or data.get("subject") or existing["title"]
            position = int(position if position is not None else data.get("position") if data.get("position") is not None else existing["position"] or 0)
        else:
            status = status or data.get("status") or "active"
            title = title if title is not None else data.get("title") or data.get("name") or data.get("subject")
            position = int(position if position is not None else data.get("position") or 0)
        self.connect().execute(
            """
            INSERT INTO resources(account_id,kind,id,parent_kind,parent_id,bucket_id,status,title,position,data,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id,kind,id) DO UPDATE SET
              parent_kind=excluded.parent_kind,
              parent_id=excluded.parent_id,
              bucket_id=excluded.bucket_id,
              status=excluded.status,
              title=excluded.title,
              position=excluded.position,
              data=excluded.data,
              updated_at=excluded.updated_at
            """,
            (
                account_id,
                kind,
                int(rid),
                parent_kind,
                parent_id,
                bucket_id,
                status,
                title,
                position,
                canonical_json(data),
                data["created_at"],
                data["updated_at"],
            ),
        )
        return data

    def get_resource(self, account_id: str, kind: str, rid: int | str) -> Json | None:
        row = self.connect().execute(
            "SELECT data FROM resources WHERE account_id=? AND kind=? AND id=?",
            (account_id, kind, int(rid)),
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def find_resource_by_id(self, account_id: str, rid: int | str, kinds: list[str] | None = None) -> tuple[str, Json] | None:
        args: list[Any] = [account_id, int(rid)]
        sql = "SELECT kind,data FROM resources WHERE account_id=? AND id=?"
        if kinds:
            sql += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
            args.extend(kinds)
        row = self.connect().execute(sql, args).fetchone()
        if row:
            return row["kind"], json.loads(row["data"])
        return None

    def list_resources(
        self,
        account_id: str,
        kind: str | list[str],
        *,
        parent_kind: str | None = None,
        parent_id: int | None = None,
        bucket_id: int | None = None,
        status: str | None = None,
        search: str | None = None,
        include_trashed: bool = False,
    ) -> list[Json]:
        kinds = [kind] if isinstance(kind, str) else kind
        args: list[Any] = [account_id, *kinds]
        sql = "SELECT data FROM resources WHERE account_id=? AND kind IN (" + ",".join("?" for _ in kinds) + ")"
        if parent_kind is not None:
            sql += " AND parent_kind=?"
            args.append(parent_kind)
        if parent_id is not None:
            sql += " AND parent_id=?"
            args.append(int(parent_id))
        if bucket_id is not None:
            sql += " AND bucket_id=?"
            args.append(int(bucket_id))
        if status:
            sql += " AND status=?"
            args.append(status)
        elif not include_trashed:
            sql += " AND status!='trashed'"
        sql += " ORDER BY position ASC, id ASC"
        items = [json.loads(row["data"]) for row in self.connect().execute(sql, args).fetchall()]
        if search:
            q = search.lower()
            items = [item for item in items if q in canonical_json(item).lower()]
        return items

    def update_resource(self, account_id: str, kind: str, rid: int | str, patch: Json) -> Json:
        data = self.get_resource(account_id, kind, rid)
        if not data:
            raise ApiError(404, "not_found", f"{kind} {rid} was not found")
        data.update(clean_patch(patch))
        data["updated_at"] = iso()
        status = data.get("status", "active")
        self.put_resource(
            account_id,
            kind,
            int(rid),
            data,
            parent_kind=self._row_field(account_id, kind, rid, "parent_kind"),
            parent_id=self._row_field(account_id, kind, rid, "parent_id"),
            bucket_id=self._row_field(account_id, kind, rid, "bucket_id"),
            status=status,
            title=data.get("title") or data.get("name") or data.get("subject"),
            position=data.get("position", 0),
        )
        return data

    def _row_field(self, account_id: str, kind: str, rid: int | str, field: str) -> Any:
        row = self.connect().execute(
            f"SELECT {field} FROM resources WHERE account_id=? AND kind=? AND id=?",
            (account_id, kind, int(rid)),
        ).fetchone()
        return row[field] if row else None

    def write_event(self, account_id: str, recording_id: int | None, action: str, creator_id: int, data: Json | None = None) -> Json:
        eid = self.next_id(account_id, "event", 8000)
        event = {
            "id": eid,
            "recording_id": recording_id,
            "action": action,
            "creator_id": creator_id,
            "created_at": iso(),
            "details": data or {},
        }
        self.connect().execute(
            "INSERT INTO events(account_id,id,recording_id,action,creator_id,created_at,data) VALUES(?,?,?,?,?,?,?)",
            (account_id, eid, recording_id, action, creator_id, event["created_at"], canonical_json(event)),
        )
        return event

    def list_events(self, account_id: str, recording_id: int | None = None) -> list[Json]:
        if recording_id is None:
            rows = self.connect().execute(
                "SELECT data FROM events WHERE account_id=? ORDER BY created_at DESC, id DESC",
                (account_id,),
            ).fetchall()
        else:
            rows = self.connect().execute(
                "SELECT data FROM events WHERE account_id=? AND recording_id=? ORDER BY created_at DESC, id DESC",
                (account_id, int(recording_id)),
            ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def put_comment(self, account_id: str, recording_id: int, creator_id: int, content: str) -> Json:
        cid = self.next_id(account_id, "comment", 7000)
        parent = self.recording_parent(account_id, recording_id)
        bucket = self.recording_bucket(account_id, recording_id)
        creator = self.get_resource(account_id, "Person", creator_id) or {"id": creator_id, "name": "Unknown"}
        now = iso()
        comment = {
            "id": cid,
            "status": "active",
            "visible_to_clients": False,
            "created_at": now,
            "updated_at": now,
            "title": "Comment",
            "inherits_status": True,
            "type": "Comment",
            "url": f"/{account_id}/comments/{cid}",
            "app_url": app_url(account_id, f"/comments/{cid}"),
            "bookmark_url": f"/{account_id}/my/bookmarks/{cid}",
            "parent": parent,
            "bucket": bucket,
            "creator": creator,
            "content": content,
            "boosts_count": 0,
            "boosts_url": f"/{account_id}/recordings/{cid}/boosts.json",
        }
        self.connect().execute(
            "INSERT INTO comments(account_id,id,recording_id,creator_id,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (account_id, cid, recording_id, creator_id, "active", canonical_json(comment), now, now),
        )
        self.write_event(account_id, recording_id, "commented", creator_id, {"comment_id": cid})
        self.recount_recording(account_id, recording_id)
        return comment

    def get_comment(self, account_id: str, cid: int | str) -> Json | None:
        row = self.connect().execute(
            "SELECT data FROM comments WHERE account_id=? AND id=? AND status!='trashed'",
            (account_id, int(cid)),
        ).fetchone()
        return json.loads(row["data"]) if row else None

    def list_comments(self, account_id: str, recording_id: int | str) -> list[Json]:
        rows = self.connect().execute(
            "SELECT data FROM comments WHERE account_id=? AND recording_id=? AND status!='trashed' ORDER BY created_at ASC, id ASC",
            (account_id, int(recording_id)),
        ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def update_comment(self, account_id: str, cid: int | str, patch: Json) -> Json:
        comment = self.get_comment(account_id, cid)
        if not comment:
            raise ApiError(404, "not_found", f"Comment {cid} was not found")
        if "content" in patch:
            comment["content"] = str(patch["content"])
        comment["updated_at"] = iso()
        self.connect().execute(
            "UPDATE comments SET data=?, updated_at=? WHERE account_id=? AND id=?",
            (canonical_json(comment), comment["updated_at"], account_id, int(cid)),
        )
        return comment

    def create_boost(self, account_id: str, recording_id: int, booster_id: int, content: str, event_id: int | None = None) -> Json:
        if not self.find_resource_by_id(account_id, recording_id) and not self.get_comment(account_id, recording_id):
            raise ApiError(404, "not_found", f"Recording {recording_id} was not found")
        bid = self.next_id(account_id, "boost", 9000)
        booster = self.get_resource(account_id, "Person", booster_id) or {"id": booster_id, "name": "Unknown"}
        boost = {
            "id": bid,
            "content": content,
            "created_at": iso(),
            "booster": booster,
            "recording": self.recording_parent(account_id, recording_id),
        }
        self.connect().execute(
            "INSERT INTO boosts(account_id,id,recording_id,event_id,content,booster_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (account_id, bid, recording_id, event_id, content, booster_id, boost["created_at"]),
        )
        self.recount_recording(account_id, recording_id)
        return boost

    def get_boost(self, account_id: str, bid: int | str) -> Json | None:
        row = self.connect().execute("SELECT * FROM boosts WHERE account_id=? AND id=?", (account_id, int(bid))).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "content": row["content"],
            "created_at": row["created_at"],
            "booster": self.get_resource(account_id, "Person", row["booster_id"]),
            "recording": self.recording_parent(account_id, row["recording_id"]),
        }

    def list_boosts(self, account_id: str, recording_id: int | str, event_id: int | None = None) -> list[Json]:
        if event_id is None:
            rows = self.connect().execute(
                "SELECT id FROM boosts WHERE account_id=? AND recording_id=? ORDER BY created_at ASC",
                (account_id, int(recording_id)),
            ).fetchall()
        else:
            rows = self.connect().execute(
                "SELECT id FROM boosts WHERE account_id=? AND recording_id=? AND event_id=? ORDER BY created_at ASC",
                (account_id, int(recording_id), event_id),
            ).fetchall()
        return [self.get_boost(account_id, row["id"]) for row in rows if self.get_boost(account_id, row["id"])]

    def delete_boost(self, account_id: str, bid: int | str) -> None:
        row = self.connect().execute("SELECT recording_id FROM boosts WHERE account_id=? AND id=?", (account_id, int(bid))).fetchone()
        if not row:
            raise ApiError(404, "not_found", f"Boost {bid} was not found")
        self.connect().execute("DELETE FROM boosts WHERE account_id=? AND id=?", (account_id, int(bid)))
        self.recount_recording(account_id, row["recording_id"])

    def subscribe(self, account_id: str, recording_id: int, person_id: int, notify: bool = True) -> Json:
        self.connect().execute(
            """
            INSERT INTO subscriptions(account_id,recording_id,person_id,active,notify_on_comments,updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(account_id,recording_id,person_id) DO UPDATE SET
              active=excluded.active, notify_on_comments=excluded.notify_on_comments, updated_at=excluded.updated_at
            """,
            (account_id, recording_id, person_id, 1, 1 if notify else 0, iso()),
        )
        return self.subscription(account_id, recording_id, person_id)

    def unsubscribe(self, account_id: str, recording_id: int, person_id: int) -> None:
        self.connect().execute(
            "UPDATE subscriptions SET active=0, updated_at=? WHERE account_id=? AND recording_id=? AND person_id=?",
            (iso(), account_id, recording_id, person_id),
        )

    def subscription(self, account_id: str, recording_id: int, person_id: int) -> Json:
        row = self.connect().execute(
            "SELECT * FROM subscriptions WHERE account_id=? AND recording_id=? AND person_id=?",
            (account_id, recording_id, person_id),
        ).fetchone()
        return {
            "recording_id": recording_id,
            "person_id": person_id,
            "subscribed": bool(row and row["active"]),
            "notify_on_comments": bool(row and row["notify_on_comments"]),
            "updated_at": row["updated_at"] if row else None,
        }

    def recording_parent(self, account_id: str, rid: int | str) -> Json:
        found = self.find_resource_by_id(account_id, rid)
        if found:
            _, data = found
            return {"id": data["id"], "title": data.get("title") or data.get("name", ""), "type": data.get("type", "Recording"), "url": data.get("url")}
        comment = self.get_comment(account_id, rid)
        if comment:
            return {"id": comment["id"], "title": comment["title"], "type": "Comment", "url": comment["url"]}
        return {"id": int(rid), "title": "Unknown", "type": "Recording", "url": f"/{account_id}/recordings/{rid}"}

    def recording_bucket(self, account_id: str, rid: int | str) -> Json:
        found = self.find_resource_by_id(account_id, rid)
        if found:
            _, data = found
            if isinstance(data.get("bucket"), dict):
                return data["bucket"]
            bucket_id = data.get("bucket_id")
            if bucket_id:
                project = self.get_resource(account_id, "Project", bucket_id)
                if project:
                    return {"id": project["id"], "name": project["name"], "type": "Project"}
        return {"id": 0, "name": "Account", "type": "Account"}

    def recount_recording(self, account_id: str, rid: int | str) -> None:
        found = self.find_resource_by_id(account_id, rid)
        if not found:
            return
        kind, data = found
        comments_count = self.connect().execute(
            "SELECT count(*) AS c FROM comments WHERE account_id=? AND recording_id=? AND status!='trashed'",
            (account_id, int(rid)),
        ).fetchone()["c"]
        boosts_count = self.connect().execute(
            "SELECT count(*) AS c FROM boosts WHERE account_id=? AND recording_id=?",
            (account_id, int(rid)),
        ).fetchone()["c"]
        data["comments_count"] = comments_count
        data["boosts_count"] = boosts_count
        data["updated_at"] = iso()
        self.put_resource(account_id, kind, int(rid), data)


def person(
    pid: int,
    name: str,
    title: str,
    email: str,
    *,
    sample: bool = True,
    admin: bool = False,
    owner: bool = False,
    client: bool = False,
) -> Json:
    now = iso(DEFAULT_BASE_TIME - timedelta(days=40))
    employee = not client
    return {
        "id": pid,
        "attachable_sgid": f"person-{pid}",
        "name": name,
        "email_address": email,
        "personable_type": "User",
        "title": title,
        "bio": "",
        "location": "",
        "created_at": now,
        "updated_at": now,
        "admin": admin,
        "owner": owner,
        "client": client,
        "employee": employee,
        "sample": sample,
        "time_zone": "America/Chicago",
        "avatar_url": f"https://example.invalid/avatars/{pid}.png",
        "company": {"id": 1, "name": "Acme Studios"},
        "can_manage_projects": employee,
        "can_manage_people": admin or owner,
        "can_ping": True,
        "can_access_timesheet": employee,
        "can_access_hill_charts": employee,
    }


def account_payload(account_id: str) -> Json:
    now = iso(DEFAULT_BASE_TIME - timedelta(days=40))
    return {
        "id": int(account_id),
        "name": "Acme Studios",
        "product": "Basecamp",
        "created_at": now,
        "updated_at": now,
        "url": f"/{account_id}/account.json",
        "app_url": app_url(account_id, "/"),
        "settings": {
            "clients_enabled": True,
            "timesheet_enabled": True,
            "hill_charts_enabled": True,
            "default_visibility": "team",
        },
        "limits": {
            "projects": None,
            "people": None,
            "storage_bytes": None,
        },
        "subscription": {
            "status": "active",
            "plan": "sample",
        },
    }


def project_payload(account_id: str, pid: int, name: str, description: str, created: datetime) -> Json:
    now = iso(created)
    return {
        "id": pid,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "name": name,
        "description": description,
        "purpose": "Launch and coordinate the company website",
        "clients_enabled": True,
        "bookmark_url": f"/{account_id}/my/bookmarks/{pid}",
        "url": f"/{account_id}/projects/{pid}",
        "app_url": app_url(account_id, f"/projects/{pid}"),
        "dock": [],
        "bookmarked": True,
        "sample": True,
        "all_access": True,
        "starts_on": date_iso(created - timedelta(days=20)),
        "ends_on": date_iso(created + timedelta(weeks=8)),
        "client_company": {"id": 2, "name": "Client Co."},
        "clientside": {"url": app_url(account_id, f"/projects/{pid}/client")},
    }


def bucket(project: Json) -> Json:
    return {"id": project["id"], "name": project["name"], "type": "Project"}


def parent_ref(item: Json, fallback_type: str = "Recording") -> Json:
    return {"id": item["id"], "title": item.get("title") or item.get("name") or item.get("subject", ""), "type": item.get("type", fallback_type), "url": item.get("url")}


def recording(
    account_id: str,
    rid: int,
    rtype: str,
    title: str,
    project: Json,
    parent: Json,
    creator: Json,
    created: datetime,
    *,
    content: str = "",
    status: str = "active",
    visible_to_clients: bool = False,
    segment: str = "recordings",
    position: int = 0,
    extra: Json | None = None,
) -> Json:
    payload: Json = {
        "id": rid,
        "status": status,
        "visible_to_clients": visible_to_clients,
        "created_at": iso(created),
        "updated_at": iso(created),
        "title": title,
        "inherits_status": True,
        "type": rtype,
        "url": resource_url(account_id, segment, rid),
        "app_url": app_url(account_id, f"/buckets/{project['id']}/{segment}/{rid}"),
        "bookmark_url": f"/{account_id}/my/bookmarks/{rid}",
        "content": content,
        "comments_count": 0,
        "comments_url": f"/{account_id}/recordings/{rid}/comments.json",
        "subscription_url": f"/{account_id}/recordings/{rid}/subscription.json",
        "boosts_count": 0,
        "boosts_url": f"/{account_id}/recordings/{rid}/boosts.json",
        "position": position,
        "parent": parent,
        "bucket": bucket(project),
        "creator": creator,
    }
    if extra:
        payload.update(extra)
    return payload


def seed(store: Store, account_id: str = DEFAULT_ACCOUNT_ID, force: bool = False) -> None:
    store.init_schema()
    if store.get_meta(f"seeded:{account_id}") == "v3" and not force:
        return
    con = store.connect()
    with store.tx():
        for table in ["resources", "events", "boosts", "comments", "subscriptions", "readings", "idempotency", "counters", "tokens"]:
            con.execute(f"DELETE FROM {table} WHERE account_id=?", (account_id,))
        con.execute("DELETE FROM accounts WHERE id=?", (account_id,))

        store.put_account(account_id, account_payload(account_id))
        people = [
            person(100, "Stephen Walker", "Owner", "stephen@example.invalid", sample=False, admin=True, owner=True),
            person(101, "Maya Chen", "Project lead", "maya@example.invalid"),
            person(102, "Sam Whitaker", "Writer", "sam@example.invalid"),
            person(103, "Omar Haddad", "Designer", "omar@example.invalid"),
            person(104, "Priya Nair", "Developer", "priya@example.invalid"),
            person(105, "Lena Kowalski", "Marketing", "lena@example.invalid"),
            person(106, "Diego Ramos", "Community", "diego@example.invalid"),
            person(107, "Grace Okafor", "QA", "grace@example.invalid"),
            person(108, "Felix Berg", "Ops", "felix@example.invalid"),
            person(109, "Client Viewer", "Client", "client@example.invalid", sample=False, client=True),
        ]
        for idx, p in enumerate(people):
            store.put_resource(account_id, "Person", p["id"], p, status="active", title=p["name"], position=idx)

        store.put_token(os.getenv("BASECAMP5_OWNER_TOKEN", "dev-owner-token"), account_id, 100, "owner")
        store.put_token(os.getenv("BASECAMP5_CLIENT_TOKEN", "dev-client-token"), account_id, 109, "client")
        extra_tokens = os.getenv("BASECAMP5_EXTRA_TOKENS", "")
        for token_spec in [x for x in extra_tokens.split(",") if x.strip()]:
            parts = token_spec.split(":")
            if len(parts) >= 2:
                store.put_token(parts[0], account_id, int(parts[1]), parts[2] if len(parts) > 2 else "member")

        created = DEFAULT_BASE_TIME - timedelta(days=20)
        project = project_payload(
            account_id,
            12345,
            "Launch the new website",
            "👋 This is a sample project that shows how a team works together here. Poke around, click into things — and delete this project whenever you're ready.",
            created,
        )
        store.put_resource(account_id, "Project", project["id"], project, status="active", title=project["name"])

        maya = store.get_resource(account_id, "Person", 101)
        sam = store.get_resource(account_id, "Person", 102)
        omar = store.get_resource(account_id, "Person", 103)
        priya = store.get_resource(account_id, "Person", 104)
        lena = store.get_resource(account_id, "Person", 105)
        diego = store.get_resource(account_id, "Person", 106)
        grace = store.get_resource(account_id, "Person", 107)
        felix = store.get_resource(account_id, "Person", 108)
        cast = [maya, sam, omar, priya, lena, diego, grace, felix]

        tool_specs = [
            (201, "Message::Board", "Message Board", "Message Board"),
            (202, "Todoset", "To-dos", "To-dos"),
            (203, "Vault", "Docs & Files", "Docs & Files"),
            (204, "Chat::Transcript", "Chat", "Campfire"),
            (205, "Schedule", "Schedule", "Schedule"),
            (206, "Kanban::Board", "Card Table", "Card Table"),
            (207, "Questionnaire", "Automatic Check-ins", "Automatic Check-ins"),
            (208, "Inbox", "Email Forwards", "Email Forwards"),
        ]
        dock: list[Json] = []
        for pos, (tid, ttype, title, kind) in enumerate(tool_specs, start=1):
            item = recording(
                account_id,
                tid,
                ttype,
                title,
                project,
                {"id": project["id"], "title": project["name"], "type": "Project", "url": project["url"]},
                maya,
                DEFAULT_BASE_TIME - timedelta(days=19),
                segment="dock/tools",
                position=pos,
                visible_to_clients=pos <= 6,
                extra={"enabled": pos <= 6, "name": title, "dock_item_id": tid, "bucket_id": project["id"]},
            )
            store.put_resource(account_id, kind, tid, item, parent_kind="Project", parent_id=project["id"], bucket_id=project["id"], title=title, position=pos)
            dock.append({"id": tid, "title": title, "name": title, "enabled": pos <= 6, "position": pos, "type": ttype, "url": item["url"], "app_url": item["app_url"]})
        project["dock"] = dock
        store.put_resource(account_id, "Project", project["id"], project, status="active", title=project["name"])

        categories = [
            (501, "📣", "Announcement"),
            (502, "✨", "FYI"),
            (503, "❤️", "Heartbeat"),
            (504, "💡", "Pitch"),
            (505, "👋", "Question"),
        ]
        for pos, (cid, icon, name) in enumerate(categories, start=1):
            cat = {"id": cid, "name": name, "icon": icon, "position": pos, "url": f"/{account_id}/categories/{cid}"}
            store.put_resource(account_id, "MessageType", cid, cat, title=name, position=pos)

        board = store.get_resource(account_id, "Message Board", 201)
        board_parent = parent_ref(board)
        messages = [
            (3001, "Kickoff: the plan", maya, 501, DEFAULT_BASE_TIME - timedelta(days=4, hours=3), True, "Here is the launch plan. @Sam owns copy, @Omar owns design, @Priya owns DNS, and @Lena owns launch comms."),
            (3002, "Pitch: trim the homepage copy", sam, 504, DEFAULT_BASE_TIME - timedelta(days=4, hours=1), False, "The homepage is close. I think we can make the lead sharper and remove one section without losing the story."),
            (3003, "Nice note from a beta tester", diego, 502, DEFAULT_BASE_TIME - timedelta(days=4), False, "A beta tester wrote: “The new direction finally explains what the product does.”"),
            (3004, "Traffic this week", lena, 503, DEFAULT_BASE_TIME - timedelta(days=3, hours=22), False, "Early traffic is steady. The chart upload is in Docs & Files."),
            (3005, "Local press opportunity", maya, 503, DEFAULT_BASE_TIME - timedelta(days=3, hours=20), False, "The local business journal can run a short note if we send copy by Friday."),
        ]
        for pos, (mid, title, creator, cat_id, at, pinned, content) in enumerate(messages, start=1):
            cat = store.get_resource(account_id, "MessageType", cat_id)
            msg = recording(
                account_id,
                mid,
                "Message",
                title,
                project,
                board_parent,
                creator,
                at,
                content=content,
                segment="messages",
                position=pos,
                visible_to_clients=mid in {3001, 3003},
                extra={"subject": title, "category": cat, "pinned": pinned},
            )
            store.put_resource(account_id, "Message", mid, msg, parent_kind="Message Board", parent_id=201, bucket_id=project["id"], title=title, position=pos)
            store.write_event(account_id, mid, "created", creator["id"], {"type": "Message"})
        for pid in [102, 103, 104, 105, 106, 107, 108]:
            store.create_boost(account_id, 3001, pid, "👏")
        for content, creator_id in [
            ("I will tighten the first pass today.", 102),
            ("Design can support this structure.", 103),
            ("DNS checklist is linked from the to-dos.", 104),
            ("I will prep launch copy from this.", 105),
        ]:
            store.put_comment(account_id, 3001, creator_id, content)
        for content, creator_id in [
            ("Agree. The second proof point is doing too much.", 101),
            ("I cut 120 words and moved the customer quote up.", 102),
            ("The shorter version works better with the hero image.", 103),
            ("Can we keep one analytics mention?", 105),
            ("Yes, one line in the footer block.", 102),
            ("Ship the trim.", 101),
            ("Queued for review.", 107),
        ]:
            store.put_comment(account_id, 3002, creator_id, content)
        store.put_comment(account_id, 3005, 105, "I will draft the note.")

        todoset = store.get_resource(account_id, "To-dos", 202)
        todoset_parent = parent_ref(todoset)
        todolists = [
            (4001, "Pre-launch checklist", "", 1),
            (4002, "Launch week: content", "Everything that has to be ready before launch week.", 2),
        ]
        for lid, title, desc, pos in todolists:
            tl = recording(
                account_id,
                lid,
                "Todolist",
                title,
                project,
                todoset_parent,
                maya,
                DEFAULT_BASE_TIME - timedelta(days=4, hours=2),
                content=desc,
                segment="todolists",
                position=pos,
                extra={"description": desc, "completed": False, "todos_url": f"/{account_id}/todolists/{lid}/todos.json"},
            )
            store.put_resource(account_id, "Todolist", lid, tl, parent_kind="To-dos", parent_id=202, bucket_id=project["id"], title=title, position=pos)
        todo_specs = [
            (4101, 4001, "Set up analytics", [105], DEFAULT_BASE_TIME + timedelta(days=2), False, 1),
            (4102, 4001, "Point DNS at the new host", [104], DEFAULT_BASE_TIME + timedelta(days=4), False, 2),
            (4103, 4001, "Choose final logo", [103], DEFAULT_BASE_TIME - timedelta(days=1), True, 3),
            (4104, 4001, "Confirm launch checklist owner", [101], DEFAULT_BASE_TIME - timedelta(days=1), True, 4),
            (4201, 4002, "Email newsletter", [102, 105], DEFAULT_BASE_TIME + timedelta(days=3), False, 1),
            (4202, 4002, "Social launch posts", [105], DEFAULT_BASE_TIME + timedelta(days=5), False, 2),
            (4203, 4002, "Customer quote approvals", [106], DEFAULT_BASE_TIME + timedelta(days=6), False, 3),
            (4204, 4002, "Write release note", [102], DEFAULT_BASE_TIME + timedelta(days=7), False, 4),
            (4205, 4002, "QA final copy links", [107], DEFAULT_BASE_TIME + timedelta(days=7), False, 5),
            (4206, 4002, "Draft first announcement", [102], DEFAULT_BASE_TIME - timedelta(days=1), True, 6),
        ]
        for tid, list_id, title, assignee_ids, due, completed, pos in todo_specs:
            tl = store.get_resource(account_id, "Todolist", list_id)
            assignees = [store.get_resource(account_id, "Person", aid) for aid in assignee_ids]
            todo = recording(
                account_id,
                tid,
                "Todo",
                title,
                project,
                parent_ref(tl),
                maya,
                DEFAULT_BASE_TIME - timedelta(days=4),
                content="",
                segment="todos",
                position=pos,
                extra={
                    "description": "",
                    "completed": completed,
                    "completed_at": iso(DEFAULT_BASE_TIME - timedelta(days=1)) if completed else None,
                    "starts_on": None,
                    "due_on": date_iso(due),
                    "assignees": assignees,
                    "completion_subscribers": [maya] if tid == 4201 else [],
                    "completion_url": f"/{account_id}/todos/{tid}/completion.json",
                },
            )
            store.put_resource(account_id, "Todo", tid, todo, parent_kind="Todolist", parent_id=list_id, bucket_id=project["id"], title=title, position=pos, status="active")
        steps = [
            (4301, 4101, "Add production property", [105], False, 1),
            (4302, 4102, "Lower TTL before cutover", [104], False, 1),
        ]
        for sid, todo_id, title, assignee_ids, completed, pos in steps:
            todo = store.get_resource(account_id, "Todo", todo_id)
            step = recording(
                account_id,
                sid,
                "CardStep",
                title,
                project,
                parent_ref(todo),
                maya,
                DEFAULT_BASE_TIME - timedelta(days=3),
                segment="card_tables/steps",
                position=pos,
                extra={"completed": completed, "assignees": [store.get_resource(account_id, "Person", aid) for aid in assignee_ids]},
            )
            store.put_resource(account_id, "Step", sid, step, parent_kind="Todo", parent_id=todo_id, bucket_id=project["id"], title=title, position=pos)
        store.put_comment(account_id, 4102, 104, "I will do this once the content freeze lands.")

        card_table = store.get_resource(account_id, "Card Table", 206)
        columns = [
            (5001, "Page ideas", "#F8E6A0", False, 1, "triage"),
            (5002, "Writing", "#D8E8FF", True, 2, "column"),
            (5003, "Design", "#DDF6D2", False, 3, "column"),
            (5004, "Review", "#F7D6E0", False, 4, "column"),
            (5005, "Ready", "#E9E2FF", False, 5, "column"),
            (5006, "Done", "#D7D7D7", False, 6, "done"),
            (5007, "Not now", "#ECECEC", False, 7, "not_now"),
        ]
        for cid, title, color, on_hold, pos, lane_type in columns:
            col = recording(
                account_id,
                cid,
                "Kanban::Column",
                title,
                project,
                parent_ref(card_table),
                maya,
                DEFAULT_BASE_TIME - timedelta(days=20),
                segment="card_tables/columns",
                position=pos,
                extra={"name": title, "color": color, "on_hold": on_hold, "lane_type": lane_type, "watchers": [maya, omar, grace] if cid == 5001 else []},
            )
            store.put_resource(account_id, "CardColumn", cid, col, parent_kind="Card Table", parent_id=206, bucket_id=project["id"], title=title, position=pos)
        card_specs = [
            (5101, 5001, "Hero alternate for freelancers", [103], False, 1, 2),
            (5102, 5001, "Pricing FAQ block", [102], False, 2, 0),
            (5103, 5002, "Write launch announcement", [102], False, 1, 0),
            (5104, 5002, "Long-form customer story", [102, 106], True, 2, 4),
            (5105, 5004, "Review SEO metadata", [104, 105], False, 1, 0),
            (5106, 5005, "Finalize about page", [102], False, 1, 0),
            (5107, 5006, "Pick CMS template", [103], False, 1, 0),
            (5108, 5006, "Set redirect map", [104], False, 2, 0),
            (5109, 5006, "Compress images", [103], False, 3, 0),
            (5110, 5006, "Draft privacy note", [102], False, 4, 0),
            (5111, 5006, "Add status page link", [104], False, 5, 0),
            (5112, 5007, "Podcast landing page", [105], False, 1, 0),
            (5113, 5007, "Customer map", [106], False, 2, 0),
        ]
        for card_id, col_id, title, assignee_ids, on_hold, pos, step_count in card_specs:
            col = store.get_resource(account_id, "CardColumn", col_id)
            card = recording(
                account_id,
                card_id,
                "Kanban::Card",
                title,
                project,
                parent_ref(col),
                maya,
                DEFAULT_BASE_TIME - timedelta(days=20),
                content="",
                segment="card_tables/cards",
                position=pos,
                extra={
                    "assignees": [store.get_resource(account_id, "Person", aid) for aid in assignee_ids],
                    "column_id": col_id,
                    "on_hold": on_hold,
                    "due_on": date_iso(DEFAULT_BASE_TIME + timedelta(days=10)) if col_id in {5004, 5005} else None,
                    "watchers": [maya, grace] if col_id == 5001 else [],
                },
            )
            store.put_resource(account_id, "Card", card_id, card, parent_kind="CardColumn", parent_id=col_id, bucket_id=project["id"], title=title, position=pos)
            for i in range(step_count):
                sid = 5200 + (card_id - 5100) * 10 + i
                step = recording(
                    account_id,
                    sid,
                    "CardStep",
                    f"Step {i + 1} for {title}",
                    project,
                    parent_ref(card),
                    maya,
                    DEFAULT_BASE_TIME - timedelta(days=19),
                    segment="card_tables/steps",
                    position=i + 1,
                    extra={"completed": i == 0, "assignees": []},
                )
                store.put_resource(account_id, "Step", sid, step, parent_kind="Card", parent_id=card_id, bucket_id=project["id"], title=step["title"], position=i + 1)
        store.put_comment(account_id, 5104, 102, "On hold until we get permission to name the customer.")

        vault = store.get_resource(account_id, "Docs & Files", 203)
        doc = recording(
            account_id,
            6001,
            "Document",
            "Homepage copy — draft",
            project,
            parent_ref(vault),
            sam,
            DEFAULT_BASE_TIME - timedelta(days=3),
            content="<h1>Homepage copy</h1><p>Short, plain-language draft for launch.</p>",
            segment="documents",
            position=1,
            visible_to_clients=True,
            extra={"description": "Working draft", "color": "#F8E6A0"},
        )
        store.put_resource(account_id, "Document", 6001, doc, parent_kind="Docs & Files", parent_id=203, bucket_id=project["id"], title=doc["title"], position=1)
        upload = recording(
            account_id,
            6002,
            "Upload",
            "logo-concepts.png",
            project,
            parent_ref(vault),
            omar,
            DEFAULT_BASE_TIME - timedelta(days=3, hours=1),
            segment="uploads",
            position=2,
            extra={"byte_size": 482133, "content_type": "image/png", "download_url": f"/{account_id}/uploads/6002/download"},
        )
        store.put_resource(account_id, "Upload", 6002, upload, parent_kind="Docs & Files", parent_id=203, bucket_id=project["id"], title=upload["title"], position=2)
        cloud = recording(
            account_id,
            6003,
            "CloudFile",
            "Content calendar",
            project,
            parent_ref(vault),
            lena,
            DEFAULT_BASE_TIME - timedelta(days=3, hours=2),
            segment="uploads",
            position=3,
            extra={"service": "google-sheets", "external_url": "https://example.invalid/content-calendar"},
        )
        store.put_resource(account_id, "Upload", 6003, cloud, parent_kind="Docs & Files", parent_id=203, bucket_id=project["id"], title=cloud["title"], position=3)
        store.put_comment(account_id, 6001, 101, "This reads like us.")

        campfire = store.get_resource(account_id, "Campfire", 204)
        chat_lines = [
            (6501, maya, "Morning! Launch board is ready.", "🙌"),
            (6502, sam, "I tightened the home page draft and left the old intro in the doc history.", None),
            (6503, sam, "The new version puts the customer quote up top so it lands faster.", None),
            (6504, omar, "Logo concepts are uploaded: https://example.invalid/logo", "✨"),
            (6505, priya, "Heads up, deploying now.", None),
            (6506, grace, "I will watch smoke checks.", "👍"),
            (6507, diego, "Customer quote just came in: “This finally feels simple.”", None),
            (6508, felix, "Awww! 💖", "💖"),
        ]
        for pos, (line_id, creator, content, boost_content) in enumerate(chat_lines, start=1):
            line = recording(
                account_id,
                line_id,
                "Chat::Line",
                content[:80],
                project,
                parent_ref(campfire),
                creator,
                DEFAULT_BASE_TIME - timedelta(days=4) + timedelta(minutes=pos * 7),
                content=content,
                segment="chats/204/lines",
                position=pos,
                extra={"line": content, "attachments": []},
            )
            store.put_resource(account_id, "CampfireLine", line_id, line, parent_kind="Campfire", parent_id=204, bucket_id=project["id"], title=line["title"], position=pos)
            if boost_content:
                store.create_boost(account_id, line_id, 100, boost_content)

        schedule = store.get_resource(account_id, "Schedule", 205)
        entries = [
            (6701, "Launch day 🚀", DEFAULT_BASE_TIME + timedelta(weeks=7), None, True, [101, 102, 103, 104, 105]),
            (6702, "Content review call", DEFAULT_BASE_TIME + timedelta(weeks=2, hours=10), DEFAULT_BASE_TIME + timedelta(weeks=2, hours=11), False, [101, 102, 105]),
        ]
        for pos, (eid, title, starts, ends, all_day, participants) in enumerate(entries, start=1):
            entry = recording(
                account_id,
                eid,
                "Schedule::Entry",
                title,
                project,
                parent_ref(schedule),
                maya,
                DEFAULT_BASE_TIME - timedelta(days=3),
                segment="schedule_entries",
                position=pos,
                extra={
                    "all_day": all_day,
                    "starts_at": None if all_day else iso(starts),
                    "ends_at": None if all_day or not ends else iso(ends),
                    "starts_on": date_iso(starts),
                    "participants": [store.get_resource(account_id, "Person", pid) for pid in participants],
                },
            )
            store.put_resource(account_id, "ScheduleEntry", eid, entry, parent_kind="Schedule", parent_id=205, bucket_id=project["id"], title=title, position=pos)

        store.write_event(account_id, project["id"], "sample_seeded", 100, {"project_id": project["id"]})
        store.set_meta(f"seeded:{account_id}", "v3")


@dataclasses.dataclass
class Actor:
    account_id: str
    person_id: int
    role: str
    person: Json

    @property
    def is_owner(self) -> bool:
        return self.role in {"owner", "admin"} or bool(self.person.get("owner") or self.person.get("admin"))

    @property
    def is_client(self) -> bool:
        return self.role == "client" or bool(self.person.get("client"))


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.lock = threading.Lock()
        self.hits: dict[str, list[float]] = {}

    def check(self, key: str) -> None:
        if self.limit <= 0:
            return
        now = time.time()
        cutoff = now - self.window_seconds
        with self.lock:
            hits = [ts for ts in self.hits.get(key, []) if ts >= cutoff]
            if len(hits) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - hits[0])))
                raise ApiError(
                    429,
                    "rate_limit",
                    "Rate limit exceeded",
                    retryable=True,
                    headers={"Retry-After": str(retry_after)},
                )
            hits.append(now)
            self.hits[key] = hits


class App:
    def __init__(self, db_path: str) -> None:
        self.openapi = load_openapi()
        self.behavior = load_behavior()
        self.routes = build_routes(self.openapi, self.behavior)
        self.store = Store(db_path)
        self.store.init_schema()
        seed(self.store, DEFAULT_ACCOUNT_ID)
        self.rate_limiter = RateLimiter(
            int(os.getenv("BASECAMP5_RATE_LIMIT", "600")),
            int(os.getenv("BASECAMP5_RATE_WINDOW", "60")),
        )

    def authenticate(self, headers: dict[str, str], account_id: str | None) -> Actor:
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise ApiError(401, "auth_required", "Authentication required", hint="Send Authorization: Bearer <token>")
        token = auth.split(None, 1)[1].strip()
        row = self.store.get_token(token)
        if not row:
            raise ApiError(401, "auth_required", "Invalid or expired access token")
        if row["expires_at"] and parse_iso(row["expires_at"]) <= utcnow():
            raise ApiError(401, "auth_required", "Access token expired")
        if account_id is not None and str(row["account_id"]) != str(account_id):
            raise ApiError(403, "forbidden", "Token does not grant access to this account")
        person_data = self.store.get_resource(row["account_id"], "Person", row["person_id"])
        if not person_data:
            raise ApiError(401, "auth_required", "Token subject is not active")
        self.rate_limiter.check(sha256_text(token))
        return Actor(str(row["account_id"]), int(row["person_id"]), row["role"], person_data)

    def match(self, method: str, path: str) -> tuple[Route | None, Json, list[str]]:
        allowed: list[str] = []
        for route in self.routes:
            match = route.regex.match(path)
            if match:
                if route.method == method:
                    return route, match.groupdict(), allowed
                allowed.append(route.method)
        return None, {}, sorted(set(allowed))

    def handle(
        self,
        method: str,
        raw_path: str,
        headers: dict[str, str],
        body_raw: bytes,
        request_id: str,
    ) -> tuple[int, dict[str, str], Any]:
        parsed = urllib.parse.urlparse(raw_path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        query_one = {k: v[-1] for k, v in query.items() if v}

        if method == "GET" and path in {"/health", "/healthz", "/up"}:
            return 200, {}, {
                "ok": True,
                "service": SERVER_NAME,
                "time": iso(),
                "routes": len(self.routes),
                "account_id": DEFAULT_ACCOUNT_ID,
            }
        if method == "GET" and path == "/openapi.json":
            return 200, {}, self.openapi
        if method == "POST" and path == "/__admin/reset":
            actor = self.authenticate(headers, DEFAULT_ACCOUNT_ID)
            reset_token = os.getenv("BASECAMP5_RESET_TOKEN")
            if reset_token and not hmac.compare_digest(headers.get("x-reset-token", ""), reset_token):
                raise ApiError(403, "forbidden", "Reset token is required")
            if not actor.is_owner:
                raise ApiError(403, "forbidden", "Only owners can reset this server")
            seed(self.store, DEFAULT_ACCOUNT_ID, force=True)
            return 200, {}, {"ok": True, "reset": True, "account_id": DEFAULT_ACCOUNT_ID}

        route, params, allowed = self.match(method, path)
        if route is None:
            if allowed:
                raise ApiError(405, "usage", "Method not allowed", headers={"Allow": ", ".join(allowed)})
            raise ApiError(404, "not_found", "Endpoint not found")
        account_id = params.get("accountId")
        actor = self.authenticate(headers, account_id)
        if account_id != actor.account_id:
            raise ApiError(403, "forbidden", "Account mismatch")

        body: Any = None
        if body_raw:
            content_type = headers.get("content-type", "")
            if "application/json" not in content_type and "application/vnd.api+json" not in content_type:
                raise ApiError(415, "validation", "Only application/json request bodies are supported")
            if len(body_raw) > MAX_REQUEST_BYTES:
                raise ApiError(413, "validation", "Request body is too large")
            body = json_loads(body_raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ApiError(422, "validation", "Request body must be a JSON object")
        elif route.request_required:
            raise ApiError(422, "validation", "Request body is required")
        else:
            body = {}
        self.validate_required(route, body)

        unsafe = method in {"POST", "PUT", "DELETE", "PATCH"}
        idempotency_key = headers.get("idempotency-key")
        if unsafe and idempotency_key:
            cached = self.get_cached_idempotent(actor, idempotency_key, method, path, body)
            if cached:
                return cached

        with self.store.tx():
            status, extra_headers, payload = self.dispatch(route, params, query_one, body, actor)
            if unsafe and idempotency_key:
                self.cache_idempotent(actor, idempotency_key, method, path, body, status, extra_headers, payload)
        return status, extra_headers, payload

    def validate_required(self, route: Route, body: Json) -> None:
        if not route.request_required:
            return
        # The upstream schemas are intentionally sparse for many operations. Keep
        # validation strict for fields the contract explicitly marks required and
        # add operation-level checks in handlers for domain-essential fields.
        missing = [name for name in route.required_fields if name not in body or body[name] in (None, "")]
        if missing:
            raise ApiError(422, "validation", "Missing required fields", details={"fields": missing})

    def get_cached_idempotent(self, actor: Actor, key: str, method: str, path: str, body: Json) -> tuple[int, dict[str, str], Any] | None:
        row = self.store.connect().execute(
            "SELECT * FROM idempotency WHERE account_id=? AND actor_id=? AND key=?",
            (actor.account_id, actor.person_id, key),
        ).fetchone()
        if not row:
            return None
        body_hash = sha256_text(canonical_json(body))
        if row["method"] != method or row["path"] != path or row["body_hash"] != body_hash:
            raise ApiError(409, "validation", "Idempotency-Key was reused for a different request")
        headers = json.loads(row["headers"])
        headers["Idempotency-Replayed"] = "true"
        return int(row["status"]), headers, json.loads(row["body"])

    def cache_idempotent(self, actor: Actor, key: str, method: str, path: str, body: Json, status: int, headers: dict[str, str], payload: Any) -> None:
        self.store.connect().execute(
            """
            INSERT OR REPLACE INTO idempotency(account_id,actor_id,key,method,path,body_hash,status,headers,body,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                actor.account_id,
                actor.person_id,
                key,
                method,
                path,
                sha256_text(canonical_json(body)),
                status,
                canonical_json(headers),
                canonical_json(payload),
                iso(),
            ),
        )

    def dispatch(self, route: Route, params: Json, query: dict[str, str], body: Json, actor: Actor) -> tuple[int, dict[str, str], Any]:
        op = route.operation_id
        account_id = actor.account_id

        if op == "GetAccount":
            return 200, {}, self.store.get_account(account_id)
        if op == "UpdateAccountName":
            self.require_owner(actor)
            acct = self.store.get_account(account_id) or account_payload(account_id)
            acct["name"] = require_text(body, "name")
            acct["updated_at"] = iso()
            self.store.put_account(account_id, acct)
            return 200, {}, acct
        if op == "UpdateAccountLogo":
            self.require_owner(actor)
            acct = self.store.get_account(account_id) or account_payload(account_id)
            acct["logo"] = clean_patch(body)
            acct["updated_at"] = iso()
            self.store.put_account(account_id, acct)
            return 200, {}, acct.get("logo")
        if op == "RemoveAccountLogo":
            self.require_owner(actor)
            acct = self.store.get_account(account_id) or account_payload(account_id)
            acct.pop("logo", None)
            acct["updated_at"] = iso()
            self.store.put_account(account_id, acct)
            return 204, {}, None

        if op in {"GetMyProfile", "GetPerson"}:
            pid = int(params.get("personId") or actor.person_id)
            return 200, {}, self.get_visible_person(actor, pid)
        if op == "UpdateMyProfile":
            patch = {k: body[k] for k in ["name", "title", "bio", "location", "time_zone"] if k in body}
            updated = self.store.update_resource(account_id, "Person", actor.person_id, patch)
            return 200, {}, updated
        if op == "ListPeople":
            people = self.store.list_resources(account_id, "Person")
            return self.list_response(route, params, query, people)
        if op == "ListProjectPeople":
            self.get_project(actor, params["projectId"])
            return self.list_response(route, params, query, self.store.list_resources(account_id, "Person"))
        if op == "ListPingablePeople":
            people = [p for p in self.store.list_resources(account_id, "Person") if p.get("can_ping")]
            return self.list_response(route, params, query, people)
        if op == "GetMyPreferences":
            return 200, {}, actor.person.get("preferences") or {"time_zone": actor.person.get("time_zone", "UTC"), "appearance": "system", "first_week_day": "monday", "time_format": "12h"}
        if op == "UpdateMyPreferences":
            person_data = self.store.get_resource(account_id, "Person", actor.person_id)
            person_data["preferences"] = {**person_data.get("preferences", {}), **clean_patch(body)}
            updated = self.store.update_resource(account_id, "Person", actor.person_id, person_data)
            return 200, {}, updated["preferences"]
        if op in {"GetOutOfOffice", "EnableOutOfOffice", "DisableOutOfOffice"}:
            pid = int(params["personId"])
            target = self.get_visible_person(actor, pid)
            if op == "GetOutOfOffice":
                return 200, {}, target.get("out_of_office") or {"enabled": False}
            self.require_self_or_owner(actor, pid)
            if op == "EnableOutOfOffice":
                target["out_of_office"] = {"enabled": True, **clean_patch(body), "updated_at": iso()}
            else:
                target["out_of_office"] = {"enabled": False, "updated_at": iso()}
            return 200, {}, self.store.update_resource(account_id, "Person", pid, target).get("out_of_office")

        if op == "ListProjects":
            status = query.get("status") or "active"
            projects = self.store.list_resources(account_id, "Project", status=status if status != "all" else None, include_trashed=status == "all")
            projects = [p for p in projects if self.can_see_project(actor, p)]
            return self.list_response(route, params, query, projects)
        if op == "CreateProject":
            self.require_employee(actor)
            name = require_text(body, "name")
            pid = self.store.next_id(account_id, "project", 20000)
            project = project_payload(account_id, pid, name, str(body.get("description") or ""), utcnow())
            project["purpose"] = str(body.get("purpose") or "")
            project["sample"] = False
            project["dock"] = []
            self.store.put_resource(account_id, "Project", pid, project, status="active", title=name)
            self.store.write_event(account_id, pid, "created", actor.person_id, {"type": "Project"})
            return 201, {"Location": project["url"]}, project
        if op in {"GetProject", "UpdateProject", "TrashProject"}:
            project = self.get_project(actor, params["projectId"])
            if op == "GetProject":
                return 200, {}, project
            self.require_project_editor(actor, project)
            if op == "TrashProject":
                project["status"] = "trashed"
                project["updated_at"] = iso()
                self.store.put_resource(account_id, "Project", project["id"], project, status="trashed", title=project["name"])
                self.store.write_event(account_id, project["id"], "trashed", actor.person_id, {"type": "Project"})
                return 204, {}, None
            patch = {k: body[k] for k in ["name", "description", "purpose", "clients_enabled", "bookmarked", "starts_on", "ends_on"] if k in body}
            project.update(patch)
            project["updated_at"] = iso()
            self.store.put_resource(account_id, "Project", project["id"], project, status=project.get("status", "active"), title=project["name"])
            self.store.write_event(account_id, project["id"], "updated", actor.person_id, {"fields": sorted(patch)})
            return 200, {}, project
        if op == "UpdateProjectAccess":
            self.require_owner(actor)
            project = self.get_project(actor, params["projectId"])
            project["access"] = clean_patch(body)
            project["updated_at"] = iso()
            self.store.put_resource(account_id, "Project", project["id"], project, title=project["name"])
            return 200, {}, project["access"]

        if op in {"GetTool", "UpdateTool", "DeleteTool", "CloneTool", "EnableTool", "DisableTool", "RepositionTool"}:
            return self.handle_tools(op, params, body, actor)

        if op in {"ListMessageTypes", "CreateMessageType", "GetMessageType", "UpdateMessageType", "DeleteMessageType"}:
            return self.handle_message_types(op, route, params, query, body, actor)

        if op in {"GetMessageBoard", "ListMessages", "CreateMessage", "GetMessage", "UpdateMessage", "PinMessage", "UnpinMessage"}:
            return self.handle_messages(op, route, params, query, body, actor)

        if op in {
            "GetTodoset", "ListTodolists", "CreateTodolist", "GetTodolistOrGroup", "UpdateTodolistOrGroup",
            "ListTodolistGroups", "CreateTodolistGroup", "RepositionTodolistGroup", "ListTodos", "CreateTodo",
            "GetTodo", "UpdateTodo", "TrashTodo", "CompleteTodo", "UncompleteTodo", "RepositionTodo",
            "GetHillChart", "UpdateHillChartSettings",
        }:
            return self.handle_todos(op, route, params, query, body, actor)

        if op in {
            "GetCardTable", "CreateCardColumn", "GetCardColumn", "UpdateCardColumn", "SetCardColumnColor",
            "EnableCardColumnOnHold", "DisableCardColumnOnHold", "MoveCardColumn", "ListCards", "CreateCard",
            "GetCard", "UpdateCard", "MoveCard", "CreateCardStep", "GetCardStep", "UpdateCardStep",
            "SetCardStepCompletion", "RepositionCardStep", "SubscribeToCardColumn", "UnsubscribeFromCardColumn",
        }:
            return self.handle_cards(op, route, params, query, body, actor)

        if op in {
            "GetVault", "UpdateVault", "ListDocuments", "CreateDocument", "GetDocument", "UpdateDocument",
            "ListUploads", "CreateUpload", "GetUpload", "UpdateUpload", "ListUploadVersions",
            "ListVaults", "CreateVault", "CreateAttachment",
        }:
            return self.handle_files(op, route, params, query, body, actor)

        if op in {
            "ListCampfires", "GetCampfire", "ListCampfireLines", "CreateCampfireLine", "GetCampfireLine",
            "DeleteCampfireLine", "ListCampfireUploads", "CreateCampfireUpload", "ListChatbots", "CreateChatbot",
            "GetChatbot", "UpdateChatbot", "DeleteChatbot",
        }:
            return self.handle_campfire(op, route, params, query, body, actor)

        if op in {
            "GetSchedule", "UpdateScheduleSettings", "ListScheduleEntries", "CreateScheduleEntry",
            "GetScheduleEntry", "UpdateScheduleEntry", "GetScheduleEntryOccurrence", "GetUpcomingSchedule",
            "GetProjectTimesheet", "GetRecordingTimesheet", "CreateTimesheetEntry", "GetTimesheetEntry",
            "UpdateTimesheetEntry", "GetTimesheetReport",
        }:
            return self.handle_schedule(op, route, params, query, body, actor)

        if op in {
            "GetRecording", "ListRecordings", "ArchiveRecording", "UnarchiveRecording", "TrashRecording",
            "SetClientVisibility", "ListComments", "CreateComment", "GetComment", "UpdateComment",
            "ListRecordingBoosts", "CreateRecordingBoost", "ListEventBoosts", "CreateEventBoost",
            "GetBoost", "DeleteBoost", "GetSubscription", "Subscribe", "UpdateSubscription", "Unsubscribe",
            "ListEvents",
        }:
            return self.handle_recordings(op, route, params, query, body, actor)

        if op in {
            "Search", "GetSearchMetadata", "GetMyAssignments", "GetMyCompletedAssignments", "GetMyDueAssignments",
            "GetMyNotifications", "MarkAsRead", "GetProgressReport", "GetPersonProgress", "ListGauges",
            "ToggleGauge", "ListGaugeNeedles", "CreateGaugeNeedle", "GetGaugeNeedle", "UpdateGaugeNeedle",
            "DestroyGaugeNeedle", "GetOverdueTodos", "ListAssignablePeople", "GetAssignedTodos",
            "GetProjectTimeline", "GetQuestionReminders", "ListLineupMarkers", "CreateLineupMarker",
            "UpdateLineupMarker", "DeleteLineupMarker",
        }:
            return self.handle_reports_and_my(op, route, params, query, body, actor)

        if op in {
            "ListWebhooks", "CreateWebhook", "GetWebhook", "UpdateWebhook", "DeleteWebhook",
            "ListTemplates", "CreateTemplate", "GetTemplate", "UpdateTemplate", "DeleteTemplate",
            "CreateProjectFromTemplate", "GetProjectConstruction", "GetQuestionnaire", "ListQuestions",
            "CreateQuestion", "GetQuestion", "UpdateQuestion", "ListAnswers", "CreateAnswer", "GetAnswer",
            "UpdateAnswer", "ListQuestionAnswerers", "GetAnswersByPerson", "UpdateQuestionNotificationSettings",
            "PauseQuestion", "ResumeQuestion", "GetInbox", "ListForwards", "GetForward", "ListForwardReplies",
            "CreateForwardReply", "GetForwardReply", "ListClientApprovals", "GetClientApproval",
            "ListClientCorrespondences", "GetClientCorrespondence", "ListClientReplies", "GetClientReply",
        }:
            return self.handle_generic_ecosystem(op, route, params, query, body, actor)

        raise ApiError(
            501,
            "unsupported",
            f"{op} is registered from OpenAPI but does not have a production handler yet",
            hint="This is an explicit stub, not a successful no-op.",
            details={"operation_id": op, "path": route.template, "method": route.method},
        )

    def require_owner(self, actor: Actor) -> None:
        if not actor.is_owner:
            raise ApiError(403, "forbidden", "Owner or admin access required")

    def require_employee(self, actor: Actor) -> None:
        if actor.is_client or not actor.person.get("employee", True):
            raise ApiError(403, "forbidden", "Employee access required")

    def require_self_or_owner(self, actor: Actor, person_id: int) -> None:
        if actor.person_id != person_id and not actor.is_owner:
            raise ApiError(403, "forbidden", "You can only modify yourself")

    def require_project_editor(self, actor: Actor, project: Json) -> None:
        if actor.is_client:
            raise ApiError(403, "forbidden", "Clients cannot modify this project")
        if not self.can_see_project(actor, project):
            raise ApiError(403, "forbidden", "Project access required")

    def can_see_project(self, actor: Actor, project: Json) -> bool:
        if actor.is_owner:
            return True
        if actor.is_client:
            return bool(project.get("clients_enabled"))
        return bool(project.get("all_access", True) or actor.person.get("employee"))

    def can_see_recording(self, actor: Actor, recording_data: Json) -> bool:
        if actor.is_client and not recording_data.get("visible_to_clients"):
            return False
        bucket_data = recording_data.get("bucket") or {}
        bucket_id = bucket_data.get("id")
        if bucket_id:
            project = self.store.get_resource(actor.account_id, "Project", bucket_id)
            if project and not self.can_see_project(actor, project):
                return False
        return recording_data.get("status") != "trashed" or actor.is_owner

    def get_visible_person(self, actor: Actor, pid: int) -> Json:
        person_data = self.store.get_resource(actor.account_id, "Person", pid)
        if not person_data:
            raise ApiError(404, "not_found", f"Person {pid} was not found")
        if actor.is_client and not (person_data.get("client") or person_data.get("employee")):
            raise ApiError(403, "forbidden", "This person is not visible")
        return person_data

    def get_project(self, actor: Actor, project_id: int | str) -> Json:
        project = self.store.get_resource(actor.account_id, "Project", project_id)
        if not project or project.get("status") == "trashed":
            raise ApiError(404, "not_found", f"Project {project_id} was not found")
        if not self.can_see_project(actor, project):
            raise ApiError(403, "forbidden", "Project access required")
        return project

    def get_recording(self, actor: Actor, rid: int | str, kinds: list[str] | None = None) -> tuple[str, Json]:
        found = self.store.find_resource_by_id(actor.account_id, rid, kinds)
        if not found:
            raise ApiError(404, "not_found", f"Recording {rid} was not found")
        kind, data = found
        if not self.can_see_recording(actor, data):
            raise ApiError(403, "forbidden", "Recording is not visible")
        return kind, data

    def list_response(self, route: Route, params: Json, query: dict[str, str], items: list[Json]) -> tuple[int, dict[str, str], list[Json]]:
        page = max(1, maybe_int(query.get("page")) or 1)
        per_page = min(MAX_PAGE_SIZE, max(1, maybe_int(query.get("per_page")) or DEFAULT_PAGE_SIZE))
        total = len(items)
        start = (page - 1) * per_page
        end = start + per_page
        page_items = items[start:end]
        headers = {"X-Total-Count": str(total)}
        if end < total:
            next_query = dict(query)
            next_query["page"] = str(page + 1)
            next_query["per_page"] = str(per_page)
            path = route.template
            for name, value in params.items():
                path = path.replace("{" + name + "}", str(value))
            headers["Link"] = f'<{path}?{urllib.parse.urlencode(next_query)}>; rel="next"'
        return 200, headers, page_items

    def handle_tools(self, op: str, params: Json, body: Json, actor: Actor) -> tuple[int, dict[str, str], Any]:
        account_id = actor.account_id
        if op == "CloneTool":
            self.require_employee(actor)
            source_id = maybe_int(body.get("source_tool_id") or body.get("tool_id"))
            project_id = maybe_int(body.get("bucket_id") or body.get("project_id"))
            if not source_id or not project_id:
                raise ApiError(422, "validation", "source_tool_id and bucket_id are required")
            _, source = self.get_recording(actor, source_id)
            project = self.get_project(actor, project_id)
            new_id = self.store.next_id(account_id, "tool", 30000)
            cloned = dict(source)
            cloned.update({"id": new_id, "title": body.get("title") or source.get("title"), "name": body.get("title") or source.get("name"), "created_at": iso(), "updated_at": iso()})
            cloned["url"] = f"/{account_id}/dock/tools/{new_id}"
            cloned["app_url"] = app_url(account_id, f"/buckets/{project_id}/dock/tools/{new_id}")
            kind = self.kind_for_tool_type(cloned.get("type"))
            self.store.put_resource(account_id, kind, new_id, cloned, parent_kind="Project", parent_id=project_id, bucket_id=project_id, title=cloned["title"], position=len(project.get("dock", [])) + 1)
            project.setdefault("dock", []).append({"id": new_id, "title": cloned["title"], "name": cloned["title"], "enabled": True, "position": cloned.get("position", 0), "type": cloned.get("type"), "url": cloned["url"], "app_url": cloned["app_url"]})
            self.store.put_resource(account_id, "Project", project_id, project, title=project["name"])
            return 201, {"Location": cloned["url"]}, cloned
        tool_id = maybe_int(params.get("toolId"))
        if not tool_id:
            raise ApiError(422, "validation", "toolId is required")
        found = self.store.find_resource_by_id(account_id, tool_id, ["Message Board", "To-dos", "Docs & Files", "Campfire", "Schedule", "Card Table", "Questionnaire", "Inbox"])
        if not found:
            raise ApiError(404, "not_found", f"Tool {tool_id} was not found")
        kind, tool = found
        if op == "GetTool":
            return 200, {}, tool
        self.require_employee(actor)
        if op == "DeleteTool":
            tool["enabled"] = False
            tool["status"] = "trashed"
            self.store.put_resource(account_id, kind, tool_id, tool, status="trashed", title=tool["title"])
            return 204, {}, None
        if op == "DisableTool":
            tool["enabled"] = False
        elif op == "EnableTool":
            tool["enabled"] = True
        elif op == "RepositionTool":
            tool["position"] = maybe_int(body.get("position")) or maybe_int(params.get("position")) or tool.get("position", 0)
        else:
            tool.update({k: body[k] for k in ["title", "name", "position", "enabled"] if k in body})
            if "title" in body and "name" not in body:
                tool["name"] = body["title"]
        tool["updated_at"] = iso()
        self.store.put_resource(account_id, kind, tool_id, tool, parent_kind="Project", parent_id=tool.get("bucket", {}).get("id"), bucket_id=tool.get("bucket", {}).get("id"), title=tool.get("title") or tool.get("name"), position=tool.get("position", 0))
        return 200, {}, tool

    def kind_for_tool_type(self, tool_type: str | None) -> str:
        return {
            "Message::Board": "Message Board",
            "Todoset": "To-dos",
            "Vault": "Docs & Files",
            "Chat::Transcript": "Campfire",
            "Schedule": "Schedule",
            "Kanban::Board": "Card Table",
            "Questionnaire": "Questionnaire",
            "Inbox": "Inbox",
        }.get(tool_type or "", "Tool")

    def handle_message_types(self, op: str, route: Route, params: Json, query: dict[str, str], body: Json, actor: Actor) -> tuple[int, dict[str, str], Any]:
        account_id = actor.account_id
        if op == "ListMessageTypes":
            return self.list_response(route, params, query, self.store.list_resources(account_id, "MessageType"))
        if op == "CreateMessageType":
            self.require_employee(actor)
            mid = self.store.next_id(account_id, "message_type", 550)
            data = {"id": mid, "name": require_text(body, "name"), "icon": str(body.get("icon") or "📌"), "position": maybe_int(body.get("position")) or mid, "url": f"/{account_id}/categories/{mid}"}
            self.store.put_resource(account_id, "MessageType", mid, data, title=data["name"], position=data["position"])
            return 201, {"Location": data["url"]}, data
        type_id = int(params["typeId"])
        data = self.store.get_resource(account_id, "MessageType", type_id)
        if not data:
            raise ApiError(404, "not_found", f"Message type {type_id} was not found")
        if op == "GetMessageType":
            return 200, {}, data
        self.require_employee(actor)
        if op == "DeleteMessageType":
            self.store.update_resource(account_id, "MessageType", type_id, {"status": "trashed"})
            return 204, {}, None
        data.update({k: body[k] for k in ["name", "icon", "position"] if k in body})
        self.store.put_resource(account_id, "MessageType", type_id, data, title=data["name"], position=data.get("position", 0))
        return 200, {}, data

    def handle_messages(self, op: str, route: Route, params: Json, query: dict[str, str], body: Json, actor: Actor) -> tuple[int, dict[str, str], Any]:
        account_id = actor.account_id
        if op == "GetMessageBoard":
            _, board = self.get_recording(actor, params["boardId"], ["Message Board"])
            return 200, {}, board
        if op == "ListMessages":
            _, board = self.get_recording(actor, params["boardId"], ["Message Board"])
            items = self.store.list_resources(account_id, "Message", parent_kind="Message Board", parent_id=board["id"], status=query.get("status"), search=query.get("q"))
            items = [x for x in items if self.can_see_recording(actor, x)]
            reverse = query.get("direction", "desc") != "asc"
            key = query.get("sort", "created_at")
            items.sort(key=lambda x: x.get(key) or "", reverse=reverse)
            return self.list_response(route, params, query, items)
        if op == "CreateMessage":
            _, board = self.get_recording(actor, params["boardId"], ["Message Board"])
            self.require_employee(actor)
            subject = require_text(body, "subject", "title")
            mid = self.store.next_id(account_id, "message", 30000)
            category = None
            if body.get("category_id") or body.get("type_id"):
                category = self.store.get_resource(account_id, "MessageType", int(body.get("category_id") or body.get("type_id")))
            if category is None:
                category = self.store.list_resources(account_id, "MessageType")[0]
            project = self.get_project(actor, board["bucket"]["id"])
            msg = recording(
                account_id,
                mid,
                "Message",
                subject,
                project,
                parent_ref(board),
                actor.person,
                utcnow(),
                content=str(body.get("content") or ""),
                segment="messages",
                visible_to_clients=bool(body.get("visible_to_clients", False)),
                extra={"subject": subject, "category": category, "pinned": False},
            )
            self.store.put_resource(account_id, "Message", mid, msg, parent_kind="Message Board", parent_id=board["id"], bucket_id=project["id"], title=subject)
            self.store.write_event(account_id, mid, "created", actor.person_id, {"type": "Message"})
            return 201, {"Location": msg["url"]}, msg
        mid = int(params.get("messageId") or params.get("messageId"))
        _, msg = self.get_recording(actor, mid, ["Message"])
        if op == "GetMessage":
            return 200, {}, msg
        self.require_employee(actor)
        if op in {"PinMessage", "UnpinMessage"}:
            msg["pinned"] = op == "PinMessage"
        else:
            if "subject" in body:
                msg["subject"] = str(body["subject"])
                msg["title"] = str(body["subject"])
            if "title" in body:
                msg["title"] = str(body["title"])
                msg["subject"] = str(body["title"])
            if "content" in body:
                msg["content"] = str(body["content"])
            if "category_id" in body:
                cat = self.store.get_resource(account_id, "MessageType", int(body["category_id"]))
                if not cat:
                    raise ApiError(422, "validation", "category_id does not exist")
                msg["category"] = cat
        msg["updated_at"] = iso()
        self.store.put_resource(account_id, "Message", mid, msg, parent_kind="Message Board", parent_id=msg["parent"]["id"], bucket_id=msg["bucket"]["id"], title=msg["title"])
        self.store.write_event(account_id, mid, "updated", actor.person_id, {"operation": op})
        return 200, {}, msg

    def handle_todos(self, op: str, route: Route, params: Json, query: dict[str, str], body: Json, actor: Actor) -> tuple[int, dict[str, str], Any]:
        account_id = actor.account_id
        if op == "GetTodoset":
            _, data = self.get_recording(actor, params["todosetId"], ["To-dos"])
            return 200, {}, data
        if op == "ListTodolists":
            _, todoset = self.get_recording(actor, params["todosetId"], ["To-dos"])
            items = self.store.list_resources(account_id, "Todolist", parent_kind="To-dos", parent_id=todoset["id"])
            return self.list_response(route, params, query, [x for x in items if self.can_see_recording(actor, x)])
        if op == "CreateTodolist":
            _, todoset = self.get_recording(actor, params["todosetId"], ["To-dos"])
            self.require_employee(actor)
            return self.create_recording_under(actor, "Todolist", "Todolist", todoset, body, "todolist", "todolists")
        if op in {"GetTodolistOrGroup", "UpdateTodolistOrGroup"}:
            found = self.store.find_resource_by_id(account_id, params["id"], ["Todolist", "TodolistGroup"])
            if not found:
                raise ApiError(404, "not_found", f"Todolist or group {params['id']} was not found")
            kind, data = found
            if op == "GetTodolistOrGroup":
                return 200, {}, data
            self.require_employee(actor)
            data.update({k: body[k] for k in ["title", "description", "content", "visible_to_clients"] if k in body})
            data["updated_at"] = iso()
            self.store.put_resource(account_id, kind, data["id"], data, parent_id=data["parent"]["id"], bucket_id=data["bucket"]["id"], title=data["title"], position=data.get("position", 0))
            return 200, {}, data
        if op == "ListTodolistGroups":
            _, tl = self.get_recording(actor, params["todolistId"], ["Todolist"])
            return self.list_response(route, params, query, self.store.list_resources(account_id, "TodolistGroup", parent_kind="Todolist", parent_id=tl["id"]))
        if op == "CreateTodolistGroup":
            _, tl = self.get_recording(actor, params["todolistId"], ["Todolist"])
            self.require_employee(actor)
            return self.create_recording_under(actor, "TodolistGroup", "Todolist", tl, body, "todolist_group", "todolists")
        if op == "RepositionTodolistGroup":
            _, group = self.get_recording(actor, params["groupId"], ["TodolistGroup"])
            group["position"] = maybe_int(body.get("position")) or group.get("position", 0)
            self.store.put_resource(account_id, "TodolistGroup", group["id"], group, parent_id=group["parent"]["id"], bucket_id=group["bucket"]["id"], title=group["title"], position=group["position"])
            return 200, {}, group
        if op == "ListTodos":
            _, tl = self.get_recording(actor, params["todolistId"], ["Todolist", "TodolistGroup"])
            items = self.store.list_resources(account_id, "Todo", parent_kind=tl["type"] if tl["type"] == "TodolistGroup" else "Todolist", parent_id=tl["id"], status=query.get("status"))
            completed = truthy(query.get("completed"))
            if completed is not None:
                items = [x for x in items if bool(x.get("completed")) == completed]
            return self.list_response(route, params, query, [x for x in items if self.can_see_recording(actor, x)])
        if op == "CreateTodo":
            _, tl = self.get_recording(actor, params["todolistId"], ["Todolist", "TodolistGroup"])
            self.require_employee(actor)
            title = require_text(body, "title", "content", "description")
            rid = self.store.next_id(account_id, "todo", 43000)
            project = self.get_project(actor, tl["bucket"]["id"])
            todo = recording(
                account_id,
                rid,
                "Todo",
                title,
                project,
                parent_ref(tl),
                actor.person,
                utcnow(),
                content=str(body.get("content") or body.get("description") or ""),
                segment="todos",
                position=maybe_int(body.get("position")) or 0,
                visible_to_clients=bool(body.get("visible_to_clients", False)),
                extra={
                    "description": str(body.get("description") or ""),
                    "completed": False,
                    "starts_on": body.get("starts_on"),
                    "due_on": body.get("due_on"),
                    "assignees": self.people_from_ids(account_id, body.get("assignee_ids") or body.get("assignees") or []),
                    "completion_subscribers": self.people_from_ids(account_id, body.get("completion_subscriber_ids") or []),
                    "completion_url": f"/{account_id}/todos/{rid}/completion.json",
                },
            )
            parent_kind = "TodolistGroup" if tl["type"] == "TodolistGroup" else "Todolist"
            self.store.put_resource(account_id, "Todo", rid, todo, parent_kind=parent_kind, parent_id=tl["id"], bucket_id=project["id"], title=title, position=todo["position"])
            self.store.write_event(account_id, rid, "created", actor.person_id, {"type": "Todo"})
            return 201, {"Location": todo["url"]}, todo
        if op in {"GetTodo", "UpdateTodo", "TrashTodo", "CompleteTodo", "UncompleteTodo", "RepositionTodo"}:
            _, todo = self.get_recording(actor, params["todoId"], ["Todo"])
            if op == "GetTodo":
                return 200, {}, todo
            self.require_employee(actor)
            if op == "TrashTodo":
                todo["status"] = "trashed"
                self.store.put_resource(account_id, "Todo", todo["id"], todo, status="trashed", title=todo["title"])
                return 204, {}, None
            if op == "CompleteTodo":
                todo["completed"] = True
                todo["completed_at"] = iso()
            elif op == "UncompleteTodo":
                todo["completed"] = False
                todo["completed_at"] = None
            elif op == "RepositionTodo":
                todo["position"] = maybe_int(body.get("position")) or todo.get("position", 0)
            else:
                todo.update({k: body[k] for k in ["title", "description", "content", "due_on", "starts_on", "visible_to_clients"] if k in body})
                if "assignee_ids" in body:
                    todo["assignees"] = self.people_from_ids(account_id, body["assignee_ids"])
            todo["updated_at"] = iso()
            self.store.put_resource(account_id, "Todo", todo["id"], todo, parent_id=todo["parent"]["id"], bucket_id=todo["bucket"]["id"], title=todo["title"], position=todo.get("position", 0), status=todo.get("status", "active"))
            self.store.write_event(account_id, todo["id"], op.replace("Todo", "").lower() or "updated", actor.person_id)
            return 200, {}, todo
        if op == "GetHillChart":
            _, todoset = self.get_recording(actor, params["todosetId"], ["To-dos"])
            return 200, {}, todoset.get("hill_chart") or {"enabled": False, "points": []}
        if op == "UpdateHillChartSettings":
            self.require_employee(actor)
            _, todoset = self.get_recording(actor, params["todosetId"], ["To-dos"])
            todoset["hill_chart"] = {**todoset.get("hill_chart", {}), **clean_patch(body), "updated_at": iso()}
            self.store.put_resource(account_id, "To-dos", todoset["id"], todoset, parent_id=todoset["parent"]["id"], bucket_id=todoset["bucket"]["id"], title=todoset["title"])
            return 200, {}, todoset["hill_chart"]
        raise AssertionError(op)

    def create_recording_under(self, actor: Actor, kind: str, rtype: str, parent: Json, body: Json, counter: str, segment: str) -> tuple[int, dict[str, str], Json]:
        account_id = actor.account_id
        title = require_text(body, "title", "name")
        rid = self.store.next_id(account_id, counter, 30000)
        project = self.get_project(actor, parent["bucket"]["id"])
        rec = recording(
            account_id,
            rid,
            rtype,
            title,
            project,
            parent_ref(parent),
            actor.person,
            utcnow(),
            content=str(body.get("content") or body.get("description") or ""),
            segment=segment,
            position=maybe_int(body.get("position")) or 0,
            visible_to_clients=bool(body.get("visible_to_clients", False)),
            extra={k: v for k, v in body.items() if k not in {"title", "name", "content"}},
        )
        self.store.put_resource(account_id, kind, rid, rec, parent_kind=parent.get("type"), parent_id=parent["id"], bucket_id=project["id"], title=title, position=rec["position"])
        self.store.write_event(account_id, rid, "created", actor.person_id, {"type": rtype})
        return 201, {"Location": rec["url"]}, rec

    def people_from_ids(self, account_id: str, values: Any) -> list[Json]:
        if values is None:
            return []
        if isinstance(values, list):
            ids = [int(v["id"] if isinstance(v, dict) else v) for v in values]
        else:
            ids = [int(values)]
        people: list[Json] = []
        for pid in ids:
            p = self.store.get_resource(account_id, "Person", pid)
            if not p:
                raise ApiError(422, "validation", f"Person {pid} does not exist")
            people.append(p)
        return people

    def handle_cards(self, op: str, route: Route, params: Json, query: dict[str, str], body: Json, actor: Actor) -> tuple[int, dict[str, str], Any]:
        account_id = actor.account_id
        if op == "GetCardTable":
            _, table = self.get_recording(actor, params["cardTableId"], ["Card Table"])
            columns = self.store.list_resources(account_id, "CardColumn", parent_kind="Card Table", parent_id=table["id"])
            table["columns"] = columns
            return 200, {}, table
        if op == "CreateCardColumn":
            _, table = self.get_recording(actor, params["cardTableId"], ["Card Table"])
            self.require_employee(actor)
            return self.create_recording_under(actor, "CardColumn", "Kanban::Column", table, body, "card_column", "card_tables/columns")
        if op in {"GetCardColumn", "UpdateCardColumn", "SetCardColumnColor", "EnableCardColumnOnHold", "DisableCardColumnOnHold", "MoveCardColumn", "SubscribeToCardColumn", "UnsubscribeFromCardColumn"}:
            _, col = self.get_recording(actor, params["columnId"], ["CardColumn"])
            if op == "GetCardColumn":
                return 200, {}, col
            if op == "SubscribeToCardColumn":
                return 200, {}, self.store.subscribe(account_id, col["id"], actor.person_id)
            if op == "UnsubscribeFromCardColumn":
                self.store.unsubscribe(account_id, col["id"], actor.person_id)
                return 204, {}, None
            self.require_employee(actor)
            if op == "SetCardColumnColor":
                col["color"] = require_text(body, "color")
            elif op == "EnableCardColumnOnHold":
                col["on_hold"] = True
            elif op == "DisableCardColumnOnHold":
                col["on_hold"] = False
            elif op == "MoveCardColumn":
                col["position"] = maybe_int(body.get("position")) or col.get("position", 0)
            else:
                col.update({k: body[k] for k in ["title", "name", "color", "on_hold", "position"] if k in body})
                if "name" in body:
                    col["title"] = body["name"]
            col["updated_at"] = iso()
            self.store.put_resource(account_id, "CardColumn", col["id"], col, parent_kind="Card Table", parent_id=col["parent"]["id"], bucket_id=col["bucket"]["id"], title=col.get("title") or col.get("name"), position=col.get("position", 0))
            return 200, {}, col
        if op == "ListCards":
            _, col = self.get_recording(actor, params["columnId"], ["CardColumn"])
            cards = self.store.list_resources(account_id, "Card", parent_kind="CardColumn", parent_id=col["id"])
            return self.list_response(route, params, query, [x for x in cards if self.can_see_recording(actor, x)])
        if op == "CreateCard":
            _, col = self.get_recording(actor, params["columnId"], ["CardColumn"])
            self.require_employee(actor)
            title = require_text(body, "title")
            rid = self.store.next_id(account_id, "card", 53000)
            project = self.get_project(actor, col["bucket"]["id"])
            card = recording(
                account_id,
                rid,
                "Kanban::Card",
                title,
                project,
                parent_ref(col),
                actor.person,
                utcnow(),
                content=str(body.get("content") or body.get("notes") or ""),
                segment="card_tables/cards",
                position=maybe_int(body.get("position")) or 0,
                extra={"assignees": self.people_from_ids(account_id, body.get("assignee_ids") or []), "column_id": col["id"], "on_hold": bool(body.get("on_hold", False)), "due_on": body.get("due_on"), "watchers": []},
            )
            self.store.put_resource(account_id, "Card", rid, card, parent_kind="CardColumn", parent_id=col["id"], bucket_id=project["id"], title=title, position=card["position"])
            self.store.write_event(account_id, rid, "created", actor.person_id, {"type": "Card"})
            return 201, {"Location": card["url"]}, card
        if op in {"GetCard", "UpdateCard", "MoveCard"}:
            _, card = self.get_recording(actor, params["cardId"], ["Card"])
            if op == "GetCard":
                card["steps"] = self.store.list_resources(account_id, "Step", parent_kind="Card", parent_id=card["id"])
                return 200, {}, card
            self.require_employee(actor)
            if op == "MoveCard":
                column_id = maybe_int(body.get("column_id") or body.get("list_id"))
                if not column_id:
                    raise ApiError(422, "validation", "column_id is required")
                col = self.store.get_resource(account_id, "CardColumn", column_id)
                if not col:
                    raise ApiError(422, "validation", "column_id does not exist")
                card["parent"] = parent_ref(col)
                card["column_id"] = column_id
                card["on_hold"] = bool(body.get("on_hold", False))
                parent_id = column_id
            else:
                card.update({k: body[k] for k in ["title", "content", "due_on", "on_hold", "position"] if k in body})
                if "assignee_ids" in body:
                    card["assignees"] = self.people_from_ids(account_id, body["assignee_ids"])
                parent_id = card["parent"]["id"]
            card["updated_at"] = iso()
            self.store.put_resource(account_id, "Card", card["id"], card, parent_kind="CardColumn", parent_id=parent_id, bucket_id=card["bucket"]["id"], title=card["title"], position=card.get("position", 0))
            self.store.write_event(account_id, card["id"], "moved" if op == "MoveCard" else "updated", actor.person_id)
            return 200, {}, card
        if op == "CreateCardStep":
            _, card = self.get_recording(actor, params["cardId"], ["Card"])
            self.require_employee(actor)
            return self.create_recording_under(actor, "Step", "CardStep", card, body, "step", "card_tables/steps")
        if op in {"GetCardStep", "UpdateCardStep", "SetCardStepCompletion"}:
            _, step = self.get_recording(actor, params["stepId"], ["Step"])
            if op == "GetCardStep":
                return 200, {}, step
            self.require_employee(actor)
            if op == "SetCardStepCompletion":
                step["completed"] = bool(body.get("completed", True))
                step["completed_at"] = iso() if step["completed"] else None
            else:
                step.update({k: body[k] for k in ["title", "content", "due_on", "position"] if k in body})
                if "assignee_ids" in body:
                    step["assignees"] = self.people_from_ids(account_id, body["assignee_ids"])
            step["updated_at"] = iso()
            self.store.put_resource(account_id, "Step", step["id"], step, parent_kind=step["parent"]["type"], parent_id=step["parent"]["id"], bucket_id=step["bucket"]["id"], title=step["title"], position=step.get("position", 0))
            return 200, {}, step
        if op == "RepositionCardStep":
            _, card = self.get_recording(actor, params["cardId"], ["Card"])
            step_id = maybe_int(body.get("step_id"))
            if not step_id:
                raise ApiError(422, "validation", "step_id is required")
            _, step = self.get_recording(actor, step_id, ["Step"])
            if step["parent"]["id"] != card["id"]:
                raise ApiError(422, "validation", "step does not belong to this card")
            step["position"] = maybe_int(body.get("position")) or step.get("position", 0)
            self.store.put_resource(account_id, "Step", step["id"], step, parent_kind="Card", parent_id=card["id"], bucket_id=step["bucket"]["id"], title=step["title"], position=step["position"])
            return 200, {}, step
        raise AssertionError(op)

    def handle_files(self, op: str, route: Route, params: Json, query: dict[str, str], body: Json, actor: Actor) -> tuple[int, dict[str, str], Any]:
        account_id = actor.account_id
        if op in {"GetVault", "UpdateVault"}:
            _, vault = self.get_recording(actor, params["vaultId"], ["Docs & Files", "Vault"])
            if op == "GetVault":
                return 200, {}, vault
            self.require_employee(actor)
            vault.update({k: body[k] for k in ["title", "name", "description"] if k in body})
            self.store.put_resource(account_id, "Docs & Files" if vault["id"] == 203 else "Vault", vault["id"], vault, parent_id=vault["parent"]["id"], bucket_id=vault["bucket"]["id"], title=vault.get("title") or vault.get("name"))
            return 200, {}, vault
        if op == "ListDocuments":
            _, vault = self.get_recording(actor, params["vaultId"], ["Docs & Files", "Vault"])
            return self.list_response(route, params, query, self.store.list_resources(account_id, "Document", parent_id=vault["id"]))
        if op == "CreateDocument":
            _, vault = self.get_recording(actor, params["vaultId"], ["Docs & Files", "Vault"])
            self.require_employee(actor)
            return self.create_recording_under(actor, "Document", "Document", vault, body, "document", "documents")
        if op in {"GetDocument", "UpdateDocument"}:
            _, doc = self.get_recording(actor, params["documentId"], ["Document"])
            if op == "GetDocument":
                return 200, {}, doc
            self.require_employee(actor)
            doc.update({k: body[k] for k in ["title", "content", "description", "visible_to_clients"] if k in body})
            self.store.put_resource(account_id, "Document", doc["id"], doc, parent_id=doc["parent"]["id"], bucket_id=doc["bucket"]["id"], title=doc["title"])
            return 200, {}, doc
        if op == "ListUploads":
            _, vault = self.get_recording(actor, params["vaultId"], ["Docs & Files", "Vault"])
            return self.list_response(route, params, query, self.store.list_resources(account_id, "Upload", parent_id=vault["id"]))
        if op in {"CreateUpload", "CreateAttachment"}:
            parent = None
            if "vaultId" in params:
                _, parent = self.get_recording(actor, params["vaultId"], ["Docs & Files", "Vault"])
            else:
                project = self.get_project(actor, body.get("bucket_id") or 12345)
                parent = {"id": project["id"], "title": project["name"], "type": "Project", "bucket": bucket(project)}
            self.require_employee(actor)
            title = require_text(body, "filename", "name", "title")
            rid = self.store.next_id(account_id, "upload", 63000)
            project = self.get_project(actor, parent.get("bucket", {}).get("id") or 12345)
            upload = recording(
                account_id,
                rid,
                "Upload",
                title,
                project,
                parent_ref(parent),
                actor.person,
                utcnow(),
                segment="uploads",
                extra={"byte_size": maybe_int(body.get("byte_size")) or 0, "content_type": body.get("content_type") or mimetypes.guess_type(title)[0] or "application/octet-stream", "attachable_sgid": f"upload-{rid}", "download_url": f"/{account_id}/uploads/{rid}/download"},
            )
            self.store.put_resource(account_id, "Upload", rid, upload, parent_kind=parent.get("type"), parent_id=parent["id"], bucket_id=project["id"], title=title)
            return 201, {"Location": upload["url"]}, upload
        if op in {"GetUpload", "UpdateUpload", "ListUploadVersions"}:
            _, upload = self.get_recording(actor, params["uploadId"], ["Upload"])
            if op == "ListUploadVersions":
                return self.list_response(route, params, query, [upload])
            if op == "GetUpload":
                return 200, {}, upload
            self.require_employee(actor)
            upload.update({k: body[k] for k in ["title", "description", "visible_to_clients"] if k in body})
            self.store.put_resource(account_id, "Upload", upload["id"], upload, parent_id=upload["parent"]["id"], bucket_id=upload["bucket"]["id"], title=upload["title"])
            return 200, {}, upload
        if op == "ListVaults":
            _, vault = self.get_recording(actor, params["vaultId"], ["Docs & Files", "Vault"])
            return self.list_response(route, params, query, self.store.list_resources(account_id, "Vault", parent_id=vault["id"]))
        if op == "CreateVault":
            _, vault = self.get_recording(actor, params["vaultId"], ["Docs & Files", "Vault"])
            self.require_employee(actor)
            return self.create_recording_under(actor, "Vault", "Vault", vault, body, "vault", "vaults")
        raise AssertionError(op)

    def handle_campfire(self, op: str, route: Route, params: Json, query: dict[str, str], body: Json, actor: Actor) -> tuple[int, dict[str, str], Any]:
        account_id = actor.account_id
        if op == "ListCampfires":
            campfires = self.store.list_resources(account_id, "Campfire")
            return self.list_response(route, params, query, [x for x in campfires if self.can_see_recording(actor, x)])
        if op == "GetCampfire":
            _, campfire = self.get_recording(actor, params["campfireId"], ["Campfire"])
            return 200, {}, campfire
        if op == "ListCampfireLines":
            _, campfire = self.get_recording(actor, params["campfireId"], ["Campfire"])
            lines = self.store.list_resources(account_id, "CampfireLine", parent_kind="Campfire", parent_id=campfire["id"])
            return self.list_response(route, params, query, [x for x in lines if self.can_see_recording(actor, x)])
        if op == "CreateCampfireLine":
            _, campfire = self.get_recording(actor, params["campfireId"], ["Campfire"])
            self.require_employee(actor)
            return self.create_recording_under(actor, "CampfireLine", "Chat::Line", campfire, {"title": str(body.get("content") or body.get("line") or "")[:80], "content": body.get("content") or body.get("line")}, "campfire_line", f"chats/{campfire['id']}/lines")
        if op in {"GetCampfireLine", "DeleteCampfireLine"}:
            _, line = self.get_recording(actor, params["lineId"], ["CampfireLine"])
            if op == "GetCampfireLine":
                return 200, {}, line
            if line["creator"]["id"] != actor.person_id and not actor.is_owner:
                raise ApiError(403, "forbidden", "Only the line creator or an owner can delete this line")
            line["status"] = "trashed"
            self.store.put_resource(account_id, "CampfireLine", line["id"], line, status="trashed", title=line["title"])
            return 204, {}, None
        if op == "ListCampfireUploads":
            _, campfire = self.get_recording(actor, params["campfireId"], ["Campfire"])
            uploads = self.store.list_resources(account_id, "Upload", parent_kind="Campfire", parent_id=campfire["id"])
            return self.list_response(route, params, query, uploads)
        if op == "CreateCampfireUpload":
            _, campfire = self.get_recording(actor, params["campfireId"], ["Campfire"])
            self.require_employee(actor)
            return self.create_recording_under(actor, "Upload", "Upload", campfire, {"title": require_text(body, "filename", "name", "title"), **body}, "upload", "uploads")
        if op in {"ListChatbots", "CreateChatbot", "GetChatbot", "UpdateChatbot", "DeleteChatbot"}:
            return self.simple_child_resource(op, route, params, query, body, actor, "Chatbot", "campfireId", "Campfire", "chatbotId", "chatbot", "integrations")
        raise AssertionError(op)

    def handle_schedule(self, op: str, route: Route, params: Json, query: dict[str, str], body: Json, actor: Actor) -> tuple[int, dict[str, str], Any]:
        account_id = actor.account_id
        if op in {"GetSchedule", "UpdateScheduleSettings"}:
            _, schedule = self.get_recording(actor, params["scheduleId"], ["Schedule"])
            if op == "GetSchedule":
                return 200, {}, schedule
            self.require_employee(actor)
            schedule["settings"] = {**schedule.get("settings", {}), **clean_patch(body), "updated_at": iso()}
            self.store.put_resource(account_id, "Schedule", schedule["id"], schedule, parent_id=schedule["parent"]["id"], bucket_id=schedule["bucket"]["id"], title=schedule["title"])
            return 200, {}, schedule
        if op == "ListScheduleEntries":
            _, schedule = self.get_recording(actor, params["scheduleId"], ["Schedule"])
            entries = self.store.list_resources(account_id, "ScheduleEntry", parent_kind="Schedule", parent_id=schedule["id"])
            return self.list_response(route, params, query, [x for x in entries if self.can_see_recording(actor, x)])
        if op == "CreateScheduleEntry":
            _, schedule = self.get_recording(actor, params["scheduleId"], ["Schedule"])
            self.require_employee(actor)
            return self.create_recording_under(actor, "ScheduleEntry", "Schedule::Entry", schedule, body, "schedule_entry", "schedule_entries")
        if op in {"GetScheduleEntry", "UpdateScheduleEntry", "GetScheduleEntryOccurrence"}:
            _, entry = self.get_recording(actor, params["entryId"], ["ScheduleEntry"])
            if op == "GetScheduleEntryOccurrence":
                occurrence = dict(entry)
                occurrence["occurrence_date"] = params["date"]
                return 200, {}, occurrence
            if op == "GetScheduleEntry":
                return 200, {}, entry
            self.require_employee(actor)
            entry.update({k: body[k] for k in ["title", "content", "all_day", "starts_at", "ends_at", "starts_on", "ends_on", "visible_to_clients"] if k in body})
            self.store.put_resource(account_id, "ScheduleEntry", entry["id"], entry, parent_id=entry["parent"]["id"], bucket_id=entry["bucket"]["id"], title=entry["title"])
            return 200, {}, entry
        if op == "GetUpcomingSchedule":
            entries = self.store.list_resources(account_id, "ScheduleEntry")
            todos = [t for t in self.store.list_resources(account_id, "Todo") if t.get("due_on")]
            return self.list_response(route, params, query, sorted(entries + todos, key=lambda x: x.get("starts_on") or x.get("due_on") or ""))
        if op in {"GetProjectTimesheet", "GetRecordingTimesheet", "GetTimesheetReport"}:
            items = self.store.list_resources(account_id, "TimesheetEntry")
            if "projectId" in params:
                items = [x for x in items if x.get("bucket", {}).get("id") == int(params["projectId"])]
            if "recordingId" in params:
                items = [x for x in items if x.get("recording_id") == int(params["recordingId"])]
            return self.list_response(route, params, query, items)
        if op == "CreateTimesheetEntry":
            _, rec = self.get_recording(actor, params["recordingId"])
            self.require_employee(actor)
            rid = self.store.next_id(account_id, "timesheet_entry", 68000)
            data = {
                "id": rid,
                "recording_id": rec["id"],
                "person": actor.person,
                "date": body.get("date") or date_iso(utcnow()),
                "hours": float(body.get("hours") or 0),
                "description": str(body.get("description") or ""),
                "bucket": rec.get("bucket"),
                "created_at": iso(),
                "updated_at": iso(),
                "url": f"/{account_id}/timesheet_entries/{rid}",
            }
            self.store.put_resource(account_id, "TimesheetEntry", rid, data, bucket_id=rec.get("bucket", {}).get("id"), title=data["description"])
            return 201, {"Location": data["url"]}, data
        if op in {"GetTimesheetEntry", "UpdateTimesheetEntry"}:
            data = self.store.get_resource(account_id, "TimesheetEntry", params["entryId"])
            if not data:
                raise ApiError(404, "not_found", f"Timesheet entry {params['entryId']} was not found")
            if op == "GetTimesheetEntry":
                return 200, {}, data
            self.require_employee(actor)
            data.update({k: body[k] for k in ["date", "hours", "description"] if k in body})
            data["updated_at"] = iso()
            self.store.put_resource(account_id, "TimesheetEntry", data["id"], data, bucket_id=data.get("bucket", {}).get("id"), title=data.get("description"))
            return 200, {}, data
        raise AssertionError(op)

    def handle_recordings(self, op: str, route: Route, params: Json, query: dict[str, str], body: Json, actor: Actor) -> tuple[int, dict[str, str], Any]:
        account_id = actor.account_id
        if op == "GetRecording":
            _, rec = self.get_recording(actor, params["recordingId"])
            return 200, {}, rec
        if op == "ListRecordings":
            items = []
            for kind in ["Message", "Todo", "Todolist", "Document", "Upload", "CampfireLine", "Card", "Step", "ScheduleEntry"]:
                items.extend(self.store.list_resources(account_id, kind, status=query.get("status"), include_trashed=query.get("status") == "trashed"))
            items = [x for x in items if self.can_see_recording(actor, x)]
            return self.list_response(route, params, query, sorted(items, key=lambda x: x.get("updated_at", ""), reverse=True))
        if op in {"ArchiveRecording", "UnarchiveRecording", "TrashRecording", "SetClientVisibility"}:
            rid = int(params["recordingId"])
            kind, rec = self.get_recording(actor, rid)
            self.require_employee(actor)
            if op == "ArchiveRecording":
                rec["status"] = "archived"
            elif op == "UnarchiveRecording":
                rec["status"] = "active"
            elif op == "TrashRecording":
                rec["status"] = "trashed"
            else:
                rec["visible_to_clients"] = bool(body.get("visible_to_clients", body.get("visible", True)))
            rec["updated_at"] = iso()
            self.store.put_resource(account_id, kind, rid, rec, status=rec.get("status", "active"), title=rec.get("title"), position=rec.get("position", 0))
            self.store.write_event(account_id, rid, op.replace("Recording", "").lower(), actor.person_id)
            return 200, {}, rec
        if op == "ListComments":
            self.get_recording(actor, params["recordingId"])
            return self.list_response(route, params, query, self.store.list_comments(account_id, params["recordingId"]))
        if op == "CreateComment":
            self.get_recording(actor, params["recordingId"])
            content = require_text(body, "content")
            comment = self.store.put_comment(account_id, int(params["recordingId"]), actor.person_id, content)
            return 201, {"Location": comment["url"]}, comment
        if op == "GetComment":
            comment = self.store.get_comment(account_id, params["commentId"])
            if not comment:
                raise ApiError(404, "not_found", f"Comment {params['commentId']} was not found")
            return 200, {}, comment
        if op == "UpdateComment":
            comment = self.store.get_comment(account_id, params["commentId"])
            if not comment:
                raise ApiError(404, "not_found", f"Comment {params['commentId']} was not found")
            if comment["creator"]["id"] != actor.person_id and not actor.is_owner:
                raise ApiError(403, "forbidden", "Only the comment creator or an owner can update this comment")
            return 200, {}, self.store.update_comment(account_id, params["commentId"], body)
        if op in {"ListRecordingBoosts", "CreateRecordingBoost", "ListEventBoosts", "CreateEventBoost", "GetBoost", "DeleteBoost"}:
            if op == "GetBoost":
                boost = self.store.get_boost(account_id, params["boostId"])
                if not boost:
                    raise ApiError(404, "not_found", f"Boost {params['boostId']} was not found")
                return 200, {}, boost
            if op == "DeleteBoost":
                self.store.delete_boost(account_id, params["boostId"])
                return 204, {}, None
            rid = int(params["recordingId"])
            self.get_recording(actor, rid)
            if op in {"ListRecordingBoosts", "ListEventBoosts"}:
                event_id = maybe_int(params.get("eventId"))
                return self.list_response(route, params, query, self.store.list_boosts(account_id, rid, event_id=event_id))
            boost = self.store.create_boost(account_id, rid, actor.person_id, str(body.get("content") or body.get("emoji") or "👍"), event_id=maybe_int(params.get("eventId")))
            return 201, {}, boost
        if op in {"GetSubscription", "Subscribe", "UpdateSubscription", "Unsubscribe"}:
            rid = int(params["recordingId"])
            self.get_recording(actor, rid)
            if op == "GetSubscription":
                return 200, {}, self.store.subscription(account_id, rid, actor.person_id)
            if op == "Unsubscribe":
                self.store.unsubscribe(account_id, rid, actor.person_id)
                return 204, {}, None
            return 200, {}, self.store.subscribe(account_id, rid, actor.person_id, notify=bool(body.get("notify_on_comments", True)))
        if op == "ListEvents":
            self.get_recording(actor, params["recordingId"])
            return self.list_response(route, params, query, self.store.list_events(account_id, int(params["recordingId"])))
        raise AssertionError(op)

    def handle_reports_and_my(self, op: str, route: Route, params: Json, query: dict[str, str], body: Json, actor: Actor) -> tuple[int, dict[str, str], Any]:
        account_id = actor.account_id
        if op == "Search":
            q = query.get("q") or query.get("query") or ""
            kinds = ["Project", "Message", "Todo", "Document", "Upload", "Card", "CampfireLine", "ScheduleEntry"]
            items: list[Json] = []
            for kind in kinds:
                items.extend(self.store.list_resources(account_id, kind, search=q, include_trashed=query.get("include_trashed") == "true"))
            items = [
                x for x in items
                if (x.get("type") is None and self.can_see_project(actor, x)) or self.can_see_recording(actor, x)
            ]
            return self.list_response(route, params, query, [{"type": x.get("type") or "Project", "title": x.get("title") or x.get("name"), "recording": x, "url": x.get("url")} for x in items])
        if op == "GetSearchMetadata":
            return 200, {}, {"types": ["projects", "messages", "todos", "docs", "files", "cards", "chat", "schedule"], "filters": ["q", "type", "status", "include_trashed"]}
        if op in {"GetMyAssignments", "GetMyCompletedAssignments", "GetMyDueAssignments", "GetOverdueTodos", "ListAssignablePeople", "GetAssignedTodos"}:
            if op == "ListAssignablePeople":
                return self.list_response(route, params, query, [p for p in self.store.list_resources(account_id, "Person") if not p.get("client")])
            todos = self.store.list_resources(account_id, "Todo")
            cards = self.store.list_resources(account_id, "Card")
            assignee = int(params.get("personId") or actor.person_id)
            items = [x for x in todos + cards if any(p.get("id") == assignee for p in x.get("assignees", []))]
            today = utcnow().date().isoformat()
            if op == "GetMyCompletedAssignments":
                items = [x for x in items if x.get("completed")]
            elif op == "GetMyDueAssignments":
                items = [x for x in items if x.get("due_on")]
            elif op == "GetOverdueTodos":
                items = [x for x in todos if x.get("due_on") and x["due_on"] < today and not x.get("completed")]
            return self.list_response(route, params, query, items)
        if op in {"GetMyNotifications", "MarkAsRead"}:
            if op == "GetMyNotifications":
                events = self.store.list_events(account_id)
                return self.list_response(route, params, query, [{"id": e["id"], "recording_id": e.get("recording_id"), "event": e, "read": False} for e in events[:100]])
            now = iso()
            for rid in body.get("recording_ids") or []:
                self.store.connect().execute(
                    "INSERT OR REPLACE INTO readings(account_id,person_id,recording_id,read_at,data,updated_at) VALUES(?,?,?,?,?,?)",
                    (account_id, actor.person_id, int(rid), now, "{}", now),
                )
            return 204, {}, None
        if op in {"GetProgressReport", "GetPersonProgress", "GetProjectTimeline"}:
            rid = maybe_int(params.get("projectId"))
            events = self.store.list_events(account_id, rid)
            return self.list_response(route, params, query, events)
        if op == "GetQuestionReminders":
            return self.list_response(route, params, query, [])
        if op in {"ListGauges", "ToggleGauge", "ListGaugeNeedles", "CreateGaugeNeedle", "GetGaugeNeedle", "UpdateGaugeNeedle", "DestroyGaugeNeedle"}:
            return self.simple_account_or_project_resource(op, route, params, query, body, actor, "GaugeNeedle", "needleId", "gauge_needle")
        if op in {"ListLineupMarkers", "CreateLineupMarker", "UpdateLineupMarker", "DeleteLineupMarker"}:
            return self.simple_account_or_project_resource(op, route, params, query, body, actor, "LineupMarker", "markerId", "lineup_marker")
        raise AssertionError(op)

    def handle_generic_ecosystem(self, op: str, route: Route, params: Json, query: dict[str, str], body: Json, actor: Actor) -> tuple[int, dict[str, str], Any]:
        if "Webhook" in op:
            return self.simple_account_or_project_resource(op, route, params, query, body, actor, "Webhook", "webhookId", "webhook")
        if "Template" in op or "Construction" in op:
            return self.simple_account_or_project_resource(op, route, params, query, body, actor, "Template", "templateId", "template")
        if "Questionnaire" in op:
            _, q = self.get_recording(actor, params["questionnaireId"], ["Questionnaire"])
            return 200, {}, q
        if "Question" in op or "Answer" in op:
            return self.simple_account_or_project_resource(op, route, params, query, body, actor, "Question", "questionId", "question")
        if "Forward" in op or "Inbox" in op:
            return self.simple_account_or_project_resource(op, route, params, query, body, actor, "Forward", "forwardId", "forward")
        if "Client" in op:
            return self.simple_account_or_project_resource(op, route, params, query, body, actor, "ClientRecord", "approvalId", "client_record")
        raise ApiError(501, "unsupported", f"{op} is explicitly unsupported in this single-file server")

    def simple_account_or_project_resource(
        self,
        op: str,
        route: Route,
        params: Json,
        query: dict[str, str],
        body: Json,
        actor: Actor,
        kind: str,
        id_param: str,
        counter: str,
    ) -> tuple[int, dict[str, str], Any]:
        account_id = actor.account_id
        if op.startswith("List") or op in {"GetTimesheetReport", "ListGauges"}:
            items = self.store.list_resources(account_id, kind, bucket_id=maybe_int(params.get("bucketId") or params.get("projectId")))
            return self.list_response(route, params, query, items)
        if op.startswith("Create"):
            self.require_employee(actor)
            rid = self.store.next_id(account_id, counter, 71000)
            title = str(body.get("title") or body.get("name") or f"{kind} {rid}")
            data = {"id": rid, "type": kind, "title": title, **clean_patch(body), "created_at": iso(), "updated_at": iso(), "url": f"/{account_id}/{kind.lower()}s/{rid}"}
            bucket_id = maybe_int(params.get("bucketId") or params.get("projectId") or body.get("bucket_id"))
            self.store.put_resource(account_id, kind, rid, data, bucket_id=bucket_id, title=title)
            return 201, {"Location": data["url"]}, data
        rid = maybe_int(params.get(id_param) or params.get("needleId") or params.get("markerId") or params.get("templateId") or params.get("answerId") or params.get("approvalId") or params.get("correspondenceId") or params.get("replyId") or params.get("forwardId"))
        if not rid:
            raise ApiError(422, "validation", f"{id_param} is required")
        data = self.store.get_resource(account_id, kind, rid)
        if not data:
            raise ApiError(404, "not_found", f"{kind} {rid} was not found")
        if op.startswith("Get"):
            return 200, {}, data
        self.require_employee(actor)
        if op.startswith("Delete") or op.startswith("Destroy"):
            data["status"] = "trashed"
            self.store.put_resource(account_id, kind, rid, data, status="trashed", title=data.get("title"))
            return 204, {}, None
        data.update(clean_patch(body))
        data["updated_at"] = iso()
        self.store.put_resource(account_id, kind, rid, data, title=data.get("title") or data.get("name"))
        return 200, {}, data

    def simple_child_resource(
        self,
        op: str,
        route: Route,
        params: Json,
        query: dict[str, str],
        body: Json,
        actor: Actor,
        kind: str,
        parent_param: str,
        parent_kind: str,
        id_param: str,
        counter: str,
        segment: str,
    ) -> tuple[int, dict[str, str], Any]:
        account_id = actor.account_id
        _, parent = self.get_recording(actor, params[parent_param], [parent_kind])
        if op.startswith("List"):
            return self.list_response(route, params, query, self.store.list_resources(account_id, kind, parent_kind=parent_kind, parent_id=parent["id"]))
        if op.startswith("Create"):
            self.require_employee(actor)
            return self.create_recording_under(actor, kind, kind, parent, body, counter, segment)
        rid = int(params[id_param])
        _, data = self.get_recording(actor, rid, [kind])
        if op.startswith("Get"):
            return 200, {}, data
        self.require_employee(actor)
        if op.startswith("Delete"):
            data["status"] = "trashed"
            self.store.put_resource(account_id, kind, rid, data, status="trashed", title=data.get("title"))
            return 204, {}, None
        data.update(clean_patch(body))
        self.store.put_resource(account_id, kind, rid, data, parent_kind=parent_kind, parent_id=parent["id"], bucket_id=parent.get("bucket", {}).get("id"), title=data.get("title"))
        return 200, {}, data


class Handler(BaseHTTPRequestHandler):
    server_version = "Basecamp5API/1.0"
    app: App

    def do_GET(self) -> None:
        self.respond("GET")

    def do_POST(self) -> None:
        self.respond("POST")

    def do_PUT(self) -> None:
        self.respond("PUT")

    def do_DELETE(self) -> None:
        self.respond("DELETE")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.write_common_headers("application/json", "0", request_id=self.request_id())
        self.end_headers()

    def request_id(self) -> str:
        return self.headers.get("X-Request-Id") or uuid.uuid4().hex

    def respond(self, method: str) -> None:
        started = time.time()
        request_id = self.request_id()
        try:
            raw_length = self.headers.get("Content-Length")
            length = int(raw_length or "0")
            if length > MAX_REQUEST_BYTES:
                raise ApiError(413, "validation", "Request body is too large")
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, extra_headers, payload = self.app.handle(method, self.path, headers, body, request_id)
            self.send_payload(status, extra_headers, payload, request_id)
        except ApiError as exc:
            self.send_payload(exc.status, exc.headers, exc.body(request_id), request_id)
        except Exception as exc:
            logging.exception("Unhandled request error")
            payload = ApiError(500, "api_error", "Internal server error", retryable=True, details={"exception": exc.__class__.__name__}).body(request_id)
            if os.getenv("BASECAMP5_DEBUG_ERRORS") == "1":
                payload["traceback"] = traceback.format_exc()[:MAX_ERROR_BYTES]
            self.send_payload(500, {}, payload, request_id)
        finally:
            elapsed_ms = int((time.time() - started) * 1000)
            logging.info("%s %s request_id=%s elapsed_ms=%s", method, self.path, request_id, elapsed_ms)

    def send_payload(self, status: int, headers: dict[str, str], payload: Any, request_id: str) -> None:
        if payload is None:
            raw = b""
            content_type = "application/json"
        else:
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        if len(raw) > MAX_RESPONSE_BYTES:
            status = 500
            payload = ApiError(500, "api_error", "Response body exceeded safety limit", retryable=True).body(request_id)
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        etag = '"' + hashlib.sha256(raw).hexdigest() + '"'
        if status == 200 and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.write_common_headers(content_type, "0", request_id=request_id)
            self.send_header("ETag", etag)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            return
        self.send_response(status)
        self.write_common_headers(content_type, str(len(raw)), request_id=request_id)
        if status == 200:
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", email.utils.formatdate(usegmt=True))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if raw and self.command != "HEAD":
            self.wfile.write(raw)

    def write_common_headers(self, content_type: str, content_length: str, *, request_id: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", content_length)
        self.send_header("X-Request-Id", request_id)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "private, no-store")
        origin = os.getenv("BASECAMP5_CORS_ORIGIN", "*")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Idempotency-Key, X-Request-Id, X-Reset-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.debug("client=%s " + fmt, self.client_address[0], *args)


class GracefulHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the single-file Basecamp 5 API server")
    parser.add_argument("--host", default=os.getenv("BASECAMP5_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("BASECAMP5_PORT", "8080")))
    parser.add_argument("--db", default=os.getenv("BASECAMP5_DB", DEFAULT_DB_PATH))
    parser.add_argument("--log-level", default=os.getenv("BASECAMP5_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
    app = App(args.db)
    Handler.app = app

    server = GracefulHTTPServer((args.host, args.port), Handler)
    stopping = threading.Event()

    def stop(signum: int, _frame: Any) -> None:
        if stopping.is_set():
            return
        stopping.set()
        logging.info("signal=%s shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logging.info("Basecamp 5 API listening on http://%s:%s account=%s", args.host, args.port, DEFAULT_ACCOUNT_ID)
    logging.info("Use Authorization: Bearer dev-owner-token")
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
