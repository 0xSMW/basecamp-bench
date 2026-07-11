"""Cross-process admission control for paid benchmark work."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from basecamp_bench.safety import validate_identifier

__all__ = ["acquire_lane_locks"]

_LOCK_DIR_NAME = ".basecamp-bench-locks"


def _validated_lane(lane: object) -> tuple[str, str]:
    if not isinstance(lane, tuple) or len(lane) != 2:
        raise ValueError("benchmark lane must be a (harness, track) tuple")
    harness = validate_identifier(lane[0], field="lane harness")
    track = validate_identifier(lane[1], field="lane track")
    return harness, track


def _lane_name(lane: tuple[str, str]) -> str:
    harness, track = lane
    return f"{harness}--{track}"


def _lane_filename(lane: tuple[str, str]) -> str:
    payload = json.dumps(lane, separators=(",", ":"), ensure_ascii=True).encode()
    return f"lane-{hashlib.sha256(payload).hexdigest()}.lock"


def _owner(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 4096)
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "owner metadata unavailable"
    if not isinstance(payload, dict):
        return "owner metadata unavailable"
    pid = payload.get("pid")
    host = payload.get("host")
    started_at = payload.get("started_at")
    fields = []
    if isinstance(pid, int) and not isinstance(pid, bool):
        fields.append(f"pid {pid}")
    if isinstance(host, str) and host:
        fields.append(f"host {host}")
    if isinstance(started_at, str) and started_at:
        fields.append(f"since {started_at}")
    return ", ".join(fields) or "owner metadata unavailable"


def _open_lock(directory_fd: int, filename: str, display_path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(filename, flags, 0o600, dir_fd=directory_fd)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        os.close(fd)
        raise ValueError(f"benchmark lane lock is unsafe: {display_path}")
    return fd


@contextmanager
def acquire_lane_locks(project_root: Path, lanes: Iterable[tuple[str, str]]) -> Iterator[None]:
    """Exclusively hold sorted ``(harness, track)`` lanes until the context exits.

    Locks are process-wide OS advisory locks. A crash releases them automatically;
    persistent lock files retain bounded owner metadata for actionable conflicts.
    No benchmark run directory or provider process should be created before entry.
    """
    selected = sorted({_validated_lane(lane) for lane in lanes})
    if not selected:
        raise ValueError("benchmark run has no implementation lanes")
    root = Path(project_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"benchmark project root is not a directory: {root}")
    lock_dir = root / _LOCK_DIR_NAME
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(lock_dir, directory_flags)
    except OSError as exc:
        raise ValueError(f"benchmark lock path is unsafe: {lock_dir}") from exc
    directory_info = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != os.geteuid()
        or stat.S_IMODE(directory_info.st_mode) & 0o077
    ):
        os.close(directory_fd)
        raise ValueError(f"benchmark lock path is unsafe: {lock_dir}")

    held: list[int] = []
    try:
        for lane in selected:
            path = lock_dir / _lane_filename(lane)
            fd = _open_lock(directory_fd, path.name, path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                owner = _owner(fd)
                os.close(fd)
                raise ValueError(
                    f"benchmark lane already active: {_lane_name(lane)} ({owner})"
                ) from exc
            held.append(fd)
            metadata = {
                "host": socket.gethostname(),
                "lane": _lane_name(lane),
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(),
            }
            encoded = (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode()
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, encoded)
            os.fsync(fd)
        yield
    finally:
        for fd in reversed(held):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        os.close(directory_fd)
