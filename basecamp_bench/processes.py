"""Managed subprocess execution with process-group cleanup and bounded logs."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

__all__ = ["ProcessResult", "run_managed"]

_READ_CHUNK = 64 * 1024
_POLL_INTERVAL_S = 0.05


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Outcome of a managed child process run."""

    returncode: int | None
    duration_s: float
    timed_out: bool
    interrupted: bool
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    error: str | None


class _StreamDrainState:
    __slots__ = ("bytes_written", "truncated", "error")

    def __init__(self) -> None:
        self.bytes_written: int = 0
        self.truncated: bool = False
        self.error: str | None = None


def _validate_limits(
    *,
    timeout_s: float | None,
    grace_s: float,
    max_stream_bytes: int,
) -> None:
    if max_stream_bytes < 0:
        raise ValueError(f"max_stream_bytes must be >= 0, got {max_stream_bytes!r}")
    if grace_s < 0:
        raise ValueError(f"grace_s must be >= 0, got {grace_s!r}")
    if timeout_s is not None and timeout_s < 0:
        raise ValueError(f"timeout_s must be >= 0 or None, got {timeout_s!r}")


def _drain_stream(
    stream: IO[bytes],
    path: Path,
    max_stream_bytes: int,
    state: _StreamDrainState,
) -> None:
    """Copy *stream* to *path*, capping file size while draining fully."""
    try:
        with path.open("wb") as out:
            while True:
                chunk = stream.read(_READ_CHUNK)
                if not chunk:
                    break
                if state.bytes_written < max_stream_bytes:
                    room = max_stream_bytes - state.bytes_written
                    if len(chunk) <= room:
                        out.write(chunk)
                        state.bytes_written += len(chunk)
                    else:
                        if room:
                            out.write(chunk[:room])
                            state.bytes_written += room
                        state.truncated = True
                else:
                    state.truncated = True
    except Exception as exc:  # noqa: BLE001 — surface to caller, avoid silent hang
        state.error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def _write_stdin(pipe: IO[bytes] | None, data: bytes | None, errors: list[str]) -> None:
    if pipe is None:
        return
    try:
        if data:
            pipe.write(data)
            pipe.flush()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"stdin write failed: {type(exc).__name__}: {exc}")
    finally:
        try:
            pipe.close()
        except Exception:  # noqa: BLE001
            pass


def _signal_process_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    """Deliver *sig* to the child's process group on POSIX, else to the child."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, sig)
            return
        except ProcessLookupError:
            return
        except OSError:
            # Fall back to signalling the direct child only.
            pass
    try:
        if sig in (getattr(signal, "SIGKILL", None), getattr(signal, "SIGTERM", None)):
            if sig == getattr(signal, "SIGKILL", object()):
                proc.kill()
            else:
                proc.terminate()
        else:
            proc.send_signal(sig)
    except ProcessLookupError:
        return
    except OSError:
        return


def _terminate_and_reap(proc: subprocess.Popen[bytes], grace_s: float) -> None:
    """Terminate the process group, escalate after *grace_s*, and always reap.

    On POSIX the group receives SIGTERM, then after the grace period SIGKILL is
    always delivered to the group. Escalation is not gated on the direct child
    exiting: a grandchild that ignores SIGTERM must still be reaped.
    """
    if proc.poll() is not None:
        # Direct child already gone; still try to clear any group leftovers on POSIX.
        if os.name == "posix":
            _signal_process_group(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=0)
        except Exception:  # noqa: BLE001
            pass
        return

    if os.name == "posix":
        _signal_process_group(proc, signal.SIGTERM)
    else:
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass

    deadline = time.monotonic() + grace_s
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_S)

    if os.name == "posix":
        # Always escalate: the direct child may have exited while descendants live.
        _signal_process_group(proc, signal.SIGKILL)
    elif proc.poll() is None:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass

    # Always attempt to reap the launched child.
    try:
        proc.wait(timeout=max(grace_s, 1.0))
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def _spawn_failure_result(started: float, error: str) -> ProcessResult:
    return ProcessResult(
        returncode=None,
        duration_s=time.perf_counter() - started,
        timed_out=False,
        interrupted=False,
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        error=error,
    )


def run_managed(
    command: Sequence[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str],
    stdout_path: Path,
    stderr_path: Path,
    timeout_s: float | None,
    grace_s: float = 3.0,
    max_stream_bytes: int = 50_000_000,
    stdin: bytes | None = None,
) -> ProcessResult:
    """Run *command* with bounded streaming logs and process-group cleanup.

    The child is launched via :class:`subprocess.Popen` (never ``shell=True``).
    On POSIX a new session/process group is created so timeout and interrupt
    cleanup can reach grandchildren. Stdout and stderr are drained concurrently
    to disk, writing at most *max_stream_bytes* per stream while still consuming
    any remaining data so the child cannot block on a full pipe.
    """
    _validate_limits(
        timeout_s=timeout_s,
        grace_s=grace_s,
        max_stream_bytes=max_stream_bytes,
    )

    started = time.perf_counter()
    stdout_path = Path(stdout_path)
    stderr_path = Path(stderr_path)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    if not command:
        return _spawn_failure_result(started, "command must be a non-empty sequence")

    popen_kwargs: dict = {
        "args": list(command),
        "cwd": str(cwd) if cwd is not None else None,
        "env": dict(env),
        "stdin": subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(**popen_kwargs)  # noqa: S603 — intentional argv exec
    except Exception as exc:  # noqa: BLE001 — structured spawn failure
        msg = str(exc).strip() or type(exc).__name__
        return _spawn_failure_result(started, msg)

    assert proc.stdout is not None
    assert proc.stderr is not None

    stdout_state = _StreamDrainState()
    stderr_state = _StreamDrainState()
    stdin_errors: list[str] = []

    stdout_thread = threading.Thread(
        target=_drain_stream,
        args=(proc.stdout, stdout_path, max_stream_bytes, stdout_state),
        name="run_managed-stdout",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_stream,
        args=(proc.stderr, stderr_path, max_stream_bytes, stderr_state),
        name="run_managed-stderr",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    stdin_thread: threading.Thread | None = None
    if stdin is not None:
        stdin_thread = threading.Thread(
            target=_write_stdin,
            args=(proc.stdin, stdin, stdin_errors),
            name="run_managed-stdin",
            daemon=True,
        )
        stdin_thread.start()

    timed_out = False
    interrupted = False
    wait_error: str | None = None

    try:
        if timeout_s is None:
            proc.wait()
        else:
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_and_reap(proc, grace_s)
    except KeyboardInterrupt:
        interrupted = True
        _terminate_and_reap(proc, grace_s)
    except Exception as exc:  # noqa: BLE001
        wait_error = f"{type(exc).__name__}: {exc}"
        _terminate_and_reap(proc, grace_s)

    # Ensure the child is reaped even on the happy path if wait already finished.
    if proc.poll() is None:
        _terminate_and_reap(proc, grace_s)

    returncode: int | None
    try:
        returncode = proc.wait(timeout=0)
    except Exception:  # noqa: BLE001
        returncode = proc.poll()

    if stdin_thread is not None:
        stdin_thread.join(timeout=max(grace_s, 1.0))
    stdout_thread.join(timeout=max(grace_s, 5.0))
    stderr_thread.join(timeout=max(grace_s, 5.0))

    errors: list[str] = []
    if wait_error:
        errors.append(wait_error)
    errors.extend(stdin_errors)
    if stdout_state.error:
        errors.append(f"stdout drain: {stdout_state.error}")
    if stderr_state.error:
        errors.append(f"stderr drain: {stderr_state.error}")
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        errors.append("stream drain thread(s) did not finish")

    error: str | None = "; ".join(errors) if errors else None
    if timed_out and error is None:
        # Timeout is signalled via timed_out; keep error free unless something else failed.
        pass

    return ProcessResult(
        returncode=returncode,
        duration_s=time.perf_counter() - started,
        timed_out=timed_out,
        interrupted=interrupted,
        stdout_bytes=stdout_state.bytes_written,
        stderr_bytes=stderr_state.bytes_written,
        stdout_truncated=stdout_state.truncated,
        stderr_truncated=stderr_state.truncated,
        error=error,
    )
