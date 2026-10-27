"""Shared isolated jobs for adapter protocol tests."""

from __future__ import annotations

from pathlib import Path

from basecamp_bench.adapters import AgentJob, ModelSpec
from tests._support import TempDirTestCase as _TempDirTestCase

SENTINEL = "PROMPT_SENTINEL_NEVER_IN_ARGV_9f3a2c1e7b"


class TempDirTestCase(_TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
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
