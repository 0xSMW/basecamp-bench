"""Real-subprocess end-to-end coverage for the benchmark lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from basecamp_bench import adapters as adapters_module
from basecamp_bench.adapters import (
    AgentJob,
    Harness,
    ParsedOutput,
    Usage,
    register_harness,
)
from basecamp_bench.config import BenchConfig, EvaluatorSpec, HarnessSpec, TrackSpec
from basecamp_bench.manifest import export_run, verify_run
from basecamp_bench.runner import RunOptions, reevaluate_run, run_benchmark
from basecamp_bench.safety import tree_manifest

_PROMPT = b"Build the strongest complete implementation from the supplied materials.\n"
_RUBRIC = "# Evaluation\n\nScore craft from directly observed behavior.\n"
_CONTRACT = {
    "schema_version": "1.0",
    "contract_version": "e2e-1",
    "track": "fe",
    "description": "Real subprocess end-to-end fixture",
    "dimensions": [
        {
            "id": "craft",
            "label": "Craft",
            "weight": 1.0,
            "anchors": {"0": "absent", "5": "partial", "10": "complete"},
        }
    ],
    "overall_policy": {"method": "weighted_sum", "precision": 4, "missing": "invalidate"},
}


class FakeSubprocessHarness(Harness):
    name = "fake-e2e"

    def build_command(self, job: AgentJob) -> list[str]:
        fixture = Path(__file__).parent / "fixtures" / "fake_agent.py"
        command = [
            self.resolve_binary(),
            os.fspath(fixture),
            "--kind",
            job.kind,
            "--workdir",
            os.fspath(job.workdir),
            "--prompt",
            os.fspath(job.prompt_path),
            "--model",
            job.model.model,
            "--last-message",
            os.fspath(job.last_message_path),
        ]
        for evidence in job.evidence_dirs:
            command.extend(("--evidence", os.fspath(evidence)))
        return command

    def parse_output(self, job: AgentJob, stdout_text: str) -> ParsedOutput:
        try:
            payload = json.loads(stdout_text)
        except (json.JSONDecodeError, TypeError):
            return ParsedOutput()
        raw_usage = payload.get("usage")
        usage = None
        if isinstance(raw_usage, dict):
            try:
                usage = Usage(
                    **{
                        key: int(raw_usage.get(key, 0))
                        for key in (
                            "input_tokens",
                            "cached_input_tokens",
                            "cache_write_tokens",
                            "output_tokens",
                        )
                    }
                )
            except (TypeError, ValueError):
                usage = None
        cost = payload.get("reported_cost_usd")
        return ParsedOutput(
            usage=usage,
            reported_cost_usd=float(cost)
            if isinstance(cost, (int, float)) and not isinstance(cost, bool)
            else None,
            last_message=payload.get("last_message")
            if isinstance(payload.get("last_message"), str)
            else None,
            session_id=payload.get("session_id")
            if isinstance(payload.get("session_id"), str)
            else None,
        )


class Ids:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.index = 0

    def __call__(self) -> str:
        self.index += 1
        return f"{self.prefix}-{self.index:03d}"


class E2EFixture(unittest.TestCase):
    def setUp(self) -> None:
        previous = adapters_module._HARNESS_TYPES.get(FakeSubprocessHarness.name)
        register_harness(FakeSubprocessHarness, replace=True)
        self.addCleanup(self._restore_adapter, previous)
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.run_root = self.root / "runs"
        self.run_root.mkdir()
        self.seed = self.root / "Repo"
        self.seed.mkdir()
        (self.seed / "INIT.md").write_text("Seed specification\n", encoding="utf-8")
        self.reference = self.seed / "reference"
        self.reference.mkdir()
        reference_bytes = b"reference evidence\n"
        (self.reference / "evidence.txt").write_bytes(reference_bytes)
        self.reference_manifest = self.root / "reference-pack.json"
        self.reference_manifest.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "pack_id": "e2e-pack",
                    "pack_version": "1",
                    "distributable": True,
                    "assets": [
                        {
                            "path": "evidence.txt",
                            "sha256": hashlib.sha256(reference_bytes).hexdigest(),
                            "owner": "fixture",
                            "source": "generated test fixture",
                            "license": "CC0-1.0",
                            "modifications": "none",
                            "distributable": True,
                        }
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        benchmark = self.root / "benchmarks" / "fe"
        benchmark.mkdir(parents=True)
        self.prompt = benchmark / "prompt.md"
        self.prompt.write_bytes(_PROMPT)
        self.rubric = benchmark / "eval.md"
        self.rubric.write_text(_RUBRIC, encoding="utf-8")
        self.contract = benchmark / "contract.json"
        self.contract.write_text(json.dumps(_CONTRACT, sort_keys=True) + "\n", encoding="utf-8")
        (self.root / "schemas").mkdir()
        (self.root / "schemas" / "fixture.json").write_text("{}\n", encoding="utf-8")

    @staticmethod
    def _restore_adapter(previous: type[Harness] | None) -> None:
        if previous is None:
            adapters_module._HARNESS_TYPES.pop(FakeSubprocessHarness.name, None)
        else:
            adapters_module._HARNESS_TYPES[FakeSubprocessHarness.name] = previous

    def config(
        self,
        *,
        producer_model: str = "fake-success",
        evaluator_models: tuple[str, ...] = ("fake-judge-a", "fake-judge-b"),
        mode: str = "local",
        repetitions: int = 1,
        timeout_s: int = 10,
    ) -> BenchConfig:
        harness = HarnessSpec(
            id="fake-producer",
            adapter=FakeSubprocessHarness.name,
            model=producer_model,
            effort="high",
            provider_family="fixture",
            display_name="Fake Producer",
            binary=sys.executable,
            enabled=True,
        )
        evaluators = tuple(
            EvaluatorSpec(
                id=f"evaluator-{index}",
                harness="fake-producer",
                model=model,
                effort="high",
                provider_family="fixture",
                enabled=True,
            )
            for index, model in enumerate(evaluator_models, 1)
        )
        return BenchConfig(
            root=self.root,
            mode=mode,  # type: ignore[arg-type]
            run_root=self.run_root,
            seed_root=self.seed,
            reference_root=self.reference,
            reference_manifest=self.reference_manifest,
            timeout_s=timeout_s,
            full_access=False,
            repetitions=repetitions,
            harnesses={"fake-producer": harness},
            evaluators=evaluators,
            tracks={"fe": TrackSpec("fe", self.prompt, self.rubric, self.contract)},
            pricing_overrides={},
        )

    @staticmethod
    def pricing(*models: str) -> dict:
        return {
            "fixture": {
                "models": {
                    model: {
                        "cost": {
                            "input": 1.0,
                            "output": 2.0,
                            "cache_read": 0.5,
                            "cache_write": 1.5,
                        }
                    }
                    for model in models
                }
            }
        }

    def run_benchmark_e2e(
        self,
        config: BenchConfig,
        *,
        prefix: str,
    ) -> Path:
        models = [spec.model for spec in config.harnesses.values() if spec.enabled]
        models.extend(spec.model for spec in config.evaluators if spec.enabled)
        return run_benchmark(
            config,
            options=RunOptions(
                allow_unsafe_host_execution=False,
                confirmed_isolated_environment=True,
                allow_network_pricing=False,
            ),
            id_factory=Ids(prefix),
            pricing_data=self.pricing(*models),
            now=datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC),
        )

    @staticmethod
    def attempts(run_dir: Path) -> list[dict]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((run_dir / "attempts").glob("*.json"))
        ]

    @staticmethod
    def manifest(run_dir: Path) -> dict:
        return json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))


class PublicationLifecycleTests(E2EFixture):
    def test_real_publication_pipeline_verify_and_deterministic_export(self) -> None:
        config = self.config(mode="publication", repetitions=3)
        run_dir = self.run_benchmark_e2e(config, prefix="publication")
        attempts = self.attempts(run_dir)
        self.assertEqual(len(attempts), 3)
        self.assertTrue(all(item["implementation_success"] for item in attempts))
        self.assertTrue(all(item["evaluation_success"] for item in attempts))
        self.assertTrue(all(len(item["evaluator_ids"]) == 2 for item in attempts))
        self.assertTrue(all(not item["ineligible_reasons"] for item in attempts))
        self.assertEqual(self.manifest(run_dir)["status"], "complete")
        self.assertEqual(verify_run(run_dir), [])
        self.assertEqual(
            {path.read_bytes() for path in (run_dir / "prompts").glob("implement-*.md")},
            {_PROMPT},
        )
        snapshots = sorted((run_dir / "snapshots").iterdir())
        self.assertEqual(len(snapshots), 3)
        self.assertTrue(all((snapshot / "artifact.py").is_file() for snapshot in snapshots))
        eval_jobs = [job for job in self.manifest(run_dir)["jobs"] if job["kind"] == "evaluate"]
        self.assertEqual(len(eval_jobs), 6)
        self.assertTrue(all(job["valid"] for job in eval_jobs))
        leaderboard = json.loads(
            next((run_dir / "leaderboards").glob("*.json")).read_text(encoding="utf-8")
        )
        self.assertEqual(leaderboard["schema_version"], "2.0")
        self.assertEqual(leaderboard["mode"], "publication")
        self.assertNotIn("entries", leaderboard)
        self.assertIn("attempts", leaderboard)
        ledger_attempts = leaderboard["attempts"]
        self.assertEqual(len(ledger_attempts), 3)
        self.assertEqual(
            sorted(item["repetition"] for item in ledger_attempts),
            [1, 2, 3],
        )
        self.assertTrue(all(item["implementation_success"] is True for item in ledger_attempts))
        self.assertTrue(all(item["evaluation_success"] is True for item in ledger_attempts))
        self.assertTrue(all(len(item["evaluator_ids"]) == 2 for item in ledger_attempts))
        self.assertTrue(all(item["ineligible_reasons"] == [] for item in ledger_attempts))
        self.assertTrue(all(item["track"] == "fe" for item in ledger_attempts))
        self.assertEqual(leaderboard["track"], "fe")
        self.assertTrue(
            all(
                item["contract_version"] == leaderboard["contract_version"]
                and item["contract_sha256"] == leaderboard["contract_sha256"]
                for item in ledger_attempts
            )
        )
        report = (run_dir / "report.html").read_text(encoding="utf-8")
        self.assertIn("Expected implementation cost per valid result", report)
        self.assertIn("attempts-table", report)
        first, second = self.root / "export-a.zip", self.root / "export-b.zip"
        export_run(run_dir, first)
        export_run(run_dir, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())


class ReevaluateLifecycleTests(E2EFixture):
    def test_reevaluate_immutable_prior_snapshot_fresh_evaluators(self) -> None:
        """Reevaluate a local prior: fresh evaluators, no new implementation, lineage intact."""

        config = self.config(mode="local", repetitions=1)
        prior = self.run_benchmark_e2e(config, prefix="reeval-prior")
        prior_tree = tree_manifest(prior)
        prior_manifest = self.manifest(prior)
        prior_id = prior_manifest["run"]["id"]
        prior_attempt = self.attempts(prior)[0]
        prior_sid = prior_attempt["submission_id"]
        prior_impl_cost = prior_attempt["implementation_cost_usd"]
        impl_jobs_prior = [j for j in prior_manifest["jobs"] if j.get("kind") == "implement"]
        self.assertEqual(len(impl_jobs_prior), 1)
        usage = impl_jobs_prior[0].get("usage") or {}
        prior_impl_tokens = (
            int(usage.get("input_tokens", 0))
            + int(usage.get("cached_input_tokens", 0))
            + int(usage.get("cache_write_tokens", 0))
            + int(usage.get("output_tokens", 0))
        )
        models = [spec.model for spec in config.harnesses.values() if spec.enabled]
        models.extend(spec.model for spec in config.evaluators if spec.enabled)
        new_dir = reevaluate_run(
            config,
            prior,
            options=RunOptions(
                allow_unsafe_host_execution=False,
                confirmed_isolated_environment=True,
                allow_network_pricing=False,
            ),
            id_factory=Ids("reeval-new"),
            pricing_data=self.pricing(*models),
            now=datetime(2026, 7, 11, 13, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(tree_manifest(prior), prior_tree)
        self.assertEqual(verify_run(prior), [])
        self.assertEqual(verify_run(new_dir), [])
        self.assertNotEqual(new_dir.resolve(), prior.resolve())
        man = self.manifest(new_dir)
        self.assertEqual(man["status"], "complete")
        impl_jobs = [j for j in man["jobs"] if j.get("kind") == "implement"]
        self.assertEqual(len(impl_jobs), 1)
        self.assertIn("reuse prior_run=", impl_jobs[0]["command_preview"])
        self.assertIn(f"prior_run={prior_id}", impl_jobs[0]["command_preview"])
        eval_jobs = [j for j in man["jobs"] if j.get("kind") == "evaluate"]
        self.assertEqual(len(eval_jobs), 2)
        self.assertTrue(all(job.get("valid") for job in eval_jobs))
        # Reuse records implementation provenance without materializing a workspace tree.
        workspace_root = new_dir / "workspaces"
        self.assertFalse(workspace_root.exists() and any(workspace_root.iterdir()))
        att = self.attempts(new_dir)[0]
        self.assertEqual(att["submission_id"], prior_sid)
        self.assertEqual(att["run_id"], man["run"]["id"])
        self.assertTrue(att["implementation_success"])
        self.assertTrue(att["evaluation_success"])
        # Historical attribution retained on the attempt; incurred run costs are eval-only.
        self.assertEqual(att["implementation_cost_usd"], prior_impl_cost)
        self.assertEqual(man["costs"]["known_implementation_usd"], 0)
        self.assertEqual(
            man["costs"]["known_total_usd"],
            man["costs"]["known_evaluation_usd"],
        )
        self.assertTrue(man["costs"]["complete"])
        self.assertEqual(man["costs"]["unknown_job_count"], 0)
        # Two evaluators × (7+1+0+3) tokens plus historical implementation tokens.
        self.assertEqual(att["tokens"], prior_impl_tokens + 22)
        self.assertIn(f"reuse_snapshot_tree:{prior_sid}", man["inputs"])
        self.assertIn(f"prior_run:{prior_id}", man["inputs"])
        snaps = [path for path in (new_dir / "snapshots").iterdir() if path.is_dir()]
        self.assertEqual(len(snaps), 1)
        self.assertTrue((snaps[0] / "artifact.py").is_file())


class FailureIntegrityTests(E2EFixture):
    def test_invalid_evaluators_never_count(self) -> None:
        modes = (
            "fake-judge-nonzero",
            "fake-judge-malformed",
            "fake-judge-missing-result",
            "fake-judge-mutate-seed",
            "fake-judge-mutate-submission",
        )
        for index, mode in enumerate(modes, 1):
            with self.subTest(mode=mode):
                run_dir = self.run_benchmark_e2e(
                    self.config(evaluator_models=(mode,)),
                    prefix=f"invalid-{index}",
                )
                attempt = self.attempts(run_dir)[0]
                self.assertTrue(attempt["implementation_success"])
                self.assertFalse(attempt["evaluation_success"])
                self.assertEqual(attempt["evaluator_ids"], [])
                jobs = [job for job in self.manifest(run_dir)["jobs"] if job["kind"] == "evaluate"]
                self.assertEqual(len(jobs), 1)
                self.assertFalse(jobs[0]["valid"])
                self.assertTrue(jobs[0]["invalid_reasons"])

    def test_partial_failed_implementation_is_never_snapshotted(self) -> None:
        run_dir = self.run_benchmark_e2e(
            self.config(producer_model="fake-partial", evaluator_models=("fake-judge-a",)),
            prefix="partial",
        )
        attempt = self.attempts(run_dir)[0]
        self.assertFalse(attempt["implementation_success"])
        self.assertFalse(attempt["evaluation_success"])
        self.assertEqual(list((run_dir / "snapshots").iterdir()), [])
        self.assertEqual(list((run_dir / "evaluations").iterdir()), [])
        self.assertTrue(any((run_dir / "workspaces").rglob("artifact.py")))

    @unittest.skipUnless(os.name == "posix", "process-group descendant checks require POSIX")
    def test_timeout_reaps_grandchild_and_does_not_snapshot(self) -> None:
        run_dir = self.run_benchmark_e2e(
            self.config(
                producer_model="fake-timeout",
                evaluator_models=("fake-judge-a",),
                timeout_s=1,
            ),
            prefix="timeout",
        )
        pid_path = next((run_dir / "workspaces").rglob("grandchild.pid"))
        pid = int(pid_path.read_text(encoding="ascii"))
        deadline = time.monotonic() + 3.0
        while self._pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self._pid_alive(pid), f"grandchild {pid} survived process-group timeout")
        attempt = self.attempts(run_dir)[0]
        self.assertFalse(attempt["implementation_success"])
        self.assertIn("implementation_timeout", attempt["ineligible_reasons"])
        self.assertEqual(list((run_dir / "snapshots").iterdir()), [])

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return bool(status) and not status.startswith("Z")


if __name__ == "__main__":
    unittest.main()
