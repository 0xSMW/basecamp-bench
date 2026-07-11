from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from basecamp_bench.manifest import verify_run
from basecamp_bench.reporting import write_report

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "baseline"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BaselineTests(unittest.TestCase):
    def test_collection_manifest_and_report_are_reproducible(self) -> None:
        manifest = json.loads((BASELINE / "baseline-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["models"],
            [
                "claude-fable-5",
                "claude-sonnet-5",
                "gpt-5.5",
                "gpt-5.6-sol",
                "grok-4.5",
            ],
        )
        self.assertEqual(manifest["tracks"], ["be", "fe"])

        for run in manifest["runs"]:
            run_dir = BASELINE / run["path"]
            self.assertEqual(verify_run(run_dir), [])
            self.assertEqual(
                _sha256(run_dir / "run-manifest.json"),
                run["run_manifest_sha256"],
            )

        self.assertEqual(_sha256(BASELINE / "report.html"), manifest["report_sha256"])
        with tempfile.TemporaryDirectory() as tmp:
            regenerated = write_report(
                sorted((BASELINE / "runs").rglob("leaderboard_*.json")),
                Path(tmp) / "report.html",
            )
            self.assertEqual(regenerated.read_bytes(), (BASELINE / "report.html").read_bytes())

    def test_only_accepted_model_results_are_published(self) -> None:
        observed: set[str] = set()
        rows = 0
        for path in (BASELINE / "runs").rglob("leaderboard_*.json"):
            leaderboard = json.loads(path.read_text(encoding="utf-8"))
            for entry in leaderboard["entries"]:
                observed.add(entry["model_id"])
                rows += 1
        self.assertEqual(
            observed,
            {
                "claude-fable-5",
                "claude-sonnet-5",
                "gpt-5.5",
                "gpt-5.6-sol",
                "grok-4.5",
            },
        )
        self.assertEqual(rows, 10)


if __name__ == "__main__":
    unittest.main()
