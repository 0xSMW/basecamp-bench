"""Tests for basecamp_bench.processes.run_managed."""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from basecamp_bench.processes import ProcessResult, run_managed


def _env() -> dict[str, str]:
    return os.environ.copy()


def _write_script(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o700)
    return path


def _poll_until(predicate, *, timeout_s: float = 2.0, interval_s: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class RunManagedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.stdout_path = self.root / "logs" / "out.log"
        self.stderr_path = self.root / "logs" / "err.log"

    def _run(self, command, **kwargs) -> ProcessResult:
        defaults = dict(
            cwd=self.root,
            env=_env(),
            stdout_path=self.stdout_path,
            stderr_path=self.stderr_path,
            timeout_s=5.0,
            grace_s=1.0,
            max_stream_bytes=50_000_000,
        )
        defaults.update(kwargs)
        return run_managed(command, **defaults)

    def test_simultaneous_stdout_and_stderr_capture(self) -> None:
        script = _write_script(
            self.root,
            "both_streams.py",
            """\
            import sys
            sys.stdout.write("OUT-LINE\\n")
            sys.stdout.flush()
            sys.stderr.write("ERR-LINE\\n")
            sys.stderr.flush()
            """,
        )
        result = self._run([sys.executable, str(script)])
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.interrupted)
        self.assertIsNone(result.error)
        self.assertTrue(self.stdout_path.exists())
        self.assertTrue(self.stderr_path.exists())
        self.assertIn(b"OUT-LINE", self.stdout_path.read_bytes())
        self.assertIn(b"ERR-LINE", self.stderr_path.read_bytes())
        self.assertGreater(result.stdout_bytes, 0)
        self.assertGreater(result.stderr_bytes, 0)
        self.assertFalse(result.stdout_truncated)
        self.assertFalse(result.stderr_truncated)

    def test_independent_per_stream_truncation_while_child_completes(self) -> None:
        # Produce more than the cap on both streams; limits apply independently.
        script = _write_script(
            self.root,
            "truncate_streams.py",
            """\
            import sys
            # 200 bytes stdout, 120 bytes stderr
            sys.stdout.buffer.write(b"A" * 200)
            sys.stdout.buffer.flush()
            sys.stderr.buffer.write(b"B" * 120)
            sys.stderr.buffer.flush()
            """,
        )
        out_limit = 50
        # Use separate limits by running once with max that caps both, asserting
        # each file is independently truncated to the shared max and the child
        # still exits 0 (pipes continue to drain after the file cap).
        result = self._run(
            [sys.executable, str(script)],
            max_stream_bytes=out_limit,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.error)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)
        self.assertEqual(result.stdout_bytes, out_limit)
        self.assertEqual(result.stderr_bytes, out_limit)
        self.assertEqual(len(self.stdout_path.read_bytes()), out_limit)
        self.assertEqual(len(self.stderr_path.read_bytes()), out_limit)
        self.assertEqual(self.stdout_path.read_bytes(), b"A" * out_limit)
        self.assertEqual(self.stderr_path.read_bytes(), b"B" * out_limit)

        # Prove independent flags: only stdout exceeds a higher cap for stderr.
        self.stdout_path.unlink(missing_ok=True)
        self.stderr_path.unlink(missing_ok=True)
        script2 = _write_script(
            self.root,
            "truncate_stdout_only.py",
            """\
            import sys
            sys.stdout.buffer.write(b"X" * 80)
            sys.stdout.buffer.flush()
            sys.stderr.buffer.write(b"Y" * 10)
            sys.stderr.buffer.flush()
            """,
        )
        result2 = self._run(
            [sys.executable, str(script2)],
            max_stream_bytes=40,
        )
        self.assertEqual(result2.returncode, 0)
        self.assertTrue(result2.stdout_truncated)
        self.assertFalse(result2.stderr_truncated)
        self.assertEqual(result2.stdout_bytes, 40)
        self.assertEqual(result2.stderr_bytes, 10)
        self.assertEqual(len(self.stdout_path.read_bytes()), 40)
        self.assertEqual(len(self.stderr_path.read_bytes()), 10)

    def test_spawn_error_structured_result(self) -> None:
        missing = self.root / "definitely-does-not-exist-binary"
        result = self._run([str(missing), "arg"])
        self.assertIsNone(result.returncode)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.interrupted)
        self.assertIsNotNone(result.error)
        self.assertIsInstance(result.error, str)
        self.assertGreater(len(result.error or ""), 0)
        self.assertEqual(result.stdout_bytes, 0)
        self.assertEqual(result.stderr_bytes, 0)
        self.assertFalse(result.stdout_truncated)
        self.assertFalse(result.stderr_truncated)
        self.assertIsInstance(result.duration_s, float)
        self.assertGreaterEqual(result.duration_s, 0.0)

    def test_pre_cancelled_run_never_spawns(self) -> None:
        cancel = threading.Event()
        cancel.set()
        result = self._run(
            [str(self.root / "missing-binary-that-must-not-be-spawned")],
            cancel_event=cancel,
        )
        self.assertTrue(result.interrupted)
        self.assertFalse(result.timed_out)
        self.assertIsNone(result.returncode)
        self.assertIsNone(result.error)

    def test_nonzero_exit_code_preserved(self) -> None:
        script = _write_script(
            self.root,
            "fail.py",
            """\
            import sys
            sys.stderr.write("boom\\n")
            sys.exit(17)
            """,
        )
        result = self._run([sys.executable, str(script)])
        self.assertEqual(result.returncode, 17)
        self.assertFalse(result.timed_out)
        self.assertIsNone(result.error)
        self.assertIn(b"boom", self.stderr_path.read_bytes())

    def test_timeout_kills_posix_grandchild(self) -> None:
        if os.name != "posix":
            self.skipTest("process-group grandchild cleanup is POSIX-specific")

        marker = self.root / "grandchild.pid"
        # Child stays alive past timeout and forks a grandchild that ignores SIGTERM
        # so only process-group SIGKILL proves full cleanup.
        sticky = _write_script(
            self.root,
            "sticky_parent.py",
            f"""\
            import os
            import signal
            import time

            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            pid = os.fork()
            if pid == 0:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                with open({str(marker)!r}, "w", encoding="utf-8") as fh:
                    fh.write(str(os.getpid()))
                    fh.flush()
                while True:
                    time.sleep(1)
            # Parent remains alive so run_managed hits timeout with a live group.
            # Brief wait so the grandchild can write its pid marker first.
            time.sleep(0.05)
            while True:
                time.sleep(1)
            """,
        )
        # Wait until the grandchild publishes its pid, using a deadline rather than sleep.
        # We start the managed run with a timeout long enough for the marker write.
        result = self._run(
            [sys.executable, str(sticky)],
            timeout_s=0.4,
            grace_s=0.5,
        )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.interrupted)
        # Child was spawned and reaped; returncode is whatever the OS reported.
        self.assertIsNotNone(result.returncode)

        self.assertTrue(
            marker.exists(),
            "grandchild did not write pid marker (test setup race)",
        )
        grandchild_pid = int(marker.read_text(encoding="utf-8").strip())
        gone = _poll_until(lambda: not _pid_alive(grandchild_pid), timeout_s=2.0)
        self.assertTrue(
            gone,
            f"grandchild pid {grandchild_pid} still alive after run_managed returned",
        )
        self.assertFalse(
            _pid_alive(grandchild_pid),
            f"grandchild pid {grandchild_pid} still alive after run_managed returned",
        )

    def test_cancel_event_kills_posix_grandchild(self) -> None:
        if os.name != "posix":
            self.skipTest("process-group grandchild cleanup is POSIX-specific")

        marker = self.root / "cancelled-grandchild.pid"
        sticky = _write_script(
            self.root,
            "cancel_sticky_parent.py",
            f"""\
            import os
            import signal
            import time

            pid = os.fork()
            if pid == 0:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                with open({str(marker)!r}, "w", encoding="utf-8") as fh:
                    fh.write(str(os.getpid()))
                    fh.flush()
                while True:
                    time.sleep(1)
            while True:
                time.sleep(1)
            """,
        )
        cancel = threading.Event()
        outcome: dict[str, ProcessResult] = {}

        def invoke() -> None:
            outcome["result"] = self._run(
                [sys.executable, str(sticky)],
                timeout_s=30.0,
                grace_s=0.5,
                cancel_event=cancel,
            )

        worker = threading.Thread(target=invoke, name="cancel-run-managed-test")
        worker.start()
        self.assertTrue(_poll_until(marker.exists, timeout_s=2.0))
        cancel.set()
        worker.join(timeout=4.0)
        self.assertFalse(worker.is_alive())
        result = outcome["result"]
        self.assertTrue(result.interrupted)
        self.assertFalse(result.timed_out)
        grandchild_pid = int(marker.read_text(encoding="utf-8").strip())
        self.assertTrue(_poll_until(lambda: not _pid_alive(grandchild_pid), timeout_s=2.0))

    def test_creates_log_parent_directories(self) -> None:
        deep_out = self.root / "a" / "b" / "c" / "stdout.log"
        deep_err = self.root / "a" / "b" / "c" / "stderr.log"
        result = self._run(
            [sys.executable, "-c", "print('hi')"],
            stdout_path=deep_out,
            stderr_path=deep_err,
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(deep_out.is_file())
        self.assertTrue(deep_err.is_file())

    def test_invalid_limits_raise(self) -> None:
        with self.assertRaises(ValueError):
            self._run([sys.executable, "-c", "pass"], max_stream_bytes=-1)
        with self.assertRaises(ValueError):
            self._run([sys.executable, "-c", "pass"], grace_s=-0.1)
        with self.assertRaises(ValueError):
            self._run([sys.executable, "-c", "pass"], timeout_s=-1)


if __name__ == "__main__":
    unittest.main()
