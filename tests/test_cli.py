"""Focused tests for the packaged command-line interface."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from basecamp_bench import cli


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def invoke(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(args)
        return code, stdout.getvalue(), stderr.getvalue()


class RunCommandTests(TempDirTestCase):
    @patch("basecamp_bench.cli.run_benchmark")
    @patch("basecamp_bench.cli.load_config")
    def test_run_passes_overrides_options_and_prints_only_path(self, load: Mock, run: Mock) -> None:
        config = object()
        load.return_value = config
        result = self.root / "runs" / "run-1"
        run.return_value = result
        code, stdout, stderr = self.invoke(
            "run",
            "--root",
            os.fspath(self.root),
            "--mode",
            "publication",
            "--harness",
            "grok,codex",
            "--track",
            "fe",
            "--track",
            "be",
            "--repetitions",
            "3",
            "--timeout",
            "42",
            "--allow-unsafe-host-execution",
            "--isolated-environment",
            "--offline-pricing",
            "--max-parallel-agents",
            "7",
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout, f"{result}\n")
        load.assert_called_once_with(
            None,
            root=self.root.absolute(),
            mode_override="publication",
            selected_harnesses=("grok", "codex"),
            selected_tracks=("fe", "be"),
            repetitions_override=3,
            timeout_override=42,
        )
        options = run.call_args.kwargs["options"]
        self.assertTrue(options.allow_unsafe_host_execution)
        self.assertTrue(options.confirmed_isolated_environment)
        self.assertFalse(options.allow_network_pricing)
        self.assertEqual(options.max_parallel_agents, 7)
        self.assertTrue(callable(options.progress))
        self.assertIs(run.call_args.args[0], config)

    @patch("basecamp_bench.cli.run_benchmark", return_value=Path("result"))
    @patch("basecamp_bench.cli.load_config", return_value=object())
    def test_quiet_disables_progress(self, _load: Mock, run: Mock) -> None:
        code, _, stderr = self.invoke("run", "--root", os.fspath(self.root), "--quiet")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIsNone(run.call_args.kwargs["options"].progress)

    def test_progress_printer_emits_atomic_sorted_event_line(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            cli._progress_printer()("evaluate.started", {"track": "fe", "model": "sol"})
        self.assertEqual(stderr.getvalue(), "[evaluate.started] model=sol track=fe\n")

    def test_progress_printer_counts_parallel_completions(self) -> None:
        stderr = io.StringIO()
        printer = cli._progress_printer()
        with redirect_stderr(stderr):
            printer("run.planned", {"implementations": 2, "evaluators": 2})
            printer("build.finished", {"model": "sol"})
            printer("build.finished", {"model": "grok"})
            printer("evaluate.finished", {"model": "sol"})
        lines = stderr.getvalue().splitlines()
        self.assertIn("progress=1/2", lines[1])
        self.assertIn("progress=2/2", lines[2])
        self.assertIn("progress=1/2", lines[3])

    @patch("basecamp_bench.cli.run_benchmark", return_value=Path("result"))
    @patch("basecamp_bench.cli.load_config", return_value=object())
    def test_default_config_is_root_bench_toml_only_when_present(
        self, load: Mock, _run: Mock
    ) -> None:
        code, _, _ = self.invoke("run", "--root", os.fspath(self.root))
        self.assertEqual(code, 0)
        self.assertIsNone(load.call_args.args[0])
        (self.root / "bench.toml").write_text("mode='local'\n", encoding="utf-8")
        code, _, _ = self.invoke("run", "--root", os.fspath(self.root))
        self.assertEqual(code, 0)
        self.assertEqual(load.call_args.args[0], self.root / "bench.toml")

    @patch("basecamp_bench.cli.run_benchmark", return_value=Path("result"))
    @patch("basecamp_bench.cli.load_config", return_value=object())
    def test_bench_toml_in_cwd_is_ignored_for_different_root(self, load: Mock, _run: Mock) -> None:
        other = self.root / "project"
        other.mkdir()
        (self.root / "bench.toml").write_text("mode='publication'\n", encoding="utf-8")
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            code, _, _ = self.invoke("run", "--root", os.fspath(other))
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0)
        self.assertIsNone(load.call_args.args[0])
        self.assertEqual(load.call_args.kwargs["root"], other.absolute())

    @patch("basecamp_bench.cli.load_config", side_effect=ValueError("bad config"))
    def test_expected_error_has_no_traceback(self, _load: Mock) -> None:
        code, stdout, stderr = self.invoke("run", "--root", os.fspath(self.root))
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr, "error: bad config\n")
        self.assertNotIn("Traceback", stderr)

    def test_argparse_rejects_bad_positive_values(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.main(("run", "--timeout", "0"))
        self.assertEqual(ctx.exception.code, 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.main(("run", "--max-parallel-agents", "0"))
        self.assertEqual(ctx.exception.code, 2)


class ReevaluateCommandTests(TempDirTestCase):
    @patch("basecamp_bench.cli.load_config", return_value=object())
    def test_calls_documented_runner_api(self, _load: Mock) -> None:
        prior = self.root / "prior"
        result = self.root / "new"
        with patch(
            "basecamp_bench.runner.reevaluate_run", create=True, return_value=result
        ) as reevaluate:
            code, stdout, stderr = self.invoke(
                "reevaluate",
                os.fspath(prior),
                "--root",
                os.fspath(self.root),
                "--offline-pricing",
                "--allow-unsafe-host-execution",
            )
        self.assertEqual((code, stdout, stderr), (0, f"{result}\n", ""))
        config = reevaluate.call_args.args[0]
        self.assertIs(config, _load.return_value)
        self.assertEqual(reevaluate.call_args.args[1], prior)
        self.assertFalse(reevaluate.call_args.kwargs["options"].allow_network_pricing)


class ReportCommandTests(TempDirTestCase):
    def _leaderboard(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        return path

    @patch("basecamp_bench.cli.write_report")
    def test_recursive_deterministic_discovery_ignores_other_json(self, write: Mock) -> None:
        b = self._leaderboard(self.root / "nested" / "leaderboard_b.json")
        a = self._leaderboard(self.root / "leaderboard_a.json")
        self._leaderboard(self.root / "unrelated.json")
        output = self.root / "report.html"
        write.return_value = output
        code, stdout, stderr = self.invoke(
            "report", os.fspath(self.root), "--output", os.fspath(output)
        )
        self.assertEqual((code, stdout, stderr), (0, f"{output}\n", ""))
        self.assertEqual(write.call_args.args, ([a.resolve(), b.resolve()], output))

    @patch("basecamp_bench.cli.write_report")
    def test_rejects_duplicate_resolved_files_and_no_matches(self, write: Mock) -> None:
        item = self._leaderboard(self.root / "leaderboard_a.json")
        code, stdout, stderr = self.invoke(
            "report", os.fspath(self.root), os.fspath(item), "-o", os.fspath(self.root / "x.html")
        )
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("duplicate leaderboard JSON", stderr)
        empty = self.root / "empty"
        empty.mkdir()
        code, _, stderr = self.invoke(
            "report", os.fspath(empty), "-o", os.fspath(self.root / "x.html")
        )
        self.assertEqual(code, 1)
        self.assertIn("no leaderboard JSON files", stderr)
        write.assert_not_called()

    def test_rejects_explicit_non_leaderboard_json(self) -> None:
        path = self._leaderboard(self.root / "data.json")
        code, _, stderr = self.invoke(
            "report", os.fspath(path), "-o", os.fspath(self.root / "x.html")
        )
        self.assertEqual(code, 1)
        self.assertIn("not a leaderboard JSON", stderr)


class UtilityCommandTests(TempDirTestCase):
    @patch("basecamp_bench.cli.verify_run", return_value=[])
    def test_verify_success(self, verify: Mock) -> None:
        run = self.root / "run"
        code, stdout, stderr = self.invoke("verify-run", os.fspath(run))
        self.assertEqual((code, stdout, stderr), (0, f"verified: {run}\n", ""))
        verify.assert_called_once_with(run)

    @patch("basecamp_bench.cli.verify_run", return_value=["z error", "a error"])
    def test_verify_failure_is_actionable(self, _verify: Mock) -> None:
        code, stdout, stderr = self.invoke("verify-run", os.fspath(self.root / "run"))
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr, "error: z error\nerror: a error\n")

    @patch("basecamp_bench.cli.export_run")
    def test_export_delegates_overwrite_refusal(self, export: Mock) -> None:
        export.side_effect = ValueError("output zip already exists")
        code, stdout, stderr = self.invoke(
            "export-run", os.fspath(self.root / "run"), os.fspath(self.root / "out.zip")
        )
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr, "error: output zip already exists\n")

    @patch("basecamp_bench.cli.config_to_public_dict")
    @patch("basecamp_bench.cli.load_config", return_value=object())
    def test_show_config_public_deterministic_json(self, _load: Mock, public: Mock) -> None:
        public.return_value = {"z": 1, "a": {"y": 2}}
        code, stdout, stderr = self.invoke("show-config", "--root", os.fspath(self.root))
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout, '{"a":{"y":2},"z":1}\n')
        self.assertEqual(json.loads(stdout), public.return_value)


if __name__ == "__main__":
    unittest.main()
