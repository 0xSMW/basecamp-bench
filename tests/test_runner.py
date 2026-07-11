"""Unit/integration tests for basecamp_bench.runner (stdlib unittest only)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from basecamp_bench import adapters as adapters_mod
from basecamp_bench.adapters import (
    AgentJob,
    Harness,
    ModelSpec,
    ParsedOutput,
    Usage,
    get_harness,
    register_harness,
    registered_harnesses,
)
from basecamp_bench.config import BenchConfig, EvaluatorSpec, HarnessSpec, TrackSpec
from basecamp_bench.manifest import verify_run
from basecamp_bench.processes import ProcessResult
from basecamp_bench.runner import (
    AgentExecution,
    RunOptions,
    execute_agent,
    materialize_seed,
    new_run_id,
    reevaluate_run,
    run_benchmark,
)
from basecamp_bench.safety import tree_manifest

_PROMPT_BYTES = b"EXACT_PROMPT_BYTES_v1_do_not_wrap\n"
_RUBRIC = "# Rubric\n\nScore craft from evidence.\n"
_CONTRACT = {
    "schema_version": "1.0",
    "contract_version": "1.0",
    "track": "fe",
    "description": "Minimal test contract",
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


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _proc(
    *,
    returncode: int | None = 0,
    timed_out: bool = False,
    error: str | None = None,
) -> ProcessResult:
    return ProcessResult(
        returncode=returncode,
        duration_s=0.125,
        timed_out=timed_out,
        interrupted=False,
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        error=error,
    )


class _Ids:
    def __init__(self, prefix: str = "id") -> None:
        self.n, self.prefix = 0, prefix

    def __call__(self) -> str:
        self.n += 1
        return f"{self.prefix}{self.n:03d}"


class FakeHarness(Harness):
    name = "fake"

    def build_command(self, job: AgentJob) -> list[str]:
        return [
            "/usr/local/bin/fake-agent",
            "--cwd",
            str(job.workdir),
            "--prompt-file",
            str(job.prompt_path),
            "--kind",
            job.kind,
        ]

    def parse_output(self, job: AgentJob, stdout_text: str) -> ParsedOutput:
        try:
            data = json.loads(stdout_text) if stdout_text.strip() else {}
        except json.JSONDecodeError:
            data = {}
        usage = None
        if isinstance(data.get("usage"), dict):
            u = data["usage"]
            usage = Usage(
                input_tokens=int(u.get("input_tokens", 0)),
                cached_input_tokens=int(u.get("cached_input_tokens", 0)),
                cache_write_tokens=int(u.get("cache_write_tokens", 0)),
                output_tokens=int(u.get("output_tokens", 0)),
            )
        cost = data.get("reported_cost_usd")
        return ParsedOutput(
            usage=usage,
            reported_cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            last_message=data.get("last_message")
            if isinstance(data.get("last_message"), str)
            else None,
        )


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        register_harness(FakeHarness, replace=True)
        self.addCleanup(adapters_mod._HARNESS_TYPES.pop, "fake", None)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.run_root = self.root / "runs"
        self.run_root.mkdir()
        self.seed = self.root / "seed"
        self.seed.mkdir()
        (self.seed / "AGENTS.md").write_text("seed\n", encoding="utf-8")
        (self.seed / ".env").write_text("SECRET=should-not-copy\n", encoding="utf-8")
        (self.seed / ".git").mkdir()
        (self.seed / ".git" / "config").write_text("x", encoding="utf-8")
        (self.seed / "reference").mkdir()
        (self.seed / "reference" / "stale.txt").write_text("old\n", encoding="utf-8")
        self.ref = self.root / "reference"
        self.ref.mkdir()
        body = b"reference-asset-v1\n"
        (self.ref / "note.txt").write_bytes(body)
        self.manifest = self.root / "reference-pack.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "pack_id": "test-pack",
                    "pack_version": "1",
                    "distributable": True,
                    "assets": [
                        {
                            "path": "note.txt",
                            "sha256": _sha(body),
                            "owner": "O",
                            "source": "S",
                            "license": "MIT",
                            "modifications": "None",
                            "distributable": True,
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        tdir = self.root / "benchmarks" / "fe"
        tdir.mkdir(parents=True)
        self.prompt = tdir / "prompt.md"
        self.prompt.write_bytes(_PROMPT_BYTES)
        self.rubric = tdir / "eval.md"
        self.rubric.write_text(_RUBRIC, encoding="utf-8")
        self.contract = tdir / "contract.json"
        self.contract.write_text(json.dumps(_CONTRACT, indent=2) + "\n", encoding="utf-8")
        (self.root / "schemas").mkdir()
        (self.root / "schemas" / "x.json").write_text("{}\n", encoding="utf-8")
        self.bin = self.root / "fake-agent"
        self.bin.write_text("#!/bin/sh\n", encoding="utf-8")
        self.bin.chmod(0o755)
        self.pricing = {
            "testco": {
                "models": {
                    "contestant-model": {"cost": {"input": 1.0, "output": 2.0}},
                    "judge-model-a": {"cost": {"input": 1.0, "output": 2.0}},
                    "judge-model-b": {"cost": {"input": 1.0, "output": 2.0}},
                }
            }
        }
        self.impl_fail = self.impl_timeout = False
        self.mutate_seed = self.mutate_sub = False
        self.missing_report = self.missing_result = self.malformed = False
        self.eval_fail = False
        self.raise_impl = False
        self.raise_eval = False
        self.interrupt_impl = False
        self.eval_score = 8.0
        self.version_probe_count = 0
        self.version_output = "fake-agent 1.2.3\n"
        self.version_failure = False

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def harness(
        self,
        hid: str = "agent-x",
        model: str = "contestant-model",
        adapter: str = "fake",
    ) -> HarnessSpec:
        return HarnessSpec(
            id=hid,
            adapter=adapter,
            model=model,
            effort="high",
            provider_family="test",
            display_name=f"Display {hid}",
            binary=str(self.bin),
            enabled=True,
        )

    def evaluator(
        self,
        eid: str,
        model: str,
        harness: str = "agent-x",
    ) -> EvaluatorSpec:
        return EvaluatorSpec(
            id=eid,
            harness=harness,
            model=model,
            effort="high",
            provider_family="test",
            enabled=True,
        )

    def config(
        self,
        *,
        mode: str = "local",
        repetitions: int = 1,
        full_access: bool = False,
        harnesses: dict[str, HarnessSpec] | None = None,
        evaluators: tuple[EvaluatorSpec, ...] | None = None,
        pricing_overrides: dict | None = None,
    ) -> BenchConfig:
        h = harnesses or {"agent-x": self.harness()}
        e = evaluators or (
            self.evaluator("eval-a", "judge-model-a"),
            self.evaluator("eval-b", "judge-model-b"),
        )
        return BenchConfig(
            root=self.root,
            mode=mode,  # type: ignore[arg-type]
            run_root=self.run_root,
            seed_root=self.seed,
            reference_root=self.ref,
            reference_manifest=self.manifest,
            timeout_s=30,
            full_access=full_access,
            repetitions=repetitions,
            harnesses=h,
            evaluators=e,
            tracks={
                "fe": TrackSpec(
                    id="fe",
                    prompt_file=self.prompt,
                    rubric_file=self.rubric,
                    contract_file=self.contract,
                )
            },
            pricing_overrides=pricing_overrides or {},
        )

    def options(self, **kw: Any) -> RunOptions:
        base = dict(
            allow_unsafe_host_execution=True,
            confirmed_isolated_environment=True,
            allow_network_pricing=False,
        )
        base.update(kw)
        return RunOptions(**base)

    def side_effect(self, command, **kwargs):  # type: ignore[no-untyped-def]
        stdout_path: Path = kwargs["stdout_path"]
        if "--version" in command:
            self.version_probe_count += 1
            stdout_path.write_text(self.version_output, encoding="utf-8")
            return _proc(returncode=1 if self.version_failure else 0)
        kind = command[command.index("--kind") + 1] if "--kind" in command else "implement"
        workdir = Path(kwargs["cwd"]) if kwargs.get("cwd") else None
        if workdir is None and "--cwd" in command:
            workdir = Path(command[command.index("--cwd") + 1])
        if kind == "implement":
            if self.interrupt_impl:
                raise KeyboardInterrupt("interrupted at private path " + str(self.root))
            if self.raise_impl:
                raise RuntimeError("implementation exploded at " + str(self.root))
            if self.impl_timeout:
                stdout_path.write_text("{}", encoding="utf-8")
                return _proc(returncode=None, timed_out=True)
            if self.impl_fail:
                stdout_path.write_text("{}", encoding="utf-8")
                return _proc(returncode=1)
            assert workdir is not None
            (workdir / "app.py").write_text("print('hi')\n", encoding="utf-8")
            stdout_path.write_text(
                json.dumps(
                    {
                        "usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 0,
                            "cache_write_tokens": 0,
                            "output_tokens": 5,
                        },
                        "reported_cost_usd": 0.01,
                        "last_message": "implemented",
                    }
                ),
                encoding="utf-8",
            )
            return _proc()
        if self.raise_eval:
            raise RuntimeError("evaluator exploded at " + str(self.root))
        prompt_path = Path(command[command.index("--prompt-file") + 1])
        text = prompt_path.read_text(encoding="utf-8")
        report_m = re.search(r"Markdown report path: (.+)", text)
        result_m = re.search(r"Result JSON path: (.+)", text)
        seed_m = re.search(r"Seed directory: (.+)", text)
        sub_m = re.search(r"Submission directory: (.+)", text)
        sid_m = re.search(r"Opaque submission ID: ([a-z0-9._-]+)", text)
        judge_m = re.search(r'"judge_id":\s*"([a-z0-9._-]+)"', text)
        report = Path(report_m.group(1).strip()) if report_m else None
        result = Path(result_m.group(1).strip()) if result_m else None
        seed_dir = Path(seed_m.group(1).strip()) if seed_m else None
        sub_dir = Path(sub_m.group(1).strip()) if sub_m else None
        sid = sid_m.group(1) if sid_m else "unknown"
        judge = judge_m.group(1) if judge_m else "eval-a"
        self.assertIsNotNone(workdir)
        self.assertIsNotNone(report)
        self.assertEqual(report.parent, workdir)
        if self.mutate_seed and seed_dir is not None:
            (seed_dir / "mutated.txt").write_text("x", encoding="utf-8")
        if self.mutate_sub and sub_dir is not None:
            (sub_dir / "mutated.txt").write_text("x", encoding="utf-8")
        if report is not None and not self.missing_report:
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("# Report\nok\n", encoding="utf-8")
        if result is not None and not self.missing_result:
            result.parent.mkdir(parents=True, exist_ok=True)
            if self.malformed:
                result.write_text("{not-json", encoding="utf-8")
            else:
                result.write_text(
                    json.dumps(
                        {
                            "schema_version": "1.0",
                            "track": "fe",
                            "submission_id": sid,
                            "contract_sha256": _sha(self.contract.read_bytes()),
                            "judge_id": judge,
                            "dimensions": {
                                "craft": {
                                    "score": self.eval_score,
                                    "notes": "ok",
                                    "evidence": ["app.py"],
                                }
                            },
                            "summary": "good",
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        stdout_path.write_text(
            json.dumps(
                {
                    "usage": {
                        "input_tokens": 3,
                        "cached_input_tokens": 0,
                        "cache_write_tokens": 0,
                        "output_tokens": 2,
                    },
                    "reported_cost_usd": 0.002,
                    "last_message": "evaluated",
                }
            ),
            encoding="utf-8",
        )
        return _proc(returncode=1 if self.eval_fail else 0)

    def run_bench(
        self,
        config: BenchConfig | None = None,
        *,
        options: RunOptions | None = None,
        id_factory: Any = None,
        pricing_data: dict | None = None,
    ) -> Path:
        with mock.patch("basecamp_bench.runner.run_managed", side_effect=self.side_effect):
            return run_benchmark(
                config or self.config(),
                options=options or self.options(),
                id_factory=id_factory or _Ids("x"),
                pricing_data=self.pricing if pricing_data is None else pricing_data,
                now=datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC),
            )

    def attempt_json(self, run_dir: Path) -> dict:
        return json.loads(next((run_dir / "attempts").glob("*.json")).read_text(encoding="utf-8"))

    def read_run_manifest(self, run_dir: Path) -> dict:
        return json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))


class NewRunIdTests(unittest.TestCase):
    def test_deterministic(self) -> None:
        now = datetime(2026, 7, 11, 15, 30, 0, tzinfo=UTC)
        rid = new_run_id(now=now, nonce="abc123")
        self.assertEqual(rid, "20260711t153000z-abc123")


class MaterializeTests(Fixture):
    def test_excludes_and_overlays(self) -> None:
        dest = self.root / "ws"
        man = materialize_seed(self.config(), dest)
        self.assertTrue((dest / "AGENTS.md").is_file())
        self.assertFalse((dest / ".env").exists())
        self.assertFalse((dest / ".git").exists())
        self.assertFalse((dest / "reference" / "stale.txt").exists())
        self.assertEqual((dest / "reference" / "note.txt").read_bytes(), b"reference-asset-v1\n")
        self.assertIn("reference/note.txt", man)


class ExecuteTests(Fixture):
    def test_preview_and_reported_cost(self) -> None:
        work = self.root / "w"
        work.mkdir()
        p = self.root / "p.md"
        p.write_bytes(_PROMPT_BYTES)
        job = AgentJob(
            kind="implement",
            harness="fake",
            model=ModelSpec(model="contestant-model", effort="high"),
            workdir=work,
            prompt_path=p,
            log_path=self.root / "job.log",
            last_message_path=self.root / "last.md",
        )

        def se(command, **kwargs):  # type: ignore[no-untyped-def]
            kwargs["stdout_path"].write_text(
                json.dumps(
                    {
                        "usage": {
                            "input_tokens": 1_000_000,
                            "cached_input_tokens": 0,
                            "cache_write_tokens": 0,
                            "output_tokens": 0,
                        },
                        "reported_cost_usd": 9.99,
                        "last_message": "ok",
                    }
                ),
                encoding="utf-8",
            )
            return _proc()

        with mock.patch("basecamp_bench.runner.run_managed", side_effect=se):
            result = execute_agent(
                self.config(),
                job,
                pricing_data=self.pricing,
                pricing_retrieved_at="2026-07-11T00:00:00Z",
                options=self.options(),
            )
        self.assertIsInstance(result, AgentExecution)
        self.assertEqual(result.cost_usd, 9.99)
        self.assertNotIn("/usr/local/bin", result.command_preview)
        self.assertIn("fake-agent", result.command_preview)


class PipelineTests(Fixture):
    def test_happy_path(self) -> None:
        run_dir = self.run_bench(id_factory=_Ids("s"))
        prompts = list((run_dir / "prompts").glob("implement-*.md"))
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0].read_bytes(), _PROMPT_BYTES)
        sid = "s002"
        self.assertTrue((run_dir / "snapshots" / sid / "app.py").is_file())
        for path in (run_dir / "snapshots").iterdir():
            n = path.name.lower()
            self.assertNotIn("agent-x", n)
            self.assertNotIn("contestant-model", n)
        evals = list((run_dir / "evaluations" / sid).iterdir())
        self.assertEqual(len(evals), 2)
        for d in evals:
            self.assertNotIn("agent-x", d.name)
            self.assertTrue((d / "output" / "report.md").is_file())
            self.assertTrue((d / "output" / "result.json").is_file())
            prompt = (d / "prompt.md").read_text(encoding="utf-8")
            self.assertNotIn("contestant-model", prompt)
            self.assertIn(sid, prompt)
        self.assertTrue((run_dir / "attempts" / f"{sid}.json").is_file())
        self.assertTrue((run_dir / "report.html").is_file())
        man = self.read_run_manifest(run_dir)
        self.assertEqual(man["status"], "complete")
        for key in man["artifacts"]:
            self.assertFalse(str(key).startswith("logs/"))
            self.assertFalse(str(key).startswith("workspaces/"))
        self.assertGreaterEqual(len(list((run_dir / "leaderboards").glob("*.json"))), 1)

    def test_tooling_records_all_roles_and_deduplicates_version_probe(self) -> None:
        man = self.read_run_manifest(self.run_bench(id_factory=_Ids("t")))
        self.assertEqual(self.version_probe_count, 1)
        tooling = man["tooling"]
        self.assertEqual(len(tooling), 3)
        self.assertEqual(
            {(r["role"], r["config_id"], r["evaluator_id"]) for r in tooling},
            {
                ("implementation", "agent-x", None),
                ("evaluator", "agent-x", "eval-a"),
                ("evaluator", "agent-x", "eval-b"),
            },
        )
        for record in tooling:
            self.assertEqual(record["executable_version"], "fake-agent 1.2.3")
            self.assertIsNone(record["version_error"])
            self.assertEqual(record["adapter_version"], record["runner_version"])
            self.assertEqual(record["deterministic_seed"]["supported"], False)

    def test_evaluator_role_uses_disabled_implementation_harness_config(self) -> None:
        eval_harness = HarnessSpec(
            id="judge-h",
            adapter="fake",
            model="unused-model",
            effort="medium",
            provider_family="test",
            display_name="Judge harness",
            binary=str(self.bin),
            enabled=False,
        )
        cfg = self.config(
            harnesses={"agent-x": self.harness(), "judge-h": eval_harness},
            evaluators=(self.evaluator("eval-a", "judge-model-a", harness="judge-h"),),
        )
        tooling = self.read_run_manifest(self.run_bench(config=cfg, id_factory=_Ids("d")))[
            "tooling"
        ]
        self.assertEqual(self.version_probe_count, 1)
        self.assertEqual(len(tooling), 2)
        judge = next(r for r in tooling if r["role"] == "evaluator")
        self.assertEqual(judge["config_id"], "judge-h")
        self.assertEqual(judge["evaluator_id"], "eval-a")
        self.assertEqual(judge["model_id"], "judge-model-a")

    def test_local_version_error_is_sanitized_and_retained(self) -> None:
        self.version_failure = True
        man = self.read_run_manifest(self.run_bench(id_factory=_Ids("v")))
        self.assertEqual(man["status"], "complete")
        for record in man["tooling"]:
            self.assertIsNone(record["executable_version"])
            self.assertIn("status 1", record["version_error"])

    def test_version_output_redacts_paths_and_credentials(self) -> None:
        secret = "unit-test-secret-token"
        self.version_output = f"fake 1.2.3 binary={self.bin} token={secret}\n"
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": secret}):
            tooling = self.read_run_manifest(self.run_bench(id_factory=_Ids("r")))["tooling"]
        blob = json.dumps(tooling, sort_keys=True)
        self.assertNotIn(str(self.root), blob)
        self.assertNotIn(secret, blob)
        self.assertIn("<path>", blob)
        self.assertIn("<secret>", blob)

    def test_impl_failure_no_snapshot_no_eval(self) -> None:
        self.impl_fail = True
        run_dir = self.run_bench(id_factory=_Ids("f"))
        sid = "f002"
        self.assertFalse((run_dir / "snapshots" / sid).exists())
        self.assertFalse((run_dir / "evaluations" / sid).exists())
        att = self.attempt_json(run_dir)
        self.assertFalse(att["implementation_success"])
        self.assertFalse(att["evaluation_success"])

    def test_manifest_transitions_planned_running_complete(self) -> None:
        from basecamp_bench import runner as runner_module

        statuses: list[str] = []
        transition_errors: list[list[str]] = []
        original = runner_module._write_manifest

        def recording(*args, **kwargs):
            statuses.append(args[4])
            result = original(*args, **kwargs)
            transition_errors.append(verify_run(args[1]))
            return result

        with mock.patch("basecamp_bench.runner._write_manifest", side_effect=recording):
            run_dir = self.run_bench(id_factory=_Ids("state"))
        self.assertEqual(statuses[:2], ["planned", "running"])
        self.assertIn("running", statuses[2:-1])
        self.assertEqual(statuses[-1], "complete")
        self.assertTrue(transition_errors)
        self.assertTrue(all(not errors for errors in transition_errors))
        self.assertEqual(verify_run(run_dir), [])

    def test_implementation_exception_checkpoints_failed_manifest(self) -> None:
        self.raise_impl = True
        with self.assertRaisesRegex(RuntimeError, "implementation exploded"):
            self.run_bench(id_factory=_Ids("impl-crash"))
        run_dir = self.run_root / "impl-crash001"
        manifest = self.read_run_manifest(run_dir)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(len(manifest["jobs"]), 1)
        self.assertEqual(manifest["jobs"][0]["kind"], "implement")
        self.assertIn("implementation exploded", manifest["jobs"][0]["error"])
        self.assertNotIn(str(self.root), json.dumps(manifest))
        self.assertEqual(manifest["artifacts"], {})
        self.assertEqual(verify_run(run_dir), [])

    def test_evaluator_exception_preserves_snapshot_and_failed_job(self) -> None:
        self.raise_eval = True
        with self.assertRaisesRegex(RuntimeError, "evaluator exploded"):
            self.run_bench(id_factory=_Ids("eval-crash"))
        run_dir = self.run_root / "eval-crash001"
        manifest = self.read_run_manifest(run_dir)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual([job["kind"] for job in manifest["jobs"]], ["implement", "evaluate"])
        self.assertFalse(manifest["jobs"][-1]["valid"])
        snapshot_artifacts = {
            key: value
            for key, value in manifest["artifacts"].items()
            if key.startswith("snapshots/")
        }
        self.assertTrue(snapshot_artifacts)
        self.assertEqual(verify_run(run_dir), [])

    def test_keyboard_interrupt_checkpoints_failed_manifest_and_reraises(self) -> None:
        self.interrupt_impl = True
        with self.assertRaises(KeyboardInterrupt):
            self.run_bench(id_factory=_Ids("interrupt"))
        run_dir = self.run_root / "interrupt001"
        manifest = self.read_run_manifest(run_dir)
        self.assertEqual(manifest["status"], "failed")
        self.assertTrue(manifest["jobs"][0]["interrupted"])
        self.assertNotIn(str(self.root), json.dumps(manifest))
        self.assertEqual(verify_run(run_dir), [])


class EvalIntegrityTests(Fixture):
    def test_failed_evaluator_process_cannot_count_valid_artifacts(self) -> None:
        self.eval_fail = True
        run_dir = self.run_bench(id_factory=_Ids("e"))
        att = self.attempt_json(run_dir)
        self.assertFalse(att["evaluation_success"])
        eval_jobs = [
            job for job in self.read_run_manifest(run_dir)["jobs"] if job.get("kind") == "evaluate"
        ]
        self.assertTrue(eval_jobs)
        self.assertTrue(all(not job["valid"] for job in eval_jobs))
        self.assertTrue(
            all("evaluator_execution_failed" in job["invalid_reasons"] for job in eval_jobs)
        )

    def test_seed_mutation(self) -> None:
        self.mutate_seed = True
        att = self.attempt_json(self.run_bench(id_factory=_Ids("m")))
        self.assertFalse(att["evaluation_success"])

    def test_submission_mutation(self) -> None:
        self.mutate_sub = True
        att = self.attempt_json(self.run_bench(id_factory=_Ids("u")))
        self.assertFalse(att["evaluation_success"])

    def test_malformed_result(self) -> None:
        self.malformed = True
        att = self.attempt_json(self.run_bench(id_factory=_Ids("j")))
        self.assertFalse(att["evaluation_success"])

    def test_missing_artifacts(self) -> None:
        self.missing_report = self.missing_result = True
        run_dir = self.run_bench(id_factory=_Ids("n"))
        att = self.attempt_json(run_dir)
        self.assertFalse(att["evaluation_success"])
        self.assertGreaterEqual(len(list((run_dir / "evaluations").rglob("prompt.md"))), 1)


class OverlapTests(Fixture):
    def test_skip_exact_model_overlap(self) -> None:
        cfg = self.config(
            evaluators=(
                self.evaluator("eval-same", "contestant-model"),
                self.evaluator("eval-b", "judge-model-b"),
            )
        )
        run_dir = self.run_bench(config=cfg, id_factory=_Ids("o"))
        skips = [
            j
            for j in self.read_run_manifest(run_dir)["jobs"]
            if j.get("reason") == "contestant_evaluator_model_overlap"
        ]
        self.assertEqual(len(skips), 1)
        att = self.attempt_json(run_dir)
        self.assertTrue(att["evaluation_success"])
        self.assertEqual(att["evaluator_ids"], ["judge-model-b"])


class PublicationTests(Fixture):
    def test_repetitions_gate(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.run_bench(config=self.config(mode="publication", repetitions=2))
        self.assertIn("repetitions", str(ctx.exception).lower())

    def test_isolation_gate(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.run_bench(
                config=self.config(mode="publication", repetitions=3),
                options=self.options(confirmed_isolated_environment=False),
            )
        self.assertIn("isolated", str(ctx.exception).lower())

    def test_missing_pricing_ineligible(self) -> None:
        run_dir = self.run_bench(
            config=self.config(mode="publication", repetitions=3),
            pricing_data={},
            id_factory=_Ids("z"),
        )
        man = self.read_run_manifest(run_dir)
        self.assertEqual(man["status"], "ineligible")
        self.assertFalse(man["pricing"]["complete"])

    def test_missing_tool_version_is_publication_ineligible(self) -> None:
        self.version_failure = True
        run_dir = self.run_bench(
            config=self.config(mode="publication", repetitions=3),
            id_factory=_Ids("tv"),
        )
        man = self.read_run_manifest(run_dir)
        self.assertEqual(man["status"], "ineligible")
        self.assertTrue(all(r["executable_version"] is None for r in man["tooling"]))
        self.assertTrue(all(r["version_error"] for r in man["tooling"]))
        for path in (run_dir / "attempts").glob("*.json"):
            reasons = json.loads(path.read_text(encoding="utf-8"))["ineligible_reasons"]
            self.assertTrue(
                any(reason.startswith("tool_version_unavailable:") for reason in reasons)
            )
        for path in (run_dir / "leaderboards").glob("*.json"):
            board = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(all(not entry["eligible"] for entry in board["entries"]))

    def test_two_evaluators_required(self) -> None:
        cfg = self.config(
            mode="publication",
            repetitions=3,
            evaluators=(self.evaluator("eval-a", "judge-model-a"),),
        )
        run_dir = self.run_bench(config=cfg, id_factory=_Ids("q"))
        self.assertEqual(self.read_run_manifest(run_dir)["status"], "ineligible")
        for path in (run_dir / "attempts").glob("*.json"):
            self.assertFalse(json.loads(path.read_text(encoding="utf-8"))["evaluation_success"])


class UnsafeTests(Fixture):
    def test_workspace_sandboxed_claude_does_not_require_host_ack(self) -> None:
        @register_harness(replace=True)
        class ClaudeLike(Harness):
            name = "claude"

            def build_command(self, job: AgentJob) -> list[str]:
                return ["claude", "x"]

        try:
            cfg = self.config(
                harnesses={"c1": self.harness(hid="c1", adapter="claude", model="m1")},
                evaluators=(self.evaluator("e1", "judge-model-a", harness="c1"),),
                pricing_overrides={
                    "m1": {"input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0},
                    "judge-model-a": {
                        "input": 1.0,
                        "output": 1.0,
                        "cache_read": 1.0,
                        "cache_write": 1.0,
                    },
                },
            )
            run_dir = self.run_bench(
                config=cfg,
                options=RunOptions(
                    allow_unsafe_host_execution=False,
                    confirmed_isolated_environment=False,
                    allow_network_pricing=False,
                ),
                id_factory=_Ids("c"),
            )
            self.assertEqual(self.read_run_manifest(run_dir)["status"], "complete")
        finally:
            from basecamp_bench.adapters import ClaudeHarness

            register_harness(ClaudeHarness, replace=True)

    def test_codex_workspace_write_safe(self) -> None:
        @register_harness(replace=True)
        class CodexLike(Harness):
            name = "codex"

            def build_command(self, job: AgentJob) -> list[str]:
                return [
                    "/opt/codex",
                    "--cwd",
                    str(job.workdir),
                    "--prompt-file",
                    str(job.prompt_path),
                    "--kind",
                    job.kind,
                ]

            def parse_output(self, job: AgentJob, stdout_text: str) -> ParsedOutput:
                return FakeHarness().parse_output(job, stdout_text)

        try:
            cfg = self.config(
                harnesses={"cx": self.harness(hid="cx", adapter="codex")},
                evaluators=(
                    self.evaluator("eval-a", "judge-model-a", harness="cx"),
                    self.evaluator("eval-b", "judge-model-b", harness="cx"),
                ),
                full_access=False,
            )
            run_dir = self.run_bench(
                config=cfg,
                options=RunOptions(
                    allow_unsafe_host_execution=False,
                    confirmed_isolated_environment=False,
                    allow_network_pricing=False,
                ),
                id_factory=_Ids("d"),
            )
            self.assertEqual(self.read_run_manifest(run_dir)["status"], "complete")
        finally:
            from basecamp_bench.adapters import CodexHarness

            register_harness(CodexHarness, replace=True)


class TimeoutCollisionShareTests(Fixture):
    def test_timeout_surfaced(self) -> None:
        self.impl_timeout = True
        run_dir = self.run_bench(id_factory=_Ids("t"))
        att = self.attempt_json(run_dir)
        self.assertFalse(att["implementation_success"])
        self.assertIn("implementation_timeout", att["ineligible_reasons"])
        impl = [j for j in self.read_run_manifest(run_dir)["jobs"] if j.get("kind") == "implement"]
        self.assertTrue(any(j.get("timed_out") for j in impl))

    def test_collision_no_overwrite(self) -> None:
        (self.run_root / "k001").mkdir()
        with self.assertRaises(ValueError) as ctx:
            self.run_bench(id_factory=_Ids("k"))
        self.assertIn("already exists", str(ctx.exception).lower())

    def test_shareable_json_clean(self) -> None:
        run_dir = self.run_bench(id_factory=_Ids("v"))
        abs_root = str(self.root.resolve())
        home = str(Path.home())
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        forbidden = [
            abs_root,
            home,
            "SECRET=should-not-copy",
            "EXACT_PROMPT_BYTES",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "XAI_API_KEY",
            "/usr/local/bin/fake-agent",
        ]
        if user:
            forbidden.append(f"/Users/{user}")
        docs: list[Any] = []
        man = self.read_run_manifest(run_dir)
        docs.append(
            {
                "config": man.get("config"),
                "jobs": man.get("jobs"),
                "artifacts": man.get("artifacts"),
                "pricing": man.get("pricing"),
                "inputs": man.get("inputs"),
            }
        )
        for path in (run_dir / "attempts").glob("*.json"):
            docs.append(json.loads(path.read_text(encoding="utf-8")))
        for path in (run_dir / "leaderboards").glob("*.json"):
            docs.append(json.loads(path.read_text(encoding="utf-8")))

        def walk(obj: Any, path: str = "$") -> None:
            if isinstance(obj, Mapping):
                for k, v in obj.items():
                    walk(k, path)
                    walk(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    walk(v, f"{path}[{i}]")
            elif isinstance(obj, str):
                for bad in forbidden:
                    if bad and bad in obj:
                        self.fail(f"sensitive {bad!r} at {path}")
                if obj.startswith("/") and obj.count("/") >= 2 and not obj.startswith("<"):
                    self.fail(f"absolute path at {path}: {obj!r}")

        for data in docs:
            walk(data)


class RegistryTests(Fixture):
    def test_fake_registered(self) -> None:
        self.assertIn("fake", registered_harnesses())
        self.assertIsInstance(get_harness("fake"), FakeHarness)


class ReevaluateTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.agent_calls = 0
        base = self.side_effect

        def counting(command, **kwargs):  # type: ignore[no-untyped-def]
            if "--version" not in command:
                self.agent_calls += 1
            return base(command, **kwargs)

        self.side_effect = counting  # type: ignore[method-assign]

    def reeval(
        self,
        prior: Path,
        *,
        config: BenchConfig | None = None,
        id_factory: Any = None,
        options: RunOptions | None = None,
        pricing_data: dict | None = None,
    ) -> Path:
        with mock.patch("basecamp_bench.runner.run_managed", side_effect=self.side_effect):
            return reevaluate_run(
                config or self.config(),
                prior,
                options=options or self.options(),
                id_factory=id_factory or _Ids("r"),
                pricing_data=self.pricing if pricing_data is None else pricing_data,
                now=datetime(2026, 7, 11, 13, 0, 0, tzinfo=UTC),
            )

    def test_happy_path_no_impl_fresh_evals(self) -> None:
        prior = self.run_bench(id_factory=_Ids("p"))
        prior_tree = tree_manifest(prior)
        prior_id = prior.name
        prior_att = self.attempt_json(prior)
        prior_sid = prior_att["submission_id"]
        prior_impl_cost = prior_att["implementation_cost_usd"]
        prior_impl_tokens = 15  # 10+5 from fake implement usage
        prior_impl_duration = 0.125
        self.agent_calls = 0
        new_dir = self.reeval(prior, id_factory=_Ids("n"))
        self.assertNotEqual(new_dir.name, prior_id)
        self.assertEqual(tree_manifest(prior), prior_tree)
        self.assertEqual(self.agent_calls, 2)
        man = self.read_run_manifest(new_dir)
        self.assertEqual(man["status"], "complete")
        self.assertEqual(man["run"]["id"], new_dir.name)
        impl_jobs = [j for j in man["jobs"] if j.get("kind") == "implement"]
        self.assertEqual(len(impl_jobs), 1)
        self.assertIn("reuse prior_run=", impl_jobs[0]["command_preview"])
        self.assertIn(f"prior_run={prior_id}", impl_jobs[0]["command_preview"])
        eval_jobs = [j for j in man["jobs"] if j.get("kind") == "evaluate" and not j.get("skipped")]
        self.assertEqual(len(eval_jobs), 2)
        for job in eval_jobs:
            self.assertNotEqual(job.get("eval_attempt_id"), prior_sid)
        att = self.attempt_json(new_dir)
        self.assertEqual(att["submission_id"], prior_sid)
        self.assertEqual(att["run_id"], new_dir.name)
        self.assertTrue(att["implementation_success"])
        self.assertTrue(att["evaluation_success"])
        self.assertEqual(att["implementation_cost_usd"], prior_impl_cost)
        self.assertEqual(att["tokens"], prior_impl_tokens + 10)
        self.assertAlmostEqual(att["duration_s"], prior_impl_duration + 0.25)
        self.assertAlmostEqual(att["evaluation_cost_usd"], 0.004)
        self.assertEqual(set(att["evaluator_ids"]), {"judge-model-a", "judge-model-b"})
        self.assertTrue((new_dir / "snapshots" / prior_sid / "app.py").is_file())
        self.assertTrue((new_dir / "report.html").is_file())
        self.assertGreaterEqual(len(list((new_dir / "leaderboards").glob("*.json"))), 1)
        snap_keys = [k for k in man["artifacts"] if k.startswith(f"snapshots/{prior_sid}/")]
        self.assertTrue(snap_keys)
        self.assertIn(f"reuse_snapshot_tree:{prior_sid}", man["inputs"])
        self.assertIn(f"prior_run:{prior_id}", man["inputs"])
        self.assertEqual(
            man["inputs"][f"prior_run:{prior_id}"], _sha((prior / "run-manifest.json").read_bytes())
        )
        blob = json.dumps(man)
        self.assertNotIn(str(prior.resolve()), blob)
        self.assertNotIn(str(self.root.resolve()), man["jobs"][0]["command_preview"])

    def test_seed_hash_mismatch_rejects_before_evaluator_execution(self) -> None:
        prior = self.run_bench(id_factory=_Ids("seed"))
        (self.seed / "AGENTS.md").write_text("changed seed\n", encoding="utf-8")
        self.agent_calls = 0
        with self.assertRaisesRegex(ValueError, "re-evaluation input mismatch: seed_root"):
            self.reeval(prior, id_factory=_Ids("seed-new"))
        self.assertEqual(self.agent_calls, 0)

    def test_reference_hash_mismatch_rejects_before_evaluator_execution(self) -> None:
        prior = self.run_bench(id_factory=_Ids("ref"))
        changed = b"changed reference\n"
        (self.ref / "note.txt").write_bytes(changed)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["assets"][0]["sha256"] = _sha(changed)
        self.manifest.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        self.agent_calls = 0
        with self.assertRaisesRegex(ValueError, "re-evaluation input mismatch"):
            self.reeval(prior, id_factory=_Ids("ref-new"))
        self.assertEqual(self.agent_calls, 0)

    def test_publication_rejects_local_prior_before_evaluator_execution(self) -> None:
        prior = self.run_bench(
            config=self.config(mode="local", repetitions=3),
            id_factory=_Ids("local-prior"),
        )
        self.agent_calls = 0
        with self.assertRaisesRegex(ValueError, "requires a publication prior run"):
            self.reeval(
                prior,
                config=self.config(mode="publication", repetitions=3),
                id_factory=_Ids("publication-new"),
            )
        self.assertEqual(self.agent_calls, 0)

    def test_publication_rejects_ineligible_tooling_prior_before_evaluator_execution(self) -> None:
        config = self.config(mode="publication", repetitions=3)
        self.version_failure = True
        prior = self.run_bench(config=config, id_factory=_Ids("tooling-prior"))
        self.assertEqual(self.read_run_manifest(prior)["status"], "ineligible")
        for path in (prior / "attempts").glob("*.json"):
            reasons = json.loads(path.read_text(encoding="utf-8"))["ineligible_reasons"]
            self.assertTrue(
                any(reason.startswith("tool_version_unavailable:") for reason in reasons)
            )
        self.version_failure = False
        self.agent_calls = 0
        with self.assertRaisesRegex(ValueError, "completed eligible prior run"):
            self.reeval(prior, config=config, id_factory=_Ids("tooling-new"))
        self.assertEqual(self.agent_calls, 0)

    def test_changed_implementation_prompt_rejects_before_evaluator_execution(self) -> None:
        config = self.config(mode="publication", repetitions=3)
        prior = self.run_bench(config=config, id_factory=_Ids("prompt-prior"))
        self.prompt.write_bytes(b"changed implementation goal\n")
        self.agent_calls = 0
        with self.assertRaisesRegex(ValueError, "re-evaluation input mismatch: prompt:fe"):
            self.reeval(prior, config=config, id_factory=_Ids("prompt-new"))
        self.assertEqual(self.agent_calls, 0)

    def test_verify_run_error_rejects_before_agents(self) -> None:
        prior = self.run_bench(id_factory=_Ids("v"))
        (prior / "run-manifest.json").write_text("{not-json", encoding="utf-8")
        self.agent_calls = 0
        with self.assertRaises(ValueError) as ctx:
            self.reeval(prior, id_factory=_Ids("x"))
        self.assertIn("verification failed", str(ctx.exception).lower())
        self.assertEqual(self.agent_calls, 0)

    def test_undeclared_snapshot_file_rejects(self) -> None:
        prior = self.run_bench(id_factory=_Ids("u"))
        sid = self.attempt_json(prior)["submission_id"]
        (prior / "snapshots" / sid / "extra.txt").write_text("undeclared\n", encoding="utf-8")
        prior_tree = tree_manifest(prior)
        self.agent_calls = 0
        with self.assertRaises(ValueError) as ctx:
            self.reeval(prior, id_factory=_Ids("x"))
        self.assertIn("undeclared", str(ctx.exception).lower())
        self.assertEqual(self.agent_calls, 0)
        self.assertEqual(tree_manifest(prior), prior_tree)

    def test_hash_mismatched_snapshot_rejects(self) -> None:
        prior = self.run_bench(id_factory=_Ids("h"))
        sid = self.attempt_json(prior)["submission_id"]
        (prior / "snapshots" / sid / "app.py").write_text("mutated\n", encoding="utf-8")
        self.agent_calls = 0
        with self.assertRaises(ValueError) as ctx:
            self.reeval(prior, id_factory=_Ids("x"))
        msg = str(ctx.exception).lower()
        self.assertTrue("verification failed" in msg or "hash mismatch" in msg)
        self.assertEqual(self.agent_calls, 0)

    def test_outside_run_root_and_symlink_reject(self) -> None:
        prior = self.run_bench(id_factory=_Ids("o"))
        outside = self.root / "outside-run"
        outside.mkdir()
        self.agent_calls = 0
        with self.assertRaises(ValueError):
            self.reeval(outside, id_factory=_Ids("x"))
        self.assertEqual(self.agent_calls, 0)
        link = self.run_root / "sym-prior"
        link.symlink_to(prior)
        with self.assertRaises(ValueError) as ctx:
            self.reeval(link, id_factory=_Ids("y"))
        self.assertIn("symlink", str(ctx.exception).lower())
        self.assertEqual(self.agent_calls, 0)

    def test_collision_refuses_overwrite(self) -> None:
        prior = self.run_bench(id_factory=_Ids("c"))
        (self.run_root / "k001").mkdir()
        with self.assertRaises(ValueError) as ctx:
            self.reeval(prior, id_factory=_Ids("k"))
        self.assertIn("already exists", str(ctx.exception).lower())

    def test_changed_contract_lineage_and_costs(self) -> None:
        prior = self.run_bench(id_factory=_Ids("g"))
        prior_tree = tree_manifest(prior)
        prior_att = self.attempt_json(prior)
        prior_cv = prior_att["contract_version"]
        prior_ch = prior_att["contract_sha256"]
        prior_impl_cost = prior_att["implementation_cost_usd"]
        changed = dict(_CONTRACT)
        changed["contract_version"] = "2.0"
        self.contract.write_text(json.dumps(changed, indent=2) + "\n", encoding="utf-8")
        new_dir = self.reeval(prior, id_factory=_Ids("w"))
        self.assertEqual(tree_manifest(prior), prior_tree)
        att = self.attempt_json(new_dir)
        self.assertEqual(att["contract_version"], "2.0")
        self.assertNotEqual(att["contract_sha256"], prior_ch)
        self.assertEqual(att["implementation_cost_usd"], prior_impl_cost)
        self.assertAlmostEqual(att["evaluation_cost_usd"], 0.004)
        self.assertEqual(att["tokens"], 15 + 10)
        man = self.read_run_manifest(new_dir)
        preview = next(j["command_preview"] for j in man["jobs"] if j.get("kind") == "implement")
        self.assertIn(f"prior_contract_version={prior_cv}", preview)
        self.assertIn(f"prior_contract_sha256={prior_ch}", preview)
        self.assertIn("current_contract_version=2.0", preview)
        self.assertIn(f"current_contract_sha256={att['contract_sha256']}", preview)

    def test_changed_rubric_remains_allowed(self) -> None:
        prior = self.run_bench(id_factory=_Ids("rubric-prior"))
        self.rubric.write_text("# Revised rubric\n\nUse current evidence.\n", encoding="utf-8")
        self.agent_calls = 0
        new_dir = self.reeval(prior, id_factory=_Ids("rubric-new"))
        self.assertTrue(self.attempt_json(new_dir)["evaluation_success"])
        self.assertGreater(self.agent_calls, 0)


if __name__ == "__main__":
    unittest.main()
