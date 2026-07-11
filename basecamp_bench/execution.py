"""Adapter preparation, managed process execution, and result normalization.

This boundary translates a harness ``AgentJob`` into a portable execution
record. Benchmark planning, checkpointing, and evaluation policy remain owned
by :mod:`basecamp_bench.runner`.
"""

from __future__ import annotations

import re
import shlex
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from basecamp_bench.adapters import AgentJob, Harness, ParsedOutput, Usage, get_harness
from basecamp_bench.config import BenchConfig
from basecamp_bench.pricing import compute_cost, find_exact_rates
from basecamp_bench.processes import ProcessResult, run_managed
from basecamp_bench.safety import redact_text
from basecamp_bench.validation import is_finite_number

__all__ = ["AgentExecution", "execute_agent"]


class ExecutionOptions(Protocol):
    """The runner options required by the execution boundary."""

    @property
    def max_log_bytes(self) -> int:
        """Maximum bytes retained for each process stream."""
        ...


@dataclass(frozen=True)
class AgentExecution:
    """Normalized process, usage, cost, output, and error for one agent call."""

    process: ProcessResult
    usage: Usage | None
    cost_usd: float | None
    reported_cost_usd: float | None
    last_message: str | None
    command_preview: str
    error: str | None


def execute_agent(
    config: BenchConfig,
    job: AgentJob,
    *,
    pricing_data: Mapping[str, Any] | None,
    pricing_retrieved_at: str | None,
    options: ExecutionOptions,
    cancel_event: threading.Event | None = None,
) -> AgentExecution:
    """Run *job* through its adapter and normalize all observable results."""
    adapter = get_harness(job.harness, binary=_binary_for_job(config, job))
    roots, secrets = (config.root, config.run_root, job.workdir), _secret_env_values(adapter)
    context = adapter.execution_context(job)
    try:
        context.__enter__()
    except Exception as exc:  # noqa: BLE001
        return AgentExecution(
            process=ProcessResult.not_started(str(exc)),
            usage=None,
            cost_usd=None,
            reported_cost_usd=None,
            last_message=None,
            command_preview="<execution-setup-failed>",
            error=safe_error(str(exc), roots=roots, secret_values=secrets),
        )
    try:
        return _execute_agent_prepared(
            config,
            job,
            adapter=adapter,
            roots=roots,
            secrets=secrets,
            pricing_data=pricing_data,
            pricing_retrieved_at=pricing_retrieved_at,
            options=options,
            cancel_event=cancel_event,
        )
    finally:
        context.__exit__(None, None, None)


def _execute_agent_prepared(
    config: BenchConfig,
    job: AgentJob,
    *,
    adapter: Harness,
    roots: tuple[Path, Path, Path],
    secrets: Sequence[str],
    pricing_data: Mapping[str, Any] | None,
    pricing_retrieved_at: str | None,
    options: ExecutionOptions,
    cancel_event: threading.Event | None,
) -> AgentExecution:
    try:
        command = list(adapter.build_command(job))
    except Exception as exc:  # noqa: BLE001
        return AgentExecution(
            process=ProcessResult.not_started(str(exc)),
            usage=None,
            cost_usd=None,
            reported_cost_usd=None,
            last_message=None,
            command_preview="<command-build-failed>",
            error=safe_error(str(exc), roots=roots, secret_values=secrets),
        )
    preview = _command_preview(command, roots=roots, secret_values=secrets)
    stdout_path = job.log_path
    stderr_path = job.log_path.with_name(job.log_path.name + ".stderr")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    timeout_s: float | None = float(config.timeout_s) if config.timeout_s > 0 else None
    process = run_managed(
        command,
        cwd=adapter.working_directory(job),
        env=adapter.prepare_env(),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout_s=timeout_s,
        max_stream_bytes=options.max_log_bytes,
        stdin=adapter.stdin_for(job),
        cancel_event=cancel_event,
    )
    parse_error: str | None = None
    try:
        parsed = adapter.parse_output(job, read_text(stdout_path))
    except Exception as exc:  # noqa: BLE001 — malformed vendor output must fail closed
        parsed = ParsedOutput()
        parse_error = f"output parse failed: {type(exc).__name__}: {exc}"
    last = parsed.last_message
    if last is None and job.last_message_path.is_file():
        last = read_text(job.last_message_path).strip() or None
    execution_error = _execution_error(process)
    if execution_error is None and parse_error is not None:
        execution_error = parse_error
    if execution_error is None and adapter.requires_usage and parsed.usage is None:
        execution_error = "missing or incomplete required usage capture"
    return AgentExecution(
        process=process,
        usage=parsed.usage,
        cost_usd=_resolve_cost(
            job.model.model,
            parsed.usage,
            parsed.reported_cost_usd,
            pricing_data,
            pricing_retrieved_at,
            config.pricing_overrides,
        ),
        reported_cost_usd=parsed.reported_cost_usd,
        last_message=last,
        command_preview=preview,
        error=safe_error(execution_error, roots=roots, secret_values=secrets),
    )


def binary_for_job(config: BenchConfig, job: AgentJob) -> str | None:
    """Resolve the configured binary for an implementation or evaluator job."""
    for spec in config.harnesses.values():
        if spec.adapter == job.harness or spec.id == job.harness:
            return spec.binary
    for evaluator in config.evaluators:
        if evaluator.harness == job.harness:
            harness = config.harnesses.get(evaluator.harness)
            return harness.binary if harness else None
    return None


def secret_env_values(adapter: Harness) -> list[str]:
    """Collect configured secret-like values for output redaction."""
    values: list[str] = []
    for key, value in adapter.prepare_env().items():
        low = key.lower()
        if value and any(
            token in low for token in ("key", "token", "secret", "password", "credential")
        ):
            values.append(value)
    return values


def _command_preview(
    command: Sequence[str],
    *,
    roots: Sequence[Path],
    secret_values: Sequence[str],
) -> str:
    parts: list[str] = []
    for index, arg in enumerate(command):
        if index == 0:
            parts.append(Path(arg).name)
            continue
        text = redact_text(arg, roots=roots, secret_values=secret_values)
        if text.startswith("/") or (len(text) >= 2 and text[1] == ":" and text[0].isalpha()):
            text = Path(text).name or "<path>"
        parts.append(shlex.quote(text))
    return " ".join(parts)


def read_text(path: Path) -> str:
    """Read UTF-8 text lossily, returning an empty string for I/O failures."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _execution_error(process: ProcessResult) -> str | None:
    if process.error:
        return process.error
    if process.timed_out:
        return "timed out"
    if process.interrupted:
        return "interrupted"
    if process.returncode is None:
        return "process failed to start"
    if process.returncode != 0:
        return f"exit code {process.returncode}"
    return None


def safe_error(
    message: str | None,
    *,
    roots: Sequence[Path],
    secret_values: Sequence[str],
) -> str | None:
    """Redact and bound an exception or process error for portable records."""
    if message is None:
        return None
    text = redact_text(str(message), roots=roots, secret_values=secret_values)
    text = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s:]+)", "<path>", text)
    text = re.sub(r"\b[A-Za-z]:\\[^\s:]+", "<path>", text)
    return " ".join(text.split())[:500]


def process_ok(process: ProcessResult) -> bool:
    """Return whether a managed process completed successfully."""
    return (
        process.returncode == 0
        and not process.timed_out
        and not process.interrupted
        and process.error is None
    )


def _resolve_cost(
    model_id: str,
    usage: Usage | None,
    reported: float | None,
    pricing_data: Mapping[str, Any] | None,
    retrieved_at: str | None,
    overrides: Mapping[str, Any] | None,
) -> float | None:
    if reported is not None and _nonnegative(reported):
        return float(reported)
    if usage is None:
        return None
    lookup = find_exact_rates(model_id, pricing_data, overrides, retrieved_at=retrieved_at)
    if lookup.rates is None:
        return None
    try:
        return compute_cost(usage, lookup.rates)
    except (TypeError, ValueError):
        return None


def _nonnegative(value: object) -> bool:
    return is_finite_number(value) and float(value) >= 0.0


# Internal compatibility aliases keep call sites concise during the runner split.
_binary_for_job = binary_for_job
_secret_env_values = secret_env_values
