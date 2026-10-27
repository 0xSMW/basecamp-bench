"""Session cost accounting for request-tiered models."""

import unittest

from basecamp_bench.adapters import Usage
from basecamp_bench.execution import _resolve_cost

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
