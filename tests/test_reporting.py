"""Unit tests for basecamp_bench.reporting (stdlib unittest only)."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from basecamp_bench.reporting import (
    ReportPoint,
    build_report_payload,
    expected_cost,
    load_leaderboards,
    pareto_frontier,
    render_report_html,
    write_report,
)

_DEFAULT_SHA = "b" * 64


def _raw_attempt(
    *,
    run_id: str = "run-1",
    submission_id: str = "sub-1",
    repetition: int = 1,
    track: str = "fe",
    contract_version: str = "1.0",
    contract_sha256: str | None = None,
    harness: str = "h1",
    model_id: str = "m1",
    display_name: str | None = None,
    implementation_success: bool = True,
    evaluation_success: bool = True,
    score: float | None = 5.0,
    dimensions: dict[str, float] | None = None,
    judge_spread: float | None = 0.05,
    implementation_cost_usd: float | None = 1.0,
    evaluation_cost_usd: float | None = 0.1,
    tokens: int = 100,
    duration_s: float = 12.5,
    evaluator_ids: list[str] | None = None,
    ineligible_reasons: list[str] | None = None,
    **overrides: object,
) -> dict:
    if evaluation_success:
        sc = 5.0 if score is None else float(score)
        dims = (
            dimensions if dimensions is not None else {"quality": sc, "craft": max(0.0, sc - 0.5)}
        )
        spread = 0.05 if judge_spread is None else judge_spread
    else:
        dims = {} if dimensions is None else dimensions
        spread = judge_spread
        sc = score
    base: dict = {
        "run_id": run_id,
        "submission_id": submission_id,
        "repetition": repetition,
        "track": track,
        "contract_version": contract_version,
        "contract_sha256": contract_sha256 or _DEFAULT_SHA,
        "harness": harness,
        "model_id": model_id,
        "display_name": display_name or model_id,
        "implementation_success": implementation_success,
        "evaluation_success": evaluation_success,
        "score": sc,
        "dimensions": dims,
        "judge_spread": spread,
        "implementation_cost_usd": implementation_cost_usd,
        "evaluation_cost_usd": evaluation_cost_usd,
        "tokens": tokens,
        "duration_s": duration_s,
        "evaluator_ids": list(evaluator_ids) if evaluator_ids is not None else ["j1"],
        "ineligible_reasons": list(ineligible_reasons) if ineligible_reasons is not None else [],
    }
    base.update(overrides)
    return base


def _failed_raw_attempt(**overrides: object) -> dict:
    defaults: dict = {
        "implementation_success": False,
        "evaluation_success": False,
        "score": None,
        "dimensions": {},
        "judge_spread": None,
        "implementation_cost_usd": None,
        "evaluation_cost_usd": None,
        "ineligible_reasons": ["failed"],
    }
    defaults.update(overrides)
    return _raw_attempt(**defaults)  # type: ignore[arg-type]


def _point(
    *,
    model_id: str = "model-a",
    display_name: str | None = None,
    track: str = "fe",
    contract_version: str = "1.0",
    contract_sha256: str = "a" * 64,
    harness: str = "harness-x",
    score: float = 5.0,
    score_mean: float | None = None,
    score_stdev: float = 0.1,
    score_min: float | None = None,
    score_max: float | None = None,
    score_range: float = 0.0,
    judge_spread: float = 0.2,
    cost_per_attempt: float = 1.0,
    cost_mean: float | None = None,
    cost_stdev: float = 0.05,
    cost_min: float | None = None,
    cost_max: float | None = None,
    cost_range: float = 0.0,
    success_rate: float = 1.0,
    repetitions: int = 3,
    dimensions: dict[str, float] | None = None,
    tokens: int = 1000,
    tokens_mean: float | None = None,
    tokens_min: int | None = None,
    tokens_max: int | None = None,
    tokens_range: int = 0,
    duration_s: float = 10.0,
    duration_mean_s: float | None = None,
    duration_min_s: float | None = None,
    duration_max_s: float | None = None,
    duration_range_s: float = 0.0,
    eligible: bool = True,
    ineligible_reasons: tuple[str, ...] = (),
    run_ids: tuple[str, ...] = ("run-1",),
    implementation_cost_per_attempt: float | None = None,
    evaluation_cost_per_attempt: float = 0.1,
    raw_attempts: tuple | None = None,
    mode: str = "publication",
) -> ReportPoint:
    impl = (
        cost_per_attempt
        if implementation_cost_per_attempt is None
        else implementation_cost_per_attempt
    )
    return ReportPoint(
        track=track,
        contract_version=contract_version,
        contract_sha256=contract_sha256,
        model_id=model_id,
        display_name=display_name or model_id,
        harness=harness,
        score=score,
        score_mean=score if score_mean is None else score_mean,
        score_stdev=score_stdev,
        score_min=score if score_min is None else score_min,
        score_max=score if score_max is None else score_max,
        score_range=score_range,
        judge_spread=judge_spread,
        cost_per_attempt=cost_per_attempt,
        cost_mean=cost_per_attempt if cost_mean is None else cost_mean,
        cost_stdev=cost_stdev,
        cost_min=cost_per_attempt if cost_min is None else cost_min,
        cost_max=cost_per_attempt if cost_max is None else cost_max,
        cost_range=cost_range,
        success_rate=success_rate,
        repetitions=repetitions,
        dimensions=dimensions if dimensions is not None else {"dim_a": 5.0},
        tokens=tokens,
        tokens_mean=float(tokens) if tokens_mean is None else tokens_mean,
        tokens_min=tokens if tokens_min is None else tokens_min,
        tokens_max=tokens if tokens_max is None else tokens_max,
        tokens_range=tokens_range,
        duration_s=duration_s,
        duration_mean_s=duration_s if duration_mean_s is None else duration_mean_s,
        duration_min_s=duration_s if duration_min_s is None else duration_min_s,
        duration_max_s=duration_s if duration_max_s is None else duration_max_s,
        duration_range_s=duration_range_s,
        eligible=eligible,
        ineligible_reasons=ineligible_reasons,
        run_ids=run_ids,
        implementation_cost_per_attempt=impl,
        evaluation_cost_per_attempt=evaluation_cost_per_attempt,
        raw_attempts=raw_attempts if raw_attempts is not None else (),
        mode=mode,
    )


def _entry(
    model_id: str,
    *,
    score: float = 5.0,
    cost_per_attempt: float = 1.0,
    success_rate: float = 1.0,
    eligible: bool = True,
    ineligible_reasons: list[str] | None = None,
    display_name: str | None = None,
    harness: str = "h1",
    evaluation_cost_per_attempt: float = 0.1,
    track: str = "fe",
    contract_version: str = "1.0",
    contract_sha256: str | None = None,
    raw_attempts: list[dict] | None = None,
    **overrides: object,
) -> dict:
    sha = contract_sha256 or _DEFAULT_SHA
    if raw_attempts is None:
        if success_rate == 0.0:
            raw_attempts = [
                _failed_raw_attempt(
                    run_id=f"{model_id}-run-1",
                    submission_id=f"{model_id}-sub-1",
                    model_id=model_id,
                    display_name=display_name or model_id,
                    harness=harness,
                    track=track,
                    contract_version=contract_version,
                    contract_sha256=sha,
                    implementation_cost_usd=cost_per_attempt,
                    evaluation_cost_usd=evaluation_cost_per_attempt,
                )
            ]
        else:
            raw_attempts = [
                _raw_attempt(
                    run_id=f"{model_id}-run-1",
                    submission_id=f"{model_id}-sub-1",
                    model_id=model_id,
                    display_name=display_name or model_id,
                    harness=harness,
                    track=track,
                    contract_version=contract_version,
                    contract_sha256=sha,
                    score=score,
                    dimensions={"quality": score, "craft": max(0.0, score - 0.5)},
                    implementation_cost_usd=cost_per_attempt,
                    evaluation_cost_usd=evaluation_cost_per_attempt,
                )
            ]
    base: dict = {
        "model_id": model_id,
        "display_name": display_name or model_id,
        "harness": harness,
        "score": score,
        "score_mean": score,
        "score_stdev": 0.1,
        "score_min": max(0.0, score - 0.1),
        "score_max": min(10.0, score + 0.1),
        "score_range": min(10.0, score + 0.1) - max(0.0, score - 0.1),
        "judge_spread": 0.05,
        "cost_per_attempt": cost_per_attempt,
        "cost_mean": cost_per_attempt,
        "cost_stdev": 0.02,
        "cost_min": max(0.0, cost_per_attempt - 0.02),
        "cost_max": cost_per_attempt + 0.02,
        "cost_range": cost_per_attempt + 0.02 - max(0.0, cost_per_attempt - 0.02),
        "success_rate": success_rate,
        "repetitions": 3,
        "dimensions": {"quality": score, "craft": max(0.0, score - 0.5)},
        "tokens": 100,
        "tokens_mean": 100.0,
        "tokens_min": 100,
        "tokens_max": 100,
        "tokens_range": 0,
        "duration_s": 12.5,
        "duration_mean_s": 12.5,
        "duration_min_s": 12.5,
        "duration_max_s": 12.5,
        "duration_range_s": 0.0,
        "eligible": eligible,
        "ineligible_reasons": ineligible_reasons if ineligible_reasons is not None else [],
        "run_ids": [f"{model_id}-run-1"],
        "implementation_cost_per_attempt": cost_per_attempt,
        "evaluation_cost_per_attempt": evaluation_cost_per_attempt,
        "raw_attempts": raw_attempts,
    }
    base.update(overrides)
    return base


def _sync_entry_identity(
    entry: dict,
    *,
    track: str,
    contract_version: str,
    contract_sha256: str,
) -> dict:
    """Align raw-attempt identity fields with the leaderboard root and entry."""
    e = dict(entry)
    if "raw_attempts" in e:
        raws: list[dict] = []
        for raw in e["raw_attempts"] or []:
            r = dict(raw)
            r["track"] = track
            r["contract_version"] = contract_version
            r["contract_sha256"] = contract_sha256
            r["model_id"] = e["model_id"]
            r["harness"] = e["harness"]
            raws.append(r)
        e["raw_attempts"] = raws
    return e


def _leaderboard(
    entries: list[dict],
    *,
    track: str = "fe",
    contract_version: str = "1.0",
    contract_sha256: str | None = None,
    schema_version: str = "1.0",
    generated_at: str = "2026-01-01T00:00:00Z",
    sync_identity: bool = True,
) -> dict:
    sha = contract_sha256 or _DEFAULT_SHA
    fixed = (
        [
            _sync_entry_identity(
                e, track=track, contract_version=contract_version, contract_sha256=sha
            )
            for e in entries
        ]
        if sync_identity
        else list(entries)
    )
    dimension_ids = sorted(
        {
            key
            for entry in fixed
            for raw in entry.get("raw_attempts", [])
            for key in raw.get("dimensions", {})
        }
    ) or ["quality"]
    weight = 1.0 / len(dimension_ids)
    return {
        "schema_version": schema_version,
        "mode": "publication",
        "track": track,
        "contract_version": contract_version,
        "contract_sha256": sha,
        "generated_at": generated_at,
        "runner_source_sha256": "1" * 64,
        "seed_tree_sha256": "2" * 64,
        "reference_manifest_sha256": "3" * 64,
        "reference_tree_sha256": "4" * 64,
        "prompt_sha256": "5" * 64,
        "rubric_sha256": "6" * 64,
        "schema_bundle_sha256": "7" * 64,
        "dimension_profile": [
            {"id": dim_id, "label": dim_id.title(), "weight": weight} for dim_id in dimension_ids
        ],
        "entries": fixed,
    }


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def write_json(self, name: str, data: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


class ReportPointTests(unittest.TestCase):
    def test_freezes_and_normalizes_containers(self) -> None:
        raw = _raw_attempt(
            model_id="model-a",
            harness="harness-x",
            contract_sha256="a" * 64,
            dimensions={"z": 1.0, "a": 2.0},
        )
        point = _point(
            dimensions={"z": 1.0, "a": 2.0},
            ineligible_reasons=["x"],  # type: ignore[arg-type]
            run_ids=["r1", "r2"],  # type: ignore[arg-type]
            raw_attempts=(raw,),  # type: ignore[arg-type]
        )
        self.assertIsInstance(point.ineligible_reasons, tuple)
        self.assertIsInstance(point.run_ids, tuple)
        self.assertEqual(point.ineligible_reasons, ("x",))
        self.assertEqual(point.run_ids, ("r1", "r2"))
        self.assertIsInstance(point.raw_attempts, tuple)
        self.assertIsInstance(point.raw_attempts[0], MappingProxyType)
        # Mapping is immutable for consumers.
        with self.assertRaises(TypeError):
            point.dimensions["z"] = 9.0  # type: ignore[index]
        with self.assertRaises(TypeError):
            point.raw_attempts[0]["run_id"] = "mutated"  # type: ignore[index]
        dims = point.raw_attempts[0]["dimensions"]
        self.assertIsInstance(dims, MappingProxyType)
        with self.assertRaises(TypeError):
            dims["z"] = 9.0  # type: ignore[index]


class ExpectedCostTests(unittest.TestCase):
    def test_normalizes_cost_by_success_rate(self) -> None:
        point = _point(cost_per_attempt=2.0, success_rate=0.5)
        self.assertEqual(expected_cost(point), 4.0)

    def test_zero_success_returns_none(self) -> None:
        point = _point(success_rate=0.0, eligible=False)
        self.assertIsNone(expected_cost(point))

    def test_unknown_or_incomplete_implementation_cost_returns_none(self) -> None:
        for reason in ("implementation_cost_unknown", "implementation_cost_incomplete"):
            with self.subTest(reason=reason):
                point = _point(
                    cost_per_attempt=0.0,
                    ineligible_reasons=(reason,),
                    eligible=False,
                )
                self.assertIsNone(expected_cost(point))

    def test_unknown_implementation_cost_is_not_plotted_at_zero(self) -> None:
        point = _point(
            cost_per_attempt=0.0,
            ineligible_reasons=("implementation_cost_unknown",),
            eligible=False,
        )
        payload = build_report_payload([point])
        model = payload["sections"][0]["models"][0]
        self.assertIsNone(model["expected_cost"])
        html_out = render_report_html(payload)
        self.assertIn("No plottable points", html_out)
        self.assertNotIn('class="ineligible-point"', html_out)

    def test_report_exposes_total_observed_cost(self) -> None:
        payload = build_report_payload(
            [
                _point(
                    cost_per_attempt=2.0,
                    implementation_cost_per_attempt=2.0,
                    evaluation_cost_per_attempt=0.25,
                )
            ]
        )
        model = payload["sections"][0]["models"][0]
        self.assertEqual(model["total_cost_per_attempt"], 2.25)

    def test_invalid_non_finite_and_negative(self) -> None:
        self.assertIsNone(expected_cost(_point(cost_per_attempt=float("nan"))))
        self.assertIsNone(expected_cost(_point(cost_per_attempt=float("inf"))))
        self.assertIsNone(expected_cost(_point(cost_per_attempt=-1.0)))
        self.assertIsNone(expected_cost(_point(success_rate=-0.1)))
        self.assertIsNone(expected_cost(_point(success_rate=1.1)))


class ParetoFrontierTests(unittest.TestCase):
    def test_dominance_basic(self) -> None:
        # a: high score, low cost — dominates b (worse score, higher cost)
        a = _point(model_id="a", score=8.0, cost_per_attempt=1.0, success_rate=1.0)
        b = _point(model_id="b", score=5.0, cost_per_attempt=2.0, success_rate=1.0)
        frontier, dom = pareto_frontier([a, b])
        self.assertEqual(frontier, {("harness-x", "a")})
        self.assertIsNone(dom[("harness-x", "a")])
        self.assertEqual(dom[("harness-x", "b")], ("harness-x", "a"))

    def test_nondominated_tradeoff(self) -> None:
        cheap_low = _point(model_id="cheap", score=4.0, cost_per_attempt=1.0)
        pricey_high = _point(model_id="pricey", score=9.0, cost_per_attempt=5.0)
        frontier, dom = pareto_frontier([cheap_low, pricey_high])
        self.assertEqual(frontier, {("harness-x", "cheap"), ("harness-x", "pricey")})
        self.assertIsNone(dom[("harness-x", "cheap")])
        self.assertIsNone(dom[("harness-x", "pricey")])

    def test_exact_score_cost_tie_lex_smaller_wins(self) -> None:
        left = _point(model_id="alpha", score=7.0, cost_per_attempt=3.0)
        right = _point(model_id="beta", score=7.0, cost_per_attempt=3.0)
        frontier, dom = pareto_frontier([right, left])
        self.assertEqual(frontier, {("harness-x", "alpha")})
        self.assertIsNone(dom[("harness-x", "alpha")])
        self.assertEqual(dom[("harness-x", "beta")], ("harness-x", "alpha"))

    def test_multiple_dominators_prefer_lowest_cost_then_score_then_lex(self) -> None:
        # cand is dominated by both frontier points (tradeoff pair).
        cand = _point(model_id="cand", score=3.0, cost_per_attempt=10.0)
        d1 = _point(model_id="d1", score=5.0, cost_per_attempt=1.0)  # cheaper
        d2 = _point(model_id="d2", score=8.0, cost_per_attempt=4.0)  # higher score
        frontier, dom = pareto_frontier([cand, d1, d2])
        self.assertEqual(frontier, {("harness-x", "d1"), ("harness-x", "d2")})
        self.assertEqual(dom[("harness-x", "cand")], ("harness-x", "d1"))

        # Same cost among dominators → highest score preferred
        cand2 = _point(model_id="cand2", score=1.0, cost_per_attempt=20.0)
        s1 = _point(model_id="s1", score=4.0, cost_per_attempt=2.0)
        s2 = _point(model_id="s2", score=7.0, cost_per_attempt=2.0)
        # s1 is dominated by s2 (same cost, higher score), so only s2 is frontier.
        # Use two non-dominating equal-cost? Impossible without score tradeoff.
        # Instead: equal score+cost ties for weak's dominator map via e1/e2.
        frontier_s, dom_s = pareto_frontier([cand2, s1, s2])
        self.assertEqual(frontier_s, {("harness-x", "s2")})
        self.assertEqual(dom_s[("harness-x", "cand2")], ("harness-x", "s2"))
        self.assertEqual(dom_s[("harness-x", "s1")], ("harness-x", "s2"))

        # Equal cost and score among dominators → lex smallest
        e1 = _point(model_id="e1", score=8.0, cost_per_attempt=1.0)
        e2 = _point(model_id="e2", score=8.0, cost_per_attempt=1.0)
        weak = _point(model_id="weak", score=1.0, cost_per_attempt=9.0)
        frontier2, dom2 = pareto_frontier([weak, e2, e1])
        self.assertEqual(frontier2, {("harness-x", "e1")})
        self.assertEqual(dom2[("harness-x", "weak")], ("harness-x", "e1"))

    def test_ineligible_and_zero_success_not_frontier(self) -> None:
        good = _point(model_id="good", score=5.0, cost_per_attempt=1.0)
        bad = _point(
            model_id="bad",
            score=9.0,
            cost_per_attempt=0.5,
            eligible=False,
            ineligible_reasons=("policy",),
        )
        zero = _point(
            model_id="zero",
            score=9.0,
            cost_per_attempt=0.1,
            success_rate=0.0,
            eligible=False,
        )
        frontier, dom = pareto_frontier([good, bad, zero])
        self.assertEqual(frontier, {("harness-x", "good")})
        self.assertIsNone(dom[("harness-x", "bad")])
        self.assertIsNone(dom[("harness-x", "zero")])
        self.assertIsNone(dom[("harness-x", "good")])

    def test_negative_score_excluded_from_frontier(self) -> None:
        neg = _point(model_id="neg", score=-1.0, cost_per_attempt=1.0)
        pos = _point(model_id="pos", score=1.0, cost_per_attempt=2.0)
        frontier, dom = pareto_frontier([neg, pos])
        self.assertEqual(frontier, {("harness-x", "pos")})
        self.assertIsNone(dom[("harness-x", "neg")])

    def test_direct_local_point_is_never_frontier_eligible(self) -> None:
        local = _point(model_id="local", eligible=True, mode="local")
        frontier, dominators = pareto_frontier([local])
        self.assertEqual(frontier, set())
        self.assertIsNone(dominators[("harness-x", "local")])
        model = build_report_payload([local])["sections"][0]["models"][0]
        self.assertEqual(model["classification"], "ineligible")

    def test_omitted_mode_defaults_local_and_is_ineligible(self) -> None:
        publication = _point(model_id="omitted")
        values = {
            name: getattr(publication, name)
            for name in ReportPoint.__dataclass_fields__
            if name != "mode"
        }
        omitted = ReportPoint(**values)
        self.assertEqual(omitted.mode, "local")
        self.assertEqual(pareto_frontier([omitted])[0], set())
        model = build_report_payload([omitted])["sections"][0]["models"][0]
        self.assertEqual(model["classification"], "ineligible")

    def test_report_point_rejects_unknown_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "mode"):
            _point(mode="preview")


class LoadLeaderboardsTests(TempDirTestCase):
    def test_loads_valid_file(self) -> None:
        path = self.write_json(
            "lb.json",
            _leaderboard([_entry("m1", score=6.0, cost_per_attempt=2.0, success_rate=0.5)]),
        )
        points = load_leaderboards([path])
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].model_id, "m1")
        self.assertEqual(points[0].track, "fe")
        self.assertEqual(expected_cost(points[0]), 2.0)
        self.assertIsInstance(points[0].dimensions, type(points[0].dimensions))
        self.assertIsInstance(points[0].run_ids, tuple)

    def test_zero_success_forces_ineligible_reason(self) -> None:
        path = self.write_json(
            "lb.json",
            _leaderboard(
                [
                    _entry(
                        "m0",
                        success_rate=0.0,
                        eligible=True,
                        ineligible_reasons=[],
                    )
                ]
            ),
        )
        points = load_leaderboards([path])
        self.assertFalse(points[0].eligible)
        self.assertIn("success_rate is zero", points[0].ineligible_reasons)

    def test_rejects_unknown_and_missing_root_keys(self) -> None:
        data = _leaderboard([_entry("m1")])
        data["extra"] = 1
        path = self.write_json("bad-root.json", data)
        with self.assertRaises(ValueError) as ctx:
            load_leaderboards([path])
        self.assertIn("unknown", str(ctx.exception).lower())

        data2 = _leaderboard([_entry("m1")])
        del data2["generated_at"]
        path2 = self.write_json("missing-root.json", data2)
        with self.assertRaises(ValueError) as ctx2:
            load_leaderboards([path2])
        self.assertIn("missing", str(ctx2.exception).lower())

    def test_rejects_unknown_and_missing_entry_keys(self) -> None:
        entry = _entry("m1")
        entry["bonus"] = 1
        path = self.write_json("bad-entry.json", _leaderboard([entry]))
        with self.assertRaises(ValueError) as ctx:
            load_leaderboards([path])
        self.assertIn("unknown", str(ctx.exception).lower())

        entry2 = _entry("m1")
        del entry2["tokens"]
        path2 = self.write_json("missing-entry.json", _leaderboard([entry2]))
        with self.assertRaises(ValueError) as ctx2:
            load_leaderboards([path2])
        self.assertIn("missing", str(ctx2.exception).lower())

    def test_rejects_bad_types_bool_as_number_and_nan(self) -> None:
        cases = [
            ("score", True),
            ("score", float("nan")),
            ("score", float("inf")),
            ("repetitions", 1.5),
            ("repetitions", True),
            ("tokens", -1),
            ("success_rate", 1.5),
            ("success_rate", True),
            ("eligible", 1),
            ("cost_per_attempt", -0.1),
            ("dimensions", {"a": True}),
            ("ineligible_reasons", "nope"),
            ("run_ids", [1]),
            ("model_id", ""),
            ("display_name", ""),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                entry = _entry("m1")
                entry[field] = value
                path = self.write_json(f"bad-{field}.json", _leaderboard([entry]))
                with self.assertRaises(ValueError):
                    load_leaderboards([path])

    def test_deduplicates_byte_equivalent_attempts_across_files(self) -> None:
        e = _entry("same")
        p1 = self.write_json("a.json", _leaderboard([e], track="fe", contract_sha256="c" * 64))
        p2 = self.write_json("b.json", _leaderboard([e], track="fe", contract_sha256="c" * 64))
        points = load_leaderboards([p1, p2])
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].repetitions, 1)

    def test_rejects_duplicate_within_file(self) -> None:
        path = self.write_json(
            "dup.json",
            _leaderboard([_entry("x"), _entry("x", score=1.0)]),
        )
        with self.assertRaises(ValueError) as ctx:
            load_leaderboards([path])
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_invalid_json(self) -> None:
        path = self.root / "not.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            load_leaderboards([path])
        self.assertIn("json", str(ctx.exception).lower())

    def test_metadata_remains_attached_across_sequential_loads(self) -> None:
        p1 = self.write_json(
            "fe.json",
            _leaderboard(
                [_entry("m1")],
                track="fe",
                schema_version="1.0",
                generated_at="2020-01-01T00:00:00Z",
                contract_sha256="d" * 64,
            ),
        )
        pts1 = load_leaderboards([p1])
        p2 = self.write_json(
            "be.json",
            _leaderboard(
                [_entry("m2")],
                track="be",
                schema_version="1.0",
                generated_at="2021-01-01T00:00:00Z",
                contract_sha256="e" * 64,
            ),
        )
        pts2 = load_leaderboards([p2])
        payload = build_report_payload(pts2)
        self.assertEqual(len(payload["sections"]), 1)
        self.assertEqual(payload["sections"][0]["track"], "be")
        self.assertEqual(payload["sections"][0]["generated_at"], "2021-01-01T00:00:00Z")

        retained = build_report_payload(pts1)["sections"][0]
        self.assertEqual(retained["generated_at_values"], ["2020-01-01T00:00:00Z"])
        self.assertEqual(retained["source_run_ids"], ["m1-run-1"])

        # Programmatic points without load → null provenance
        payload_null = build_report_payload([_point(model_id="solo")])
        self.assertIsNone(payload_null["sections"][0]["schema_version"])
        self.assertIsNone(payload_null["sections"][0]["generated_at"])


class BuildReportPayloadTests(TempDirTestCase):
    def test_separates_fe_be_and_contract_versions(self) -> None:
        points = [
            _point(model_id="fe1", track="fe", contract_version="1.0", contract_sha256="1" * 64),
            _point(model_id="be1", track="be", contract_version="1.0", contract_sha256="2" * 64),
            _point(model_id="fe2", track="fe", contract_version="2.0", contract_sha256="3" * 64),
        ]
        payload = build_report_payload(points)
        keys = {
            (s["track"], s["contract_version"], s["contract_sha256"]) for s in payload["sections"]
        }
        self.assertEqual(len(payload["sections"]), 3)
        self.assertIn(("fe", "1.0", "1" * 64), keys)
        self.assertIn(("be", "1.0", "2" * 64), keys)
        self.assertIn(("fe", "2.0", "3" * 64), keys)
        # Deterministic section order
        ordered = [
            (s["track"], s["contract_version"], s["contract_sha256"]) for s in payload["sections"]
        ]
        self.assertEqual(ordered, sorted(ordered))

    def test_includes_ineligible_and_classifications(self) -> None:
        good = _point(model_id="good", score=8.0, cost_per_attempt=1.0)
        weak = _point(model_id="weak", score=2.0, cost_per_attempt=5.0)
        bad = _point(
            model_id="bad",
            score=9.0,
            eligible=False,
            ineligible_reasons=("unsafe",),
        )
        payload = build_report_payload([good, weak, bad])
        models = {m["model_id"]: m for m in payload["sections"][0]["models"]}
        self.assertEqual(models["good"]["classification"], "frontier")
        self.assertEqual(models["weak"]["classification"], "dominated")
        self.assertEqual(models["weak"]["dominator"], "harness-x:good")
        self.assertEqual(models["bad"]["classification"], "ineligible")
        self.assertIsNone(models["bad"]["dominator"])
        self.assertIsNotNone(models["good"]["expected_cost"])

    def test_adjacent_frontier_marginal_cost(self) -> None:
        # Frontier: low score/cheap then high score/expensive
        a = _point(model_id="a", score=2.0, cost_per_attempt=2.0, success_rate=1.0)
        b = _point(model_id="b", score=6.0, cost_per_attempt=6.0, success_rate=1.0)
        # Dominated interior
        c = _point(model_id="c", score=3.0, cost_per_attempt=10.0, success_rate=1.0)
        payload = build_report_payload([a, b, c])
        section = payload["sections"][0]
        self.assertEqual(section["frontier"], ["harness-x:a", "harness-x:b"])
        by_id = {m["model_id"]: m for m in section["models"]}
        self.assertIsNone(by_id["a"]["marginal_cost_per_quality"])
        # (6-2)/(6-2) = 1.0
        self.assertEqual(by_id["b"]["marginal_cost_per_quality"], 1.0)
        self.assertIsNone(by_id["c"]["marginal_cost_per_quality"])

    def test_new_model_appears_automatically(self) -> None:
        base = _leaderboard([_entry("old", score=5.0, cost_per_attempt=2.0)])
        path = self.write_json("lb.json", base)
        payload1 = build_report_payload(load_leaderboards([path]))
        ids1 = {m["model_id"] for m in payload1["sections"][0]["models"]}
        self.assertEqual(ids1, {"old"})

        base["entries"].append(_entry("new-model", score=7.0, cost_per_attempt=3.0))
        path.write_text(json.dumps(base), encoding="utf-8")
        payload2 = build_report_payload(load_leaderboards([path]))
        ids2 = {m["model_id"] for m in payload2["sections"][0]["models"]}
        self.assertEqual(ids2, {"old", "new-model"})
        html = render_report_html(payload2)
        self.assertIn("new-model", html)

    def test_deterministic_payload_and_html_bytes(self) -> None:
        points = [
            _point(model_id="z", score=4.0, cost_per_attempt=2.0),
            _point(model_id="a", score=7.0, cost_per_attempt=3.0),
            _point(
                model_id="mid",
                score=1.0,
                cost_per_attempt=9.0,
                eligible=False,
                ineligible_reasons=("x",),
            ),
        ]
        p1 = build_report_payload(points)
        p2 = build_report_payload(list(reversed(points)))
        self.assertEqual(
            json.dumps(p1, sort_keys=True),
            json.dumps(p2, sort_keys=True),
        )
        h1 = render_report_html(p1)
        h2 = render_report_html(p2)
        self.assertEqual(h1, h2)
        self.assertEqual(h1.encode("utf-8"), h2.encode("utf-8"))


class RenderReportHtmlTests(unittest.TestCase):
    def _sample_payload(self) -> dict:
        points = [
            _point(
                model_id="alpha",
                display_name="Alpha",
                score=8.0,
                cost_per_attempt=2.0,
                score_stdev=0.3,
                cost_stdev=0.1,
                dimensions={"quality": 8.0, "craft": 7.0},
            ),
            _point(
                model_id="beta",
                display_name="Beta",
                score=5.0,
                cost_per_attempt=1.0,
                dimensions={"quality": 5.0, "craft": 5.5},
            ),
            _point(
                model_id="gamma",
                display_name="Gamma <script>alert(1)</script>",
                score=9.0,
                cost_per_attempt=0.5,
                eligible=False,
                ineligible_reasons=('evil "attr" & stuff',),
                run_ids=("run<script>",),
            ),
            _point(
                model_id="be-model",
                track="be",
                contract_version="9.9",
                contract_sha256="f" * 64,
                score=6.0,
                cost_per_attempt=1.5,
            ),
        ]
        return build_report_payload(points)

    def test_xss_escaping(self) -> None:
        payload = self._sample_payload()
        html_out = render_report_html(payload)
        self.assertNotIn("<script>alert(1)</script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)
        self.assertIn("evil &quot;attr&quot;", html_out)
        # Embedded JSON must not allow script breakout via literal </script>
        self.assertNotIn("</script>alert", html_out)
        self.assertIn("\\u003c", html_out)

    def test_no_network_or_external_resources(self) -> None:
        html_out = render_report_html(self._sample_payload())
        lowered = html_out.lower()
        for needle in (
            "http://",
            "https://",
            "cdn.",
            "<iframe",
            "fetch(",
            "xmlhttprequest",
            "websocket",
        ):
            self.assertNotIn(needle, lowered)

    def test_chart_accessibility_error_bars_frontier_cheaper_right(self) -> None:
        html_out = render_report_html(self._sample_payload())
        self.assertIn('role="img"', html_out)
        self.assertIn("aria-label=", html_out)
        self.assertIn("<title>", html_out)
        self.assertIn("<desc>", html_out)
        self.assertIn("error-bar", html_out)
        self.assertIn("frontier-line", html_out)
        self.assertIn("point-label", html_out)
        self.assertIn("cheaper to the right", html_out.lower())
        self.assertIn("Expected implementation cost (cheaper to the right)", html_out)

    def test_tables_methodology_and_sections(self) -> None:
        html_out = render_report_html(self._sample_payload())
        self.assertIn("dim-table", html_out)
        self.assertIn("raw-table", html_out)
        self.assertIn("attempts-table", html_out)
        self.assertIn("Methodology and provenance", html_out)
        self.assertIn("implementation_cost_per_attempt / success_rate", html_out)
        self.assertIn("Track fe", html_out)
        self.assertIn("Track be", html_out)
        self.assertIn("contract 9.9", html_out)
        self.assertIn("Score", html_out)
        self.assertIn("Expected implementation cost per valid result", html_out)
        self.assertIn("Implementation cost median per attempt", html_out)
        self.assertIn("Evaluation overhead per attempt", html_out)
        self.assertIn("Total cost per attempt", html_out)
        self.assertIn("End-to-end agent duration median (s)", html_out)
        self.assertIn("End-to-end agent duration (s)", html_out)
        self.assertIn("critical-path evaluator process time", html_out)
        self.assertIn("Judge spread", html_out)
        self.assertIn("report-payload", html_out)
        self.assertIn("<caption>", html_out)

    def test_no_filesystem_paths_rendered(self) -> None:
        points = [
            _point(
                model_id="m",
                run_ids=("abc123",),
            )
        ]
        html_out = render_report_html(build_report_payload(points))
        self.assertIn("abc123", html_out)
        self.assertNotIn("/Users/", html_out)
        self.assertNotIn("C:\\", html_out)


class WriteReportTests(TempDirTestCase):
    def test_atomic_write_and_return_path(self) -> None:
        lb = self.write_json(
            "lb.json",
            _leaderboard(
                [
                    _entry("m1", score=5.0, cost_per_attempt=1.0),
                    _entry("m2", score=7.0, cost_per_attempt=3.0),
                ],
                track="fe",
                generated_at="2026-02-02T12:00:00Z",
            ),
        )
        out = self.root / "nested" / "report.html"
        result = write_report([lb], out)
        self.assertEqual(result, out)
        self.assertTrue(out.is_file())
        text = out.read_text(encoding="utf-8")
        self.assertIn("m1", text)
        self.assertIn("m2", text)
        self.assertIn("2026-02-02T12:00:00Z", text)
        self.assertTrue(text.startswith("<!DOCTYPE html>"))
        # No leftover temps
        leftovers = list(out.parent.glob(".report.html.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_write_report_deterministic_bytes(self) -> None:
        lb = self.write_json(
            "lb.json",
            _leaderboard(
                [
                    _entry("z-model", score=4.0),
                    _entry("a-model", score=6.0, cost_per_attempt=2.0),
                ]
            ),
        )
        out1 = self.root / "r1.html"
        out2 = self.root / "r2.html"
        write_report([lb], out1)
        write_report([lb], out2)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())

    def test_write_report_cleanup_on_failure(self) -> None:
        # Invalid leaderboard should not leave the output or orphan tmp in a
        # successful state; parent may be created.
        bad = self.root / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        out = self.root / "out" / "report.html"
        with self.assertRaises(ValueError):
            write_report([bad], out)
        self.assertFalse(out.exists())


class EndToEndProvenanceTests(TempDirTestCase):
    def test_carries_schema_and_generated_at(self) -> None:
        path = self.write_json(
            "lb.json",
            _leaderboard(
                [_entry("m1")],
                schema_version="1.0",
                generated_at="2025-12-25T08:30:00Z",
                contract_sha256="aa" * 32,
            ),
        )
        points = load_leaderboards([path])
        payload = build_report_payload(points)
        section = payload["sections"][0]
        self.assertEqual(section["schema_version"], "1.0")
        self.assertEqual(section["generated_at"], "2025-12-25T08:30:00Z")
        self.assertEqual(section["contract_sha256"], "aa" * 32)
        html_out = render_report_html(payload)
        self.assertIn("2025-12-25T08:30:00Z", html_out)
        self.assertIn("aa" * 32, html_out)


class MathEdgeTests(unittest.TestCase):
    def test_expected_cost_unit(self) -> None:
        p = _point(cost_per_attempt=3.0, success_rate=0.25)
        self.assertTrue(math.isclose(expected_cost(p) or 0.0, 12.0))

    def test_marginal_none_when_score_delta_not_positive(self) -> None:
        # Two frontier points with increasing score is normal; if scores equal
        # only lex-smaller is frontier so marginal only on ordered frontier.
        a = _point(model_id="a", score=5.0, cost_per_attempt=1.0)
        b = _point(model_id="b", score=5.0, cost_per_attempt=1.0)
        payload = build_report_payload([a, b])
        models = {m["model_id"]: m for m in payload["sections"][0]["models"]}
        self.assertEqual(models["a"]["classification"], "frontier")
        self.assertEqual(models["b"]["classification"], "dominated")
        self.assertIsNone(models["a"]["marginal_cost_per_quality"])

    def test_expected_cost_uses_implementation_not_evaluation(self) -> None:
        p = _point(
            cost_per_attempt=2.0,
            implementation_cost_per_attempt=2.0,
            evaluation_cost_per_attempt=99.0,
            success_rate=0.5,
        )
        self.assertEqual(expected_cost(p), 4.0)


class EnrichedEntryValidationTests(TempDirTestCase):
    def test_rejects_missing_and_extra_enriched_fields(self) -> None:
        for field in (
            "implementation_cost_per_attempt",
            "evaluation_cost_per_attempt",
            "raw_attempts",
        ):
            with self.subTest(missing=field):
                entry = _entry("m1")
                del entry[field]
                path = self.write_json(f"miss-{field}.json", _leaderboard([entry]))
                with self.assertRaises(ValueError) as ctx:
                    load_leaderboards([path])
                self.assertIn("missing", str(ctx.exception).lower())

            with self.subTest(extra=field):
                entry = _entry("m1")
                entry["unexpected_enriched"] = 1
                path = self.write_json("extra.json", _leaderboard([entry]))
                with self.assertRaises(ValueError) as ctx:
                    load_leaderboards([path])
                self.assertIn("unknown", str(ctx.exception).lower())

    def test_rejects_cost_equality_mismatch(self) -> None:
        entry = _entry("m1", cost_per_attempt=1.0)
        entry["implementation_cost_per_attempt"] = 2.0
        path = self.write_json("mismatch.json", _leaderboard([entry]))
        with self.assertRaises(ValueError) as ctx:
            load_leaderboards([path])
        self.assertIn("cost_per_attempt", str(ctx.exception))

    def test_rejects_nonfinite_and_invalid_enriched_costs(self) -> None:
        cases = [
            ("implementation_cost_per_attempt", float("nan")),
            ("implementation_cost_per_attempt", float("inf")),
            ("implementation_cost_per_attempt", -0.1),
            ("implementation_cost_per_attempt", True),
            ("evaluation_cost_per_attempt", float("nan")),
            ("evaluation_cost_per_attempt", -1.0),
            ("evaluation_cost_per_attempt", True),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                entry = _entry("m1", cost_per_attempt=1.0)
                entry[field] = value
                if field == "implementation_cost_per_attempt" and not (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                ):
                    # Keep cost_per_attempt equal only when value is a valid
                    # finite nonnegative candidate; otherwise leave mismatch
                    # aside and test type/range rejection.
                    if (
                        isinstance(value, (int, float))
                        and math.isfinite(float(value))
                        and float(value) >= 0
                    ):
                        entry["cost_per_attempt"] = value
                path = self.write_json(f"bad-{field}.json", _leaderboard([entry]))
                with self.assertRaises(ValueError):
                    load_leaderboards([path])

    def test_rejects_incoherent_distribution_statistics(self) -> None:
        cases = [
            ("score_range", 99.0),
            ("cost_mean", 99.0),
            ("tokens_min", 101),
            ("duration_range_s", 1.0),
            ("score_mean", float("nan")),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                entry = _entry("m1")
                entry[field] = value
                path = self.write_json(f"bad-distribution-{field}.json", _leaderboard([entry]))
                with self.assertRaises(ValueError):
                    load_leaderboards([path])

    def test_deep_immutability_of_loaded_raw_attempts(self) -> None:
        entry = _entry(
            "m1",
            raw_attempts=[
                _raw_attempt(
                    model_id="m1",
                    dimensions={"quality": 5.0, "craft": 4.5},
                    evaluator_ids=["j1", "j2"],
                    ineligible_reasons=["note"],
                )
            ],
        )
        # Keep a mutable reference to the JSON-shaped structure.
        mutable_raw = entry["raw_attempts"][0]
        path = self.write_json("imm.json", _leaderboard([entry]))
        points = load_leaderboards([path])
        loaded = points[0].raw_attempts[0]
        mutable_raw["run_id"] = "mutated-after-load"
        mutable_raw["dimensions"]["quality"] = 99.0
        mutable_raw["evaluator_ids"].append("evil")
        self.assertEqual(loaded["run_id"], "run-1")
        self.assertEqual(loaded["dimensions"]["quality"], 5.0)
        self.assertEqual(list(loaded["evaluator_ids"]), ["j1", "j2"])
        with self.assertRaises(TypeError):
            loaded["run_id"] = "x"  # type: ignore[index]
        with self.assertRaises(TypeError):
            loaded["dimensions"]["quality"] = 1.0  # type: ignore[index]


class RawAttemptValidationTests(TempDirTestCase):
    def _load_with_raw(self, raw: dict, **entry_kw: object) -> list[ReportPoint]:
        entry = _entry("m1", **entry_kw)  # type: ignore[arg-type]
        entry["raw_attempts"] = [raw]
        path = self.write_json("raw.json", _leaderboard([entry], sync_identity=False))
        return load_leaderboards([path])

    def test_rejects_raw_missing_and_extra_keys(self) -> None:
        raw = _raw_attempt(model_id="m1")
        del raw["tokens"]
        with self.assertRaises(ValueError) as ctx:
            self._load_with_raw(raw)
        self.assertIn("missing", str(ctx.exception).lower())

        raw2 = _raw_attempt(model_id="m1")
        raw2["extra_field"] = "nope"
        with self.assertRaises(ValueError) as ctx2:
            self._load_with_raw(raw2)
        self.assertIn("unknown", str(ctx2.exception).lower())

    def test_rejects_identity_mismatches(self) -> None:
        cases = [
            {"track": "be"},
            {"contract_version": "9.9"},
            {"contract_sha256": "c" * 64},
            {"harness": "other-harness"},
            {"model_id": "other-model"},
        ]
        for patch in cases:
            with self.subTest(patch=patch):
                raw = _raw_attempt(model_id="m1")
                raw.update(patch)
                with self.assertRaises(ValueError):
                    self._load_with_raw(raw)

    def test_rejects_bad_types_and_success_coherence(self) -> None:
        # evaluation_success without implementation_success
        raw = _raw_attempt(
            model_id="m1",
            implementation_success=False,
            evaluation_success=True,
            score=5.0,
            dimensions={"quality": 5.0},
            judge_spread=0.1,
        )
        with self.assertRaises(ValueError) as ctx:
            self._load_with_raw(raw)
        self.assertIn("implementation_success", str(ctx.exception))

        # bool as repetition
        raw2 = _raw_attempt(model_id="m1", repetition=True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self._load_with_raw(raw2)

        # non-positive repetition
        raw3 = _raw_attempt(model_id="m1", repetition=0)
        with self.assertRaises(ValueError):
            self._load_with_raw(raw3)

        # failed eval with score present
        raw4 = _failed_raw_attempt(model_id="m1", score=1.0)
        with self.assertRaises(ValueError):
            self._load_with_raw(raw4)

        # success with empty dimensions
        raw5 = _raw_attempt(model_id="m1", dimensions={})
        with self.assertRaises(ValueError):
            self._load_with_raw(raw5)

        # success with score out of range
        raw6 = _raw_attempt(model_id="m1", score=11.0, dimensions={"quality": 11.0})
        with self.assertRaises(ValueError):
            self._load_with_raw(raw6)

        # success with None judge_spread
        raw7 = _raw_attempt(model_id="m1")
        raw7["judge_spread"] = None
        with self.assertRaises(ValueError):
            self._load_with_raw(raw7)

        # failed with nonempty dimensions
        raw8 = _failed_raw_attempt(model_id="m1", dimensions={"quality": 1.0})
        with self.assertRaises(ValueError):
            self._load_with_raw(raw8)

        # optional cost negative
        raw9 = _raw_attempt(model_id="m1", implementation_cost_usd=-1.0)
        with self.assertRaises(ValueError):
            self._load_with_raw(raw9)

        # tokens bool
        raw10 = _raw_attempt(model_id="m1", tokens=True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self._load_with_raw(raw10)

        # evaluator_ids not list of safe ids
        raw11 = _raw_attempt(model_id="m1", evaluator_ids=["not safe!"])
        with self.assertRaises(ValueError):
            self._load_with_raw(raw11)

        # ineligible_reasons not list
        raw12 = _raw_attempt(model_id="m1")
        raw12["ineligible_reasons"] = "nope"
        with self.assertRaises(ValueError):
            self._load_with_raw(raw12)

    def test_deduplicates_byte_equivalent_raw_identity(self) -> None:
        r1 = _raw_attempt(model_id="m1", run_id="run-a", submission_id="sub-a", repetition=1)
        r2 = _raw_attempt(model_id="m1", run_id="run-a", submission_id="sub-a", repetition=1)
        entry = _entry("m1", raw_attempts=[r1, r2])
        path = self.write_json("dup-raw.json", _leaderboard([entry]))
        points = load_leaderboards([path])
        self.assertEqual(points[0].repetitions, 1)

    def test_rejects_conflicting_duplicate_raw_identity(self) -> None:
        r1 = _raw_attempt(model_id="m1", run_id="run-a", submission_id="sub-a", score=4.0)
        r2 = _raw_attempt(model_id="m1", run_id="run-a", submission_id="sub-a", score=8.0)
        entry = _entry("m1", raw_attempts=[r1, r2])
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            load_leaderboards([self.write_json("conflict.json", _leaderboard([entry]))])

    def test_rejects_path_command_and_provenance_shapes(self) -> None:
        cases = [
            {"run_id": "/tmp/run"},
            {"run_id": "file://evil"},
            {"submission_id": "../escape"},
            {"display_name": "/Users/secret/path"},
            {"display_name": "python -m evil --flag"},
            {"display_name": "file:///etc/passwd"},
            {"ineligible_reasons": ["/var/log/x"]},
            {"ineligible_reasons": ["argv=--prompt secret"]},
            {"ineligible_reasons": ["bash -c rm"]},
            {"harness": "C:\\Windows\\system32"},
        ]
        for patch in cases:
            with self.subTest(patch=patch):
                raw = _raw_attempt(model_id="m1")
                if "ineligible_reasons" in patch:
                    raw["ineligible_reasons"] = patch["ineligible_reasons"]
                else:
                    raw.update(patch)
                with self.assertRaises(ValueError):
                    self._load_with_raw(raw)

    def test_allows_xss_shaped_display_and_reason_labels(self) -> None:
        raw = _raw_attempt(
            model_id="m1",
            display_name='Nice <script>alert(1)</script> "name"',
            ineligible_reasons=['reason <img src=x onerror=alert(1)> & "q"'],
        )
        points = self._load_with_raw(raw)
        self.assertEqual(
            points[0].raw_attempts[0]["display_name"],
            'Nice <script>alert(1)</script> "name"',
        )
        payload = build_report_payload(points)
        html_out = render_report_html(payload)
        self.assertNotIn("<script>alert(1)</script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)
        self.assertIn("&lt;img", html_out)
        self.assertIn("&quot;q&quot;", html_out)


class EnrichedReportBehaviorTests(TempDirTestCase):
    def test_payload_includes_costs_and_raw_attempts(self) -> None:
        failed = _failed_raw_attempt(
            run_id="run-fail",
            submission_id="sub-fail",
            model_id="m1",
            implementation_cost_usd=None,
            evaluation_cost_usd=None,
        )
        ok = _raw_attempt(
            run_id="run-ok",
            submission_id="sub-ok",
            repetition=2,
            model_id="m1",
            score=6.0,
            implementation_cost_usd=2.0,
            evaluation_cost_usd=0.25,
        )
        entry = _entry(
            "m1",
            score=6.0,
            cost_per_attempt=2.0,
            evaluation_cost_per_attempt=0.25,
            success_rate=0.5,
            raw_attempts=[failed, ok],
        )
        path = self.write_json("lb.json", _leaderboard([entry]))
        points = load_leaderboards([path])
        payload = build_report_payload(points)
        model = payload["sections"][0]["models"][0]
        self.assertIsNone(model["implementation_cost_per_attempt"])
        self.assertEqual(model["evaluation_cost_per_attempt"], 0.25)
        self.assertIsNone(model["total_cost_per_attempt"])
        self.assertIsNone(model["expected_cost"])
        self.assertIn("implementation_cost_incomplete", model["ineligible_reasons"])
        for key in (
            "cost_per_attempt",
            "implementation_cost_per_attempt",
            "cost_mean",
            "cost_stdev",
            "cost_min",
            "cost_max",
            "cost_range",
        ):
            self.assertIsNone(model[key])
        # Aggregate statistics are recomputed from raw attempts; source summary
        # fields cannot override the comparison math.
        self.assertEqual(model["repetitions"], 2)
        self.assertEqual(model["success_rate"], 0.5)
        self.assertEqual(model["score_stdev"], 0.0)
        self.assertIsNone(model["cost_stdev"])
        self.assertEqual(len(model["raw_attempts"]), 2)
        # Deterministic order (json sort key).
        ids = [r["run_id"] for r in model["raw_attempts"]]
        self.assertEqual(ids, sorted(ids, key=lambda x: x))
        html_out = render_report_html(payload)
        self.assertIn("run-fail", html_out)


class CompatibilityAggregationTests(TempDirTestCase):
    def test_combines_compatible_later_runs_and_recomputes_raw(self) -> None:
        first_raw = _raw_attempt(
            run_id="run-one",
            submission_id="sub-one",
            model_id="m1",
            score=4.0,
            dimensions={"quality": 4.0},
            implementation_cost_usd=1.0,
        )
        second_raw = _raw_attempt(
            run_id="run-two",
            submission_id="sub-two",
            model_id="m1",
            score=8.0,
            dimensions={"quality": 8.0},
            implementation_cost_usd=3.0,
        )
        p1 = self.write_json(
            "one.json",
            _leaderboard(
                [_entry("m1", score=4.0, cost_per_attempt=1.0, raw_attempts=[first_raw])],
                generated_at="2026-01-01T00:00:00Z",
            ),
        )
        p2 = self.write_json(
            "two.json",
            _leaderboard(
                [_entry("m1", score=8.0, cost_per_attempt=3.0, raw_attempts=[second_raw])],
                generated_at="2026-02-01T00:00:00Z",
            ),
        )
        points = load_leaderboards([p2, p1])
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].repetitions, 2)
        self.assertEqual(points[0].score, 6.0)
        self.assertEqual(points[0].implementation_cost_per_attempt, 2.0)
        section = build_report_payload(points)["sections"][0]
        self.assertEqual(
            section["generated_at_values"], ["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"]
        )
        self.assertEqual(section["source_run_ids"], ["run-one", "run-two"])

    def test_incompatible_prompt_hashes_form_separate_sections(self) -> None:
        a = _leaderboard([_entry("m1")])
        b = _leaderboard(
            [
                _entry(
                    "m2",
                    raw_attempts=[
                        _raw_attempt(run_id="run-two", submission_id="sub-two", model_id="m2")
                    ],
                )
            ]
        )
        b["prompt_sha256"] = "8" * 64
        points = load_leaderboards([self.write_json("a.json", a), self.write_json("b.json", b)])
        self.assertEqual(len(build_report_payload(points)["sections"]), 2)

    def test_local_runs_with_different_runner_sources_share_exploratory_section(self) -> None:
        first = _leaderboard([_entry("m1")])
        second = _leaderboard(
            [
                _entry(
                    "m2",
                    raw_attempts=[
                        _raw_attempt(run_id="run-two", submission_id="sub-two", model_id="m2")
                    ],
                )
            ]
        )
        first["mode"] = "local"
        second["mode"] = "local"
        second["runner_source_sha256"] = "9" * 64
        points = load_leaderboards(
            [self.write_json("runner-a.json", first), self.write_json("runner-b.json", second)]
        )
        sections = build_report_payload(points)["sections"]
        self.assertEqual(len(sections), 1)
        self.assertEqual(
            sections[0]["runner_source_sha256_values"],
            ["1" * 64, "9" * 64],
        )
        self.assertEqual({row["model_id"] for row in sections[0]["models"]}, {"m1", "m2"})

    def test_publication_runs_with_different_runner_sources_stay_separate(self) -> None:
        first = _leaderboard([_entry("m1")])
        second = _leaderboard(
            [
                _entry(
                    "m2",
                    raw_attempts=[
                        _raw_attempt(run_id="run-two", submission_id="sub-two", model_id="m2")
                    ],
                )
            ],
        )
        second["runner_source_sha256"] = "9" * 64
        points = load_leaderboards(
            [
                self.write_json("publication-a.json", first),
                self.write_json("publication-b.json", second),
            ]
        )
        self.assertEqual(len(build_report_payload(points)["sections"]), 2)

    def test_same_model_through_two_harnesses_is_two_points(self) -> None:
        one = _entry(
            "shared",
            harness="h1",
            raw_attempts=[
                _raw_attempt(
                    run_id="run-one", submission_id="sub-one", model_id="shared", harness="h1"
                )
            ],
        )
        two = _entry(
            "shared",
            harness="h2",
            raw_attempts=[
                _raw_attempt(
                    run_id="run-two", submission_id="sub-two", model_id="shared", harness="h2"
                )
            ],
        )
        points = load_leaderboards([self.write_json("both.json", _leaderboard([one, two]))])
        self.assertEqual(
            {(p.harness, p.model_id) for p in points}, {("h1", "shared"), ("h2", "shared")}
        )
        payload = build_report_payload(points)
        self.assertEqual(
            {m["point_id"] for m in payload["sections"][0]["models"]}, {"h1:shared", "h2:shared"}
        )

    def test_dimension_labels_weights_and_hashes_are_visible(self) -> None:
        board = _leaderboard([_entry("m1")])
        board["dimension_profile"][0]["label"] = "Craft quality"
        html_out = render_report_html(
            build_report_payload(load_leaderboards([self.write_json("labels.json", board)]))
        )
        self.assertIn("Craft quality", html_out)
        self.assertIn("runner_source_sha256", html_out)
        self.assertIn("publication", html_out)

    def test_pareto_unchanged_when_only_evaluation_overhead_changes(self) -> None:
        a_low_eval = _point(
            model_id="a",
            score=8.0,
            cost_per_attempt=1.0,
            evaluation_cost_per_attempt=0.01,
        )
        b_low_eval = _point(
            model_id="b",
            score=5.0,
            cost_per_attempt=2.0,
            evaluation_cost_per_attempt=0.01,
        )
        f1, d1 = pareto_frontier([a_low_eval, b_low_eval])
        a_high_eval = _point(
            model_id="a",
            score=8.0,
            cost_per_attempt=1.0,
            evaluation_cost_per_attempt=50.0,
        )
        b_high_eval = _point(
            model_id="b",
            score=5.0,
            cost_per_attempt=2.0,
            evaluation_cost_per_attempt=50.0,
        )
        f2, d2 = pareto_frontier([a_high_eval, b_high_eval])
        self.assertEqual(f1, f2)
        self.assertEqual(d1, d2)
        self.assertEqual(f1, {("harness-x", "a")})

        p1 = build_report_payload([a_low_eval, b_low_eval])
        p2 = build_report_payload([a_high_eval, b_high_eval])
        self.assertEqual(p1["sections"][0]["frontier"], p2["sections"][0]["frontier"])
        m1 = {m["model_id"]: m for m in p1["sections"][0]["models"]}
        m2 = {m["model_id"]: m for m in p2["sections"][0]["models"]}
        self.assertEqual(m1["a"]["expected_cost"], m2["a"]["expected_cost"])
        self.assertEqual(m1["b"]["expected_cost"], m2["b"]["expected_cost"])
        self.assertNotEqual(
            m1["a"]["evaluation_cost_per_attempt"],
            m2["a"]["evaluation_cost_per_attempt"],
        )

    def test_pareto_uses_median_cost_not_distribution_statistics(self) -> None:
        baseline = [
            _point(model_id="a", score=8.0, cost_per_attempt=1.0),
            _point(model_id="b", score=5.0, cost_per_attempt=2.0),
        ]
        altered = [
            _point(
                model_id="a",
                score=8.0,
                cost_per_attempt=1.0,
                cost_mean=50.0,
                cost_stdev=40.0,
                cost_min=0.0,
                cost_max=100.0,
                cost_range=100.0,
            ),
            _point(
                model_id="b",
                score=5.0,
                cost_per_attempt=2.0,
                cost_mean=0.1,
                cost_stdev=0.0,
                cost_min=0.1,
                cost_max=0.1,
                cost_range=0.0,
            ),
        ]
        self.assertEqual(pareto_frontier(baseline), pareto_frontier(altered))
        payload_1 = build_report_payload(baseline)
        payload_2 = build_report_payload(altered)
        self.assertEqual(
            payload_1["sections"][0]["frontier"],
            payload_2["sections"][0]["frontier"],
        )

    def test_raw_failure_neutral_missing_values_in_html(self) -> None:
        failed = _failed_raw_attempt(
            run_id="fail-1",
            submission_id="sub-fail",
            model_id="m1",
            implementation_cost_usd=None,
            evaluation_cost_usd=None,
            tokens=0,
            duration_s=0.0,
        )
        entry = _entry(
            "m1",
            success_rate=0.0,
            eligible=False,
            score=0.0,
            raw_attempts=[failed],
        )
        path = self.write_json("fail.json", _leaderboard([entry]))
        html_out = render_report_html(build_report_payload(load_leaderboards([path])))
        self.assertIn("fail-1", html_out)
        self.assertIn("sub-fail", html_out)
        # Score and costs for the failed attempt are None → em dash.
        self.assertIn("—", html_out)
        self.assertIn("attempts-table", html_out)


if __name__ == "__main__":
    unittest.main()
