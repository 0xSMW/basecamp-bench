"""Shared scaffolding for the basecamp-bench test suite."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path


class TempDirTestCase(unittest.TestCase):
    """Test case with an auto-cleaned temporary directory at ``self.root``."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

    def write_json(self, name: str, data: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


def can_symlink() -> bool:
    """True when the platform/filesystem allows creating symlinks."""
    tmp = tempfile.mkdtemp()
    try:
        target = Path(tmp) / "t"
        target.write_text("x", encoding="utf-8")
        link = Path(tmp) / "l"
        try:
            link.symlink_to(target)
            return True
        except (OSError, NotImplementedError):
            return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def minimal_manifest_kwargs(**overrides: object) -> dict:
    """Complete, valid kwargs for build_manifest; override per test."""
    file_hash = sha256_text("hello")
    config = {
        "mode": "local",
        "run_root": "runs",
        "seed_root": "Repo",
        "reference_root": "Repo/reference",
        "reference_manifest": "benchmarks/reference-pack.json",
        "timeout_s": 14400,
        "full_access": False,
        "repetitions": 1,
        "harnesses": {
            "codex": {
                "adapter": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "provider_family": "openai",
                "display_name": "Codex",
                "enabled": True,
            }
        },
        "evaluators": [
            {
                "id": "eval-sol",
                "harness": "codex",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "provider_family": "openai",
                "enabled": True,
            }
        ],
        "tracks": {
            "fe": {
                "prompt": "benchmarks/fe/prompt.md",
                "rubric": "benchmarks/fe/eval.md",
                "contract": "benchmarks/fe/contract.json",
            }
        },
        "pricing": {},
    }
    pricing = {
        "source": "fixture",
        "retrieved_at": "2026-07-11T11:59:00Z",
        "stale": False,
        "error": None,
        "complete": True,
        "limitations": [],
        "url": "https://models.dev/api.json",
        "cache_path": ".pricing-cache.json",
    }
    job = {
        "id": "job-1",
        "kind": "implement",
        "harness": "codex",
        "track": "fe",
        "repetition": 1,
        "submission_id": "submission-1",
        "command_preview": "agent run",
        "returncode": 0,
        "duration_s": 1.0,
        "timed_out": False,
        "interrupted": False,
        "error": None,
        "cost_usd": 0.1,
        "reported_cost_usd": None,
        "usage": {
            "input_tokens": 10,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 5,
        },
    }
    tooling = [
        {
            "role": "implementation",
            "config_id": "codex",
            "evaluator_id": None,
            "adapter": "codex",
            "model_id": "gpt-5.6-sol",
            "provider_family": "openai",
            "effort": "high",
            "executable_version": "codex 1.2.3",
            "version_error": None,
            "adapter_version": "1.0.0a1",
            "runner_version": "1.0.0a1",
            "deterministic_seed": {
                "supported": False,
                "limitation": "Agent CLI exposes no deterministic seed control.",
            },
        }
    ]
    kwargs: dict = {
        "runner_version": "1.0.0a1",
        "run_id": "run-test-001",
        "mode": "local",
        "config": config,
        "inputs": {"seed": file_hash},
        "pricing": pricing,
        "tooling": tooling,
        "jobs": [job],
        "artifacts": {"results/out.txt": file_hash},
        "status": "complete",
        "started_at": "2026-07-11T12:00:00Z",
        "finished_at": "2026-07-11T12:05:00Z",
        "repo": None,
    }
    kwargs.update(overrides)
    return kwargs
