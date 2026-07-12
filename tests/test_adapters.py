"""Unit tests for basecamp_bench.adapters (stdlib unittest only)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from basecamp_bench.adapters import (
    AGY_MODEL_ALIASES,
    DEFAULT_GROK_BINARY,
    PI_MODEL_ALIASES,
    RETAINED_ENV_NAMES,
    AgentJob,
    AgyHarness,
    ClaudeHarness,
    CodexHarness,
    GrokHarness,
    Harness,
    ModelSpec,
    ParsedOutput,
    PiHarness,
    Usage,
    get_harness,
    is_retained_env_name,
    register_harness,
    registered_harnesses,
)

SENTINEL = "PROMPT_SENTINEL_NEVER_IN_ARGV_9f3a2c1e7b"


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.workdir = self.root / "workdir"
        self.workdir.mkdir()
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.prompt_path = self.root / "prompt.md"
        self.prompt_path.write_text(SENTINEL, encoding="utf-8")
        self.log_path = self.root / "job.log"
        self.last_message_path = self.root / "last.md"
        self.fake_bin = self.root / "fake-agent"
        self.fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        self.fake_bin.chmod(0o755)

    def _job(
        self,
        *,
        kind: str = "implement",
        harness: str = "codex",
        model: str = "test-model",
        effort: str = "high",
        evidence_dirs: tuple[Path, ...] | None = None,
        sandbox_mode: str = "workspace-write",
    ) -> AgentJob:
        return AgentJob(
            kind=kind,  # type: ignore[arg-type]
            harness=harness,
            model=ModelSpec(model=model, effort=effort),
            workdir=self.workdir,
            prompt_path=self.prompt_path,
            log_path=self.log_path,
            last_message_path=self.last_message_path,
            evidence_dirs=() if evidence_dirs is None else evidence_dirs,
            sandbox_mode=sandbox_mode,
        )

    def _assert_no_sentinel_in_argv(self, cmd: list[str]) -> None:
        joined = "\x00".join(cmd)
        self.assertNotIn(SENTINEL, joined)
        for arg in cmd:
            self.assertNotIn(SENTINEL, arg)


class UsageTests(unittest.TestCase):
    def test_add_and_total(self) -> None:
        a = Usage(input_tokens=10, cached_input_tokens=2, cache_write_tokens=3, output_tokens=4)
        b = Usage(input_tokens=1, cached_input_tokens=1, cache_write_tokens=1, output_tokens=1)
        c = a.add(b)
        self.assertEqual(c.input_tokens, 11)
        self.assertEqual(c.cached_input_tokens, 3)
        self.assertEqual(c.cache_write_tokens, 4)
        self.assertEqual(c.output_tokens, 5)
        self.assertEqual(c.total(), 23)
        # Immutability: original unchanged
        self.assertEqual(a.total(), 19)


class RegistryTests(unittest.TestCase):
    def test_builtin_registration_deterministic(self) -> None:
        names = registered_harnesses()
        self.assertEqual(names, sorted(names))
        self.assertEqual(names, ["agy", "claude", "codex", "grok", "pi"])
        self.assertIsInstance(get_harness("agy"), AgyHarness)
        self.assertIsInstance(get_harness("codex"), CodexHarness)
        self.assertIsInstance(get_harness("claude"), ClaudeHarness)
        self.assertIsInstance(get_harness("grok"), GrokHarness)
        self.assertIsInstance(get_harness("pi"), PiHarness)

    def test_unknown_harness(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            get_harness("not-a-real-harness")
        self.assertIn("not-a-real-harness", str(ctx.exception))
        self.assertIn("Registered", str(ctx.exception))

    def test_duplicate_rejected_unless_replace(self) -> None:
        class DupHarness(Harness):
            name = "codex"

            def build_command(self, job: AgentJob) -> list[str]:
                return ["dup"]

        with self.assertRaises(ValueError) as ctx:
            register_harness(DupHarness)
        self.assertIn("already registered", str(ctx.exception))

        try:
            register_harness(DupHarness, replace=True)
            self.assertIsInstance(get_harness("codex"), DupHarness)
        finally:
            register_harness(CodexHarness, replace=True)

    def test_invalid_registration_missing_name(self) -> None:
        class NoName(Harness):
            def build_command(self, job: AgentJob) -> list[str]:
                return []

        with self.assertRaises(ValueError):
            register_harness(NoName)

    def test_register_new_and_list(self) -> None:
        class TempHarness(Harness):
            name = "zz-temp-adapter-test"

            def build_command(self, job: AgentJob) -> list[str]:
                return ["temp"]

        try:
            register_harness(TempHarness)
            names = registered_harnesses()
            self.assertIn("zz-temp-adapter-test", names)
            self.assertEqual(names, sorted(names))
            self.assertIsInstance(get_harness("zz-temp-adapter-test"), TempHarness)
        finally:
            # Clean up registry without touching builtins.
            from basecamp_bench import adapters as adapters_mod

            adapters_mod._HARNESS_TYPES.pop("zz-temp-adapter-test", None)


class ResolveBinaryTests(TempDirTestCase):
    def test_missing_absolute_binary_fails_clearly(self) -> None:
        missing = self.root / "missing" / "codex"
        h = CodexHarness(binary=str(missing))
        with self.assertRaises(FileNotFoundError) as ctx:
            h.resolve_binary()
        msg = str(ctx.exception)
        self.assertIn("codex", msg.lower())
        self.assertIn(str(missing), msg)

    def test_missing_path_does_not_substitute_path_lookup(self) -> None:
        # Even if a same-named binary exists on PATH, a configured absolute path
        # must not fall back to it.
        missing = self.root / "definitely-missing-binary-xyz"
        h = CodexHarness(binary=str(missing))
        with mock.patch("basecamp_bench.adapters.shutil.which", return_value="/usr/bin/codex"):
            with self.assertRaises(FileNotFoundError):
                h.resolve_binary()

    def test_existing_absolute_binary(self) -> None:
        h = CodexHarness(binary=str(self.fake_bin))
        self.assertEqual(h.resolve_binary(), str(self.fake_bin))

    def test_version_command_minimal(self) -> None:
        h = CodexHarness(binary=str(self.fake_bin))
        self.assertEqual(h.version_command(), [str(self.fake_bin), "--version"])

    def test_grok_default_binary_constant(self) -> None:
        h = GrokHarness()
        self.assertEqual(h.configured_binary(), DEFAULT_GROK_BINARY)
        self.assertEqual(DEFAULT_GROK_BINARY, "grok")

    def test_grok_constructor_override(self) -> None:
        h = GrokHarness(binary=str(self.fake_bin))
        self.assertEqual(h.resolve_binary(), str(self.fake_bin))


class CodexCommandTests(TempDirTestCase):
    def test_implement_argv(self) -> None:
        h = CodexHarness(binary=str(self.fake_bin))
        job = self._job(kind="implement", sandbox_mode="danger-full-access")
        cmd = h.build_command(job)
        self._assert_no_sentinel_in_argv(cmd)
        self.assertEqual(cmd[0], str(self.fake_bin))
        self.assertEqual(cmd[1], "exec")
        self.assertIn("-m", cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "test-model")
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "model_reasoning_effort=high")
        self.assertIn("-C", cmd)
        self.assertEqual(cmd[cmd.index("-C") + 1], str(self.workdir))
        self.assertIn("-s", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "danger-full-access")
        self.assertIn("--json", cmd)
        self.assertIn("-o", cmd)
        self.assertEqual(cmd[cmd.index("-o") + 1], str(self.last_message_path))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertEqual(cmd[-1], "-")
        self.assertNotIn("--add-dir", cmd)
        stdin = h.stdin_for(job)
        self.assertIsNotNone(stdin)
        assert stdin is not None
        self.assertEqual(stdin.decode("utf-8"), SENTINEL)
        self.assertIsNone(h.working_directory(job))

    def test_evaluate_argv_evidence_only(self) -> None:
        h = CodexHarness(binary=str(self.fake_bin))
        job = self._job(
            kind="evaluate",
            model="judge-model",
            evidence_dirs=(self.evidence,),
            sandbox_mode="workspace-write",
        )
        cmd = h.build_command(job)
        self._assert_no_sentinel_in_argv(cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "judge-model")
        self.assertIn("--add-dir", cmd)
        self.assertEqual(cmd[cmd.index("--add-dir") + 1], str(self.evidence))
        # No producer identity leakage: only evaluator model appears.
        self.assertNotIn("producer", " ".join(cmd))
        self.assertNotIn("submission_harness", " ".join(cmd))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)


class ClaudeCommandTests(TempDirTestCase):
    def test_implement_argv_stdin_prompt(self) -> None:
        h = ClaudeHarness(binary=str(self.fake_bin))
        job = self._job(kind="implement", harness="claude")
        cmd = h.build_command(job)
        self._assert_no_sentinel_in_argv(cmd)
        self.assertEqual(cmd[0], str(self.fake_bin))
        self.assertIn("-p", cmd)
        p_idx = cmd.index("-p")
        self.assertEqual(cmd[p_idx + 1], "--model")
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "test-model")
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertIn("--output-format", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--permission-mode", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--allowedTools", cmd)
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        self.assertTrue(settings["sandbox"]["enabled"])
        self.assertTrue(settings["sandbox"]["failIfUnavailable"])
        self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
        self.assertIn(str(self.workdir), settings["sandbox"]["filesystem"]["allowWrite"])
        self.assertEqual(settings["sandbox"]["filesystem"]["denyRead"], ["/**"])
        self.assertNotIn("--add-dir", cmd)
        stdin = h.stdin_for(job)
        self.assertIsNotNone(stdin)
        assert stdin is not None
        self.assertEqual(stdin.decode("utf-8"), SENTINEL)

    def test_evaluate_argv(self) -> None:
        h = ClaudeHarness(binary=str(self.fake_bin))
        job = self._job(
            kind="evaluate",
            harness="claude",
            model="judge-claude",
            evidence_dirs=(self.evidence,),
        )
        cmd = h.build_command(job)
        self._assert_no_sentinel_in_argv(cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "judge-claude")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--add-dir", cmd)
        self.assertEqual(cmd[cmd.index("--add-dir") + 1], str(self.evidence))
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        filesystem = settings["sandbox"]["filesystem"]
        self.assertIn(str(self.evidence), filesystem["allowRead"])
        self.assertIn(str(self.evidence), filesystem["denyWrite"])
        self.assertIn(f"Write({self.evidence}/**)", settings["permissions"]["deny"])

    def test_danger_full_access_does_not_enable_sandbox(self) -> None:
        h = ClaudeHarness(binary=str(self.fake_bin))
        job = self._job(harness="claude", sandbox_mode="danger-full-access")
        cmd = h.build_command(job)
        self.assertNotIn("--settings", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")


class GrokCommandTests(TempDirTestCase):
    def test_implement_argv(self) -> None:
        h = GrokHarness(binary=str(self.fake_bin))
        job = self._job(kind="implement", harness="grok", model="grok-4.5")
        cmd = h.build_command(job)
        self._assert_no_sentinel_in_argv(cmd)
        self.assertEqual(cmd[0], str(self.fake_bin))
        self.assertIn("--prompt-file", cmd)
        self.assertEqual(
            cmd[cmd.index("--prompt-file") + 1],
            str(self.workdir / ".grok" / ".basecamp-bench-prompt.md"),
        )
        self.assertIn("--cwd", cmd)
        self.assertEqual(cmd[cmd.index("--cwd") + 1], str(self.workdir))
        self.assertIn("-m", cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "grok-4.5")
        self.assertIn("--reasoning-effort", cmd)
        self.assertEqual(cmd[cmd.index("--reasoning-effort") + 1], "high")
        self.assertIn("--output-format", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        self.assertIn("--no-memory", cmd)
        self.assertIn("--verbatim", cmd)
        self.assertNotIn("--no-plan", cmd)
        self.assertNotIn("--no-subagents", cmd)
        self.assertNotIn("--always-approve", cmd)
        self.assertNotIn("--tools", cmd)
        self.assertIn("--disallowed-tools", cmd)
        denied = cmd[cmd.index("--disallowed-tools") + 1].split(",")
        self.assertIn("web_search", denied)
        self.assertNotIn("run_terminal_cmd", denied)
        self.assertNotIn("get_task_output", denied)
        self.assertNotIn("kill_task", denied)
        self.assertIn("Bash(*)", cmd)
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "basecamp_bench")
        self.assertIsNone(h.stdin_for(job))
        self.assertEqual(h.prepare_env({"PATH": "/bin"})["GROK_SUBAGENTS"], "0")
        # Prompt lives only in the file referenced by --prompt-file.
        self.assertEqual(self.prompt_path.read_text(encoding="utf-8"), SENTINEL)

    def test_evaluate_permission_scope(self) -> None:
        h = GrokHarness(binary=str(self.fake_bin))
        job = self._job(
            kind="evaluate",
            harness="grok",
            model="judge-grok",
            evidence_dirs=(self.evidence,),
        )
        cmd = h.build_command(job)
        self._assert_no_sentinel_in_argv(cmd)
        self.assertNotIn("--always-approve", cmd)
        self.assertIn("--allow", cmd)
        self.assertIn("--deny", cmd)
        # All path-bearing allow/deny rules must reference only workdir / evidence.
        for i, arg in enumerate(cmd):
            if arg in ("--allow", "--deny") and i + 1 < len(cmd):
                rule = cmd[i + 1]
                if rule == "Bash(*)":
                    continue
                if "(" in rule and ")" in rule:
                    inner = rule[rule.index("(") + 1 : rule.rindex(")")]
                    if inner:
                        self.assertTrue(
                            inner.startswith(str(self.workdir))
                            or inner.startswith(str(self.evidence)),
                            msg=f"permission rule escapes supplied roots: {rule}",
                        )
        self.assertTrue(any(str(self.workdir) in a for a in cmd))
        self.assertTrue(any(str(self.evidence) in a for a in cmd))
        # No extra provenance path beyond evidence_dirs.
        sibling = self.root / "other-submission"
        sibling.mkdir()
        self.assertFalse(any(str(sibling) in a for a in cmd))

    def test_workspace_execution_context_installs_and_restores_sandbox(self) -> None:
        h = GrokHarness(binary=str(self.fake_bin))
        job = self._job(
            kind="evaluate",
            harness="grok",
            evidence_dirs=(self.evidence,),
        )
        profile_path = self.workdir / ".grok" / "sandbox.toml"
        tool_bin = self.root / "tools" / "bin"
        tool_bin.mkdir(parents=True)
        prompt_copy = self.workdir / ".grok" / ".basecamp-bench-prompt.md"
        with mock.patch.dict(os.environ, {"PATH": f"/usr/bin:{tool_bin}"}):
            with h.execution_context(job):
                profile = profile_path.read_text(encoding="utf-8")
                self.assertIn("[profiles.basecamp_bench]", profile)
                self.assertIn('extends = "strict"', profile)
                self.assertIn(str(self.evidence), profile)
                self.assertIn(str(tool_bin), profile)
                self.assertEqual(prompt_copy.read_text(encoding="utf-8"), SENTINEL)
        self.assertFalse(profile_path.exists())
        self.assertFalse(prompt_copy.exists())
        self.assertFalse(profile_path.parent.exists())

    def test_danger_full_access_does_not_install_sandbox(self) -> None:
        h = GrokHarness(binary=str(self.fake_bin))
        job = self._job(harness="grok", sandbox_mode="danger-full-access")
        with h.execution_context(job):
            self.assertFalse((self.workdir / ".grok" / "sandbox.toml").exists())
        cmd = h.build_command(job)
        self.assertNotIn("--sandbox", cmd)


class PiCommandTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Key resolution is host-dependent; pin both sources so tests are
        # hermetic on machines without OPENROUTER_API_KEY or a key file.
        patcher = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_glm_argv_and_transient_configuration(self) -> None:
        h = PiHarness(binary=str(self.fake_bin))
        job = self._job(harness="pi", model="glm-5.2")
        private_root = h._private_root(job)
        with h.execution_context(job):
            cmd = h.build_command(job)
            self._assert_no_sentinel_in_argv(cmd)
            self.assertEqual(cmd[0], str(self.fake_bin))
            self.assertEqual(cmd[cmd.index("--provider") + 1], "openrouter")
            self.assertEqual(cmd[cmd.index("--model") + 1], "z-ai/glm-5.2")
            self.assertEqual(cmd[cmd.index("--thinking") + 1], "high")
            self.assertEqual(cmd[cmd.index("--tools") + 1], "read,bash,edit,write,grep,find,ls")
            self.assertEqual(cmd[cmd.index("--mode") + 1], "text")
            self.assertNotIn("--no-session", cmd)
            self.assertEqual(cmd[cmd.index("--session-dir") + 1], str(h._session_dir(job)))
            self.assertIn("--approve", cmd)
            self.assertEqual(cmd[-2:], ["-p", f"@{self.prompt_path}"])

            config_path = private_root / "config" / "models.json"
            model_config = json.loads(config_path.read_text(encoding="utf-8"))
            model = model_config["providers"]["openrouter"]["models"][0]
            self.assertEqual(model["id"], PI_MODEL_ALIASES["glm-5.2"])
            self.assertEqual(model["contextWindow"], 1_048_576)
            routing = model["compat"]["openRouterRouting"]
            self.assertFalse(routing["allow_fallbacks"])
            self.assertIn("baidu", routing["order"])
            settings = json.loads(
                (private_root / "config" / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(settings, {"httpIdleTimeoutMs": 600_000})
            env = h.prepare_env({"PATH": "/usr/bin"})
            self.assertEqual(env["PI_CODING_AGENT_DIR"], str(private_root / "config"))
            # Pi may create additional auth/cache files; the whole private root
            # must still be removed before the submission is snapshotted.
            (private_root / "config" / "auth.json").write_text("{}\n", encoding="utf-8")
        self.assertFalse(private_root.exists())
        self.assertEqual(list(self.workdir.iterdir()), [])

    def test_rejects_unknown_model_alias(self) -> None:
        h = PiHarness(binary=str(self.fake_bin))
        job = self._job(harness="pi", model="z-ai/glm-5.2")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            h.build_command(job)

    def test_glm_requires_high_thinking(self) -> None:
        h = PiHarness(binary=str(self.fake_bin))
        job = self._job(harness="pi", model="glm-5.2", effort="medium")
        with self.assertRaisesRegex(ValueError, "requires thinking effort 'high'"):
            h.build_command(job)

    def test_reserved_paths_fail_closed(self) -> None:
        h = PiHarness(binary=str(self.fake_bin))
        job = self._job(harness="pi", model="glm-5.2")
        h._private_root(job).mkdir()
        with self.assertRaisesRegex(ValueError, "reserved"):
            with h.execution_context(job):
                pass

    def test_missing_openrouter_key_fails_closed_before_any_setup(self) -> None:
        h = PiHarness(binary=str(self.fake_bin))
        job = self._job(harness="pi", model="glm-5.2")
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch(
                "basecamp_bench.adapters.OPENROUTER_KEY_FILE",
                self.root / "no-such-key-file",
            ),
        ):
            with self.assertRaisesRegex(ValueError, "OpenRouter key"):
                with h.execution_context(job):
                    pass
        self.assertFalse(h._private_root(job).exists())

    def test_openrouter_key_file_fallback_and_env_precedence(self) -> None:
        key_file = self.root / "openrouter-api-key"
        key_file.write_text("sk-or-from-file\n", encoding="utf-8")
        key_file.chmod(0o600)
        h = PiHarness(binary=str(self.fake_bin))
        job = self._job(harness="pi", model="glm-5.2")
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("basecamp_bench.adapters.OPENROUTER_KEY_FILE", key_file),
        ):
            with h.execution_context(job):
                filled = h.prepare_env({"PATH": "/usr/bin"})
                self.assertEqual(filled["OPENROUTER_API_KEY"], "sk-or-from-file")
                explicit = h.prepare_env({"PATH": "/usr/bin", "OPENROUTER_API_KEY": "env-wins"})
                self.assertEqual(explicit["OPENROUTER_API_KEY"], "env-wins")

    def test_openrouter_key_file_rejects_public_permissions(self) -> None:
        key_file = self.root / "openrouter-api-key"
        key_file.write_text("sk-or-public\n", encoding="utf-8")
        key_file.chmod(0o644)
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch("basecamp_bench.adapters.OPENROUTER_KEY_FILE", key_file),
        ):
            self.assertIsNone(
                PiHarness(binary=str(self.fake_bin)).prepare_env().get("OPENROUTER_API_KEY")
            )


class AgyCommandTests(TempDirTestCase):
    def test_gemini_31_pro_model_aliases(self) -> None:
        h = AgyHarness(binary=str(self.fake_bin))
        for effort in ("low", "high"):
            with self.subTest(effort=effort):
                job = self._job(harness="agy", model="gemini-3.1-pro", effort=effort)
                with h.execution_context(job):
                    cmd = h.build_command(job)
                self.assertEqual(
                    cmd[cmd.index("--model") + 1],
                    AGY_MODEL_ALIASES["gemini-3.1-pro"][effort],
                )

    def test_sandboxed_argv_and_disposable_evidence(self) -> None:
        (self.evidence / "app.py").write_text("print('evidence')\n", encoding="utf-8")
        self.prompt_path.write_text(
            f"Inspect {self.evidence}\n{SENTINEL}\n",
            encoding="utf-8",
        )
        h = AgyHarness(binary=str(self.fake_bin))
        job = self._job(
            kind="evaluate",
            harness="agy",
            model="gemini-3.5-flash",
            evidence_dirs=(self.evidence,),
        )
        with h.execution_context(job):
            cmd = h.build_command(job)
            self._assert_no_sentinel_in_argv(cmd)
            self.assertEqual(cmd[0], sys.executable)
            self.assertEqual(cmd[3], str(self.fake_bin))
            self.assertEqual(
                cmd[cmd.index("--model") + 1],
                AGY_MODEL_ALIASES["gemini-3.5-flash"]["high"],
            )
            self.assertEqual(cmd[cmd.index("--mode") + 1], "accept-edits")
            self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
            self.assertIn("--sandbox", cmd)
            self.assertIn("--dangerously-skip-permissions", cmd)
            self.assertNotIn("--add-dir", cmd)
            self.assertNotIn("-p", cmd)

            staged_root = self.workdir / ".basecamp-bench-agy"
            staged_evidence = staged_root / "evidence-0"
            staged_prompt = (staged_root / "prompt.md").read_text(encoding="utf-8")
            launcher = (staged_root / "launch.py").read_text(encoding="utf-8")
            self.assertNotIn(SENTINEL, launcher)
            self.assertIn(str(staged_evidence), staged_prompt)
            self.assertNotIn(str(self.evidence), staged_prompt)
            self.assertEqual(
                (staged_evidence / "app.py").read_text(encoding="utf-8"),
                "print('evidence')\n",
            )
        self.assertFalse((self.workdir / ".basecamp-bench-agy").exists())
        self.assertEqual(
            (self.evidence / "app.py").read_text(encoding="utf-8"), "print('evidence')\n"
        )

    def test_danger_full_access_omits_sandbox(self) -> None:
        h = AgyHarness(binary=str(self.fake_bin))
        job = self._job(
            harness="agy",
            model="gemini-3.5-flash",
            sandbox_mode="danger-full-access",
        )
        with h.execution_context(job):
            cmd = h.build_command(job)
        self.assertNotIn("--sandbox", cmd)

    def test_rejects_unknown_model_and_effort(self) -> None:
        h = AgyHarness(binary=str(self.fake_bin))
        with self.assertRaisesRegex(ValueError, "unsupported"):
            h.build_command(self._job(harness="agy", model="gemini-pro"))
        with self.assertRaisesRegex(ValueError, "does not support effort"):
            h.build_command(self._job(harness="agy", model="gemini-3.5-flash", effort="xhigh"))

    def test_reserved_path_fails_closed(self) -> None:
        h = AgyHarness(binary=str(self.fake_bin))
        job = self._job(harness="agy", model="gemini-3.5-flash")
        (self.workdir / ".basecamp-bench-agy").mkdir()
        with self.assertRaisesRegex(ValueError, "reserved"):
            with h.execution_context(job):
                pass


class EnvironmentTests(TempDirTestCase):
    def test_allowlist_keeps_required_drops_arbitrary(self) -> None:
        h = CodexHarness(binary=str(self.fake_bin))
        base = {
            "PATH": "/usr/bin",
            "HOME": "/home/test",
            "TMPDIR": "/tmp",
            "TEMP": "/tmp",
            "TMP": "/tmp",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "C",
            "LC_CTYPE": "UTF-8",
            "SSL_CERT_FILE": "/etc/ssl/cert.pem",
            "REQUESTS_CA_BUNDLE": "/etc/ssl/cert.pem",
            "HTTP_PROXY": "http://proxy:8080",
            "HTTPS_PROXY": "http://proxy:8080",
            "NO_PROXY": "localhost",
            "http_proxy": "http://proxy:8080",
            "OPENAI_API_KEY": "sk-openai-secret",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "XAI_API_KEY": "xai-secret",
            "GROK_HOME": "/tmp/grok-home",
            "OPENROUTER_API_KEY": "or-secret",
            # Arbitrary / secret noise that must be dropped
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "MY_CUSTOM_TOKEN": "custom-secret",
            "DATABASE_URL": "postgres://x",
            "SECRET_KEY": "nope",
            "UNRELATED": "drop-me",
        }
        env = h.prepare_env(base)
        for key in (
            "PATH",
            "HOME",
            "TMPDIR",
            "TEMP",
            "TMP",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "SSL_CERT_FILE",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "XAI_API_KEY",
            "GROK_HOME",
            "OPENROUTER_API_KEY",
        ):
            self.assertIn(key, env)
            self.assertEqual(env[key], base[key])
        for key in (
            "AWS_SECRET_ACCESS_KEY",
            "MY_CUSTOM_TOKEN",
            "DATABASE_URL",
            "SECRET_KEY",
            "UNRELATED",
        ):
            self.assertNotIn(key, env)

    def test_retained_name_helper_is_testable(self) -> None:
        self.assertTrue(is_retained_env_name("PATH"))
        self.assertTrue(is_retained_env_name("OPENAI_API_KEY"))
        self.assertTrue(is_retained_env_name("ANTHROPIC_API_KEY"))
        self.assertTrue(is_retained_env_name("XAI_API_KEY"))
        self.assertTrue(is_retained_env_name("OPENROUTER_API_KEY"))
        self.assertTrue(is_retained_env_name("LC_MESSAGES"))
        self.assertTrue(is_retained_env_name("SSL_CERT_DIR"))
        self.assertFalse(is_retained_env_name("AWS_SECRET_ACCESS_KEY"))
        self.assertFalse(is_retained_env_name("MY_CUSTOM_TOKEN"))
        self.assertFalse(is_retained_env_name("RANDOM_VAR"))
        self.assertIn("PATH", RETAINED_ENV_NAMES)
        self.assertIn("OPENAI_API_KEY", RETAINED_ENV_NAMES)

    @mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
    def test_pi_env_uses_isolated_config_directory(self) -> None:
        h = PiHarness(binary=str(self.fake_bin))
        job = self._job(harness="pi", model="glm-5.2")
        with h.execution_context(job):
            env = h.prepare_env({"PATH": "/usr/bin", "OPENROUTER_API_KEY": "secret"})
            self.assertEqual(
                env["PI_CODING_AGENT_DIR"],
                str(h._private_root(job) / "config"),
            )
            self.assertEqual(env["PI_TELEMETRY"], "0")
            self.assertEqual(env["OPENROUTER_API_KEY"], "secret")

    def test_prepare_env_does_not_copy_os_environ_arbitrarily(self) -> None:
        h = ClaudeHarness(binary=str(self.fake_bin))
        with mock.patch.dict(os.environ, {"LEAK_ME_PLEASE": "secret-value"}, clear=False):
            env = h.prepare_env()
        self.assertNotIn("LEAK_ME_PLEASE", env)


class ParseOutputTests(TempDirTestCase):
    def test_codex_jsonl_turn_completed_and_cumulative(self) -> None:
        h = CodexHarness(binary=str(self.fake_bin))
        job = self._job()
        # Two turn.completed events (incremental) plus a final cumulative token_count.
        # Cumulative should win and not be double-counted with turns.
        text = "\n".join(
            [
                "noise before",
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 20,
                            "output_tokens": 10,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 50,
                            "cached_input_tokens": 10,
                            "output_tokens": 5,
                        },
                    }
                ),
                json.dumps(
                    {
                        "msg": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 200,
                                    "cached_input_tokens": 40,
                                    "output_tokens": 15,
                                    "cache_write_tokens": 8,
                                }
                            },
                        }
                    }
                ),
                "trailing noise",
            ]
        )
        parsed = h.parse_output(job, text)
        self.assertIsNotNone(parsed.usage)
        assert parsed.usage is not None
        # input_tokens normalized: 200 - 40 = 160
        self.assertEqual(parsed.usage.input_tokens, 160)
        self.assertEqual(parsed.usage.cached_input_tokens, 40)
        self.assertEqual(parsed.usage.cache_write_tokens, 8)
        self.assertEqual(parsed.usage.output_tokens, 15)
        self.assertEqual(parsed.usage.total(), 160 + 40 + 8 + 15)

    def test_codex_per_turn_when_no_cumulative(self) -> None:
        h = CodexHarness(binary=str(self.fake_bin))
        job = self._job()
        text = "\n".join(
            [
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 30,
                            "cached_input_tokens": 10,
                            "output_tokens": 4,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 20,
                            "cached_input_tokens": 0,
                            "output_tokens": 6,
                        },
                    }
                ),
            ]
        )
        parsed = h.parse_output(job, text)
        assert parsed.usage is not None
        # (30-10) + (20-0) = 40 input; cached 10; out 10
        self.assertEqual(parsed.usage.input_tokens, 40)
        self.assertEqual(parsed.usage.cached_input_tokens, 10)
        self.assertEqual(parsed.usage.output_tokens, 10)

    def test_claude_json_model_usage_and_cost(self) -> None:
        h = ClaudeHarness(binary=str(self.fake_bin))
        job = self._job(harness="claude")
        payload = {
            "type": "result",
            "subtype": "success",
            "result": "evaluation complete",
            "total_cost_usd": 0.42,
            "session_id": "claude-sess-1",
            "modelUsage": {
                "claude-primary": {
                    "inputTokens": 100,
                    "cacheReadInputTokens": 25,
                    "cacheCreationInputTokens": 15,
                    "outputTokens": 50,
                },
                "claude-sub": {
                    "inputTokens": 10,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                    "outputTokens": 5,
                },
            },
        }
        parsed = h.parse_output(job, json.dumps(payload))
        assert parsed.usage is not None
        self.assertEqual(parsed.usage.input_tokens, 110)
        self.assertEqual(parsed.usage.cached_input_tokens, 25)
        self.assertEqual(parsed.usage.cache_write_tokens, 15)
        self.assertEqual(parsed.usage.output_tokens, 55)
        self.assertEqual(parsed.reported_cost_usd, 0.42)
        self.assertEqual(parsed.last_message, "evaluation complete")
        self.assertEqual(parsed.session_id, "claude-sess-1")

    def test_claude_top_level_usage_fallback(self) -> None:
        h = ClaudeHarness(binary=str(self.fake_bin))
        job = self._job(harness="claude")
        payload = {
            "type": "result",
            "usage": {
                "input_tokens": 12,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
                "output_tokens": 7,
            },
            "result": "ok",
        }
        parsed = h.parse_output(job, json.dumps(payload))
        assert parsed.usage is not None
        self.assertEqual(parsed.usage.input_tokens, 12)
        self.assertEqual(parsed.usage.cached_input_tokens, 3)
        self.assertEqual(parsed.usage.cache_write_tokens, 2)
        self.assertEqual(parsed.usage.output_tokens, 7)

    def test_grok_json_inline_usage(self) -> None:
        h = GrokHarness(binary=str(self.fake_bin))
        job = self._job(harness="grok")
        payload = {
            "text": "final answer",
            "sessionId": "sess-abc",
            "usage": {
                "prompt_tokens": 100,
                "cached_prompt_tokens": 30,
                "cache_write_tokens": 5,
                "completion_tokens": 20,
            },
            "total_cost_usd": 0.01,
        }
        parsed = h.parse_output(job, json.dumps(payload))
        assert parsed.usage is not None
        self.assertEqual(parsed.usage.input_tokens, 70)
        self.assertEqual(parsed.usage.cached_input_tokens, 30)
        self.assertEqual(parsed.usage.cache_write_tokens, 5)
        self.assertEqual(parsed.usage.output_tokens, 20)
        self.assertEqual(parsed.last_message, "final answer")
        self.assertEqual(parsed.session_id, "sess-abc")
        self.assertEqual(parsed.reported_cost_usd, 0.01)
        # Never surface global log paths.
        blob = json.dumps(parsed.__dict__, default=str)
        self.assertNotIn("unified.jsonl", blob)
        self.assertNotIn(".grok/logs", blob)

    def test_grok_global_log_only_for_matching_session(self) -> None:
        h = GrokHarness(binary=str(self.fake_bin))
        job = self._job(harness="grok")
        grok_home = self.root / "grok-home"
        log_dir = grok_home / "logs"
        log_dir.mkdir(parents=True)
        log_path = log_dir / "unified.jsonl"
        lines = [
            json.dumps(
                {
                    "sid": "other-session",
                    "type": "shell.turn.inference_done",
                    "ctx": {
                        "prompt_tokens": 999,
                        "cached_prompt_tokens": 0,
                        "completion_tokens": 999,
                    },
                }
            ),
            json.dumps(
                {
                    "sid": "match-me",
                    "type": "shell.turn.inference_done",
                    "ctx": {
                        "prompt_tokens": 80,
                        "cached_prompt_tokens": 20,
                        "completion_tokens": 12,
                    },
                }
            ),
            json.dumps(
                {
                    "sid": "match-me",
                    "type": "shell.turn.inference_done",
                    "ctx": {
                        "prompt_tokens": 40,
                        "cached_prompt_tokens": 0,
                        "completion_tokens": 8,
                    },
                }
            ),
        ]
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        stdout = json.dumps({"text": "hi", "sessionId": "match-me"})
        with mock.patch.dict(os.environ, {"GROK_HOME": str(grok_home)}):
            parsed = h.parse_output(job, stdout)
        assert parsed.usage is not None
        # Sum of matching incremental events only: (80-20)+(40-0)=100 in, 20 cached, 20 out
        self.assertEqual(parsed.usage.input_tokens, 100)
        self.assertEqual(parsed.usage.cached_input_tokens, 20)
        self.assertEqual(parsed.usage.output_tokens, 20)
        self.assertEqual(parsed.session_id, "match-me")
        # ParsedOutput must not embed the log path.
        self.assertNotIn(str(log_path), repr(parsed))

    def test_grok_no_global_log_without_session(self) -> None:
        h = GrokHarness(binary=str(self.fake_bin))
        job = self._job(harness="grok")
        grok_home = self.root / "grok-home2"
        log_dir = grok_home / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "unified.jsonl").write_text(
            json.dumps(
                {
                    "sid": "x",
                    "type": "shell.turn.inference_done",
                    "ctx": {"prompt_tokens": 10, "completion_tokens": 1},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"GROK_HOME": str(grok_home)}):
            # No session id in stdout → do not harvest global log.
            parsed = h.parse_output(job, '{"text":"only text"}')
        self.assertEqual(parsed.last_message, "only text")
        self.assertIsNone(parsed.usage)

    def test_pi_session_usage_and_message(self) -> None:
        h = PiHarness(binary=str(self.fake_bin))
        job = self._job(harness="pi", model="glm-5.2")
        session_dir = h._session_dir(job)
        session_dir.mkdir(parents=True)
        entries = [
            {"type": "session", "id": "pi-session", "version": 3},
            {"type": "message", "message": {"role": "user", "content": "task"}},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "first"}],
                    "usage": {
                        "input": 100,
                        "output": 20,
                        "cacheRead": 30,
                        "cacheWrite": 4,
                        "cost": {"total": 99},
                    },
                },
            },
            {"type": "compaction", "summary": "old history remains billable"},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "final"}],
                    "usage": {
                        "input": 10,
                        "output": 5,
                        "cacheRead": 2,
                        "cacheWrite": 0,
                    },
                },
            },
        ]
        (session_dir / "session.jsonl").write_text(
            "".join(json.dumps(entry) + "\n" for entry in entries),
            encoding="utf-8",
        )
        parsed = h.parse_output(job, "bounded final stdout")
        self.assertEqual(parsed.usage, Usage(110, 32, 4, 25))
        self.assertEqual(parsed.last_message, "final")
        self.assertEqual(parsed.session_id, "pi-session")
        self.assertIsNone(parsed.reported_cost_usd)

    def test_pi_session_capture_fails_closed(self) -> None:
        h = PiHarness(binary=str(self.fake_bin))
        job = self._job(harness="pi", model="glm-5.2")
        self.assertIsNone(h.parse_output(job, "ignored").usage)

        session_dir = h._session_dir(job)
        session_dir.mkdir(parents=True)
        (session_dir / "one.jsonl").write_text("{}\n", encoding="utf-8")
        (session_dir / "two.jsonl").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            h.parse_output(job, "ignored")

    def test_agy_json_usage_and_response(self) -> None:
        h = AgyHarness(binary=str(self.fake_bin))
        job = self._job(harness="agy", model="gemini-3.5-flash")
        payload = {
            "conversation_id": "agy-session",
            "status": "SUCCESS",
            "response": "done\n",
            "usage": {
                "input_tokens": 17_492,
                "output_tokens": 5,
                "thinking_tokens": 2,
                "total_tokens": 17_497,
            },
        }
        parsed = h.parse_output(job, json.dumps(payload))
        self.assertEqual(parsed.usage, Usage(input_tokens=17_492, output_tokens=5))
        self.assertEqual(parsed.last_message, "done\n")
        self.assertEqual(parsed.session_id, "agy-session")
        self.assertIsNone(parsed.reported_cost_usd)

    def test_malformed_and_noisy_output_tolerated(self) -> None:
        for harness_cls, harness_name in (
            (AgyHarness, "agy"),
            (CodexHarness, "codex"),
            (ClaudeHarness, "claude"),
            (GrokHarness, "grok"),
            (PiHarness, "pi"),
        ):
            with self.subTest(harness=harness_name):
                h = harness_cls(binary=str(self.fake_bin))
                job = self._job(harness=harness_name)
                noisy = 'not json\n{broken\n<<<>>>\n{"partial": true\n'
                parsed = h.parse_output(job, noisy)
                self.assertIsInstance(parsed, ParsedOutput)
                # Empty / garbage yields empty recovery, not an exception.
                empty = h.parse_output(job, "")
                self.assertIsInstance(empty, ParsedOutput)


if __name__ == "__main__":
    unittest.main()
