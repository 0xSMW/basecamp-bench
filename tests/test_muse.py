"""Muse CLI contracts captured from Muse Code 1.4.7."""

from __future__ import annotations

import copy
import json
import subprocess
from unittest import mock

from basecamp_bench.adapters import MUSE_MODELS, MuseHarness, Usage
from tests.test_adapters import TempDirTestCase

SID = "01a1398c-ea8e-7721-ba20-0012b9435714"


def usage_export():
    return {
        "export_schema_version": 1,
        "sessions": [{"session_id": SID, "accepted_spawns": [], "saw_history_gap": False}],
        "events": [
            {
                "kind": "record",
                "envelope": {
                    "id": "completion-1",
                    "stream": {"kind": "session", "id": SID},
                    "payload_type": "runtime.session",
                    "payload": {
                        "kind": "run",
                        "event": {
                            "kind": "model_completed",
                            "model": "muse-spark-1.2",
                            "usage": {
                                "input_tokens": 1000,
                                "output_tokens": 200,
                                "cached_tokens": 300,
                                "cache_read_tokens": 300,
                                "cache_write_tokens": 0,
                                "reasoning_tokens": 100,
                            },
                        },
                    },
                },
            }
        ],
    }


def stdout(terminal="completed", model="muse-spark-1.2"):
    return "\n".join(
        json.dumps(
            {
                "schema_version": 1,
                "stream": {"kind": "session", "id": SID},
                "payload_type": kind,
                "payload": payload,
            }
        )
        for kind, payload in [
            ("run.model.configured", {"model_id": model}),
            ("run.output.delta", {"text": "partial"}),
            ("run.terminal." + terminal, {"terminal": terminal, "text": "READY"}),
        ]
    )


class MuseTests(TempDirTestCase):
    def job(self, **kwargs):
        return self._job(harness="muse", model="muse-spark-1.2", **kwargs)

    def adapter(self):
        return MuseHarness(binary=str(self.fake_bin))

    def test_all_models_command_and_permissions(self):
        for model in MUSE_MODELS:
            job = self._job(harness="muse", model=model)
            cmd = self.adapter().build_command(job)
            self._assert_no_sentinel_in_argv(cmd)
            self.assertEqual(cmd[1:3], ["exec", "--json"])
            self.assertEqual(cmd[cmd.index("--model") + 1], model)
            self.assertEqual(cmd[cmd.index("--contributor-mode") + 1], "off")
            self.assertIn("agents.execution_capacity=1", cmd)
            self.assertNotIn("--disable-sandbox", cmd)
            self.assertNotIn("--no-session-log", cmd)
            self.assertIsNone(self.adapter().stdin_for(job))
            self.assertEqual(self.adapter().working_directory(job), self.workdir)
        self.assertIn(
            "--disable-sandbox",
            self.adapter().build_command(self.job(sandbox_mode="danger-full-access")),
        )

    def test_invalid_model_effort_and_sandbox(self):
        for kwargs in (
            {"model": "muse-image-1.0"},
            {"effort": "unrecognized"},
            {"sandbox_mode": "read-only"},
        ):
            with self.assertRaises(ValueError):
                self.adapter().build_command(self._job(harness="muse", **kwargs))

    def test_environment_retains_auth_not_arbitrary_secrets(self):
        env = self.adapter().prepare_env(
            {"META_API_KEY": "test", "MUSE_AUTH_PATH": "/auth", "UNRELATED_SECRET": "no"}
        )
        self.assertEqual(env, {"META_API_KEY": "test", "MUSE_AUTH_PATH": "/auth"})

    def test_exact_session_export_and_terminal_answer(self):
        job = self.job()
        export = job.log_path.with_name(job.log_path.name + ".muse-export.json")
        export.write_text(json.dumps(usage_export()))
        with mock.patch("basecamp_bench.adapters.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            parsed = self.adapter().parse_output(job, stdout())
        self.assertEqual(parsed.usage, Usage(700, 300, 0, 200))
        self.assertEqual(parsed.last_message, "READY")
        self.assertEqual(parsed.session_id, SID)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--session") + 1], SID)
        self.assertNotIn("--last", argv)
        self.assertIn("--redacted", argv)
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_completion_summing_deduplication_and_no_attribution_double_count(self):
        doc = usage_export()
        second = copy.deepcopy(doc["events"][0])
        second["envelope"]["id"] = "completion-2"
        doc["events"].extend(
            [
                copy.deepcopy(doc["events"][0]),
                second,
                {
                    "envelope": {
                        "payload": {
                            "event": {
                                "kind": "goal_usage_attribution",
                                "quantity": {"input_tokens": 9999},
                            }
                        }
                    }
                },
            ]
        )
        self.assertEqual(
            self.adapter()._export_usage(doc, SID, "muse-spark-1.2"), Usage(1400, 600, 0, 400)
        )

    def test_usage_missing_is_unknown(self):
        doc = usage_export()
        doc["events"] = []
        self.assertIsNone(self.adapter()._export_usage(doc, SID, "muse-spark-1.2"))

    def test_intentionally_omitted_live_tool_deltas_preserve_usage(self):
        doc = usage_export()
        doc["sessions"][0]["saw_history_gap"] = True
        doc["diagnostics"] = {"gaps": 1, "omitted_live_only": 1}
        gap = {
            "kind": "gap",
            "stream": {"kind": "session", "id": SID},
            "marker": "omitted_live_only",
            "omitted_record": {
                "record_type": "status",
                "durability": "ephemeral",
                "payload_type": "runtime.session",
                "payload_kind": "task",
                "omission_class": "task_tool_delta_v1",
            },
        }
        doc["events"].append(gap)
        self.assertEqual(
            self.adapter()._export_usage(doc, SID, "muse-spark-1.2"), Usage(700, 300, 0, 200)
        )
        gap["omitted_record"]["durability"] = "durable"
        with self.assertRaisesRegex(ValueError, "durable history"):
            self.adapter()._export_usage(doc, SID, "muse-spark-1.2")

    def test_bad_exports_fail_closed(self):
        variants = []
        for field, value in [
            ("export_schema_version", 2),
            ("sessions", []),
            ("events", None),
            ("session_terminated_abnormally", True),
            ("diagnostics", {"unparseable_lines": 1}),
            ("diagnostics", {"unknown_payload_kinds": 1}),
            ("diagnostics", {"gaps": 1}),
        ]:
            doc = usage_export()
            doc[field] = value
            variants.append(doc)
        for field, value in [
            ("session_id", "wrong"),
            ("accepted_spawns", ["child"]),
            ("saw_history_gap", True),
        ]:
            doc = usage_export()
            doc["sessions"][0][field] = value
            variants.append(doc)
        for field, value in [
            ("input_tokens", -1),
            ("output_tokens", True),
            ("cached_tokens", 1001),
            ("cache_read_tokens", None),
        ]:
            doc = usage_export()
            doc["events"][0]["envelope"]["payload"]["event"]["usage"][field] = value
            variants.append(doc)
        doc = usage_export()
        doc["events"][0]["envelope"]["payload"]["event"]["model"] = "wrong"
        variants.append(doc)
        doc = usage_export()
        doc["events"][0]["envelope"]["stream"]["id"] = "wrong"
        variants.append(doc)
        for doc in variants:
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                self.adapter()._export_usage(doc, SID, "muse-spark-1.2")

    def test_failed_incomplete_or_mismatched_stdout_never_exports(self):
        for text in ("noise", stdout("failed"), stdout(model="wrong")):
            with mock.patch("basecamp_bench.adapters.subprocess.run") as run:
                with self.assertRaises(ValueError):
                    self.adapter().parse_output(self.job(), text)
                run.assert_not_called()

    def test_export_failure_and_timeout_propagate(self):
        with mock.patch("basecamp_bench.adapters.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1)
            with self.assertRaisesRegex(ValueError, "export failed"):
                self.adapter().parse_output(self.job(), stdout())
            run.side_effect = subprocess.TimeoutExpired("muse export", 30)
            with self.assertRaises(subprocess.TimeoutExpired):
                self.adapter().parse_output(self.job(), stdout())
