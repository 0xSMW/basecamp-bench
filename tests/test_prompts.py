from __future__ import annotations

import json
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
    def prompt(self, **overrides) -> str:
        kwargs = {
            "track": "fe",
            "submission_id": "s",
            "evaluator_id": "e",
            "contract_sha256": "b" * 64,
            "contract": {"dimensions": [{"id": "craft"}]},
            "rubric": "rubric",
            "seed_dir": Path("seed"),
            "submission_dir": Path("submission"),
            "report_path": Path("out/report.md"),
            "result_path": Path("out/result.json"),
        }
        kwargs.update(overrides)
        return build_evaluator_prompt(**kwargs)

    def test_browser_verification_and_evidence_contract(self) -> None:
        url = "http://127.0.0.1:8123/prototype.html"
        prompt = self.prompt(
            browser_url=url, browser_evidence_path=Path("out/browser-evidence.json")
        )
        self.assertIn("write only to the three output paths", prompt)
        self.assertIn("Browser evidence JSON path: out/browser-evidence.json", prompt)
        self.assertIn(f"Runner-served browser URL: {url}", prompt)
        self.assertIn("immutable submission snapshot", prompt)
        self.assertIn("must use cua_repl with Chrome", prompt)
        self.assertIn("inspect the served document before scoring", prompt)
        self.assertIn("Do not start another server", prompt)
        self.assertIn("headless browser", prompt)
        self.assertIn("browser file URLs", prompt)
        self.assertIn("1440px", prompt)
        self.assertIn("390px", prompt)
        self.assertIn("tests are not browser evidence", prompt)
        self.assertIn("set passed to false", prompt)
        self.assertIn("status 'blocked'", prompt)
        self.assertIn("STOP without writing numerical result.json", prompt)
        self.assertIn("never include an absolute host path in any output", prompt)
        decoder = json.JSONDecoder()
        evidence, _ = decoder.raw_decode(prompt[prompt.index('{\n  "schema_version"') :])
        self.assertEqual(evidence["url"], url)
        self.assertEqual(evidence["schema_version"], "1.0")
        self.assertIs(evidence["rendered"], True)
        self.assertEqual(evidence["status"], "complete")
        self.assertEqual(
            [shot["viewport"] for shot in evidence["screenshots"]], ["desktop", "mobile"]
        )
        self.assertTrue(all(shot["evidence"] for shot in evidence["screenshots"]))
        self.assertEqual(
            [item["action"] for item in evidence["interactions"]], ["click", "type", "reload"]
        )
        self.assertTrue(
            all(item["observed"] and item["passed"] is True for item in evidence["interactions"])
        )
        # Browser requirements do not add fields to the numerical result contract.
        result_start = prompt.index('{\n  "schema_version"', prompt.index("exact keys"))
        result, _ = decoder.raw_decode(prompt[result_start:])
        self.assertEqual(
            set(result),
            {
                "schema_version",
                "track",
                "submission_id",
                "contract_sha256",
                "judge_id",
                "dimensions",
                "summary",
            },
        )

    def test_product_rendering_defects_are_scored_and_environment_failures_block(self) -> None:
        prompt = self.prompt(
            browser_url="http://127.0.0.1:8123/",
            browser_evidence_path=Path("out/browser-evidence.json"),
        )
        self.assertIn("rendered=true means the actual served document was inspected", prompt)
        self.assertIn("not that the product works", prompt)
        self.assertIn("blank page or JavaScript error caused by the submission", prompt)
        self.assertIn("capture the actual page/error and score it with status 'complete'", prompt)
        self.assertIn("environmental browser/tool/setup failure", prompt)
        self.assertIn("prevents Chrome from loading or inspecting the served document", prompt)
        self.assertIn(
            "record the attempted action and the observed absence with passed=false", prompt
        )
        self.assertIn("Read the current CUA documentation", prompt)
        self.assertIn("do not guess API methods", prompt)
        self.assertNotIn("If browser access or rendering fails", prompt)

    def test_browser_parameters_are_paired_and_fe_only(self) -> None:
        for kwargs in (
            {"browser_url": "http://127.0.0.1:8123/"},
            {"browser_evidence_path": Path("out/browser-evidence.json")},
            {
                "track": "be",
                "browser_url": "http://127.0.0.1:8123/",
                "browser_evidence_path": Path("out/browser-evidence.json"),
            },
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.prompt(**kwargs)

    def test_browser_url_must_be_http(self) -> None:
        for url in ("", "prototype.html", "file:///tmp/prototype.html", "http:///missing-host"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.prompt(
                    browser_url=url, browser_evidence_path=Path("out/browser-evidence.json")
                )

    def test_default_fe_and_be_prompts_keep_original_output_boundary(self) -> None:
        for track in ("fe", "be"):
            with self.subTest(track=track):
                prompt = self.prompt(track=track)
                self.assertIn("write only to the two output paths", prompt)
                self.assertIn("never include an absolute host path in either output", prompt)
                self.assertNotIn("Browser evidence", prompt)
                self.assertNotIn("cua_repl", prompt)
                self.assertNotIn('"screenshots"', prompt)

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
