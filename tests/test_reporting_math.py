"""Tests for reporting math: points, expected cost, Pareto frontier."""

from __future__ import annotations

import unittest
from types import MappingProxyType

from basecamp_bench.reporting import (
    ReportPoint,
    build_report_payload,
    expected_cost,
    pareto_frontier,
    render_report_html,
)
from tests._reporting_fixtures import _point, _raw_attempt


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

    def test_expected_cost_uses_implementation_not_evaluation(self) -> None:
        p = _point(
            cost_per_attempt=2.0,
            implementation_cost_per_attempt=2.0,
            evaluation_cost_per_attempt=99.0,
            success_rate=0.5,
        )
        self.assertEqual(expected_cost(p), 4.0)


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
