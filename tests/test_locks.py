from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from basecamp_bench.locks import acquire_lane_locks

_HOLDER = """
import sys
from pathlib import Path
from basecamp_bench.locks import acquire_lane_locks

root = Path(sys.argv[1])
lanes = [tuple(value.split(':', 1)) for value in sys.argv[2:]]
with acquire_lane_locks(root, lanes):
    print('ready', flush=True)
    sys.stdin.buffer.read(1)
"""


class LaneLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.run_root = Path(self.temp.name) / "runs"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def holder(self, *lanes: tuple[str, str]) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _HOLDER,
                str(self.run_root),
                *(f"{harness}:{track}" for harness, track in lanes),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._stop, process)
        assert process.stdout is not None
        self.assertEqual(process.stdout.readline().strip(), "ready")
        return process

    @staticmethod
    def _stop(process: subprocess.Popen[str]) -> None:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def test_overlapping_lane_reports_owner(self) -> None:
        process = self.holder(("grok", "fe"))
        with self.assertRaisesRegex(
            ValueError, rf"benchmark lane already active: grok--fe \(pid {process.pid},"
        ):
            with acquire_lane_locks(self.run_root, [("grok", "fe")]):
                self.fail("overlapping lane was admitted")

    def test_disjoint_lanes_can_run_concurrently(self) -> None:
        self.holder(("grok", "fe"), ("grok", "be"))
        with acquire_lane_locks(self.run_root, [("pi-glm", "fe"), ("pi-glm", "be")]):
            self.assertTrue((self.run_root / ".locks" / "pi-glm--fe.lock").is_file())

    def test_partial_conflict_releases_earlier_sorted_lane(self) -> None:
        self.holder(("zeta", "fe"))
        with self.assertRaisesRegex(ValueError, "zeta--fe"):
            with acquire_lane_locks(
                self.run_root, [("alpha", "fe"), ("zeta", "fe")]
            ):
                self.fail("partially overlapping lanes were admitted")
        with acquire_lane_locks(self.run_root, [("alpha", "fe")]):
            pass

    def test_process_termination_releases_lane(self) -> None:
        process = self.holder(("codex", "be"))
        process.kill()
        process.wait(timeout=5)
        with acquire_lane_locks(self.run_root, [("codex", "be")]):
            pass


if __name__ == "__main__":
    unittest.main()
