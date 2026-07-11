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
        self.run_root = Path(self.temp.name)
        self.lock_dir = self.run_root / ".basecamp-bench-locks"

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
            metadata = [path.read_text() for path in self.lock_dir.glob("lane-*.lock")]
            self.assertTrue(any('"lane":"pi-glm--fe"' in value for value in metadata))

    def test_partial_conflict_releases_earlier_sorted_lane(self) -> None:
        self.holder(("zeta", "fe"))
        with self.assertRaisesRegex(ValueError, "zeta--fe"):
            with acquire_lane_locks(self.run_root, [("alpha", "fe"), ("zeta", "fe")]):
                self.fail("partially overlapping lanes were admitted")
        with acquire_lane_locks(self.run_root, [("alpha", "fe")]):
            pass

    def test_process_termination_releases_lane(self) -> None:
        process = self.holder(("codex", "be"))
        process.kill()
        process.wait(timeout=5)
        with acquire_lane_locks(self.run_root, [("codex", "be")]):
            pass

    def test_lane_components_are_validated_before_path_creation(self) -> None:
        for lane in (("../escape", "fe"), ("grok", "/tmp"), ("a/b", "fe")):
            with self.assertRaisesRegex(ValueError, "lane (harness|track)"):
                with acquire_lane_locks(self.run_root, [lane]):
                    self.fail("unsafe lane was admitted")

    def test_delimiter_bearing_identifiers_do_not_alias(self) -> None:
        self.holder(("a--b", "c"))
        with acquire_lane_locks(self.run_root, [("a", "b--c")]):
            metadata = [path.read_text() for path in self.lock_dir.glob("lane-*.lock")]
            self.assertTrue(any('"lane":"a--b--c"' in value for value in metadata))
            self.assertEqual(len(metadata), 2)

    def test_rejects_preexisting_public_lock_directory(self) -> None:
        self.lock_dir.mkdir(mode=0o755)
        self.lock_dir.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "lock path is unsafe"):
            with acquire_lane_locks(self.run_root, [("grok", "fe")]):
                self.fail("unsafe lock directory was admitted")


if __name__ == "__main__":
    unittest.main()
