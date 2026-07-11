"""Agent-harness adapter layer for Basecamp Bench.

Isolates CLI-specific command construction, environment allowlisting, and
output parsing behind a small registry of :class:`Harness` adapters. This
module is intentionally free of runner pipeline imports (no ``run_bench``).
"""

from __future__ import annotations

import json
import os
import shutil
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

__all__ = [
    "ModelSpec",
    "Usage",
    "ParsedOutput",
    "AgentJob",
    "Harness",
    "CodexHarness",
    "ClaudeHarness",
    "GrokHarness",
    "register_harness",
    "get_harness",
    "registered_harnesses",
    "is_retained_env_name",
    "RETAINED_ENV_NAMES",
    "DEFAULT_GROK_BINARY",
]


# =============================================================================
# Immutable value types
# =============================================================================


@dataclass(frozen=True)
class ModelSpec:
    """Harness-native model identifier and reasoning effort."""

    model: str
    effort: str


@dataclass(frozen=True)
class Usage:
    """Normalized, disjoint token buckets.

    ``input_tokens`` excludes cached reads and cache writes. Adapters normalize
    each CLI's native semantics into these four buckets.
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: Usage) -> Usage:
        """Return a new :class:`Usage` with bucket-wise sums."""
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    def total(self) -> int:
        """Sum of all four token buckets."""
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.cache_write_tokens
            + self.output_tokens
        )


@dataclass(frozen=True)
class ParsedOutput:
    """Trustworthy fields recovered from a harness CLI's stdout/log text.

    Never contains secret values or global log paths — only usage, cost,
    final message text, and session identifiers when confidently parsed.
    """

    usage: Usage | None = None
    reported_cost_usd: float | None = None
    last_message: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class AgentJob:
    """One isolated implement or evaluate invocation for a harness adapter."""

    kind: Literal["implement", "evaluate"]
    harness: str
    model: ModelSpec
    workdir: Path
    prompt_path: Path
    log_path: Path
    last_message_path: Path
    evidence_dirs: tuple[Path, ...] = ()
    sandbox_mode: str = "workspace-write"

    def __post_init__(self) -> None:
        if self.kind not in ("implement", "evaluate"):
            raise ValueError(f"AgentJob.kind must be 'implement' or 'evaluate', got {self.kind!r}")


# =============================================================================
# Environment allowlist (names only — values never logged by this module)
# =============================================================================

# Exact names retained by :meth:`Harness.prepare_env`. Exported for tests.
RETAINED_ENV_NAMES: frozenset[str] = frozenset(
    {
        # Runtime / paths
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TEMP",
        "TMP",
        # Locale
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LC_COLLATE",
        "LC_MONETARY",
        "LC_NUMERIC",
        "LC_TIME",
        "TZ",
        # SSL / certificates
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        # Proxy (common cases, both casings)
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        # OpenAI / Codex
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
        "CODEX_API_KEY",
        # Anthropic / Claude
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_BASE",
        "CLAUDE_API_KEY",
        "CLAUDE_CODE_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        # Grok / xAI
        "XAI_API_KEY",
        "XAI_BASE_URL",
        "GROK_API_KEY",
        "GROK_HOME",
    }
)

_RETAINED_ENV_PREFIXES: tuple[str, ...] = ("LC_",)


def is_retained_env_name(name: str) -> bool:
    """Return True if *name* is on the portable environment allowlist.

    Matching is by exact name (see :data:`RETAINED_ENV_NAMES`) or by the
    ``LC_`` locale prefix. Values are never inspected.
    """
    if name in RETAINED_ENV_NAMES:
        return True
    return any(name.startswith(prefix) for prefix in _RETAINED_ENV_PREFIXES)


# =============================================================================
# JSON helpers
# =============================================================================


def _iter_json_objects(text: str) -> Iterable[dict[str, Any]]:
    """Yield JSON objects from whole-body JSON or JSONL; noise-tolerant."""
    body = text.strip()
    if body.startswith("{"):
        try:
            obj = json.loads(body)
            if isinstance(obj, dict):
                yield obj
                return
        except json.JSONDecodeError:
            pass
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _int_of(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _float_of(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# =============================================================================
# Harness interface
# =============================================================================


class Harness(ABC):
    """Adapter for one agent CLI.

    Subclass, set ``name``, implement :meth:`build_command`, and register with
    :func:`register_harness`. Override cwd/stdin/env/parse hooks as needed.
    """

    name: ClassVar[str]

    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary

    def configured_binary(self) -> str:
        """Configured binary name or path (not necessarily resolved)."""
        return self._binary if self._binary is not None else self.name

    def resolve_binary(self) -> str:
        """Resolve the configured binary to an executable path.

        Absolute or path-like configurations are checked exactly as given and
        never silently substituted with a same-named executable from ``PATH``.
        Bare names are looked up via ``PATH``. Raises :class:`FileNotFoundError`
        with a clear message when the configured binary is missing.
        """
        configured = self.configured_binary()
        if not configured:
            raise FileNotFoundError(f"Harness '{self.name}': no binary configured")
        looks_like_path = (
            os.path.isabs(configured)
            or os.sep in configured
            or (os.altsep is not None and os.altsep in configured)
        )
        if looks_like_path:
            path = Path(configured)
            if not path.is_file():
                raise FileNotFoundError(
                    f"Harness '{self.name}': configured binary not found: {configured}"
                )
            return str(path)
        found = shutil.which(configured)
        if not found:
            raise FileNotFoundError(
                f"Harness '{self.name}': binary '{configured}' not found on PATH"
            )
        return found

    def version_command(self) -> list[str]:
        """Minimal, side-effect-free argv that prints the CLI version."""
        return [self.resolve_binary(), "--version"]

    @abstractmethod
    def build_command(self, job: AgentJob) -> list[str]:
        """Build argv for a full agent run (list of strings, never a shell string).

        Prompt contents must not appear in argv — use stdin or a prompt file.
        """

    def working_directory(self, job: AgentJob) -> Path | None:
        """Subprocess cwd, or ``None`` when the CLI sets workspace via flags only.

        Default: ``job.workdir``.
        """
        return job.workdir

    def stdin_for(self, job: AgentJob) -> bytes | None:
        """Bytes to pass as stdin, or ``None`` when unused."""
        return None

    def prepare_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Build a fresh environment from the portable allowlist.

        Copies only retained names from *base* (or ``os.environ`` when *base*
        is ``None``). Does not pass through arbitrary host variables.
        """
        source: Mapping[str, str] = os.environ if base is None else base
        return {k: v for k, v in source.items() if is_retained_env_name(k)}

    def parse_output(self, job: AgentJob, stdout_text: str) -> ParsedOutput:
        """Recover usage / cost / message / session from stdout or command log text.

        Best-effort and non-raising for ordinary parse failures: return whatever
        trustworthy fields were recovered. Never returns secret values or global
        log paths.
        """
        return ParsedOutput()


# =============================================================================
# Registry
# =============================================================================

_HARNESS_TYPES: dict[str, type[Harness]] = {}


def register_harness(
    cls: type[Harness] | None = None,
    *,
    replace: bool = False,
) -> type[Harness] | Callable[[type[Harness]], type[Harness]]:
    """Register a :class:`Harness` subclass under its ``name`` ClassVar.

    May be used as ``@register_harness`` or ``@register_harness(replace=True)``.
    Rejects missing/empty names and duplicate names unless *replace* is True.
    """

    def decorator(harness_cls: type[Harness]) -> type[Harness]:
        name = getattr(harness_cls, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{harness_cls.__name__} must define a non-empty ClassVar 'name'")
        if name in _HARNESS_TYPES and not replace:
            raise ValueError(
                f"Harness '{name}' is already registered; pass replace=True to override"
            )
        _HARNESS_TYPES[name] = harness_cls
        return harness_cls

    if cls is not None:
        return decorator(cls)
    return decorator


def get_harness(name: str, *, binary: str | None = None) -> Harness:
    """Return a new adapter instance for *name*.

    Raises :class:`KeyError` for unknown harness names. Optional *binary*
    overrides the adapter's default executable configuration.
    """
    try:
        harness_cls = _HARNESS_TYPES[name]
    except KeyError as exc:
        known = ", ".join(registered_harnesses()) or "(none)"
        raise KeyError(f"Unknown harness '{name}'. Registered: {known}") from exc
    return harness_cls(binary=binary)


def registered_harnesses() -> list[str]:
    """Sorted list of registered harness names (deterministic order)."""
    return sorted(_HARNESS_TYPES)


# =============================================================================
# Built-in harnesses
# =============================================================================


@register_harness
class CodexHarness(Harness):
    """OpenAI Codex CLI — ``exec``, JSONL events, prompt on stdin."""

    name = "codex"

    def working_directory(self, job: AgentJob) -> Path | None:
        # Workspace is selected with ``-C``; keep process cwd neutral.
        return None

    def stdin_for(self, job: AgentJob) -> bytes | None:
        return job.prompt_path.read_bytes()

    def build_command(self, job: AgentJob) -> list[str]:
        cmd: list[str] = [
            self.resolve_binary(),
            "exec",
            "-m",
            job.model.model,
            "-c",
            f"model_reasoning_effort={job.model.effort}",
            "-C",
            str(job.workdir),
            "-s",
            job.sandbox_mode,
            "--skip-git-repo-check",
            "--json",
            "-o",
            str(job.last_message_path),
        ]
        if job.sandbox_mode == "danger-full-access":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        # Evaluators may only read explicit evidence dirs (never implicit provenance).
        for evidence in job.evidence_dirs:
            cmd.extend(["--add-dir", str(evidence)])
        cmd.append("-")  # prompt on stdin
        return cmd

    def parse_output(self, job: AgentJob, stdout_text: str) -> ParsedOutput:
        # Codex ``input_tokens`` includes cached reads; normalize to disjoint buckets.
        # Prefer a final cumulative ``token_count`` when present; otherwise sum
        # demonstrably per-turn ``turn.completed`` usage events.
        def from_codex(data: dict[str, Any]) -> Usage:
            raw_input = _int_of(data.get("input_tokens"))
            cached = _int_of(data.get("cached_input_tokens"))
            cache_write = _int_of(
                data.get("cache_write_tokens") or data.get("cache_creation_input_tokens")
            )
            return Usage(
                input_tokens=max(raw_input - cached, 0),
                cached_input_tokens=cached,
                cache_write_tokens=cache_write,
                output_tokens=_int_of(data.get("output_tokens")),
            )

        cumulative: Usage | None = None
        per_turn = Usage()
        saw_turn = False
        last_message: str | None = None
        session_id: str | None = None
        cost: float | None = None

        for obj in _iter_json_objects(stdout_text):
            if obj.get("type") == "turn.completed" and isinstance(obj.get("usage"), dict):
                per_turn = per_turn.add(from_codex(obj["usage"]))
                saw_turn = True

            msg = obj.get("msg")
            if isinstance(msg, dict) and msg.get("type") == "token_count":
                info = msg.get("info") or {}
                if isinstance(info, dict):
                    total = info.get("total_token_usage")
                    if isinstance(total, dict):
                        cumulative = from_codex(total)

            if isinstance(obj.get("session_id"), str):
                session_id = obj["session_id"]
            if isinstance(obj.get("sessionId"), str):
                session_id = obj["sessionId"]

            if isinstance(obj.get("last_agent_message"), str):
                last_message = obj["last_agent_message"]
            result = obj.get("result")
            if isinstance(result, str):
                last_message = result

            reported = obj.get("total_cost_usd")
            parsed_cost = _float_of(reported)
            if parsed_cost is not None:
                cost = parsed_cost

        usage = cumulative if cumulative is not None else (per_turn if saw_turn else None)
        return ParsedOutput(
            usage=usage,
            reported_cost_usd=cost,
            last_message=last_message,
            session_id=session_id,
        )


@register_harness
class ClaudeHarness(Harness):
    """Claude Code — headless ``-p`` with prompt on stdin, JSON result."""

    name = "claude"

    def stdin_for(self, job: AgentJob) -> bytes | None:
        return job.prompt_path.read_bytes()

    def build_command(self, job: AgentJob) -> list[str]:
        # With no positional prompt, ``-p`` reads the prompt from stdin.
        cmd: list[str] = [
            self.resolve_binary(),
            "-p",
            "--model",
            job.model.model,
            "--effort",
            job.model.effort,
            "--output-format",
            "json",
            "--no-session-persistence",
        ]
        if job.sandbox_mode == "danger-full-access":
            cmd.extend(["--permission-mode", "bypassPermissions"])
        else:
            cmd.extend(
                [
                    "--permission-mode",
                    "dontAsk",
                    "--allowedTools",
                    "Bash,Edit,Glob,Grep,Read,Write",
                ]
            )
        for evidence in job.evidence_dirs:
            cmd.extend(["--add-dir", str(evidence)])
        return cmd

    def parse_output(self, job: AgentJob, stdout_text: str) -> ParsedOutput:
        # Prefer summing ``modelUsage`` (per-model, includes subagents). Fall back
        # to top-level ``usage``. Buckets are already disjoint in Claude output.
        for obj in _iter_json_objects(stdout_text):
            if obj.get("type") != "result" and "usage" not in obj and "modelUsage" not in obj:
                continue
            usage: Usage | None = None
            model_usage = obj.get("modelUsage")
            if isinstance(model_usage, dict) and model_usage:
                usage = Usage()
                for mu in model_usage.values():
                    if not isinstance(mu, dict):
                        continue
                    usage = usage.add(
                        Usage(
                            input_tokens=_int_of(mu.get("inputTokens")),
                            cached_input_tokens=_int_of(mu.get("cacheReadInputTokens")),
                            cache_write_tokens=_int_of(mu.get("cacheCreationInputTokens")),
                            output_tokens=_int_of(mu.get("outputTokens")),
                        )
                    )
            elif isinstance(obj.get("usage"), dict):
                u = obj["usage"]
                usage = Usage(
                    input_tokens=_int_of(u.get("input_tokens")),
                    cached_input_tokens=_int_of(u.get("cache_read_input_tokens")),
                    cache_write_tokens=_int_of(u.get("cache_creation_input_tokens")),
                    output_tokens=_int_of(u.get("output_tokens")),
                )

            cost = _float_of(obj.get("total_cost_usd"))
            result = obj.get("result")
            last_message = result if isinstance(result, str) else None
            session_id = None
            for key in ("session_id", "sessionId"):
                val = obj.get(key)
                if isinstance(val, str):
                    session_id = val
                    break
            return ParsedOutput(
                usage=usage,
                reported_cost_usd=cost,
                last_message=last_message,
                session_id=session_id,
            )
        return ParsedOutput()


DEFAULT_GROK_BINARY = "grok"


@register_harness
class GrokHarness(Harness):
    """Grok Build — ``--prompt-file`` + ``--cwd``, JSON output, scoped permissions."""

    name = "grok"

    def __init__(self, binary: str | None = None) -> None:
        # Default to the absolute Grok install path; constructor may override.
        super().__init__(binary=DEFAULT_GROK_BINARY if binary is None else binary)

    def working_directory(self, job: AgentJob) -> Path | None:
        # Workspace is selected with ``--cwd``; process cwd optional.
        return None

    def stdin_for(self, job: AgentJob) -> bytes | None:
        # Prompt is carried via ``--prompt-file`` only.
        return None

    def build_command(self, job: AgentJob) -> list[str]:
        cmd: list[str] = [
            self.resolve_binary(),
            "--prompt-file",
            str(job.prompt_path),
            "--cwd",
            str(job.workdir),
            "-m",
            job.model.model,
            "--reasoning-effort",
            job.model.effort,
            "--output-format",
            "json",
            "--no-memory",
            "--verbatim",
        ]
        if job.sandbox_mode == "danger-full-access":
            cmd.append("--always-approve")
        else:
            cmd.extend(
                [
                    "--permission-mode",
                    "dontAsk",
                    "--tools",
                    "Bash,Edit,Glob,Grep,Read,Write",
                ]
            )
            cmd.extend(self._permission_rules(job))
        return cmd

    @staticmethod
    def _permission_rules(job: AgentJob) -> list[str]:
        """Build ``--allow`` / ``--deny`` rules from workdir and evidence_dirs only."""
        rules: list[str] = []
        workdir = Path(job.workdir)
        allowed_roots = (workdir, *job.evidence_dirs)

        # Read/search across workdir and evidence; write only inside workdir.
        for root in allowed_roots:
            root_s = str(root)
            rules.extend(
                [
                    "--allow",
                    f"Read({root_s})",
                    "--allow",
                    f"Read({root_s}/**)",
                    "--allow",
                    f"Grep({root_s}/**)",
                ]
            )
        workdir_s = str(workdir)
        rules.extend(
            [
                "--allow",
                "Bash(*)",
                "--allow",
                f"Write({workdir_s})",
                "--allow",
                f"Write({workdir_s}/**)",
                "--allow",
                f"Edit({workdir_s})",
                "--allow",
                f"Edit({workdir_s}/**)",
            ]
        )
        # Evidence trees are read-only: deny write/edit constructed only from
        # the supplied evidence_dirs (no implicit provenance paths).
        for evidence in job.evidence_dirs:
            evidence_s = str(evidence)
            rules.extend(
                [
                    "--deny",
                    f"Write({evidence_s})",
                    "--deny",
                    f"Write({evidence_s}/**)",
                    "--deny",
                    f"Edit({evidence_s})",
                    "--deny",
                    f"Edit({evidence_s}/**)",
                ]
            )
        return rules

    def parse_output(self, job: AgentJob, stdout_text: str) -> ParsedOutput:
        # Prefer the explicit command log / stdout. Usage may appear inline in
        # newer JSON; otherwise, only consult the global unified log when a
        # session id was observed and matches log event ``sid`` values.
        session_id: str | None = None
        last: str | None = None
        cost: float | None = None
        usage: Usage | None = None

        for obj in _iter_json_objects(stdout_text):
            if isinstance(obj.get("text"), str):
                last = obj["text"]
            if isinstance(obj.get("sessionId"), str):
                session_id = obj["sessionId"]
            if isinstance(obj.get("session_id"), str):
                session_id = obj["session_id"]

            parsed_cost = _float_of(obj.get("total_cost_usd") or obj.get("cost_usd"))
            if parsed_cost is not None:
                cost = parsed_cost

            # Inline usage (when present) — treat as authoritative for this object.
            if isinstance(obj.get("usage"), dict):
                inline = self._usage_from_mapping(obj["usage"])
            elif any(
                key in obj
                for key in (
                    "prompt_tokens",
                    "completion_tokens",
                    "input_tokens",
                    "output_tokens",
                    "cached_prompt_tokens",
                )
            ):
                inline = self._usage_from_mapping(obj)
            else:
                inline = None
            if inline is not None and inline.total() > 0:
                usage = inline

        if usage is None and session_id:
            usage = self._usage_from_global_log(session_id)

        return ParsedOutput(
            usage=usage,
            reported_cost_usd=cost,
            last_message=last,
            session_id=session_id,
        )

    @staticmethod
    def _usage_from_mapping(data: Mapping[str, Any] | None) -> Usage | None:
        if not isinstance(data, Mapping):
            return None
        # Support both Grok-native and OpenAI-ish field names.
        if "prompt_tokens" in data or "cached_prompt_tokens" in data or "completion_tokens" in data:
            prompt = _int_of(data.get("prompt_tokens"))
            cached = _int_of(data.get("cached_prompt_tokens"))
            cache_write = _int_of(
                data.get("cache_write_tokens") or data.get("cache_creation_tokens")
            )
            return Usage(
                input_tokens=max(prompt - cached, 0),
                cached_input_tokens=cached,
                cache_write_tokens=cache_write,
                output_tokens=_int_of(data.get("completion_tokens") or data.get("output_tokens")),
            )
        if "input_tokens" in data or "output_tokens" in data:
            raw_input = _int_of(data.get("input_tokens"))
            cached = _int_of(data.get("cached_input_tokens") or data.get("cache_read_input_tokens"))
            return Usage(
                input_tokens=max(raw_input - cached, 0)
                if "cached_input_tokens" in data or "cache_read_input_tokens" in data
                else raw_input,
                cached_input_tokens=cached,
                cache_write_tokens=_int_of(
                    data.get("cache_write_tokens") or data.get("cache_creation_input_tokens")
                ),
                output_tokens=_int_of(data.get("output_tokens")),
            )
        return None

    @staticmethod
    def _usage_from_global_log(session_id: str) -> Usage | None:
        """Sum incremental inference events for *session_id* only.

        Never returns the log path. Events without a matching ``sid`` are ignored.
        """
        if not session_id:
            return None
        log_path = (
            Path(os.environ.get("GROK_HOME", Path.home() / ".grok")) / "logs" / "unified.jsonl"
        )
        if not log_path.is_file():
            return None
        total = Usage()
        found = False
        try:
            with log_path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if session_id not in line or "inference_done" not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("sid") != session_id:
                        continue
                    ctx = obj.get("ctx") or {}
                    if not isinstance(ctx, dict):
                        continue
                    prompt = _int_of(ctx.get("prompt_tokens"))
                    cached = _int_of(ctx.get("cached_prompt_tokens"))
                    cache_write = _int_of(
                        ctx.get("cache_write_tokens") or ctx.get("cache_creation_tokens")
                    )
                    total = total.add(
                        Usage(
                            input_tokens=max(prompt - cached, 0),
                            cached_input_tokens=cached,
                            cache_write_tokens=cache_write,
                            output_tokens=_int_of(ctx.get("completion_tokens")),
                        )
                    )
                    found = True
        except OSError:
            return None
        return total if found else None
