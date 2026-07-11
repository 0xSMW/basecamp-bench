"""Cross-check published JSON Schemas against current 1.0 artifact producers."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

try:
    import jsonschema
except ImportError:  # Runtime remains standard-library only.
    jsonschema = None

from basecamp_bench.leaderboard import Attempt, aggregate_attempts
from basecamp_bench.manifest import build_manifest
from tests.test_manifest import _minimal_manifest_kwargs

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipIf(jsonschema is None, "jsonschema is not installed")
class PublishedSchemaTests(unittest.TestCase):
    def schema(self, name: str) -> dict:
        return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))

    def validate(self, name: str, data: object) -> None:
        schema = self.schema(name)
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
            data
        )

    def rejected(self, name: str, data: object) -> None:
        with self.assertRaises(jsonschema.ValidationError):
            self.validate(name, data)

    def test_all_schemas_are_valid_draft_2020_12(self) -> None:
        paths = sorted((ROOT / "schemas").glob("*.json"))
        self.assertEqual(len(paths), 5)
        for path in paths:
            with self.subTest(path=path.name):
                jsonschema.Draft202012Validator.check_schema(
                    json.loads(path.read_text(encoding="utf-8"))
                )

    def test_checked_in_contracts_and_reference_pack(self) -> None:
        for track in ("fe", "be"):
            with self.subTest(track=track):
                self.validate(
                    "evaluation-contract.schema.json",
                    json.loads((ROOT / f"benchmarks/{track}/contract.json").read_text()),
                )
        self.validate(
            "reference-pack.schema.json",
            json.loads((ROOT / "benchmarks/reference-pack.json").read_text()),
        )

    def test_judge_result_shape_and_nested_rejection(self) -> None:
        contract = json.loads((ROOT / "benchmarks/fe/contract.json").read_text())
        result = {
            "schema_version": "1.0",
            "track": "fe",
            "submission_id": "sub-1",
            "contract_sha256": "a" * 64,
            "judge_id": "eval-sol",
            "summary": "Evidence-backed.",
            "dimensions": {
                dim["id"]: {"score": 8.0, "notes": "Good", "evidence": ["submission/file:1"]}
                for dim in contract["dimensions"]
            },
        }
        self.validate("judge-result.schema.json", result)
        bad = copy.deepcopy(result)
        bad["dimensions"][next(iter(bad["dimensions"]))]["score"] = 11
        self.rejected("judge-result.schema.json", bad)
        bad = copy.deepcopy(result)
        bad["dimensions"][next(iter(bad["dimensions"]))]["extra"] = True
        self.rejected("judge-result.schema.json", bad)

    def test_current_leaderboard_producer_shape(self) -> None:
        attempt = Attempt(
            run_id="run-1",
            submission_id="sub-1",
            repetition=1,
            track="fe",
            contract_version="1.0",
            contract_sha256="b" * 64,
            harness="codex",
            model_id="gpt-5.6-sol",
            display_name="Sol",
            implementation_success=True,
            evaluation_success=True,
            score=8.0,
            dimensions={"craft": 8.0},
            judge_spread=0.0,
            implementation_cost_usd=1.0,
            evaluation_cost_usd=0.2,
            tokens=100,
            duration_s=2.0,
            evaluator_ids=("eval-sol",),
            ineligible_reasons=(),
        )
        board = aggregate_attempts([attempt], mode="local", generated_at="2026-07-11T12:00:00Z")[0]
        self.validate("leaderboard.schema.json", board)
        bad = copy.deepcopy(board)
        bad["entries"][0]["raw_attempts"][0]["tokens"] = -1
        self.rejected("leaderboard.schema.json", bad)
        bad = copy.deepcopy(board)
        del bad["entries"][0]["score_mean"]
        self.rejected("leaderboard.schema.json", bad)
        bad = copy.deepcopy(board)
        bad["entries"][0]["duration_range_s"] = -1
        self.rejected("leaderboard.schema.json", bad)

    def test_current_manifest_producer_shape(self) -> None:
        manifest = build_manifest(**_minimal_manifest_kwargs())
        self.validate("run-manifest.schema.json", manifest)
        evaluation = dict(manifest["jobs"][0])
        evaluation.update(
            {
                "id": "evaluate-1",
                "kind": "evaluate",
                "evaluator_id": "eval-sol",
                "eval_attempt_id": "eval-attempt-1",
                "valid": True,
                "invalid_reasons": [],
            }
        )
        evaluated = copy.deepcopy(manifest)
        evaluated["jobs"] = [evaluation]
        self.validate("run-manifest.schema.json", evaluated)
        skipped = copy.deepcopy(manifest)
        skipped["jobs"] = [
            {
                "id": "skip-1",
                "kind": "evaluate",
                "harness": "codex",
                "track": "fe",
                "repetition": 1,
                "submission_id": "sub-1",
                "evaluator_id": "eval-sol",
                "skipped": True,
                "reason": "operator_disabled_evaluator",
            }
        ]
        self.validate("run-manifest.schema.json", skipped)
        bad = copy.deepcopy(manifest)
        del bad["jobs"][0]["duration_s"]
        self.rejected("run-manifest.schema.json", bad)
        bad = copy.deepcopy(manifest)
        bad["artifacts"] = {"logs/private.log": "a" * 64}
        self.rejected("run-manifest.schema.json", bad)
        bad = copy.deepcopy(manifest)
        bad["tooling"][0]["deterministic_seed"]["supported"] = True
        self.rejected("run-manifest.schema.json", bad)
        bad = copy.deepcopy(manifest)
        bad["tooling"][0]["version_error"] = "also set"
        self.rejected("run-manifest.schema.json", bad)
        bad = copy.deepcopy(manifest)
        bad["tooling"][0]["unexpected"] = True
        self.rejected("run-manifest.schema.json", bad)

    def test_reference_path_and_contract_nested_rejection(self) -> None:
        pack = json.loads((ROOT / "benchmarks/reference-pack.json").read_text())
        bad_pack = copy.deepcopy(pack)
        if bad_pack["assets"]:
            bad_pack["assets"][0]["path"] = "../escape"
            self.rejected("reference-pack.schema.json", bad_pack)
        contract = json.loads((ROOT / "benchmarks/fe/contract.json").read_text())
        bad_contract = copy.deepcopy(contract)
        bad_contract["overall_policy"]["extra"] = True
        self.rejected("evaluation-contract.schema.json", bad_contract)


if __name__ == "__main__":
    unittest.main()
