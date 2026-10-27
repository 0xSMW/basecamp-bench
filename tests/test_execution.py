"""Execution selection and session cost accounting for request-tiered models."""

import unittest
from dataclasses import replace
from types import SimpleNamespace

from basecamp_bench.adapters import AgentJob, ModelSpec, Usage
from basecamp_bench.config import HarnessSpec
from basecamp_bench.execution import _resolve_cost, binary_for_job
from tests._support import TempDirTestCase

RATES = {
    "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.125}
}


class CostResolutionTests(unittest.TestCase):
    def test_haiku_preserves_vendor_tiered_session_cost(self) -> None:
        usage = Usage(input_tokens=200_000, output_tokens=1000)
        self.assertEqual(
            _resolve_cost("claude-haiku-5-5", usage, 0.1025, None, None, RATES), 0.1025
        )

    def test_haiku_does_not_estimate_tiers_from_session_aggregates(self) -> None:
        for reported in (None, -1.0, float("nan")):
            with self.subTest(reported=reported):
                self.assertIsNone(
                    _resolve_cost(
                        "claude-haiku-5-5", Usage(input_tokens=200_000), reported, None, None, RATES
                    )
                )

    def test_flat_rate_models_keep_existing_fallback(self) -> None:
        self.assertEqual(
            _resolve_cost(
                "flat",
                Usage(input_tokens=1_000_000),
                None,
                None,
                None,
                {"flat": {"input": 2.0, "output": 10.0}},
            ),
            2.0,
        )


class BinarySelectionTests(TempDirTestCase):
    def test_explicit_harness_selection_preserves_binary_override_and_default(self) -> None:
        first = HarnessSpec(
            "first", "codex", "same-model", "high", "openai", "First", "/first/codex", True
        )
        job = AgentJob(
            "implement",
            "codex",
            ModelSpec("same-model", "high"),
            self.root,
            self.root / "prompt",
            self.root / "log",
            self.root / "last",
            config_harness_id="selected",
        )
        for selected_binary in ("/selected/codex", None):
            with self.subTest(binary=selected_binary):
                selected = replace(first, id="selected", binary=selected_binary)
                config = SimpleNamespace(
                    harnesses={"first": first, "selected": selected}, evaluators=()
                )
                self.assertEqual(binary_for_job(config, job), selected_binary)

    def test_evaluator_selection_uses_referenced_harness_with_overridden_model(self) -> None:
        first = HarnessSpec(
            "first", "codex", "judge-model", "high", "openai", "First", "/first/codex", True
        )
        selected = replace(first, id="selected", model="implementation-model", binary=None)
        config = SimpleNamespace(harnesses={"first": first, "selected": selected}, evaluators=())
        job = AgentJob(
            "evaluate",
            "codex",
            ModelSpec("judge-model", "high"),
            self.root,
            self.root / "prompt",
            self.root / "log",
            self.root / "last",
            config_harness_id="selected",
        )
        self.assertIsNone(binary_for_job(config, job))

    def test_invalid_explicit_selection_never_falls_back_to_another_harness(self) -> None:
        spec = HarnessSpec(
            "configured", "codex", "model", "high", "openai", "Model", "/configured/codex", True
        )
        config = SimpleNamespace(harnesses={"configured": spec}, evaluators=())
        job = AgentJob(
            "implement",
            "codex",
            ModelSpec("model", "high"),
            self.root,
            self.root / "prompt",
            self.root / "log",
            self.root / "last",
            config_harness_id="missing",
        )
        with self.assertRaisesRegex(ValueError, "unknown configured harness"):
            binary_for_job(config, job)
        with self.assertRaisesRegex(ValueError, "adapter does not match"):
            binary_for_job(config, replace(job, harness="claude", config_harness_id="configured"))
