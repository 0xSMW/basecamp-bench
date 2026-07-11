from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from basecamp_bench.config import (
    BenchConfig,
    EvaluatorSpec,
    HarnessSpec,
    TrackSpec,
    config_to_public_dict,
    load_config,
)


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _root(path: Path) -> None:
    (path / "Repo/reference").mkdir(parents=True)
    _write(path / "Repo/INIT.md")
    _write(path / "Repo/reference/item.txt")
    _write(path / "benchmarks/reference-pack.json", "{}\n")
    for track in ("fe", "be"):
        _write(path / f"benchmarks/{track}/prompt.md")
        _write(path / f"benchmarks/{track}/eval.md")
        _write(path / f"benchmarks/{track}/contract.json", "{}\n")


class TempRoot(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        _root(self.root)

    def config(self, text: str, name: str = "bench.toml") -> Path:
        path = self.root / name
        _write(path, text.strip() + "\n")
        return path


class DefaultsTests(unittest.TestCase):
    def test_checked_in_defaults(self) -> None:
        repo = Path(__file__).resolve().parent.parent
        config = load_config(root=repo)
        self.assertEqual(config.mode, "local")
        self.assertEqual(config.timeout_s, 14400)
        self.assertEqual(config.repetitions, 1)
        self.assertFalse(config.full_access)
        self.assertEqual(set(config.harnesses), {"codex", "claude", "grok"})
        self.assertEqual(
            {key: config.harnesses[key].display_name for key in ("codex", "claude", "grok")},
            {
                "codex": "GPT-5.6 Sol",
                "claude": "Claude Fable 5",
                "grok": "Grok 4.5",
            },
        )
        self.assertEqual(set(config.tracks), {"fe", "be"})
        self.assertEqual(config.evaluators[0].id, "eval-sol")
        self.assertEqual(config.run_root, repo / "runs")

    def test_public_dataclasses_are_frozen(self) -> None:
        harness = HarnessSpec("x", "codex", "m", "high", "p", "X", None, True)
        evaluator = EvaluatorSpec("e", "x", "m", "high", "p", True)
        track = TrackSpec("x", Path("a"), Path("b"), Path("c"))
        with self.assertRaises(FrozenInstanceError):
            harness.enabled = False  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            evaluator.enabled = False  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            track.id = "y"  # type: ignore[misc]
        self.assertTrue(hasattr(BenchConfig, "__dataclass_fields__"))


class LoadingTests(TempRoot):
    def test_precedence_defaults_toml_overrides(self) -> None:
        path = self.config("""
            mode = "publication"
            timeout_s = 200
            repetitions = 2
            full_access = true
            [harnesses.codex]
            model = "configured"
            [tracks.fe]
            prompt = "benchmarks/fe/prompt.md"
        """)
        config = load_config(
            path,
            root=self.root,
            mode_override="local",
            repetitions_override=4,
            timeout_override=300,
        )
        self.assertEqual((config.mode, config.repetitions, config.timeout_s), ("local", 4, 300))
        self.assertTrue(config.full_access)
        self.assertEqual(config.harnesses["codex"].model, "configured")

    def test_root_defaults_to_config_parent(self) -> None:
        path = self.config("mode = 'local'")
        self.assertEqual(load_config(path).root, self.root.resolve())

    def test_harness_merge_and_new_harness(self) -> None:
        path = self.config("""
            [harnesses.codex]
            effort = "medium"
            [harnesses.custom]
            adapter = "codex"
            model = "custom-model"
            effort = "high"
            provider_family = "openai"
            display_name = "Custom"
            binary = "/secret/tool"
            enabled = false
        """)
        config = load_config(path, root=self.root)
        self.assertEqual(config.harnesses["codex"].effort, "medium")
        self.assertEqual(config.harnesses["custom"].binary, "/secret/tool")
        self.assertFalse(config.harnesses["custom"].enabled)

    def test_new_harness_and_track_require_fields(self) -> None:
        cases = (
            "[harnesses.new]\nadapter='codex'",
            "[tracks.new]\nprompt='benchmarks/fe/prompt.md'",
        )
        for index, body in enumerate(cases):
            with self.subTest(body=body), self.assertRaises(ValueError):
                load_config(self.config(body, f"c{index}.toml"), root=self.root)

    def test_evaluators_replace_default(self) -> None:
        path = self.config("""
            [[evaluators]]
            id = "eval-grok"
            harness = "grok"
            model = "grok-4.5"
            effort = "high"
            provider_family = "xai"
            enabled = true
        """)
        self.assertEqual(
            [e.id for e in load_config(path, root=self.root).evaluators], ["eval-grok"]
        )

    def test_publication_is_structurally_accepted(self) -> None:
        config = load_config(root=self.root, mode_override="publication")
        self.assertEqual(config.mode, "publication")


class StrictSchemaTests(TempRoot):
    def test_unknown_keys_at_each_level(self) -> None:
        cases = (
            "surprise = 1",
            "[harnesses.codex]\nsurprise = 1",
            "[[evaluators]]\nid='e'\nharness='codex'\nmodel='m'\neffort='high'\nprovider_family='p'\nenabled=true\nsurprise=1",
            "[tracks.fe]\nsurprise = 1",
            "[pricing.m]\ninput=1\noutput=2\nsurprise=1",
            "[harnesses.codex]\nid='codex'",
        )
        for index, body in enumerate(cases):
            with self.subTest(body=body), self.assertRaises(ValueError):
                load_config(self.config(body, f"u{index}.toml"), root=self.root)

    def test_strict_scalar_types(self) -> None:
        cases = (
            "timeout_s=true",
            "timeout_s=0",
            "repetitions=false",
            "repetitions=-1",
            "full_access=1",
            "mode='preview'",
            "[harnesses.codex]\nenabled='yes'",
        )
        for index, body in enumerate(cases):
            with self.subTest(body=body), self.assertRaises(ValueError):
                load_config(self.config(body, f"s{index}.toml"), root=self.root)
        for kwargs in (
            {"timeout_override": True},
            {"repetitions_override": 0},
            {"mode_override": "x"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                load_config(root=self.root, **kwargs)  # type: ignore[arg-type]

    def test_unsafe_ids(self) -> None:
        cases = (
            "[harnesses.'../bad']\nadapter='codex'",
            "[tracks.'Bad ID']\nprompt='x'",
            "[pricing.'UPPER']\ninput=1\noutput=2",
            "[[evaluators]]\nid='Bad'\nharness='codex'\nmodel='m'\neffort='h'\nprovider_family='p'\nenabled=true",
        )
        for index, body in enumerate(cases):
            with self.subTest(body=body), self.assertRaises(ValueError):
                load_config(self.config(body, f"i{index}.toml"), root=self.root)

    def test_unknown_adapter(self) -> None:
        path = self.config("""
            [harnesses.bad]
            adapter='not-registered'
            model='m'
            effort='high'
            provider_family='p'
            display_name='Bad'
        """)
        with self.assertRaisesRegex(ValueError, "unknown adapter"):
            load_config(path, root=self.root)


class EvaluatorAndSelectionTests(TempRoot):
    def test_unknown_and_duplicate_evaluator_refs(self) -> None:
        unknown = self.config(
            """
            [[evaluators]]
            id='e'
            harness='missing'
            model='m'
            effort='high'
            provider_family='p'
            enabled=true
        """,
            "unknown.toml",
        )
        with self.assertRaisesRegex(ValueError, "unknown harness"):
            load_config(unknown, root=self.root)
        duplicate = self.config(
            """
            [[evaluators]]
            id='e'
            harness='codex'
            model='m'
            effort='high'
            provider_family='p'
            enabled=true
            [[evaluators]]
            id='e'
            harness='grok'
            model='m'
            effort='high'
            provider_family='p'
            enabled=true
        """,
            "duplicate.toml",
        )
        with self.assertRaisesRegex(ValueError, "duplicate evaluator"):
            load_config(duplicate, root=self.root)

    def test_disabled_implementation_harness_can_evaluate(self) -> None:
        path = self.config("[harnesses.codex]\nenabled=false")
        config = load_config(path, root=self.root)
        self.assertFalse(config.harnesses["codex"].enabled)
        self.assertEqual(config.evaluators[0].harness, "codex")

    def test_requires_enabled_fleet_and_evaluator(self) -> None:
        fleet = self.config(
            """
            [harnesses.codex]
            enabled=false
            [harnesses.claude]
            enabled=false
            [harnesses.grok]
            enabled=false
        """,
            "fleet.toml",
        )
        with self.assertRaisesRegex(ValueError, "enabled implementation harness"):
            load_config(fleet, root=self.root)
        evaluators = self.config(
            """
            [[evaluators]]
            id='e'
            harness='codex'
            model='m'
            effort='high'
            provider_family='p'
            enabled=false
        """,
            "evaluators.toml",
        )
        with self.assertRaisesRegex(ValueError, "enabled evaluator"):
            load_config(evaluators, root=self.root)

    def test_harness_selection_toggles_fleet_and_keeps_definitions(self) -> None:
        config = load_config(root=self.root, selected_harnesses=["grok"])
        self.assertEqual(set(config.harnesses), {"codex", "claude", "grok"})
        self.assertEqual(
            [key for key, value in config.harnesses.items() if value.enabled], ["grok"]
        )
        self.assertEqual(config.evaluators[0].harness, "codex")

    def test_track_selection_filters(self) -> None:
        config = load_config(root=self.root, selected_tracks=["be"])
        self.assertEqual(set(config.tracks), {"be"})

    def test_invalid_selections(self) -> None:
        for kwargs in (
            {"selected_harnesses": []},
            {"selected_tracks": []},
            {"selected_harnesses": ["missing"]},
            {"selected_tracks": ["missing"]},
            {"selected_harnesses": ["grok", "grok"]},
            {"selected_tracks": ["be", "be"]},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                load_config(root=self.root, **kwargs)

    def test_selecting_config_disabled_harness_fails(self) -> None:
        path = self.config("[harnesses.grok]\nenabled=false")
        with self.assertRaisesRegex(ValueError, "disabled"):
            load_config(path, root=self.root, selected_harnesses=["grok"])


class PricingTests(TempRoot):
    def test_rates_normalized_and_cache_defaults(self) -> None:
        path = self.config("""
            [pricing."gpt-5.6-sol"]
            input=5
            output=30.0
            [pricing.claude]
            input=3
            output=15
            cache_read=0.3
            cache_write=3.75
        """)
        rates = load_config(path, root=self.root).pricing_overrides
        self.assertEqual(
            dict(rates["gpt-5.6-sol"]),
            {"input": 5.0, "output": 30.0, "cache_read": 5.0, "cache_write": 5.0},
        )
        self.assertEqual(rates["claude"]["cache_write"], 3.75)

    def test_invalid_rates(self) -> None:
        cases = (
            "[pricing.m]\ninput=1",
            "[pricing.m]\ninput=true\noutput=2",
            "[pricing.m]\ninput=-1\noutput=2",
            "[pricing.m]\ninput=inf\noutput=2",
            "[pricing.m]\ninput=1\noutput=2\ncache_read=false",
        )
        for index, body in enumerate(cases):
            with self.subTest(body=body), self.assertRaises(ValueError):
                load_config(self.config(body, f"p{index}.toml"), root=self.root)


class PathSafetyTests(TempRoot):
    def test_rejects_absolute_traversal_and_root_run_path(self) -> None:
        cases = (
            f"seed_root='{self.root / 'Repo'}'",
            "seed_root='../outside'",
            "run_root='.'",
            "reference_manifest='../../x'",
            "[tracks.fe]\nprompt='/tmp/x'",
        )
        for index, body in enumerate(cases):
            with self.subTest(body=body), self.assertRaises(ValueError):
                load_config(self.config(body, f"path{index}.toml"), root=self.root)

    def test_run_root_may_be_absent(self) -> None:
        config = load_config(self.config("run_root='future/runs'"), root=self.root)
        self.assertEqual(config.run_root, self.root.resolve() / "future/runs")

    def test_missing_and_empty_inputs(self) -> None:
        missing = self.config("reference_manifest='missing.json'", "missing.toml")
        with self.assertRaises(ValueError):
            load_config(missing, root=self.root)
        empty = self.root / "benchmarks/fe/prompt.md"
        empty.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "empty"):
            load_config(root=self.root)

    def test_bad_config_file(self) -> None:
        with self.assertRaises(ValueError):
            load_config(self.root / "missing.toml", root=self.root)
        outside = Path(self.temp.name).parent / "outside-bench.toml"
        _write(outside, "mode='local'\n")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        with self.assertRaises(ValueError):
            load_config(outside, root=self.root)
        empty = self.root / "empty.toml"
        empty.touch()
        with self.assertRaisesRegex(ValueError, "empty"):
            load_config(empty, root=self.root)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_rejects_symlink_input_and_component(self) -> None:
        target = self.root / "Repo-real"
        target.mkdir()
        link = self.root / "Repo-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaisesRegex(ValueError, "symlink"):
            load_config(self.config("seed_root='Repo-link'"), root=self.root)
        runs_target = self.root / "runs-real"
        runs_target.mkdir()
        runs_link = self.root / "runs-link"
        runs_link.symlink_to(runs_target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            load_config(self.config("run_root='runs-link/new'", "run.toml"), root=self.root)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_rejects_symlink_config(self) -> None:
        real = self.config("mode='local'", "real.toml")
        link = self.root / "link.toml"
        try:
            link.symlink_to(real)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaisesRegex(ValueError, "symlink"):
            load_config(link, root=self.root)


class PublicSerializationTests(TempRoot):
    def test_deterministic_portable_redacted_dict(self) -> None:
        path = self.config("""
            [harnesses.codex]
            binary='/secret/codex'
            [pricing.z]
            input=2
            output=4
            [pricing.a]
            input=1
            output=3
        """)
        config = load_config(path, root=self.root, selected_harnesses=["grok"])
        first = config_to_public_dict(config)
        second = config_to_public_dict(config)
        self.assertEqual(first, second)
        json.dumps(first, sort_keys=True)
        self.assertNotIn("/secret", json.dumps(first))
        self.assertNotIn("binary", first["harnesses"]["codex"])
        self.assertEqual(list(first["harnesses"]), ["claude", "codex", "grok"])
        self.assertEqual(list(first["pricing"]), ["a", "z"])
        self.assertEqual(first["seed_root"], "Repo")
        self.assertEqual(first["tracks"]["fe"]["prompt"], "benchmarks/fe/prompt.md")
        self.assertFalse(first["harnesses"]["codex"]["enabled"])
        for value in (first["run_root"], first["seed_root"], first["reference_root"]):
            self.assertFalse(Path(value).is_absolute())


if __name__ == "__main__":
    unittest.main()
