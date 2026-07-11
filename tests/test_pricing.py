"""Tests for exact pricing lookup and cost accounting."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from dataclasses import dataclass, is_dataclass
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.error import HTTPError, URLError

from basecamp_bench.pricing import (
    PREFERRED_PROVIDERS,
    PricingLookup,
    PricingRates,
    compute_cost,
    find_exact_rates,
    load_pricing_snapshot,
    normalize_model_id,
)


def _models_dev(
    *entries: tuple[str, str, dict[str, Any]],
) -> dict[str, Any]:
    """Build models.dev-shaped data from (provider, model_id, cost) triples."""
    data: dict[str, Any] = {}
    for provider, model_id, cost in entries:
        data.setdefault(provider, {"id": provider, "models": {}})
        data[provider]["models"][model_id] = {
            "id": model_id,
            "cost": cost,
        }
    return data


@dataclass(frozen=True)
class _Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0


def _rates(**kwargs: Any) -> PricingRates:
    base = dict(
        input_usd_per_m=1.0,
        output_usd_per_m=2.0,
        cache_read_usd_per_m=0.1,
        cache_write_usd_per_m=1.25,
        currency="USD",
        source="https://models.dev/api.json",
        retrieved_at="2026-01-01T00:00:00Z",
        provider="openai",
        model_id="gpt-test",
        match_kind="exact",
    )
    base.update(kwargs)
    return PricingRates(**base)


class NormalizeModelIdTests(unittest.TestCase):
    def test_lowercase_strip_spaces_to_hyphens(self) -> None:
        self.assertEqual(normalize_model_id("  GPT 5.6 Sol  "), "gpt-5.6-sol")
        self.assertEqual(normalize_model_id("Claude-Sonnet-4"), "claude-sonnet-4")
        self.assertEqual(normalize_model_id("x"), "x")

    def test_deterministic(self) -> None:
        self.assertEqual(
            normalize_model_id("Ab C"),
            normalize_model_id("ab c"),
        )

    def test_rejects_non_string_and_empty(self) -> None:
        for value in (True, False, 1, None, b"gpt", ["gpt"]):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    normalize_model_id(value)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            normalize_model_id("")
        with self.assertRaises(ValueError):
            normalize_model_id("   ")


class DataclassContractTests(unittest.TestCase):
    def test_frozen_dataclasses(self) -> None:
        self.assertTrue(is_dataclass(PricingRates))
        self.assertTrue(is_dataclass(PricingLookup))
        rates = _rates()
        lookup = PricingLookup(rates=rates, error=None, stale=False)
        with self.assertRaises(Exception):
            rates.input_usd_per_m = 9.0  # type: ignore[misc]
        with self.assertRaises(Exception):
            lookup.stale = True  # type: ignore[misc]


class FindExactRatesTests(unittest.TestCase):
    def test_exact_match_from_models_dev(self) -> None:
        data = _models_dev(
            ("openai", "gpt-5.6", {"input": 5.0, "output": 30.0}),
        )
        result = find_exact_rates("GPT-5.6", data, {}, retrieved_at="2026-07-01T00:00:00Z")
        self.assertIsNone(result.error)
        self.assertFalse(result.stale)
        assert result.rates is not None
        self.assertEqual(result.rates.match_kind, "exact")
        self.assertEqual(result.rates.model_id, "gpt-5.6")
        self.assertEqual(result.rates.provider, "openai")
        self.assertEqual(result.rates.source, "https://models.dev/api.json")
        self.assertEqual(result.rates.input_usd_per_m, 5.0)
        self.assertEqual(result.rates.output_usd_per_m, 30.0)
        # cache rates default to input when absent
        self.assertEqual(result.rates.cache_read_usd_per_m, 5.0)
        self.assertEqual(result.rates.cache_write_usd_per_m, 5.0)
        self.assertEqual(result.rates.retrieved_at, "2026-07-01T00:00:00Z")
        self.assertEqual(result.rates.currency, "USD")

    def test_cache_rates_when_present(self) -> None:
        data = _models_dev(
            (
                "anthropic",
                "claude-sonnet-4",
                {
                    "input": 3.0,
                    "output": 15.0,
                    "cache_read": 0.3,
                    "cache_write": 3.75,
                },
            ),
        )
        result = find_exact_rates("claude-sonnet-4", data, None)
        assert result.rates is not None
        self.assertEqual(result.rates.cache_read_usd_per_m, 0.3)
        self.assertEqual(result.rates.cache_write_usd_per_m, 3.75)

    def test_override_wins_on_exact_normalized_id(self) -> None:
        data = _models_dev(
            ("openai", "gpt-x", {"input": 1.0, "output": 2.0}),
        )
        overrides = {
            "GPT X": {
                "input": 9.0,
                "output": 18.0,
                "cache_read": 0.9,
                "cache_write": 10.0,
            }
        }
        result = find_exact_rates("gpt-x", data, overrides)
        assert result.rates is not None
        self.assertEqual(result.rates.match_kind, "override")
        self.assertEqual(result.rates.source, "override")
        self.assertIsNone(result.rates.provider)
        self.assertEqual(result.rates.input_usd_per_m, 9.0)
        self.assertEqual(result.rates.cache_read_usd_per_m, 0.9)

    def test_override_defaults_cache_to_input(self) -> None:
        result = find_exact_rates(
            "pinned",
            None,
            {"pinned": {"input": 2.5, "output": 10.0}},
        )
        assert result.rates is not None
        self.assertEqual(result.rates.cache_read_usd_per_m, 2.5)
        self.assertEqual(result.rates.cache_write_usd_per_m, 2.5)

    def test_fuzzy_and_substring_rejected(self) -> None:
        data = _models_dev(
            ("openai", "gpt-5.6-sol", {"input": 5.0, "output": 30.0}),
            ("openai", "claude-sonnet-4-20250514", {"input": 3.0, "output": 15.0}),
        )
        for query in ("gpt-5.6", "gpt", "sonnet", "claude-sonnet-4"):
            with self.subTest(query=query):
                result = find_exact_rates(query, data, {})
                self.assertIsNone(result.rates)
                self.assertIsNotNone(result.error)
                self.assertIn("no exact pricing", result.error or "")

    def test_duplicate_provider_preference_order(self) -> None:
        cost_a = {"input": 1.0, "output": 2.0}
        cost_o = {"input": 10.0, "output": 20.0}
        cost_x = {"input": 100.0, "output": 200.0}
        # Insert openai before anthropic to ensure preference, not insertion order.
        data = _models_dev(
            ("openai", "shared-model", cost_o),
            ("xai", "shared-model", cost_x),
            ("anthropic", "shared-model", cost_a),
        )
        result = find_exact_rates("shared-model", data, {})
        assert result.rates is not None
        self.assertEqual(result.rates.provider, "anthropic")
        self.assertEqual(result.rates.input_usd_per_m, 1.0)

        # Without anthropic, openai wins over xai and unlisted.
        data2 = _models_dev(
            ("reseller-z", "shared-model", {"input": 0.1, "output": 0.2}),
            ("xai", "shared-model", cost_x),
            ("openai", "shared-model", cost_o),
        )
        result2 = find_exact_rates("shared-model", data2, {})
        assert result2.rates is not None
        self.assertEqual(result2.rates.provider, "openai")

    def test_unlisted_providers_deterministic_lexicographic(self) -> None:
        data = _models_dev(
            ("zeta-cloud", "m1", {"input": 1.0, "output": 2.0}),
            ("alpha-cloud", "m1", {"input": 3.0, "output": 4.0}),
            ("mid-cloud", "m1", {"input": 5.0, "output": 6.0}),
        )
        result = find_exact_rates("m1", data, {})
        assert result.rates is not None
        self.assertEqual(result.rates.provider, "alpha-cloud")
        self.assertEqual(result.rates.input_usd_per_m, 3.0)

    def test_preferred_order_constant(self) -> None:
        self.assertEqual(
            PREFERRED_PROVIDERS,
            ("anthropic", "openai", "xai", "google", "mistral", "deepseek"),
        )

    def test_invalid_rates_rejected(self) -> None:
        cases = [
            {"input": -1.0, "output": 1.0},
            {"input": 1.0, "output": float("nan")},
            {"input": 1.0, "output": float("inf")},
            {"input": True, "output": 1.0},
            {"input": "1.0", "output": 2.0},
            {"input": 1.0},  # missing output
            {"output": 1.0},  # missing input
            {"input": 1.0, "output": 2.0, "cache_read": -0.1},
        ]
        for cost in cases:
            with self.subTest(cost=cost):
                data = _models_dev(("openai", "bad-model", cost))
                result = find_exact_rates("bad-model", data, {})
                self.assertIsNone(result.rates)
                self.assertIsNotNone(result.error)

    def test_invalid_override_rates(self) -> None:
        result = find_exact_rates(
            "m",
            None,
            {"m": {"input": -1, "output": 2}},
        )
        self.assertIsNone(result.rates)
        self.assertIn("override", result.error or "")

    def test_invalid_match_skipped_for_next_provider(self) -> None:
        data = _models_dev(
            ("anthropic", "m", {"input": -1.0, "output": 1.0}),
            ("openai", "m", {"input": 4.0, "output": 8.0}),
        )
        result = find_exact_rates("m", data, {})
        assert result.rates is not None
        self.assertEqual(result.rates.provider, "openai")
        self.assertEqual(result.rates.input_usd_per_m, 4.0)

    def test_not_found_and_none_data(self) -> None:
        result = find_exact_rates("missing", None, None)
        self.assertIsNone(result.rates)
        self.assertIn("no exact pricing", result.error or "")

        result2 = find_exact_rates("missing", {}, {})
        self.assertIsNone(result2.rates)

    def test_malformed_pricing_data_type(self) -> None:
        result = find_exact_rates("m", ["not", "a", "map"], {})  # type: ignore[arg-type]
        self.assertIsNone(result.rates)
        self.assertIn("mapping", result.error or "")

    def test_normalization_on_lookup_key(self) -> None:
        data = _models_dev(
            ("google", "Gemini 2.5 Pro", {"input": 1.25, "output": 10.0}),
        )
        result = find_exact_rates("gemini-2.5-pro", data, {})
        assert result.rates is not None
        self.assertEqual(result.rates.model_id, "gemini-2.5-pro")


class ComputeCostTests(unittest.TestCase):
    def test_basic_arithmetic(self) -> None:
        rates = _rates(
            input_usd_per_m=1.0,
            output_usd_per_m=2.0,
            cache_read_usd_per_m=0.1,
            cache_write_usd_per_m=1.25,
        )
        # ordinary=1000, cached=0, write=0, out=500
        cost = compute_cost(_Usage(input_tokens=1000, output_tokens=500), rates)
        self.assertAlmostEqual(cost, (1000 * 1.0 + 500 * 2.0) / 1_000_000)

    def test_disjoint_cached_and_cache_write_buckets(self) -> None:
        rates = _rates(
            input_usd_per_m=10.0,
            output_usd_per_m=0.0,
            cache_read_usd_per_m=1.0,
            cache_write_usd_per_m=12.5,
        )
        # All usage fields are already disjoint after adapter normalization.
        usage = _Usage(
            input_tokens=1000,
            cached_input_tokens=200,
            cache_write_tokens=100,
            output_tokens=0,
        )
        cost = compute_cost(usage, rates)
        expected = (1000 * 10.0 + 200 * 1.0 + 100 * 12.5 + 0) / 1_000_000
        self.assertAlmostEqual(cost, expected)

    def test_rejects_invalid_token_counts(self) -> None:
        rates = _rates()
        with self.assertRaises(ValueError):
            compute_cost(_Usage(input_tokens=-1), rates)
        with self.assertRaises(ValueError):
            compute_cost(
                type(
                    "U",
                    (),
                    {
                        "input_tokens": 1.5,
                        "cached_input_tokens": 0,
                        "cache_write_tokens": 0,
                        "output_tokens": 0,
                    },
                )(),
                rates,
            )
        with self.assertRaises(ValueError):
            compute_cost(
                type(
                    "U",
                    (),
                    {
                        "input_tokens": True,
                        "cached_input_tokens": 0,
                        "cache_write_tokens": 0,
                        "output_tokens": 0,
                    },
                )(),
                rates,
            )

    def test_rejects_bad_rates_type(self) -> None:
        with self.assertRaises(TypeError):
            compute_cost(_Usage(), {"input": 1})  # type: ignore[arg-type]

    def test_zero_usage(self) -> None:
        self.assertEqual(compute_cost(_Usage(), _rates()), 0.0)


class LoadPricingSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.cache_path = self.root / "pricing-cache.json"
        self.url = "https://models.dev/api.json"
        self.sample = _models_dev(
            ("openai", "gpt-test", {"input": 1.0, "output": 2.0}),
        )

    def _write_cache(self, data: Any, *, age_s: float = 0) -> None:
        self.cache_path.write_text(json.dumps(data), encoding="utf-8")
        if age_s:
            st = self.cache_path.stat()
            os.utime(
                self.cache_path,
                (st.st_atime - age_s, st.st_mtime - age_s),
            )

    def test_fresh_cache_no_network(self) -> None:
        self._write_cache(self.sample, age_s=10)
        with mock.patch("basecamp_bench.pricing.urllib.request.urlopen") as urlopen:
            data, prov = load_pricing_snapshot(
                self.cache_path,
                self.url,
                max_age_s=3600,
                allow_network=True,
            )
            urlopen.assert_not_called()
        self.assertEqual(data, self.sample)
        self.assertEqual(prov["source"], "cache")
        self.assertFalse(prov["stale"])
        self.assertIsNone(prov["error"])
        self.assertEqual(prov["url"], self.url)
        self.assertIn("retrieved_at", prov)
        self.assertTrue(prov["retrieved_at"])

    def test_stale_offline_cache(self) -> None:
        self._write_cache(self.sample, age_s=10_000)
        data, prov = load_pricing_snapshot(
            self.cache_path,
            self.url,
            max_age_s=60,
            allow_network=False,
        )
        self.assertEqual(data, self.sample)
        self.assertEqual(prov["source"], "cache")
        self.assertTrue(prov["stale"])
        self.assertIsNone(prov["error"])

    def test_network_success_writes_cache_atomically(self) -> None:
        payload = json.dumps(self.sample).encode("utf-8")
        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = payload
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = None

        replace_calls: list[tuple[str, str]] = []
        real_replace = os.replace

        def tracking_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            replace_calls.append((str(src), str(dst)))
            # temp file must be same directory
            self.assertEqual(Path(src).parent, Path(dst).parent)
            real_replace(src, dst)

        with (
            mock.patch(
                "basecamp_bench.pricing.urllib.request.urlopen",
                return_value=fake_resp,
            ),
            mock.patch("basecamp_bench.pricing.os.replace", side_effect=tracking_replace),
        ):
            data, prov = load_pricing_snapshot(
                self.cache_path,
                self.url,
                max_age_s=60,
                allow_network=True,
                timeout_s=5,
            )

        self.assertEqual(data, self.sample)
        self.assertEqual(prov["source"], "network")
        self.assertFalse(prov["stale"])
        self.assertTrue(prov.get("cache_written"))
        self.assertTrue(self.cache_path.is_file())
        self.assertEqual(json.loads(self.cache_path.read_text(encoding="utf-8")), self.sample)
        self.assertEqual(len(replace_calls), 1)
        # no leftover temp files
        leftovers = list(self.root.glob(".pricing-cache.json.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_network_failure_with_stale_fallback(self) -> None:
        self._write_cache(self.sample, age_s=99_999)
        with mock.patch(
            "basecamp_bench.pricing.urllib.request.urlopen",
            side_effect=URLError("offline"),
        ):
            data, prov = load_pricing_snapshot(
                self.cache_path,
                self.url,
                max_age_s=60,
                allow_network=True,
            )
        self.assertEqual(data, self.sample)
        self.assertEqual(prov["source"], "cache")
        self.assertTrue(prov["stale"])
        self.assertIsNotNone(prov["error"])
        self.assertIn("network", (prov["error"] or "").lower())

    def test_network_failure_without_cache(self) -> None:
        with mock.patch(
            "basecamp_bench.pricing.urllib.request.urlopen",
            side_effect=URLError("offline"),
        ):
            data, prov = load_pricing_snapshot(
                self.cache_path,
                self.url,
                max_age_s=60,
                allow_network=True,
            )
        self.assertIsNone(data)
        self.assertIsNone(prov["source"])
        self.assertFalse(prov["stale"])
        self.assertIsNotNone(prov["error"])

    def test_malformed_cache_with_network_success(self) -> None:
        self.cache_path.write_text("{not-json", encoding="utf-8")
        payload = json.dumps(self.sample).encode("utf-8")
        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = payload
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = None
        with mock.patch(
            "basecamp_bench.pricing.urllib.request.urlopen",
            return_value=fake_resp,
        ):
            data, prov = load_pricing_snapshot(
                self.cache_path,
                self.url,
                max_age_s=60,
                allow_network=True,
            )
        self.assertEqual(data, self.sample)
        self.assertEqual(prov["source"], "network")

    def test_malformed_cache_offline(self) -> None:
        self.cache_path.write_text("{not-json", encoding="utf-8")
        data, prov = load_pricing_snapshot(
            self.cache_path,
            self.url,
            max_age_s=60,
            allow_network=False,
        )
        self.assertIsNone(data)
        self.assertIsNotNone(prov["error"])
        self.assertIn("malformed", (prov["error"] or "").lower())

    def test_malformed_network_json_falls_back_to_stale(self) -> None:
        self._write_cache(self.sample, age_s=99_999)
        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = b"[1,2,3]"  # valid JSON, wrong shape
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = None
        with mock.patch(
            "basecamp_bench.pricing.urllib.request.urlopen",
            return_value=fake_resp,
        ):
            data, prov = load_pricing_snapshot(
                self.cache_path,
                self.url,
                max_age_s=60,
                allow_network=True,
            )
        self.assertEqual(data, self.sample)
        self.assertTrue(prov["stale"])
        self.assertIn("object", (prov["error"] or "").lower())

    def test_malformed_network_json_no_cache(self) -> None:
        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = b"not-json"
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = None
        with mock.patch(
            "basecamp_bench.pricing.urllib.request.urlopen",
            return_value=fake_resp,
        ):
            data, prov = load_pricing_snapshot(
                self.cache_path,
                self.url,
                max_age_s=60,
                allow_network=True,
            )
        self.assertIsNone(data)
        self.assertIsNotNone(prov["error"])

    def test_http_error_handled(self) -> None:
        err = HTTPError(self.url, 503, "Unavailable", hdrs=None, fp=io.BytesIO())  # type: ignore[arg-type]
        with mock.patch(
            "basecamp_bench.pricing.urllib.request.urlopen",
            side_effect=err,
        ):
            data, prov = load_pricing_snapshot(
                self.cache_path,
                self.url,
                max_age_s=60,
                allow_network=True,
            )
        self.assertIsNone(data)
        self.assertIn("503", prov["error"] or "")

    def test_timeout_passed_to_urlopen(self) -> None:
        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = json.dumps(self.sample).encode("utf-8")
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = None
        with mock.patch(
            "basecamp_bench.pricing.urllib.request.urlopen",
            return_value=fake_resp,
        ) as urlopen:
            load_pricing_snapshot(
                self.cache_path,
                self.url,
                max_age_s=1,
                allow_network=True,
                timeout_s=7.5,
            )
            self.assertTrue(urlopen.called)
            _, kwargs = urlopen.call_args
            self.assertEqual(kwargs.get("timeout"), 7.5)

    def test_allow_network_false_never_calls_urlopen(self) -> None:
        with mock.patch("basecamp_bench.pricing.urllib.request.urlopen") as urlopen:
            data, prov = load_pricing_snapshot(
                self.cache_path,
                self.url,
                max_age_s=60,
                allow_network=False,
            )
            urlopen.assert_not_called()
        self.assertIsNone(data)
        self.assertIsNotNone(prov["error"])

    def test_cache_root_non_object_rejected(self) -> None:
        self._write_cache([1, 2, 3], age_s=0)
        data, prov = load_pricing_snapshot(
            self.cache_path,
            self.url,
            max_age_s=3600,
            allow_network=False,
        )
        self.assertIsNone(data)
        self.assertIn("object", (prov["error"] or "").lower())

    def test_provenance_always_has_required_keys(self) -> None:
        _, prov = load_pricing_snapshot(
            self.cache_path,
            self.url,
            max_age_s=60,
            allow_network=False,
        )
        for key in ("source", "url", "cache_path", "retrieved_at", "stale", "error"):
            self.assertIn(key, prov)
        self.assertEqual(prov["cache_path"], self.cache_path.name)
        self.assertNotIn(str(self.root), json.dumps(prov))


class IntegrationLookupWithSnapshotTests(unittest.TestCase):
    def test_lookup_uses_snapshot_data(self) -> None:
        sample = _models_dev(
            ("mistral", "mistral-large", {"input": 2.0, "output": 6.0}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "c.json"
            cache.write_text(json.dumps(sample), encoding="utf-8")
            data, prov = load_pricing_snapshot(
                cache,
                "https://example.test/api.json",
                max_age_s=99999,
                allow_network=False,
            )
            self.assertIsNotNone(data)
            self.assertFalse(prov["stale"])
            result = find_exact_rates(
                "mistral-large",
                data,
                {},
                retrieved_at=prov.get("retrieved_at"),
            )
            assert result.rates is not None
            self.assertEqual(result.rates.provider, "mistral")
            self.assertEqual(result.rates.retrieved_at, prov.get("retrieved_at"))


if __name__ == "__main__":
    unittest.main()
