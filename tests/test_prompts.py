from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from basecamp_bench.prompts import build_evaluator_prompt, implementation_prompt_bytes


class ImplementationPromptTests(unittest.TestCase):
    def test_returns_exact_bytes_without_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.md"
            expected = b"Build the thing.\n\n"
            path.write_bytes(expected)
            actual = implementation_prompt_bytes(path)
            self.assertEqual(actual, expected)

    def test_rejects_empty_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.md"
            path.write_text(" \n", encoding="utf-8")
            with self.assertRaises(ValueError):
                implementation_prompt_bytes(path)
            with self.assertRaises(ValueError):
                implementation_prompt_bytes(Path(tmp) / "missing.md")


class EvaluatorPromptTests(unittest.TestCase):
    def test_contains_full_context_and_only_machine_required_shape(self) -> None:
        contract = {
            "schema_version": "1.0",
            "contract_version": "v1",
            "track": "fe",
            "description": "description",
            "dimensions": [
                {
                    "id": "craft",
                    "label": "Craft",
                    "weight": 1.0,
                    "anchors": {"0": "bad", "5": "mid", "10": "great"},
                }
            ],
            "overall_policy": {"method": "weighted_sum", "precision": 6, "missing": "invalidate"},
        }
        prompt = build_evaluator_prompt(
            track="fe",
            submission_id="submission-1",
            evaluator_id="eval-1",
            contract_sha256="a" * 64,
            contract=contract,
            rubric="Full rubric context.",
            seed_dir=Path("seed"),
            submission_dir=Path("submission"),
            report_path=Path("out/report.md"),
            result_path=Path("out/result.json"),
        )
        self.assertIn("Full rubric context.", prompt)
        self.assertIn('"craft"', prompt)
        self.assertIn("Do not compute an overall score", prompt)
        # Judges are instructed never to leak host paths; this sentence backs
        # the no-absolute-path property of shared judge outputs.
        self.assertIn("never include an absolute host path", prompt)
        self.assertNotIn("npm install", prompt)
        self.assertNotIn("prototype.html", prompt)
        self.assertNotIn("api.mjs", prompt)

    def test_rejects_contract_without_dimensions(self) -> None:
        with self.assertRaises(ValueError):
            build_evaluator_prompt(
                track="be",
                submission_id="s",
                evaluator_id="e",
                contract_sha256="b" * 64,
                contract={},
                rubric="rubric",
                seed_dir=Path("seed"),
                submission_dir=Path("submission"),
                report_path=Path("report"),
                result_path=Path("result"),
            )


if __name__ == "__main__":
    unittest.main()
