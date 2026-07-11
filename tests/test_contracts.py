"""Tests for basecamp_bench.contracts."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType

from basecamp_bench.contracts import (
    Dimension,
    EvaluationContract,
    aggregate_judges,
    aggregate_repetitions,
    compute_weighted_score,
    contract_sha256,
    load_contract,
    validate_contract_data,
    validate_judge_result,
)


def _anchors() -> dict[str, str]:
    return {"0": "absent", "5": "partial", "10": "complete"}


def _valid_contract_dict(
    *,
    track: str = "fe",
    precision: int = 6,
    one_dimension: bool = False,
) -> dict:
    if one_dimension:
        dimensions = [
            {
                "id": "craft",
                "label": "Craft",
                "weight": 1.0,
                "anchors": _anchors(),
            }
        ]
    else:
        dimensions = [
            {
                "id": "craft",
                "label": "Craft",
                "weight": 0.6,
                "anchors": _anchors(),
            },
            {
                "id": "depth",
                "label": "Depth",
                "weight": 0.4,
                "anchors": _anchors(),
            },
        ]
    return {
        "schema_version": "1.0",
        "contract_version": "2026-07-11.2",
        "track": track,
        "description": "Test contract",
        "dimensions": dimensions,
        "overall_policy": {
            "method": "weighted_sum",
            "precision": precision,
            "missing": "invalidate",
        },
    }


def _write_contract(directory: Path, data: dict, name: str = "contract.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _judge_result(
    *,
    scores: dict[str, float],
    track: str = "fe",
    submission_id: str = "sub-1",
    contract_sha256_value: str = "a" * 64,
    judge_id: str = "judge-a",
    schema_version: str = "1.0",
    summary: str = "Looks solid.",
) -> dict:
    dimensions = {}
    for dim_id, score in scores.items():
        dimensions[dim_id] = {
            "score": score,
            "notes": f"notes for {dim_id}",
            "evidence": [f"evidence-{dim_id}"],
        }
    return {
        "schema_version": schema_version,
        "track": track,
        "submission_id": submission_id,
        "contract_sha256": contract_sha256_value,
        "judge_id": judge_id,
        "dimensions": dimensions,
        "summary": summary,
    }


def _is_target_shape_contract(raw: dict) -> bool:
    """True when a checked-in contract already matches the simplified shape."""
    if not isinstance(raw, dict):
        return False
    if "scenarios" in raw:
        return False
    expected_root = {
        "schema_version",
        "contract_version",
        "track",
        "description",
        "dimensions",
        "overall_policy",
    }
    if set(raw.keys()) != expected_root:
        return False
    dimensions = raw.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        return False
    for dim in dimensions:
        if not isinstance(dim, dict):
            return False
        if "owner" in dim:
            return False
        if set(dim.keys()) != {"id", "label", "weight", "anchors"}:
            return False
    return True


class ContractValidationTests(unittest.TestCase):
    def test_valid_contract_has_no_errors(self) -> None:
        errors = validate_contract_data(_valid_contract_dict())
        self.assertEqual(errors, [])

    def test_schema_version_is_fixed(self) -> None:
        data = _valid_contract_dict()
        data["schema_version"] = "2.0"
        self.assertIn(
            "contract.schema_version: expected exactly '1.0'",
            validate_contract_data(data),
        )

    def test_one_dimension_contract(self) -> None:
        errors = validate_contract_data(_valid_contract_dict(one_dimension=True))
        self.assertEqual(errors, [])

    def test_unknown_and_missing_root_keys(self) -> None:
        data = _valid_contract_dict()
        del data["description"]
        data["extra"] = "nope"
        errors = validate_contract_data(data)
        self.assertTrue(any("missing key 'description'" in e for e in errors))
        self.assertTrue(any("unknown key 'extra'" in e for e in errors))

    def test_scenarios_root_key_rejected(self) -> None:
        data = _valid_contract_dict()
        data["scenarios"] = []
        errors = validate_contract_data(data)
        self.assertTrue(any("unknown key 'scenarios'" in e for e in errors))

    def test_dimension_owner_key_rejected(self) -> None:
        data = _valid_contract_dict()
        data["dimensions"][0]["owner"] = "judge"
        errors = validate_contract_data(data)
        self.assertTrue(any("unknown key 'owner'" in e for e in errors))

    def test_unsafe_identifiers(self) -> None:
        data = _valid_contract_dict()
        data["dimensions"][0]["id"] = "Bad_ID"
        errors = validate_contract_data(data)
        self.assertTrue(any("dimensions[0].id" in e for e in errors))

        data = _valid_contract_dict()
        data["dimensions"][0]["id"] = ""
        errors = validate_contract_data(data)
        self.assertTrue(any("dimensions[0].id" in e for e in errors))

    def test_duplicate_dimension_ids(self) -> None:
        data = _valid_contract_dict()
        data["dimensions"].append(deepcopy(data["dimensions"][0]))
        data["dimensions"][-1]["weight"] = 0.01
        data["dimensions"][0]["weight"] = 0.59
        errors = validate_contract_data(data)
        self.assertTrue(any("duplicate dimension id" in e for e in errors))

    def test_invalid_boolean_nan_and_infinity_weights(self) -> None:
        data = _valid_contract_dict()
        data["dimensions"][0]["weight"] = True
        errors = validate_contract_data(data)
        self.assertTrue(any("weight" in e and "bool" in e for e in errors))

        data = _valid_contract_dict()
        data["dimensions"][0]["weight"] = float("nan")
        errors = validate_contract_data(data)
        self.assertTrue(any("weight" in e for e in errors))

        data = _valid_contract_dict()
        data["dimensions"][0]["weight"] = float("inf")
        errors = validate_contract_data(data)
        self.assertTrue(any("weight" in e for e in errors))

        data = _valid_contract_dict()
        data["dimensions"][0]["weight"] = 0
        errors = validate_contract_data(data)
        self.assertTrue(any("weight" in e for e in errors))

    def test_weights_not_summing_to_one(self) -> None:
        data = _valid_contract_dict()
        data["dimensions"][0]["weight"] = 0.9
        errors = validate_contract_data(data)
        self.assertTrue(any("sum to 1.0" in e for e in errors))

    def test_malformed_anchors(self) -> None:
        data = _valid_contract_dict()
        data["dimensions"][0]["anchors"] = {"0": "a", "5": "b"}
        errors = validate_contract_data(data)
        self.assertTrue(any("anchors" in e for e in errors))

        data = _valid_contract_dict()
        data["dimensions"][0]["anchors"] = {"0": "", "5": "b", "10": "c"}
        errors = validate_contract_data(data)
        self.assertTrue(any("anchors" in e for e in errors))

        data = _valid_contract_dict()
        data["dimensions"][0]["anchors"] = {"0": "a", "5": "b", "10": "c", "7": "x"}
        errors = validate_contract_data(data)
        self.assertTrue(any("unknown key '7'" in e for e in errors))

    def test_unknown_dimension_keys(self) -> None:
        data = _valid_contract_dict()
        data["dimensions"][0]["hint"] = "x"
        errors = validate_contract_data(data)
        self.assertTrue(any("unknown key 'hint'" in e for e in errors))

    def test_track_must_be_fe_or_be(self) -> None:
        data = _valid_contract_dict()
        data["track"] = "FE"
        errors = validate_contract_data(data)
        self.assertTrue(any("track" in e for e in errors))

    def test_overall_policy_shape(self) -> None:
        data = _valid_contract_dict()
        data["overall_policy"]["precision"] = True
        errors = validate_contract_data(data)
        self.assertTrue(any("precision" in e for e in errors))

        data = _valid_contract_dict()
        data["overall_policy"]["method"] = "mean"
        errors = validate_contract_data(data)
        self.assertTrue(any("method" in e for e in errors))

        data = _valid_contract_dict()
        data["overall_policy"]["missing"] = "zero"
        errors = validate_contract_data(data)
        self.assertTrue(any("missing" in e for e in errors))

        data = _valid_contract_dict()
        data["overall_policy"]["precision"] = 13
        errors = validate_contract_data(data)
        self.assertTrue(any("precision" in e for e in errors))

    def test_malformed_root_never_throws(self) -> None:
        self.assertIsInstance(validate_contract_data(None), list)
        self.assertIsInstance(validate_contract_data("nope"), list)
        self.assertIsInstance(validate_contract_data([]), list)


class LoadAndHashTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

    def test_load_contract_and_nested_immutability(self) -> None:
        path = _write_contract(self.root, _valid_contract_dict())
        contract = load_contract(path)
        self.assertIsInstance(contract, EvaluationContract)
        self.assertIsInstance(contract.dimensions[0], Dimension)
        self.assertEqual(contract.track, "fe")
        self.assertEqual(len(contract.dimensions), 2)
        self.assertFalse(hasattr(contract, "scenarios"))
        self.assertFalse(hasattr(contract.dimensions[0], "owner"))
        self.assertIsInstance(contract.dimensions[0].anchors, MappingProxyType)
        self.assertIsInstance(contract.overall_policy, MappingProxyType)

        with self.assertRaises(TypeError):
            contract.dimensions[0].anchors["0"] = "mutated"  # type: ignore[index]
        with self.assertRaises(TypeError):
            contract.overall_policy["method"] = "other"  # type: ignore[index]

    def test_load_one_dimension_contract(self) -> None:
        path = _write_contract(self.root, _valid_contract_dict(one_dimension=True))
        contract = load_contract(path)
        self.assertEqual(len(contract.dimensions), 1)
        self.assertEqual(contract.dimensions[0].id, "craft")
        self.assertEqual(contract.dimensions[0].weight, 1.0)

    def test_load_contract_raises_with_all_errors(self) -> None:
        data = _valid_contract_dict()
        data["track"] = "XX"
        data["dimensions"][0]["weight"] = -1
        path = _write_contract(self.root, data)
        with self.assertRaises(ValueError) as ctx:
            load_contract(path)
        message = str(ctx.exception)
        self.assertIn("track", message)
        self.assertIn("weight", message)

    def test_contract_sha256_matches_file_bytes(self) -> None:
        path = _write_contract(self.root, _valid_contract_dict())
        raw = path.read_bytes()
        expected = hashlib.sha256(raw).hexdigest()
        self.assertEqual(contract_sha256(path), expected)
        self.assertEqual(contract_sha256(path), expected.lower())

    def test_checked_in_contracts_load_if_migrated(self) -> None:
        """Load each checked-in track contract on the target simplified shape."""
        repo_root = Path(__file__).resolve().parents[1]
        expected_counts = {"fe": 11, "be": 9}
        for track, dim_count in expected_counts.items():
            path = repo_root / "benchmarks" / track / "contract.json"
            if not path.is_file():
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not _is_target_shape_contract(raw):
                continue
            contract = load_contract(path)
            self.assertEqual(contract.track, track)
            self.assertEqual(len(contract.dimensions), dim_count)
            self.assertAlmostEqual(sum(d.weight for d in contract.dimensions), 1.0)
            self.assertTrue(all(not hasattr(d, "owner") for d in contract.dimensions))


class JudgeResultValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        path = _write_contract(Path(self._tmpdir.name), _valid_contract_dict())
        self.contract = load_contract(path)
        self.hash = contract_sha256(path)
        self.base_scores = {"craft": 8.0, "depth": 7.0}

    def _validate(self, data, **kwargs):
        defaults = dict(
            expected_track="fe",
            expected_submission_id="sub-1",
            expected_contract_sha256=self.hash,
            expected_judge_id="judge-a",
        )
        defaults.update(kwargs)
        return validate_judge_result(data, self.contract, **defaults)

    def test_valid_judge_result(self) -> None:
        result = _judge_result(scores=self.base_scores, contract_sha256_value=self.hash)
        self.assertEqual(self._validate(result), [])

    def test_missing_and_extra_dimensions(self) -> None:
        result = _judge_result(
            scores={"craft": 8.0, "invented": 9.0},
            contract_sha256_value=self.hash,
        )
        errors = self._validate(result)
        joined = "\n".join(errors)
        self.assertIn("missing dimension 'depth'", joined)
        self.assertIn("unknown dimension 'invented'", joined)

    def test_all_dimensions_required(self) -> None:
        result = _judge_result(
            scores={"craft": 8.0},
            contract_sha256_value=self.hash,
        )
        errors = self._validate(result)
        self.assertTrue(any("missing dimension 'depth'" in e for e in errors))

    def test_bool_nan_infinity_and_out_of_range_scores(self) -> None:
        result = _judge_result(scores=self.base_scores, contract_sha256_value=self.hash)
        result["dimensions"]["craft"]["score"] = True
        errors = self._validate(result)
        self.assertTrue(any("score" in e and "bool" in e for e in errors))

        result = _judge_result(scores=self.base_scores, contract_sha256_value=self.hash)
        result["dimensions"]["craft"]["score"] = float("nan")
        errors = self._validate(result)
        self.assertTrue(any("score" in e for e in errors))

        result = _judge_result(scores=self.base_scores, contract_sha256_value=self.hash)
        result["dimensions"]["craft"]["score"] = float("inf")
        errors = self._validate(result)
        self.assertTrue(any("score" in e for e in errors))

        result = _judge_result(scores=self.base_scores, contract_sha256_value=self.hash)
        result["dimensions"]["craft"]["score"] = 11.0
        errors = self._validate(result)
        self.assertTrue(any("score" in e for e in errors))

        result = _judge_result(scores=self.base_scores, contract_sha256_value=self.hash)
        result["dimensions"]["craft"]["score"] = -0.1
        errors = self._validate(result)
        self.assertTrue(any("score" in e for e in errors))

    def test_malformed_notes_and_evidence(self) -> None:
        result = _judge_result(scores=self.base_scores, contract_sha256_value=self.hash)
        result["dimensions"]["craft"]["notes"] = ""
        result["dimensions"]["depth"]["evidence"] = []
        errors = self._validate(result)
        joined = "\n".join(errors)
        self.assertIn("notes", joined)
        self.assertIn("evidence", joined)

        result = _judge_result(scores=self.base_scores, contract_sha256_value=self.hash)
        result["dimensions"]["craft"]["evidence"] = ["ok", ""]
        errors = self._validate(result)
        self.assertTrue(any("evidence" in e for e in errors))

    def test_incorrect_identity_and_hash(self) -> None:
        result = _judge_result(
            scores=self.base_scores,
            track="be",
            submission_id="other",
            contract_sha256_value="b" * 64,
            judge_id="judge-b",
        )
        errors = self._validate(result)
        joined = "\n".join(errors)
        self.assertIn("track", joined)
        self.assertIn("submission_id", joined)
        self.assertIn("contract_sha256", joined)
        self.assertIn("judge_id", joined)

    def test_schema_version_must_match_contract(self) -> None:
        result = _judge_result(
            scores=self.base_scores,
            contract_sha256_value=self.hash,
            schema_version="2.0",
        )
        errors = self._validate(result)
        self.assertTrue(any("schema_version" in e for e in errors))

    def test_contract_sha256_must_be_lowercase_hex(self) -> None:
        result = _judge_result(
            scores=self.base_scores,
            contract_sha256_value="A" * 64,
        )
        errors = self._validate(
            result,
            expected_contract_sha256="A" * 64,
        )
        self.assertTrue(any("contract_sha256" in e and "hex" in e for e in errors))

    def test_submission_and_judge_id_regex(self) -> None:
        result = _judge_result(
            scores=self.base_scores,
            contract_sha256_value=self.hash,
            submission_id="Bad ID",
            judge_id="Judge!",
        )
        errors = self._validate(
            result,
            expected_submission_id="Bad ID",
            expected_judge_id="Judge!",
        )
        joined = "\n".join(errors)
        self.assertIn("submission_id", joined)
        self.assertIn("judge_id", joined)

    def test_malformed_input_never_throws(self) -> None:
        self.assertIsInstance(self._validate(None), list)
        self.assertIsInstance(self._validate("nope"), list)
        self.assertIsInstance(self._validate([]), list)


class ScoringAndAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        path = _write_contract(Path(self._tmpdir.name), _valid_contract_dict())
        self.contract = load_contract(path)
        path_one = _write_contract(
            Path(self._tmpdir.name),
            _valid_contract_dict(one_dimension=True),
            name="one_dim.json",
        )
        self.one_dim = load_contract(path_one)

    def test_compute_weighted_score_exact_rounding(self) -> None:
        # 8*0.6 + 7*0.4 = 4.8 + 2.8 = 7.6
        score = compute_weighted_score({"craft": 8, "depth": 7}, self.contract)
        self.assertEqual(score, 7.6)
        self.assertEqual(score, round(7.6, 6))

        # force six-decimal path: 8.1*0.6 + 7.2*0.4
        expected = round(8.1 * 0.6 + 7.2 * 0.4, 6)
        actual = compute_weighted_score({"craft": 8.1, "depth": 7.2}, self.contract)
        self.assertEqual(actual, expected)

    def test_compute_weighted_score_one_dimension(self) -> None:
        score = compute_weighted_score({"craft": 9.5}, self.one_dim)
        self.assertEqual(score, 9.5)

    def test_compute_weighted_score_rejects_extra_missing_invalid(self) -> None:
        with self.assertRaises(ValueError):
            compute_weighted_score({"craft": 8}, self.contract)
        with self.assertRaises(ValueError):
            compute_weighted_score(
                {"craft": 8, "depth": 7, "extra": 1},
                self.contract,
            )
        with self.assertRaises(ValueError):
            compute_weighted_score({"craft": True, "depth": 7}, self.contract)
        with self.assertRaises(ValueError):
            compute_weighted_score(
                {"craft": float("nan"), "depth": 7},
                self.contract,
            )
        with self.assertRaises(ValueError):
            compute_weighted_score(
                {"craft": float("inf"), "depth": 7},
                self.contract,
            )
        with self.assertRaises(ValueError):
            compute_weighted_score({"craft": 11, "depth": 7}, self.contract)
        with self.assertRaises(ValueError):
            compute_weighted_score({"craft": -1, "depth": 7}, self.contract)

    def test_aggregate_judges_median_stdev_min_max_and_overall(self) -> None:
        results = [
            {
                "judge_id": "judge-a",
                "dimensions": {
                    "craft": {"score": 6.0},
                    "depth": {"score": 8.0},
                },
            },
            {
                "judge_id": "judge-b",
                "dimensions": {
                    "craft": {"score": 8.0},
                    "depth": {"score": 4.0},
                },
            },
            {
                "judge_id": "judge-c",
                "dimensions": {
                    "craft": {"score": 10.0},
                    "depth": {"score": 6.0},
                },
            },
        ]
        agg = aggregate_judges(results, self.contract)

        self.assertEqual(
            set(agg.keys()),
            {"dimensions", "judges", "overall"},
        )
        self.assertEqual(set(agg["dimensions"].keys()), {"craft", "depth"})
        for dim_stats in agg["dimensions"].values():
            self.assertEqual(
                set(dim_stats.keys()),
                {"median", "stdev", "min", "max"},
            )

        craft = agg["dimensions"]["craft"]
        self.assertEqual(craft["median"], 8.0)
        self.assertEqual(craft["min"], 6.0)
        self.assertEqual(craft["max"], 10.0)
        self.assertAlmostEqual(craft["stdev"], math.sqrt(8 / 3), places=12)

        depth = agg["dimensions"]["depth"]
        self.assertEqual(depth["median"], 6.0)
        self.assertEqual(depth["min"], 4.0)
        self.assertEqual(depth["max"], 8.0)
        self.assertAlmostEqual(depth["stdev"], math.sqrt(8 / 3), places=12)

        # overall from medians: 8*0.6 + 6*0.4 = 4.8 + 2.4 = 7.2
        self.assertEqual(agg["overall"], 7.2)

        self.assertEqual(len(agg["judges"]), 3)
        self.assertEqual(agg["judges"][0]["judge_id"], "judge-a")
        self.assertEqual(
            agg["judges"][0]["scores"],
            {"craft": 6.0, "depth": 8.0},
        )
        # 6*0.6 + 8*0.4 = 3.6 + 3.2 = 6.8
        self.assertEqual(agg["judges"][0]["overall"], 6.8)
        self.assertEqual(
            agg["judges"][0]["overall"],
            compute_weighted_score(agg["judges"][0]["scores"], self.contract),
        )
        # judge-b: 8*0.6 + 4*0.4 = 4.8 + 1.6 = 6.4
        self.assertEqual(agg["judges"][1]["overall"], 6.4)
        # judge-c: 10*0.6 + 6*0.4 = 6.0 + 2.4 = 8.4
        self.assertEqual(agg["judges"][2]["overall"], 8.4)

    def test_aggregate_judges_one_dimension(self) -> None:
        results = [
            {"judge_id": "j1", "dimensions": {"craft": 4.0}},
            {"judge_id": "j2", "dimensions": {"craft": 10.0}},
        ]
        agg = aggregate_judges(results, self.one_dim)
        self.assertEqual(agg["dimensions"]["craft"]["median"], 7.0)
        self.assertEqual(agg["dimensions"]["craft"]["min"], 4.0)
        self.assertEqual(agg["dimensions"]["craft"]["max"], 10.0)
        self.assertEqual(agg["overall"], 7.0)
        self.assertEqual(agg["judges"][0]["overall"], 4.0)
        self.assertEqual(agg["judges"][1]["overall"], 10.0)

    def test_aggregate_judges_rejects_duplicate_judge_ids(self) -> None:
        results = [
            {
                "judge_id": "judge-a",
                "dimensions": {"craft": 8.0, "depth": 7.0},
            },
            {
                "judge_id": "judge-a",
                "dimensions": {"craft": 9.0, "depth": 6.0},
            },
        ]
        with self.assertRaises(ValueError) as ctx:
            aggregate_judges(results, self.contract)
        self.assertIn("duplicate judge id", str(ctx.exception))

    def test_aggregate_judges_requires_nonempty_and_complete(self) -> None:
        with self.assertRaises(ValueError):
            aggregate_judges([], self.contract)
        with self.assertRaises(ValueError):
            aggregate_judges(
                [{"judge_id": "j1", "dimensions": {"craft": 8.0}}],
                self.contract,
            )
        with self.assertRaises(ValueError):
            aggregate_judges(
                [
                    {
                        "judge_id": "j1",
                        "dimensions": {
                            "craft": 8.0,
                            "depth": 7.0,
                            "extra": 1.0,
                        },
                    }
                ],
                self.contract,
            )
        with self.assertRaises(ValueError):
            aggregate_judges(
                [
                    {
                        "judge_id": "j1",
                        "dimensions": {"craft": True, "depth": 7.0},
                    }
                ],
                self.contract,
            )

    def test_aggregate_judges_accepts_bare_scores(self) -> None:
        results = [
            {
                "judge_id": "j1",
                "dimensions": {"craft": 5.0, "depth": 9.0},
            },
            {
                "judge_id": "j2",
                "dimensions": {"craft": 7.0, "depth": 7.0},
            },
        ]
        agg = aggregate_judges(results, self.contract)
        self.assertEqual(agg["dimensions"]["craft"]["median"], 6.0)
        self.assertEqual(agg["dimensions"]["depth"]["median"], 8.0)
        # 6*0.6 + 8*0.4 = 3.6 + 3.2 = 6.8
        self.assertEqual(agg["overall"], 6.8)

    def test_aggregate_repetitions_statistics(self) -> None:
        rows = [
            {"score": 6.0, "success": True},
            {"score": 8.0, "success": True},
            {"score": 10.0, "success": False},
        ]
        agg = aggregate_repetitions(rows)
        self.assertEqual(agg["count"], 3)
        self.assertEqual(agg["median"], 8.0)
        self.assertEqual(agg["mean"], 8.0)
        self.assertAlmostEqual(agg["stdev"], math.sqrt(8 / 3), places=12)
        self.assertEqual(agg["min"], 6.0)
        self.assertEqual(agg["max"], 10.0)
        self.assertEqual(agg["success_rate"], 2 / 3)
        self.assertEqual(
            set(agg.keys()),
            {"count", "median", "mean", "stdev", "min", "max", "success_rate"},
        )

    def test_aggregate_repetitions_single_row(self) -> None:
        agg = aggregate_repetitions([{"score": 4.5, "success": False}])
        self.assertEqual(agg["count"], 1)
        self.assertEqual(agg["median"], 4.5)
        self.assertEqual(agg["mean"], 4.5)
        self.assertEqual(agg["stdev"], 0.0)
        self.assertEqual(agg["min"], 4.5)
        self.assertEqual(agg["max"], 4.5)
        self.assertEqual(agg["success_rate"], 0.0)

    def test_aggregate_repetitions_rejects_malformed_and_empty(self) -> None:
        with self.assertRaises(ValueError):
            aggregate_repetitions([])
        with self.assertRaises(ValueError):
            aggregate_repetitions([{"score": True, "success": True}])
        with self.assertRaises(ValueError):
            aggregate_repetitions([{"score": 1.0, "success": 1}])
        with self.assertRaises(ValueError):
            aggregate_repetitions([{"score": 1.0}])
        with self.assertRaises(ValueError):
            aggregate_repetitions([{"score": float("nan"), "success": True}])
        with self.assertRaises(ValueError):
            aggregate_repetitions([{"score": float("inf"), "success": True}])
        with self.assertRaises(ValueError):
            aggregate_repetitions([{"score": 1.0, "success": True, "extra": 1}])


if __name__ == "__main__":
    unittest.main()
