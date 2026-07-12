"""Unit tests for basecamp_bench.reporting (stdlib unittest only)."""

from __future__ import annotations

import json
import unittest

from basecamp_bench.reporting import (
    ReportPoint,
    build_report_payload,
    expected_cost,
    load_leaderboards,
    pareto_frontier,
    render_report_html,
)
from tests._reporting_fixtures import (
    _entry,
    _failed_raw_attempt,
    _leaderboard,
    _point,
    _raw_attempt,
)
from tests._support import TempDirTestCase


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

    def test_rejects_missing_raw_attempts_on_legacy_entry(self) -> None:
        entry = _entry("m1")
        del entry["raw_attempts"]
        path = self.write_json("missing-raw.json", _leaderboard([entry]))
        with self.assertRaises(ValueError) as ctx:
            load_leaderboards([path])
        self.assertIn("raw_attempts", str(ctx.exception).lower())

    def test_ignores_stale_aggregate_fields_on_legacy_entries(self) -> None:
        """Persisted aggregate fields are never authoritative after load."""
        entry = _entry("m1", score=6.0, cost_per_attempt=2.0)
        entry["score"] = 0.0
        entry["score_mean"] = True
        entry["success_rate"] = 0.0
        entry["eligible"] = True
        entry["cost_per_attempt"] = -1.0
        entry["tokens"] = -1
        path = self.write_json("stale-agg.json", _leaderboard([entry]))
        points = load_leaderboards([path])
        self.assertEqual(len(points), 1)
        # Recomputed from the raw attempt (score 6.0), not the stale summary.
        self.assertEqual(points[0].score, 6.0)
        self.assertEqual(points[0].success_rate, 1.0)
        self.assertEqual(points[0].implementation_cost_per_attempt, 2.0)

    def test_rejects_bad_raw_types_bool_as_number_and_nan(self) -> None:
        cases = [
            ("score", True),
            ("score", float("nan")),
            ("score", float("inf")),
            ("tokens", -1),
            ("tokens", True),
            ("duration_s", float("nan")),
            ("implementation_cost_usd", -0.1),
            ("dimensions", {"quality": True}),
            ("ineligible_reasons", "nope"),
            ("model_id", ""),
            ("display_name", ""),
            ("repetition", True),
            ("repetition", 0),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                raw = _raw_attempt(model_id="m1")
                raw[field] = value
                entry = _entry("m1", raw_attempts=[raw])
                path = self.write_json(
                    f"bad-raw-{field}.json", _leaderboard([entry], sync_identity=False)
                )
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


class LedgerShapeValidationTests(TempDirTestCase):
    def test_new_ledger_shape_loads_and_matches_legacy_semantics(self) -> None:
        from basecamp_bench.leaderboard import (
            attempt_from_raw,
            build_attempt_ledgers,
            write_attempt_ledgers,
        )

        raw = _raw_attempt(model_id="m1", score=6.0, implementation_cost_usd=2.0)
        attempt = attempt_from_raw(raw)
        ledgers = build_attempt_ledgers(
            [attempt],
            mode="publication",
            generated_at="2026-01-01T00:00:00Z",
            comparison_provenance={
                "runner_source_sha256": "1" * 64,
                "seed_tree_sha256": "2" * 64,
                "reference_manifest_sha256": "3" * 64,
                "reference_tree_sha256": "4" * 64,
                "prompt_sha256": "5" * 64,
                "rubric_sha256": "6" * 64,
                "schema_bundle_sha256": "7" * 64,
            },
            dimension_profiles={
                "fe": [
                    {"id": "quality", "label": "Quality", "weight": 0.5},
                    {"id": "craft", "label": "Craft", "weight": 0.5},
                ]
            },
        )
        paths = write_attempt_ledgers(self.root / "new-ledgers", ledgers)
        json_path = next(p for p in paths if p.suffix == ".json")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], "2.0")
        self.assertIn("attempts", payload)
        self.assertNotIn("entries", payload)
        points = load_leaderboards([json_path])
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].score, 6.0)
        self.assertEqual(points[0].implementation_cost_per_attempt, 2.0)

    def test_legacy_and_new_ledgers_combine_when_compatible(self) -> None:
        from basecamp_bench.leaderboard import (
            attempt_from_raw,
            build_attempt_ledgers,
            write_attempt_ledgers,
        )

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
        legacy = self.write_json(
            "legacy.json",
            _leaderboard(
                [_entry("m1", score=4.0, cost_per_attempt=1.0, raw_attempts=[first_raw])],
                generated_at="2026-01-01T00:00:00Z",
            ),
        )
        ledgers = build_attempt_ledgers(
            [attempt_from_raw(second_raw)],
            mode="publication",
            generated_at="2026-02-01T00:00:00Z",
            comparison_provenance={
                "runner_source_sha256": "1" * 64,
                "seed_tree_sha256": "2" * 64,
                "reference_manifest_sha256": "3" * 64,
                "reference_tree_sha256": "4" * 64,
                "prompt_sha256": "5" * 64,
                "rubric_sha256": "6" * 64,
                "schema_bundle_sha256": "7" * 64,
            },
            dimension_profiles={"fe": [{"id": "quality", "label": "Quality", "weight": 1.0}]},
        )
        new_paths = write_attempt_ledgers(self.root / "mixed", ledgers)
        new_json = next(p for p in new_paths if p.suffix == ".json")
        points = load_leaderboards([legacy, new_json])
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].repetitions, 2)
        self.assertEqual(points[0].score, 6.0)

    def test_mixed_schema_metadata_is_input_order_independent(self) -> None:
        from basecamp_bench.leaderboard import (
            attempt_from_raw,
            build_attempt_ledgers,
            write_attempt_ledgers,
        )

        legacy_raw = _raw_attempt(
            run_id="legacy-run",
            submission_id="legacy-sub",
            model_id="m1",
            score=4.0,
            dimensions={"quality": 4.0},
        )
        canonical_raw = _raw_attempt(
            run_id="canonical-run",
            submission_id="canonical-sub",
            model_id="m1",
            score=8.0,
            dimensions={"quality": 8.0},
        )
        legacy = self.write_json(
            "legacy-order.json",
            _leaderboard(
                [_entry("m1", score=4.0, raw_attempts=[legacy_raw])],
                generated_at="2026-01-01T00:00:00Z",
            ),
        )
        canonical = write_attempt_ledgers(
            self.root / "canonical-order",
            build_attempt_ledgers(
                [attempt_from_raw(canonical_raw)],
                mode="publication",
                generated_at="2026-02-01T00:00:00Z",
                comparison_provenance={
                    "runner_source_sha256": "1" * 64,
                    "seed_tree_sha256": "2" * 64,
                    "reference_manifest_sha256": "3" * 64,
                    "reference_tree_sha256": "4" * 64,
                    "prompt_sha256": "5" * 64,
                    "rubric_sha256": "6" * 64,
                    "schema_bundle_sha256": "7" * 64,
                },
                dimension_profiles={"fe": [{"id": "quality", "label": "Quality", "weight": 1.0}]},
            ),
        )[0]

        forward = build_report_payload(load_leaderboards([legacy, canonical]))
        reverse = build_report_payload(load_leaderboards([canonical, legacy]))
        self.assertEqual(forward, reverse)
        self.assertIsNone(forward["sections"][0]["schema_version"])

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
