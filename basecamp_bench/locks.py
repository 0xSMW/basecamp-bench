"""Cross-process admission control for paid benchmark work."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["acquire_lane_locks"]


def _lane_name(lane: tuple[str, str]) -> str:
    harness, track = lane
    return f"{harness}--{track}"


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


def _open_lock(path: Path) -> int:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"benchmark lane lock is not a regular file: {path}")
    return fd


@contextmanager
def acquire_lane_locks(
    run_root: Path, lanes: Iterable[tuple[str, str]]
) -> Iterator[None]:
    """Exclusively hold sorted ``(harness, track)`` lanes until the context exits.

    Locks are process-wide OS advisory locks. A crash releases them automatically;
    persistent lock files retain bounded owner metadata for actionable conflicts.
    No benchmark run directory or provider process should be created before entry.
    """
    selected = sorted(set(lanes))
    if not selected:
        raise ValueError("benchmark run has no implementation lanes")
    root = Path(run_root)
    root.mkdir(parents=True, exist_ok=True)
    lock_dir = root / ".locks"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    if lock_dir.is_symlink() or not lock_dir.is_dir():
        raise ValueError(f"benchmark lock path must be a non-symlink directory: {lock_dir}")

    held: list[int] = []
    try:
        for lane in selected:
            path = lock_dir / f"{_lane_name(lane)}.lock"
            fd = _open_lock(path)
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
