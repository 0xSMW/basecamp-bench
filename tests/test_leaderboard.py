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
    attempt_from_raw,
    attempt_to_raw,
    build_attempt_ledgers,
    load_attempt_ledger,
    write_attempt_ledgers,
    write_leaderboards,
    write_tabular_views,
)
from tests._support import TempDirTestCase

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

    def test_root_is_legacy_schema_and_loads_through_reporting(self) -> None:
        roots = aggregate_attempts([_attempt()], mode="local", generated_at="2026-01-01T00:00:00Z")
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["schema_version"], "1.0")
        self.assertIn("entries", roots[0])
        # The strict schema-2.0 key set is pinned in
        # test_ledger_json_has_no_derived_statistics; here it only matters
        # that the legacy view round-trips through the reporting loader.
        from basecamp_bench.reporting import load_leaderboards

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.json"
            path.write_text(json.dumps(roots[0]), encoding="utf-8")
            points = load_leaderboards([path])
        self.assertEqual(len(points), 1)


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
        # All-failed attempts cannot infer a profile; supply one explicitly.
        roots = aggregate_attempts(
            attempts,
            mode="local",
            generated_at="2026-01-01T00:00:00Z",
            dimension_profiles={
                "fe": [{"id": "craft", "label": "Craft", "weight": 1.0}],
            },
        )
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


class WriteAttemptLedgersTests(TempDirTestCase):
    def _ledgers(self):
        return build_attempt_ledgers(
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

    def test_writes_json_deterministically(self) -> None:
        ledgers = self._ledgers()
        out1 = self.root / "out1"
        out2 = self.root / "out2"
        paths1 = write_attempt_ledgers(out1, ledgers)
        paths2 = write_attempt_ledgers(out2, ledgers)
        self.assertEqual(len(paths1), 2)  # one JSON ledger per track/contract
        self.assertEqual([p.name for p in paths1], sorted(p.name for p in paths1))
        by_name1 = {p.name: p.read_bytes() for p in paths1}
        by_name2 = {p.name: p.read_bytes() for p in paths2}
        self.assertEqual(by_name1, by_name2)
        for p in paths1:
            data = p.read_bytes()
            self.assertTrue(data.endswith(b"\n"))
            parsed = json.loads(data)
            self.assertEqual(
                data,
                (json.dumps(parsed, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"),
            )
            self.assertIn("attempts", parsed)
            self.assertNotIn("entries", parsed)
            self.assertEqual(parsed["schema_version"], "2.0")
            for attempt in parsed["attempts"]:
                self.assertNotIn("score_mean", attempt)
                self.assertNotIn("eligible", attempt)

    def test_json_preserves_display_name_text(self) -> None:
        tricky = _attempt(
            display_name='Name, "quoted" pipe|slash\\end',
            model_id="model-x",
        )
        paths = write_attempt_ledgers(
            self.root / "esc",
            build_attempt_ledgers([tricky], mode="local", generated_at="2026-01-01T00:00:00Z"),
        )
        payload = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["attempts"][0]["display_name"], 'Name, "quoted" pipe|slash\\end')
        self.assertEqual(payload["attempts"][0]["model_id"], "model-x")

    def test_collision_detected_before_any_write(self) -> None:
        ledgers = self._ledgers()
        out = self.root / "col"
        first = write_attempt_ledgers(out, ledgers)
        before_names = {p.name for p in out.iterdir()}
        self.assertEqual({p.name for p in first}, before_names)
        with self.assertRaises(ValueError) as ctx:
            write_attempt_ledgers(out, ledgers)
        self.assertIn("overwrite", str(ctx.exception).lower())
        self.assertEqual({p.name for p in out.iterdir()}, before_names)

    def test_output_dir_invalid_and_create(self) -> None:
        file_path = self.root / "not-a-dir"
        file_path.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            write_attempt_ledgers(file_path, self._ledgers())
        new_dir = self.root / "nested" / "out"
        paths = write_attempt_ledgers(new_dir, self._ledgers())
        self.assertTrue(new_dir.is_dir())
        self.assertTrue(all(p.is_file() for p in paths))
        self.assertTrue(all(str(p).startswith(str(new_dir.resolve())) for p in paths))

    def test_paths_contained_and_stable_names(self) -> None:
        ledgers = build_attempt_ledgers(
            [_attempt(track="fe", contract_version="1.0", contract_sha256=_SHA_A)],
            mode="local",
            generated_at="t",
        )
        out = self.root / "stable"
        paths = write_attempt_ledgers(out, ledgers)
        names = sorted(p.name for p in paths)
        base = f"leaderboard_fe_1.0_{_SHA_A}"
        self.assertEqual(names, [f"{base}.json"])


class LegacyWriteLeaderboardsFacadeTests(TempDirTestCase):
    def test_writes_all_legacy_views_deterministically_and_refuses_collisions(self) -> None:
        roots = aggregate_attempts(
            [_attempt(score=7.0)],
            mode="local",
            generated_at="2026-01-01T00:00:00Z",
        )
        first = write_leaderboards(self.root / "first", roots)
        second = write_leaderboards(self.root / "second", roots)
        self.assertEqual([path.name for path in first], sorted(path.name for path in first))
        self.assertEqual({path.suffix for path in first}, {".json", ".csv", ".md"})
        self.assertEqual(
            {path.name: path.read_bytes() for path in first},
            {path.name: path.read_bytes() for path in second},
        )
        json_path = next(path for path in first if path.suffix == ".json")
        self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), roots[0])

        before = {path.name: path.read_bytes() for path in first}
        with self.assertRaisesRegex(ValueError, "overwrite"):
            write_leaderboards(self.root / "first", roots)
        self.assertEqual(
            {path.name: path.read_bytes() for path in (self.root / "first").iterdir()},
            before,
        )

    def test_rejects_duplicate_export_basenames_before_writing(self) -> None:
        root = aggregate_attempts([_attempt()], mode="local", generated_at="2026-01-01T00:00:00Z")[
            0
        ]
        output = self.root / "duplicate"
        with self.assertRaisesRegex(ValueError, "duplicate export basename"):
            write_leaderboards(output, [root, root])
        self.assertEqual(list(output.iterdir()), [])


class AttemptCodecAndLedgerTests(TempDirTestCase):
    def test_attempt_round_trip_codec(self) -> None:
        original = _attempt(dimensions={"depth": 3.0, "craft": 4.0})
        raw = attempt_to_raw(original)
        rebuilt = attempt_from_raw(raw)
        self.assertEqual(attempt_to_raw(rebuilt), raw)
        self.assertEqual(dict(rebuilt.dimensions), {"craft": 4.0, "depth": 3.0})

    def test_ledger_json_has_no_derived_statistics(self) -> None:
        ledgers = build_attempt_ledgers(
            [
                _attempt(score=7.0),
                _attempt(run_id="r2", submission_id="s2", repetition=2, score=5.0),
            ],
            mode="local",
            generated_at="2026-01-01T00:00:00Z",
        )
        paths = write_attempt_ledgers(self.root / "ledgers", ledgers)
        json_path = next(p for p in paths if p.suffix == ".json")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], "2.0")
        self.assertEqual(
            set(payload.keys()),
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
                "attempts",
            },
        )
        for field in (
            "score",
            "score_mean",
            "success_rate",
            "eligible",
            "cost_per_attempt",
            "entries",
        ):
            self.assertNotIn(field, payload)
        loaded = load_attempt_ledger(json_path)
        self.assertEqual(len(loaded.attempts), 2)
        self.assertEqual(loaded.attempts[0].score, 5.0)

    def test_legacy_leaderboard_loads_through_decoder(self) -> None:
        roots = aggregate_attempts(
            [_attempt(score=6.5)],
            mode="local",
            generated_at="2026-01-01T00:00:00Z",
        )
        # Manually write legacy shape (entries + aggregates) as committed baselines do.
        legacy_path = self.root / "legacy.json"
        legacy_path.write_text(
            json.dumps(roots[0], sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        ledger = load_attempt_ledger(legacy_path)
        self.assertEqual(ledger.schema_version, "1.0")
        self.assertEqual(len(ledger.attempts), 1)
        self.assertEqual(ledger.attempts[0].score, 6.5)


class WriteTabularViewsTests(TempDirTestCase):
    def _schema2_path(
        self, attempts: list[Attempt], *, generated_at: str = "2026-01-01T00:00:00Z"
    ) -> Path:
        ledgers = build_attempt_ledgers(attempts, mode="local", generated_at=generated_at)
        paths = write_attempt_ledgers(self.root / "ledgers", ledgers)
        self.assertEqual(len(paths), 1)
        return paths[0]

    def _legacy_path(
        self, attempts: list[Attempt], *, generated_at: str = "2026-01-01T00:00:00Z"
    ) -> Path:
        roots = aggregate_attempts(attempts, mode="local", generated_at=generated_at)
        path = self.root / "legacy.json"
        path.write_text(
            json.dumps(roots[0], sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def test_schema2_and_legacy_produce_identical_tabular_bytes(self) -> None:
        """Immutable CSV identity across schema 2.0 and legacy 1.0 inputs.

        Aggregation rows are schema-invariant; Markdown advertises the source
        ledger schema_version (2.0 vs 1.0) while remaining otherwise identical.
        """
        attempts = [
            _attempt(
                harness="h-b",
                model_id="model-b",
                display_name="B",
                score=8.0,
                submission_id="sub-b",
            ),
            _attempt(
                harness="h-a",
                model_id="model-a",
                display_name="A",
                score=6.0,
                submission_id="sub-a",
            ),
        ]
        schema2 = self._schema2_path(attempts)
        legacy = self._legacy_path(attempts)
        out_a = self.root / "tab-a"
        out_b = self.root / "tab-b"
        csv_a, md_a = write_tabular_views(schema2, out_a)
        csv_b, md_b = write_tabular_views(legacy, out_b)
        self.assertEqual(csv_a.name, csv_b.name)
        self.assertEqual(md_a.name, md_b.name)
        self.assertTrue(csv_a.name.startswith("leaderboard_fe_1.0_"))
        self.assertTrue(csv_a.name.endswith(".csv"))
        self.assertTrue(md_a.name.endswith(".md"))
        self.assertEqual(csv_a.read_bytes(), csv_b.read_bytes())
        md_a_text = md_a.read_text(encoding="utf-8")
        md_b_text = md_b.read_text(encoding="utf-8")
        self.assertIn("- schema_version: `2.0`", md_a_text)
        self.assertIn("- schema_version: `1.0`", md_b_text)
        self.assertEqual(
            md_a_text.replace("- schema_version: `2.0`", "- schema_version: `1.0`"),
            md_b_text,
        )
        # Second export of the same input is byte-identical.
        out_c = self.root / "tab-c"
        csv_c, md_c = write_tabular_views(schema2, out_c)
        self.assertEqual(csv_a.read_bytes(), csv_c.read_bytes())
        self.assertEqual(md_a.read_bytes(), md_c.read_bytes())

    def test_stable_columns_ordering_and_aggregate_row_order(self) -> None:
        attempts = [
            _attempt(harness="z-harness", model_id="z-model", display_name="Z", score=9.0),
            _attempt(
                harness="a-harness",
                model_id="a-model",
                display_name="A",
                score=4.0,
                submission_id="sub-2",
            ),
        ]
        json_path = self._schema2_path(attempts)
        csv_path, md_path = write_tabular_views(json_path, self.root / "ordered")
        csv_text = csv_path.read_text(encoding="utf-8")
        header = csv_text.splitlines()[0]
        # The CSV is a published artifact, so the exact column order is an
        # external contract: changing it must be a conscious edit here.
        self.assertEqual(
            header,
            ",".join(
                [
                    "model_id",
                    "display_name",
                    "harness",
                    "score",
                    "score_mean",
                    "score_stdev",
                    "score_min",
                    "score_max",
                    "score_range",
                    "judge_spread",
                    "cost_per_attempt",
                    "cost_mean",
                    "cost_stdev",
                    "cost_min",
                    "cost_max",
                    "cost_range",
                    "success_rate",
                    "repetitions",
                    "dimensions",
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
                    "eligible",
                    "ineligible_reasons",
                    "run_ids",
                    "implementation_cost_per_attempt",
                    "evaluation_cost_per_attempt",
                ]
            ),
        )
        self.assertNotIn("raw_attempts", header)
        rows = csv_text.splitlines()[1:]
        self.assertEqual(len(rows), 2)
        # aggregate_attempts groups by sorted (harness, model_id)
        self.assertTrue(rows[0].startswith("a-model,"))
        self.assertTrue(rows[1].startswith("z-model,"))
        self.assertIn("false", rows[0])  # local_mode -> ineligible
        self.assertIn('"[""local_mode""]"', rows[0])  # JSON list + CSV quote doubling
        md = md_path.read_text(encoding="utf-8")
        self.assertIn("| a-model |", md)
        self.assertLess(md.index("| a-model |"), md.index("| z-model |"))
        self.assertTrue(csv_text.endswith("\n"))
        self.assertTrue(md.endswith("\n"))
        self.assertNotIn("\r", csv_text)

    def test_csv_and_markdown_escaping(self) -> None:
        from basecamp_bench.leaderboard import _md_escape_cell, _render_csv, _render_markdown

        tricky = _attempt(
            display_name='Name, "quoted" pipe|slash\\end',
            model_id="model-x",
        )
        json_path = self._schema2_path([tricky])
        csv_path, md_path = write_tabular_views(json_path, self.root / "esc")
        csv_text = csv_path.read_text(encoding="utf-8")
        # stdlib csv escaping: quotes doubled, field quoted when needed
        self.assertIn('"Name, ""quoted"" pipe|slash\\end"', csv_text)
        md = md_path.read_text(encoding="utf-8")
        self.assertIn("pipe\\|slash\\\\end", md)
        data_lines = [line for line in md.splitlines() if line.startswith("| model-x")]
        self.assertEqual(len(data_lines), 1)
        self.assertNotIn("\r", md)
        # CR/LF inside cells collapse for Markdown (unit-level: display_name forbids controls)
        self.assertEqual(_md_escape_cell("a\\b|c\r\nd\ne\rf"), "a\\\\b\\|c d e f")
        sample_entries: list[dict[str, object]] = [
            {
                "model_id": "m",
                "display_name": 'x, "y"',
                "harness": "h",
                "score": 1.0,
                "score_mean": 1.0,
                "score_stdev": 0.0,
                "score_min": 1.0,
                "score_max": 1.0,
                "score_range": 0.0,
                "judge_spread": 0.0,
                "cost_per_attempt": 0.0,
                "cost_mean": 0.0,
                "cost_stdev": 0.0,
                "cost_min": 0.0,
                "cost_max": 0.0,
                "cost_range": 0.0,
                "success_rate": 1.0,
                "repetitions": 1,
                "dimensions": {"z": 1.0, "a": 2.0},
                "tokens": 1,
                "tokens_mean": 1.0,
                "tokens_min": 1,
                "tokens_max": 1,
                "tokens_range": 0,
                "duration_s": 1.0,
                "duration_mean_s": 1.0,
                "duration_min_s": 1.0,
                "duration_max_s": 1.0,
                "duration_range_s": 0.0,
                "eligible": False,
                "ineligible_reasons": ["local_mode"],
                "run_ids": ["run-1"],
                "implementation_cost_per_attempt": 0.0,
                "evaluation_cost_per_attempt": 0.0,
            }
        ]
        rendered_csv = _render_csv(sample_entries)
        self.assertIn('"{""a"": 2.0, ""z"": 1.0}"', rendered_csv)
        self.assertIn('"x, ""y"""', rendered_csv)
        root = {
            "track": "fe",
            "contract_version": "1.0",
            "contract_sha256": _SHA_A,
            "generated_at": "t",
            "schema_version": "1.0",
        }
        rendered_md = _render_markdown(root, sample_entries)
        self.assertIn('{"a": 2.0, "z": 1.0}', rendered_md)
        self.assertTrue(rendered_csv.endswith("\n"))
        self.assertNotIn("\r", rendered_csv)

    def test_collision_refuses_before_any_write(self) -> None:
        json_path = self._schema2_path([_attempt()])
        out = self.root / "col"
        csv_path, md_path = write_tabular_views(json_path, out)
        before = {p.name: p.read_bytes() for p in out.iterdir()}
        # Collision on CSV alone must not rewrite MD either.
        csv_path.write_bytes(b"stale")
        md_before = md_path.read_bytes()
        with self.assertRaises(ValueError) as ctx:
            write_tabular_views(json_path, out)
        self.assertIn("overwrite", str(ctx.exception).lower())
        self.assertEqual(csv_path.read_bytes(), b"stale")
        self.assertEqual(md_path.read_bytes(), md_before)
        # Pre-existing MD alone also refuses without creating a partial new CSV
        # in a clean dir that only has md.
        out2 = self.root / "col2"
        out2.mkdir()
        base = csv_path.name[: -len(".csv")]
        (out2 / f"{base}.md").write_text("keep\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            write_tabular_views(json_path, out2)
        self.assertEqual(sorted(p.name for p in out2.iterdir()), [f"{base}.md"])
        self.assertEqual((out2 / f"{base}.md").read_text(encoding="utf-8"), "keep\n")
        # Original successful pair still present and unchanged under out
        self.assertEqual(before[md_path.name], md_before)

    def test_normal_ledger_write_does_not_emit_tabular(self) -> None:
        ledgers = build_attempt_ledgers(
            [_attempt()],
            mode="local",
            generated_at="2026-01-01T00:00:00Z",
        )
        out = self.root / "json-only"
        paths = write_attempt_ledgers(out, ledgers)
        names = sorted(p.name for p in paths)
        self.assertTrue(all(name.endswith(".json") for name in names))
        self.assertEqual(list(out.glob("*.csv")), [])
        self.assertEqual(list(out.glob("*.md")), [])


class DimensionProfileSymmetryTests(TempDirTestCase):
    def test_built_canonical_ledger_round_trips_through_loader(self) -> None:
        profile = [
            {"id": "craft", "label": "Craft", "weight": 0.6},
            {"id": "depth", "label": "Depth", "weight": 0.4},
        ]
        ledgers = build_attempt_ledgers(
            [_attempt(dimensions={"craft": 7.0, "depth": 6.0})],
            mode="local",
            generated_at="2026-01-01T00:00:00Z",
            dimension_profiles={"fe": profile},
        )
        paths = write_attempt_ledgers(self.root / "rt", ledgers)
        loaded = load_attempt_ledger(paths[0])
        self.assertEqual(loaded.schema_version, "2.0")
        self.assertEqual(
            [dict(row) for row in loaded.dimension_profile],
            sorted(profile, key=lambda row: row["id"]),
        )
        self.assertEqual(len(loaded.attempts), 1)

    def test_inferred_profile_round_trip_when_dimensions_present(self) -> None:
        ledgers = build_attempt_ledgers(
            [_attempt(dimensions={"depth": 5.0, "craft": 8.0})],
            mode="local",
            generated_at="2026-01-01T00:00:00Z",
        )
        paths = write_attempt_ledgers(self.root / "inferred-rt", ledgers)
        loaded = load_attempt_ledger(paths[0])
        ids = [str(row["id"]) for row in loaded.dimension_profile]
        self.assertEqual(ids, ["craft", "depth"])
        weights = [float(row["weight"]) for row in loaded.dimension_profile]  # type: ignore[arg-type]
        self.assertAlmostEqual(sum(weights), 1.0)

    def test_inferred_empty_dimensions_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot infer dimension profile"):
            build_attempt_ledgers(
                [_failed_attempt()],
                mode="local",
                generated_at="2026-01-01T00:00:00Z",
            )

    def test_empty_explicit_profile_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected nonempty array"):
            build_attempt_ledgers(
                [_attempt()],
                mode="local",
                generated_at="2026-01-01T00:00:00Z",
                dimension_profiles={"fe": []},
            )

    def test_duplicate_and_unsafe_ids_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate dimension id"):
            build_attempt_ledgers(
                [_attempt()],
                mode="local",
                generated_at="2026-01-01T00:00:00Z",
                dimension_profiles={
                    "fe": [
                        {"id": "craft", "label": "A", "weight": 0.5},
                        {"id": "craft", "label": "B", "weight": 0.5},
                    ]
                },
            )
        with self.assertRaises(ValueError):
            build_attempt_ledgers(
                [_attempt()],
                mode="local",
                generated_at="2026-01-01T00:00:00Z",
                dimension_profiles={
                    "fe": [{"id": "Bad ID", "label": "A", "weight": 1.0}],
                },
            )

    def test_bad_labels_and_weights_fail(self) -> None:
        cases = [
            [{"id": "craft", "label": "   ", "weight": 1.0}],
            [{"id": "craft", "label": "ok", "weight": 0.0}],
            [{"id": "craft", "label": "ok", "weight": -0.5}],
            [{"id": "craft", "label": "ok", "weight": float("nan")}],
            [{"id": "craft", "label": "ok", "weight": float("inf")}],
            [{"id": "craft", "label": "ok", "weight": True}],
            [
                {"id": "craft", "label": "A", "weight": 0.3},
                {"id": "depth", "label": "B", "weight": 0.3},
            ],
        ]
        for profile in cases:
            with self.subTest(profile=profile):
                with self.assertRaises(ValueError):
                    build_attempt_ledgers(
                        [_attempt()],
                        mode="local",
                        generated_at="2026-01-01T00:00:00Z",
                        dimension_profiles={"fe": profile},
                    )


class CanonicalLedgerNanFailClosedTests(TempDirTestCase):
    def test_write_attempt_ledgers_rejects_nan_without_completed_output(self) -> None:
        from unittest import mock

        from basecamp_bench.leaderboard import AttemptLedger

        ledgers = build_attempt_ledgers(
            [_attempt()],
            mode="local",
            generated_at="2026-01-01T00:00:00Z",
        )
        out = self.root / "nan-out"
        out.mkdir()
        poisoned = dict(ledgers[0].to_raw())
        poisoned["attempts"][0]["score"] = float("nan")  # type: ignore[index]

        with mock.patch.object(AttemptLedger, "to_raw", return_value=poisoned):
            with self.assertRaises(ValueError) as ctx:
                write_attempt_ledgers(out, ledgers)
        self.assertIn("not JSON compliant", str(ctx.exception))
        self.assertEqual(list(out.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
