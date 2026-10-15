"""Runner-owned, loopback-only static runtime for immutable frontend evidence."""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


class BrowserRuntimeError(RuntimeError):
    """The runner could not prepare the frontend's browser runtime."""


class FrontendServer:
    """Serve files, never execute submission code or accept write requests.

    The listener runs in the managing runner, outside the evaluator subprocess
    sandbox. Each evaluator gets an OS-assigned port and an unguessable URL.
    Symlinks, traversal, directory listings, and non-loopback Host headers fail
    closed. The context manager always closes the listener.
    """

    def __init__(self, root: Path) -> None:
        if root.is_symlink() or not root.is_dir():
            raise BrowserRuntimeError("frontend snapshot must be a regular directory")
        self.root = root.resolve(strict=True)
        entry = self.root / "index.html"
        if not entry.is_file() or entry.is_symlink():
            candidates = [p for p in self.root.glob("*.html") if p.is_file() and not p.is_symlink()]
            if len(candidates) != 1:
                raise BrowserRuntimeError(
                    "frontend requires index.html or one root HTML entry point"
                )
            entry = candidates[0]
        self.entry = entry.name
        self.prefix = "/" + secrets.token_urlsafe(24) + "/"
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def __enter__(self) -> FrontendServer:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(3)

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                self._read(head=False)

            def do_HEAD(self) -> None:
                self._read(head=True)

            def _deny_write(self) -> None:
                self.send_response(405)
                self.send_header("Allow", "GET, HEAD")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _deny_write

            def _read(self, *, head: bool) -> None:
                assert owner._server is not None
                port = owner._server.server_port
                if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                    self.send_error(403)
                    return
                path = unquote(urlsplit(self.path).path)
                if not path.startswith(owner.prefix):
                    self.send_error(404)
                    return
                relative = path[len(owner.prefix) :]
                parts = relative.split("/")
                if any(p in {".", ".."} or "\\" in p or "\x00" in p for p in parts):
                    self.send_error(404)
                    return
                file = owner.root
                for part in parts:
                    file = file / part
                    if file.is_symlink():
                        self.send_error(404)
                        return
                if not file.is_file() or not file.resolve().is_relative_to(owner.root):
                    self.send_error(404)
                    return
                try:
                    data = file.read_bytes()
                except OSError:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Type", mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                )
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                with owner._lock:
                    if len(owner.requests) < 1000:
                        owner.requests.append(
                            {
                                "at": datetime.now(UTC).isoformat(),
                                "method": self.command,
                                "path": relative,
                                "user_agent": self.headers.get("User-Agent", ""),
                                "fetch_destination": self.headers.get("Sec-Fetch-Dest", ""),
                            }
                        )
                if not head:
                    self.wfile.write(data)

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self._server.daemon_threads = True
            self._server.block_on_close = False
            port = self._server.server_address[1]
            self.url = f"http://127.0.0.1:{port}{self.prefix}{quote(self.entry, safe='')}"
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            # Readiness uses the actual entry point, not merely an open port.
            with build_opener(ProxyHandler({})).open(
                Request(self.url, headers={"User-Agent": "basecamp-bench-preflight"}), timeout=5
            ) as response:
                if (
                    response.status != 200
                    or response.read() != (self.root / self.entry).read_bytes()
                ):
                    raise BrowserRuntimeError("frontend HTTP readiness check failed")
        except (OSError, ValueError) as exc:
            self.close()
            raise BrowserRuntimeError("frontend HTTP runtime could not start") from exc
        except BrowserRuntimeError:
            self.close()
            raise
        return self

    def close(self) -> None:
        if self._server is not None:
            if self._thread is not None and self._thread.is_alive():
                self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __exit__(self, *args: object) -> None:
        self.close()

    def chrome_loaded(self) -> bool:
        with self._lock:
            return self._chrome_loaded()

    def evidence(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": "1.0",
                "url": self.url,
                "http_ready": True,
                "chrome_loaded": self._chrome_loaded(),
                "requests": list(self.requests),
            }

    def _chrome_loaded(self) -> bool:
        return any(
            r["method"] == "GET" and r["path"] == self.entry and "Chrome/" in r["user_agent"]
            for r in self.requests
        )


def validate_browser_evidence(path: Path, *, url: str, chrome_loaded: bool) -> list[str]:
    """Require rendered Chrome evidence, while allowing observed product failures."""
    if not chrome_loaded:
        return ["browser_not_loaded"]
    if path.is_symlink() or not path.is_file():
        return ["missing_browser_evidence"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["malformed_browser_evidence"]
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != "1.0"
        or data.get("url") != url
        or data.get("status") != "complete"
        or data.get("rendered") is not True
    ):
        return ["incomplete_browser_evidence"]
    screenshots = data.get("screenshots")
    interactions = data.get("interactions")
    if not isinstance(screenshots, list) or not isinstance(interactions, list):
        return ["incomplete_browser_evidence"]
    views = {
        s["viewport"]
        for s in screenshots
        if isinstance(s, dict)
        and isinstance(s.get("viewport"), str)
        and isinstance(s.get("evidence"), str)
        and s["evidence"].strip()
    }
    actions = {
        a["action"]
        for a in interactions
        if isinstance(a, dict)
        and isinstance(a.get("action"), str)
        and isinstance(a.get("passed"), bool)
        and isinstance(a.get("observed"), str)
        and a["observed"].strip()
    }
    if not {"desktop", "mobile"}.issubset(views) or not {"click", "type", "reload"}.issubset(
        actions
    ):
        return ["incomplete_browser_evidence"]
    return []
