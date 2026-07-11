"""Unit tests for basecamp_bench.leaderboard (stdlib unittest only)."""

from __future__ import annotations

import json
import statistics
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from basecamp_bench.leaderboard import (
    Attempt,
    aggregate_attempts,
    write_leaderboards,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64


def _attempt(
    *,
    run_id: str = "run-1",
    submission_id: str = "sub-1",
    repetition: int = 1,
    track: str = "fe",
    contract_version: str = "1.0",
    contract_sha256: str = _SHA_A,
    harness: str = "harness-x",
    model_id: str = "model-a",
    display_name: str = "Model A",
    implementation_success: bool = True,
    evaluation_success: bool = True,
    score: float | None = 7.0,
    dimensions: dict[str, float] | None = None,
    judge_spread: float | None = 0.2,
    implementation_cost_usd: float | None = 1.5,
    evaluation_cost_usd: float | None = 0.5,
    tokens: int = 1000,
    duration_s: float = 12.0,
    evaluator_ids: tuple[str, ...] = ("judge-1", "judge-2"),
    ineligible_reasons: tuple[str, ...] = (),
) -> Attempt:
    if dimensions is None and evaluation_success:
        dimensions = {"craft": 7.0, "depth": 6.0}
    if dimensions is None:
        dimensions = {}
    return Attempt(
        run_id=run_id,
        submission_id=submission_id,
        repetition=repetition,
        track=track,
        contract_version=contract_version,
        contract_sha256=contract_sha256,
        harness=harness,
        model_id=model_id,
        display_name=display_name,
        implementation_success=implementation_success,
        evaluation_success=evaluation_success,
        score=score,
        dimensions=dimensions,
        judge_spread=judge_spread,
        implementation_cost_usd=implementation_cost_usd,
        evaluation_cost_usd=evaluation_cost_usd,
        tokens=tokens,
        duration_s=duration_s,
        evaluator_ids=evaluator_ids,
        ineligible_reasons=ineligible_reasons,
    )


def _failed_attempt(**overrides: object) -> Attempt:
    base: dict = {
        "evaluation_success": False,
        "score": None,
        "dimensions": {},
        "judge_spread": None,
        "evaluator_ids": (),
    }
    base.update(overrides)
    return _attempt(**base)  # type: ignore[arg-type]


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()


class AttemptValidationTests(unittest.TestCase):
    def test_accepts_valid_and_freezes_containers(self) -> None:
        dims = {"z": 1.0, "a": 9.0}
        evals = ["judge-b", "judge-a"]
        reasons = ["r1"]
        attempt = _attempt(
            dimensions=dims,
            evaluator_ids=evals,  # type: ignore[arg-type]
            ineligible_reasons=reasons,  # type: ignore[arg-type]
        )
        self.assertIsInstance(attempt.dimensions, MappingProxyType)
        self.assertIsInstance(attempt.evaluator_ids, tuple)
        self.assertIsInstance(attempt.ineligible_reasons, tuple)
        self.assertEqual(attempt.evaluator_ids, ("judge-b", "judge-a"))
        self.assertEqual(attempt.ineligible_reasons, ("r1",))
        dims["z"] = 99.0
        evals.append("judge-c")
        reasons.append("r2")
        self.assertEqual(attempt.dimensions["z"], 1.0)
        self.assertEqual(attempt.evaluator_ids, ("judge-b", "judge-a"))
        self.assertEqual(attempt.ineligible_reasons, ("r1",))
        with self.assertRaises(TypeError):
            attempt.dimensions["z"] = 3.0  # type: ignore[index]

    def test_rejects_unsafe_identifiers(self) -> None:
        for field, value in (
            ("run_id", "../x"),
            ("submission_id", "BAD"),
            ("harness", "has space"),
            ("model_id", ""),
            ("contract_version", "A"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    _attempt(**{field: value})

    def test_track_and_contract_sha256(self) -> None:
        with self.assertRaises(ValueError):
            _attempt(track="web")
        with self.assertRaises(ValueError):
            _attempt(contract_sha256="ABC" + "a" * 61)
        with self.assertRaises(ValueError):
            _attempt(contract_sha256="a" * 63)
        self.assertEqual(_attempt(track="be").track, "be")
        self.assertEqual(_attempt(contract_sha256=_SHA_B).contract_sha256, _SHA_B)

    def test_rejects_bool_and_nonfinite_numerics(self) -> None:
        cases = [
            {"repetition": True},
            {"repetition": 0},
            {"repetition": -1},
            {"score": True},
            {"score": float("nan")},
            {"score": float("inf")},
            {"score": 10.1},
            {"score": -0.1},
            {"judge_spread": True},
            {"judge_spread": -0.01},
            {"judge_spread": float("nan")},
            {"implementation_cost_usd": True},
            {"implementation_cost_usd": -1.0},
            {"evaluation_cost_usd": float("inf")},
            {"tokens": True},
            {"tokens": -1},
            {"tokens": 1.5},
            {"duration_s": True},
            {"duration_s": -0.1},
            {"duration_s": float("nan")},
            {"dimensions": {"craft": True}},
            {"dimensions": {"craft": float("nan")}},
            {"dimensions": {"craft": 11.0}},
            {"implementation_success": 1},
            {"evaluation_success": "yes"},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    _attempt(**kwargs)  # type: ignore[arg-type]

    def test_coherence_success_requires_impl_score_dims_spread(self) -> None:
        with self.assertRaises(ValueError):
            _attempt(implementation_success=False, evaluation_success=True)
        with self.assertRaises(ValueError):
            _attempt(evaluation_success=True, score=None)
        with self.assertRaises(ValueError):
            _attempt(evaluation_success=True, dimensions={})
        with self.assertRaises(ValueError):
            _attempt(evaluation_success=True, judge_spread=None)

    def test_coherence_failure_requires_null_score_empty_dims(self) -> None:
        with self.assertRaises(ValueError):
            _attempt(
                evaluation_success=False,
                score=1.0,
                dimensions={},
                judge_spread=None,
            )
        with self.assertRaises(ValueError):
            _attempt(
                evaluation_success=False,
                score=None,
                dimensions={"craft": 1.0},
                judge_spread=None,
            )
        with self.assertRaises(ValueError):
            _attempt(
                evaluation_success=False,
                score=None,
                dimensions={},
                judge_spread=0.1,
            )
        ok = _failed_attempt()
        self.assertIsNone(ok.score)
        self.assertEqual(dict(ok.dimensions), {})
        self.assertIsNone(ok.judge_spread)


class AggregateGroupingTests(unittest.TestCase):
    def test_separates_track_contract_harness_model(self) -> None:
        attempts = [
            _attempt(run_id="r1", score=5.0, harness="h1", model_id="m1"),
            _attempt(run_id="r2", score=6.0, track="be", harness="h1", model_id="m1"),
            _attempt(run_id="r3", score=7.0, contract_version="2.0", harness="h1", model_id="m1"),
            _attempt(run_id="r4", score=8.0, contract_sha256=_SHA_B, harness="h1", model_id="m1"),
            _attempt(run_id="r5", score=4.0, harness="h2", model_id="m1"),
            _attempt(run_id="r6", score=3.0, harness="h1", model_id="m2"),
        ]
        roots = aggregate_attempts(attempts, mode="local", generated_at="2026-01-01T00:00:00Z")
        self.assertEqual(len(roots), 4)
        keys = [(r["track"], r["contract_version"], r["contract_sha256"]) for r in roots]
        self.assertEqual(keys, sorted(keys))
        fe_a = next(
            r
            for r in roots
            if r["track"] == "fe"
            and r["contract_version"] == "1.0"
            and r["contract_sha256"] == _SHA_A
        )
        entry_keys = [(e["harness"], e["model_id"]) for e in fe_a["entries"]]  # type: ignore[index]
        self.assertEqual(entry_keys, sorted(entry_keys))
        self.assertEqual(len(entry_keys), 3)
        self.assertIn(("h1", "m1"), entry_keys)
        self.assertIn(("h1", "m2"), entry_keys)
        self.assertIn(("h2", "m1"), entry_keys)

    def test_rejects_invalid_mode_and_generated_at(self) -> None:
        a = _attempt()
        with self.assertRaises(ValueError):
            aggregate_attempts([a], mode="prod", generated_at="2026-01-01T00:00:00Z")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            aggregate_attempts([a], mode="local", generated_at="")
        with self.assertRaises(ValueError):
            aggregate_attempts([a], mode="local", generated_at="bad\nline")
        with self.assertRaises(ValueError):
            aggregate_attempts([a], mode="local", generated_at=True)  # type: ignore[arg-type]

    def test_root_keys_match_reporting(self) -> None:
        roots = aggregate_attempts([_attempt()], mode="local", generated_at="2026-01-01T00:00:00Z")
        self.assertEqual(len(roots), 1)
        self.assertEqual(
            set(roots[0].keys()),
            {
                "schema_version",
                "mode",
                "track",
                "contract_version",
                "contract_sha256",
                "generated_at",
                "runner_source_sha256",
                "seed_tree_sha256",
                "reference_manifest_sha256",
                "reference_tree_sha256",
                "prompt_sha256",
                "rubric_sha256",
                "schema_bundle_sha256",
                "dimension_profile",
                "entries",
            },
        )
        self.assertEqual(roots[0]["schema_version"], "1.0")


class AggregateStatisticsTests(unittest.TestCase):
    def test_median_and_population_stdev_valid_only(self) -> None:
        attempts = [
            _attempt(
                run_id="r1",
                submission_id="s1",
                repetition=1,
                score=2.0,
                dimensions={"craft": 2.0, "depth": 4.0},
                judge_spread=0.1,
                tokens=100,
                duration_s=10.0,
                implementation_cost_usd=1.0,
                evaluation_cost_usd=0.1,
            ),
            _attempt(
                run_id="r2",
                submission_id="s2",
                repetition=2,
                score=6.0,
                dimensions={"craft": 6.0, "depth": 8.0},
                judge_spread=0.3,
                tokens=300,
                duration_s=30.0,
                implementation_cost_usd=3.0,
                evaluation_cost_usd=0.3,
            ),
            _failed_attempt(
                run_id="r3",
                submission_id="s3",
                repetition=3,
                score=None,
                tokens=200,
                duration_s=20.0,
                implementation_cost_usd=2.0,
                evaluation_cost_usd=0.2,
            ),
        ]
        roots = aggregate_attempts(attempts, mode="local", generated_at="2026-01-01T00:00:00Z")
        entry = roots[0]["entries"][0]  # type: ignore[index]
        self.assertEqual(entry["score"], statistics.median([2.0, 6.0]))
        self.assertEqual(entry["score_mean"], statistics.fmean([2.0, 6.0]))
        self.assertEqual(entry["score_stdev"], statistics.pstdev([2.0, 6.0]))
        self.assertEqual(
            (entry["score_min"], entry["score_max"], entry["score_range"]), (2.0, 6.0, 4.0)
        )
        self.assertEqual(entry["dimensions"]["craft"], 4.0)
        self.assertEqual(entry["dimensions"]["depth"], 6.0)
        self.assertEqual(entry["judge_spread"], statistics.median([0.1, 0.3]))
        self.assertEqual(entry["cost_per_attempt"], statistics.median([1.0, 3.0, 2.0]))
        self.assertEqual(entry["cost_mean"], statistics.fmean([1.0, 3.0, 2.0]))
        self.assertEqual(
            entry["implementation_cost_per_attempt"],
            statistics.median([1.0, 3.0, 2.0]),
        )
        self.assertEqual(
            entry["evaluation_cost_per_attempt"],
            statistics.median([0.1, 0.3, 0.2]),
        )
        self.assertEqual(entry["cost_stdev"], statistics.pstdev([1.0, 3.0, 2.0]))
        self.assertEqual(
            (entry["cost_min"], entry["cost_max"], entry["cost_range"]), (1.0, 3.0, 2.0)
        )
        self.assertEqual(entry["tokens"], int(round(statistics.median([100, 300, 200]))))
        self.assertEqual(
            (entry["tokens_mean"], entry["tokens_min"], entry["tokens_max"], entry["tokens_range"]),
            (200.0, 100, 300, 200),
        )
        self.assertEqual(entry["duration_s"], statistics.median([10.0, 30.0, 20.0]))
        self.assertEqual(
            (
                entry["duration_mean_s"],
                entry["duration_min_s"],
                entry["duration_max_s"],
                entry["duration_range_s"],
            ),
            (20.0, 10.0, 30.0, 20.0),
        )
        self.assertEqual(entry["success_rate"], 2 / 3)
        self.assertEqual(entry["repetitions"], 3)
        self.assertFalse(entry["eligible"])
        self.assertIn("local_mode", entry["ineligible_reasons"])
        raw = entry["raw_attempts"]
        self.assertEqual(len(raw), 3)
        failed = [r for r in raw if not r["evaluation_success"]]
        self.assertEqual(len(failed), 1)
        self.assertIsNone(failed[0]["score"])
        self.assertEqual(failed[0]["dimensions"], {})

    def test_zero_success_forces_ineligible_neutral_score(self) -> None:
        attempts = [
            _failed_attempt(run_id="r1", submission_id="s1", repetition=1),
            _failed_attempt(run_id="r2", submission_id="s2", repetition=2),
        ]
        roots = aggregate_attempts(attempts, mode="local", generated_at="2026-01-01T00:00:00Z")
        entry = roots[0]["entries"][0]  # type: ignore[index]
        self.assertEqual(entry["score"], 0.0)
        self.assertEqual(entry["score_stdev"], 0.0)
        self.assertEqual(
            (entry["score_mean"], entry["score_min"], entry["score_max"], entry["score_range"]),
            (0.0, 0.0, 0.0, 0.0),
        )
        self.assertEqual(entry["dimensions"], {})
        self.assertEqual(entry["judge_spread"], 0.0)
        self.assertFalse(entry["eligible"])
        self.assertIn("no_valid_attempts", entry["ineligible_reasons"])
        self.assertEqual(entry["success_rate"], 0.0)
        self.assertEqual(len(entry["raw_attempts"]), 2)

    def test_mismatched_dimensions_force_ineligible(self) -> None:
        attempts = [
            _attempt(
                run_id="r1",
                submission_id="s1",
                dimensions={"craft": 5.0, "depth": 5.0},
            ),
            _attempt(
                run_id="r2",
                submission_id="s2",
                repetition=2,
                dimensions={"craft": 6.0, "polish": 6.0},
            ),
        ]
        roots = aggregate_attempts(attempts, mode="local", generated_at="2026-01-01T00:00:00Z")
        entry = roots[0]["entries"][0]  # type: ignore[index]
        self.assertFalse(entry["eligible"])
        self.assertIn("dimension_key_mismatch", entry["ineligible_reasons"])
        self.assertEqual(set(entry["dimensions"].keys()), {"craft"})
        self.assertEqual(entry["dimensions"]["craft"], 5.5)

    def test_missing_all_implementation_costs_local_ineligible(self) -> None:
        a = _attempt(implementation_cost_usd=None)
        roots = aggregate_attempts([a], mode="local", generated_at="2026-01-01T00:00:00Z")
        entry = roots[0]["entries"][0]  # type: ignore[index]
        self.assertEqual(entry["cost_per_attempt"], 0.0)
        self.assertEqual(
            (
                entry["cost_mean"],
                entry["cost_stdev"],
                entry["cost_min"],
                entry["cost_max"],
                entry["cost_range"],
            ),
            (0.0, 0.0, 0.0, 0.0, 0.0),
        )
        self.assertFalse(entry["eligible"])
        self.assertIn("implementation_cost_unknown", entry["ineligible_reasons"])

    def test_partial_local_implementation_costs_are_explicitly_incomplete(self) -> None:
        attempts = [
            _attempt(submission_id="s1", implementation_cost_usd=1.0),
            _attempt(submission_id="s2", repetition=2, implementation_cost_usd=None),
        ]
        roots = aggregate_attempts(attempts, mode="local", generated_at="2026-01-01T00:00:00Z")
        entry = roots[0]["entries"][0]  # type: ignore[index]
        self.assertIn("implementation_cost_incomplete", entry["ineligible_reasons"])


class EligibilityGateTests(unittest.TestCase):
    def _three_valid(self, **overrides: object) -> list[Attempt]:
        attempts = []
        for i in range(1, 4):
            kwargs = {
                "run_id": f"run-{i}",
                "submission_id": f"sub-{i}",
                "repetition": i,
                "score": float(5 + i),
                "dimensions": {"craft": float(5 + i), "depth": float(4 + i)},
                "judge_spread": 0.1 * i,
                "implementation_cost_usd": 1.0 * i,
                "evaluation_cost_usd": 0.2 * i,
                "evaluator_ids": ("judge-1", "judge-2"),
                "model_id": "contestant",
                "display_name": "Contestant",
            }
            kwargs.update(overrides)
            attempts.append(_attempt(**kwargs))  # type: ignore[arg-type]
        return attempts

    def test_local_is_always_ineligible(self) -> None:
        roots = aggregate_attempts([_attempt()], mode="local", generated_at="2026-01-01T00:00:00Z")
        entry = roots[0]["entries"][0]  # type: ignore[index]
        self.assertFalse(entry["eligible"])
        self.assertIn("local_mode", entry["ineligible_reasons"])

    def test_publication_cardinality_gates(self) -> None:
        cases = (
            (self._three_valid()[:2], "insufficient_repetitions"),
            (self._three_valid(evaluator_ids=("only-one",)), "insufficient_evaluators"),
            (self._three_valid(evaluator_ids=("same", "same")), "insufficient_evaluators"),
        )
        for attempts, reason in cases:
            with self.subTest(reason=reason):
                entry = aggregate_attempts(
                    attempts,
                    mode="publication",
                    generated_at="2026-01-01T00:00:00Z",
                )[0]["entries"][0]
                self.assertFalse(entry["eligible"])
                self.assertIn(reason, entry["ineligible_reasons"])

    def test_evaluator_model_overlap_is_allowed(self) -> None:
        attempts = self._three_valid(
            model_id="gpt-4",
            evaluator_ids=("gpt-4", "judge-other"),
        )
        roots = aggregate_attempts(
            attempts, mode="publication", generated_at="2026-01-01T00:00:00Z"
        )
        entry = roots[0]["entries"][0]  # type: ignore[index]
        self.assertTrue(entry["eligible"])

    def test_publication_requires_all_costs_known(self) -> None:
        attempts = self._three_valid()
        attempts[1] = _attempt(
            run_id="run-2",
            submission_id="sub-2",
            repetition=2,
            model_id="contestant",
            display_name="Contestant",
            implementation_cost_usd=None,
            evaluation_cost_usd=0.2,
            score=6.0,
            dimensions={"craft": 6.0, "depth": 5.0},
        )
        roots = aggregate_attempts(
            attempts, mode="publication", generated_at="2026-01-01T00:00:00Z"
        )
        entry = roots[0]["entries"][0]  # type: ignore[index]
        self.assertFalse(entry["eligible"])
        self.assertIn("implementation_cost_incomplete", entry["ineligible_reasons"])
        attempts2 = self._three_valid()
        attempts2[0] = _attempt(
            run_id="run-1",
            submission_id="sub-1",
            repetition=1,
            model_id="contestant",
            display_name="Contestant",
            evaluation_cost_usd=None,
            score=6.0,
            dimensions={"craft": 6.0, "depth": 5.0},
        )
        roots2 = aggregate_attempts(
            attempts2, mode="publication", generated_at="2026-01-01T00:00:00Z"
        )
        entry2 = roots2[0]["entries"][0]  # type: ignore[index]
        self.assertFalse(entry2["eligible"])
        self.assertIn("evaluation_cost_incomplete", entry2["ineligible_reasons"])

    def test_publication_rejects_attempt_ineligible_reasons(self) -> None:
        attempts = self._three_valid()
        attempts[0] = _attempt(
            run_id="run-1",
            submission_id="sub-1",
            repetition=1,
            model_id="contestant",
            display_name="Contestant",
            ineligible_reasons=("policy-violation",),
            score=6.0,
            dimensions={"craft": 6.0, "depth": 5.0},
        )
        local = aggregate_attempts(attempts, mode="local", generated_at="2026-01-01T00:00:00Z")
        self.assertFalse(local[0]["entries"][0]["eligible"])  # type: ignore[index]
        pub = aggregate_attempts(attempts, mode="publication", generated_at="2026-01-01T00:00:00Z")
        entry = pub[0]["entries"][0]  # type: ignore[index]
        self.assertFalse(entry["eligible"])
        self.assertIn("attempt_ineligible_reasons", entry["ineligible_reasons"])

    def test_local_rejects_attempt_ineligible_reasons(self) -> None:
        attempt = _attempt(ineligible_reasons=("unsafe_execution",))
        entry = aggregate_attempts([attempt], mode="local", generated_at="2026-01-01T00:00:00Z")[0][
            "entries"
        ][0]
        self.assertFalse(entry["eligible"])
        self.assertIn("attempt_ineligible_reasons", entry["ineligible_reasons"])

    def test_display_name_inconsistency(self) -> None:
        a1 = _attempt(run_id="r1", display_name="Alpha")
        a2 = _attempt(run_id="r2", submission_id="s2", repetition=2, display_name="Beta")
        roots = aggregate_attempts([a1, a2], mode="local", generated_at="2026-01-01T00:00:00Z")
        entry = roots[0]["entries"][0]  # type: ignore[index]
        self.assertFalse(entry["eligible"])
        self.assertIn("display_name_inconsistent", entry["ineligible_reasons"])
        self.assertEqual(entry["display_name"], "Alpha")  # lex smaller

    def test_publication_happy_path(self) -> None:
        attempts = self._three_valid()
        roots = aggregate_attempts(
            attempts, mode="publication", generated_at="2026-01-01T00:00:00Z"
        )
        entry = roots[0]["entries"][0]  # type: ignore[index]
        self.assertTrue(entry["eligible"])
        self.assertEqual(entry["ineligible_reasons"], [])
        self.assertEqual(entry["repetitions"], 3)


class DeterminismAndRawTests(unittest.TestCase):
    def test_deterministic_ordering_regardless_of_input_order(self) -> None:
        a = _attempt(
            run_id="run-z",
            track="fe",
            contract_version="1.0",
            contract_sha256=_SHA_B,
            harness="h-b",
            model_id="m-b",
            score=5.0,
        )
        b = _attempt(
            run_id="run-a",
            track="be",
            contract_version="1.0",
            contract_sha256=_SHA_A,
            harness="h-a",
            model_id="m-a",
            score=6.0,
        )
        c = _attempt(
            run_id="run-m",
            track="fe",
            contract_version="1.0",
            contract_sha256=_SHA_B,
            harness="h-a",
            model_id="m-a",
            score=7.0,
        )
        r1 = aggregate_attempts([a, b, c], mode="local", generated_at="2026-01-01T00:00:00Z")
        r2 = aggregate_attempts([c, a, b], mode="local", generated_at="2026-01-01T00:00:00Z")
        self.assertEqual(
            json.dumps(r1, sort_keys=True),
            json.dumps(r2, sort_keys=True),
        )
        bad = [
            _attempt(
                run_id="r1",
                implementation_cost_usd=None,
                display_name="Zed",
            ),
            _attempt(
                run_id="r2",
                submission_id="s2",
                repetition=2,
                implementation_cost_usd=None,
                display_name="Aye",
            ),
        ]
        entry = aggregate_attempts(bad, mode="local", generated_at="2026-01-01T00:00:00Z")[0][
            "entries"
        ][0]
        reasons = entry["ineligible_reasons"]  # type: ignore[index]
        self.assertEqual(reasons, sorted(set(reasons)))

    def test_raw_attempt_contains_every_field_portable_types(self) -> None:
        a = _attempt(
            evaluator_ids=("j1", "j2"),
            ineligible_reasons=(),
            dimensions={"depth": 3.0, "craft": 4.0},
        )
        entry = aggregate_attempts([a], mode="local", generated_at="2026-01-01T00:00:00Z")[0][
            "entries"
        ][0]
        raw = entry["raw_attempts"][0]  # type: ignore[index]
        expected_keys = {
            "run_id",
            "submission_id",
            "repetition",
            "track",
            "contract_version",
            "contract_sha256",
            "harness",
            "model_id",
            "display_name",
            "implementation_success",
            "evaluation_success",
            "score",
            "dimensions",
            "judge_spread",
            "implementation_cost_usd",
            "evaluation_cost_usd",
            "tokens",
            "duration_s",
            "evaluator_ids",
            "ineligible_reasons",
        }
        self.assertEqual(set(raw.keys()), expected_keys)
        self.assertIsInstance(raw["dimensions"], dict)
        self.assertEqual(list(raw["dimensions"].keys()), sorted(raw["dimensions"].keys()))
        self.assertIsInstance(raw["evaluator_ids"], list)
        self.assertIsInstance(raw["ineligible_reasons"], list)
        self.assertIsInstance(raw["implementation_success"], bool)
        self.assertIsInstance(raw["score"], float)
        json.dumps(raw)


class WriteLeaderboardsTests(TempDirTestCase):
    def _roots(self) -> list[dict[str, object]]:
        return aggregate_attempts(
            [
                _attempt(run_id="r1", track="fe", contract_sha256=_SHA_A, score=5.0),
                _attempt(
                    run_id="r2",
                    track="be",
                    contract_sha256=_SHA_C,
                    score=6.0,
                    model_id="model-b",
                    display_name="Model B",
                ),
            ],
            mode="local",
            generated_at="2026-01-01T00:00:00Z",
        )

    def test_writes_json_csv_md_deterministically(self) -> None:
        roots = self._roots()
        out1 = self.root / "out1"
        out2 = self.root / "out2"
        paths1 = write_leaderboards(out1, roots)
        paths2 = write_leaderboards(out2, roots)
        self.assertEqual(len(paths1), 6)  # 2 roots × 3 formats
        self.assertEqual([p.name for p in paths1], sorted(p.name for p in paths1))
        by_name1 = {p.name: p.read_bytes() for p in paths1}
        by_name2 = {p.name: p.read_bytes() for p in paths2}
        self.assertEqual(by_name1, by_name2)
        for p in paths1:
            if p.suffix == ".json":
                data = p.read_bytes()
                self.assertTrue(data.endswith(b"\n"))
                parsed = json.loads(data)
                self.assertEqual(
                    data,
                    (json.dumps(parsed, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"),
                )
                self.assertIn("raw_attempts", parsed["entries"][0])
                self.assertIn("entries", parsed)

    def test_csv_and_markdown_exclude_raw_and_escape(self) -> None:
        tricky = _attempt(
            display_name='Name, "quoted" pipe|slash\\end',
            model_id="model-x",
        )
        roots = aggregate_attempts([tricky], mode="local", generated_at="2026-01-01T00:00:00Z")
        paths = write_leaderboards(self.root / "esc", roots)
        csv_path = next(p for p in paths if p.suffix == ".csv")
        md_path = next(p for p in paths if p.suffix == ".md")
        csv_text = csv_path.read_text(encoding="utf-8")
        md_text = md_path.read_text(encoding="utf-8")
        self.assertNotIn("raw_attempts", csv_text.splitlines()[0])
        self.assertNotIn("raw_attempts", md_text)
        expected_fields = [
            "score",
            "score_mean",
            "score_stdev",
            "score_min",
            "score_max",
            "score_range",
            "cost_per_attempt",
            "cost_mean",
            "cost_stdev",
            "cost_min",
            "cost_max",
            "cost_range",
            "tokens",
            "tokens_mean",
            "tokens_min",
            "tokens_max",
            "tokens_range",
            "duration_s",
            "duration_mean_s",
            "duration_min_s",
            "duration_max_s",
            "duration_range_s",
        ]
        csv_fields = csv_text.splitlines()[0].split(",")
        md_header = next(line for line in md_text.splitlines() if line.startswith("| model_id |"))
        md_fields = [field.strip() for field in md_header.strip("|").split("|")]
        for fields in (csv_fields, md_fields):
            positions = [fields.index(field) for field in expected_fields]
            self.assertEqual(positions, sorted(positions))
        self.assertIn("model-x", csv_text)
        self.assertIn('"Name, ""quoted"" pipe|slash\\end"', csv_text)
        table_rows = [ln for ln in md_text.splitlines() if ln.startswith("| model-x |")]
        self.assertEqual(len(table_rows), 1)
        self.assertIn("\\|", table_rows[0])
        self.assertIn("\\\\", table_rows[0])

    def test_collision_detected_before_any_write(self) -> None:
        roots = self._roots()
        out = self.root / "col"
        write_leaderboards(out, roots)
        existing = list(out.glob("*.json"))[0]
        for p in out.iterdir():
            if p != existing:
                p.unlink()
        with self.assertRaises(ValueError) as ctx:
            write_leaderboards(out, roots)
        self.assertIn("overwrite", str(ctx.exception).lower())
        self.assertEqual(set(out.iterdir()), {existing})

    def test_output_dir_invalid_and_create(self) -> None:
        file_path = self.root / "not-a-dir"
        file_path.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            write_leaderboards(file_path, self._roots())
        new_dir = self.root / "nested" / "out"
        paths = write_leaderboards(new_dir, self._roots())
        self.assertTrue(new_dir.is_dir())
        self.assertTrue(all(p.is_file() for p in paths))
        self.assertTrue(all(str(p).startswith(str(new_dir.resolve())) for p in paths))

    def test_paths_contained_and_stable_names(self) -> None:
        roots = aggregate_attempts(
            [_attempt(track="fe", contract_version="1.0", contract_sha256=_SHA_A)],
            mode="local",
            generated_at="t",
        )
        out = self.root / "stable"
        paths = write_leaderboards(out, roots)
        names = sorted(p.name for p in paths)
        base = f"leaderboard_fe_1.0_{_SHA_A}"
        self.assertEqual(
            names,
            [f"{base}.csv", f"{base}.json", f"{base}.md"],
        )


if __name__ == "__main__":
    unittest.main()
