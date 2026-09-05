"""Agent-harness adapter layer for Basecamp Bench.

Isolates CLI-specific command construction, environment allowlisting, and
output parsing behind a small registry of :class:`Harness` adapters. This
module is intentionally free of runner pipeline imports (no ``run_bench``).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
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
    "OpenCodeHarness",
    "PiHarness",
    "AgyHarness",
    "register_harness",
    "get_harness",
    "registered_harnesses",
    "is_retained_env_name",
    "RETAINED_ENV_NAMES",
    "DEFAULT_GROK_BINARY",
    "PI_MODEL_ALIASES",
    "AGY_MODEL_ALIASES",
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
        # OpenCode
        "OPENCODE_API_KEY",
        # Pi / OpenRouter
        "OPENROUTER_API_KEY",
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
    requires_usage: ClassVar[bool] = False

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

    @contextmanager
    def execution_context(self, job: AgentJob) -> Iterator[None]:
        """Prepare transient per-job state, restoring it after execution."""
        yield

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

    @staticmethod
    def _sandbox_settings(job: AgentJob) -> str:
        """Return fail-closed Bash sandbox settings scoped to this job."""
        read_only = [job.workdir, *job.evidence_dirs]
        system_roots = (
            Path("/System"),
            Path("/Library"),
            Path("/bin"),
            Path("/sbin"),
            Path("/usr/bin"),
            Path("/usr/sbin"),
            Path("/usr/lib"),
            Path("/lib"),
            Path("/lib64"),
            Path("/etc"),
            Path("/dev"),
        )
        for path in system_roots:
            if path.is_dir() and path not in read_only:
                read_only.append(path)
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            path = Path(entry)
            if entry and path.is_absolute() and path.is_dir() and path not in read_only:
                read_only.append(path)

        deny_tools: list[str] = []
        for evidence in job.evidence_dirs:
            deny_tools.extend(
                [
                    f"Write({evidence})",
                    f"Write({evidence}/**)",
                    f"Edit({evidence})",
                    f"Edit({evidence}/**)",
                ]
            )
        settings = {
            "permissions": {"deny": deny_tools},
            "sandbox": {
                "enabled": True,
                "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
                "excludedCommands": [],
                "filesystem": {
                    "denyRead": ["/**"],
                    "allowRead": [str(path) for path in read_only],
                    "allowWrite": [str(job.workdir)],
                    "denyWrite": [str(path) for path in job.evidence_dirs],
                },
                "network": {
                    "allowedDomains": ["*"],
                    "allowAllUnixSockets": True,
                    "allowLocalBinding": True,
                },
            },
        }
        return json.dumps(settings, separators=(",", ":"), sort_keys=True)

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
                    "--settings",
                    self._sandbox_settings(job),
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


DEFAULT_OPENCODE_BINARY = "opencode"

OPENCODE_MODEL_ALIASES: Mapping[str, str] = {
    # Keep provider-qualified IDs out of benchmark config and map the stable
    # benchmark alias at the CLI boundary.
    "0x-alpha": "opencode/x-preview-f-free",
    "muse-spark-1.3": "opencode/muse-spark-1.3-contributor-free",
}

# Reasoning efforts each alias accepts; the effort is passed through as
# OpenCode's ``--variant``. Efforts outside this set fail closed.
OPENCODE_MODEL_EFFORTS: Mapping[str, frozenset[str]] = {
    "0x-alpha": frozenset({"max"}),
    "muse-spark-1.3": frozenset({"minimal", "low", "medium", "high", "xhigh"}),
}


@register_harness
class OpenCodeHarness(Harness):
    """OpenCode CLI using JSON event output and a per-job permission policy."""

    name = "opencode"
    requires_usage = True
    _PRIVATE_SUFFIX = ".opencode-state"
    # Some OpenCode-hosted models cap a single response well below the size of a
    # whole deliverable. A write emitted in one call is truncated at that cap and
    # the tool call is aborted, leaving no artifact, so require bounded writes.
    _COMPLETION_DIRECTIVE = (
        "\n\nNever emit a large file in one tool call: a single response that exceeds the "
        "output limit is truncated and its write is discarded, leaving no artifact. Write any "
        "file longer than roughly 300 lines as a sequence of bounded chunks (about 200-300 lines "
        "per write or append), one section at a time, and confirm each chunk landed before "
        "writing the next. Build large deliverables as separate part files if that helps, then "
        "consolidate them into the required final file and delete the parts. Verify the required "
        "file exists and is complete before finishing, and keep the closing summary under five "
        "sentences.\n"
    )

    def __init__(self, binary: str | None = None) -> None:
        super().__init__(binary=DEFAULT_OPENCODE_BINARY if binary is None else binary)
        self._active_config: Path | None = None
        self._active_config_dir: Path | None = None

    @classmethod
    def _private_root(cls, job: AgentJob) -> Path:
        return job.log_path.parent / f".{job.log_path.name}{cls._PRIVATE_SUFFIX}"

    @staticmethod
    def _native_model(model: str) -> str:
        try:
            return OPENCODE_MODEL_ALIASES[model]
        except KeyError as exc:
            known = ", ".join(sorted(OPENCODE_MODEL_ALIASES))
            raise ValueError(
                f"OpenCode model '{model}' is unsupported; configured aliases: {known}"
            ) from exc

    @contextmanager
    def execution_context(self, job: AgentJob) -> Iterator[None]:
        """Install private config so host OpenCode settings do not control the job."""
        private_root = self._private_root(job)
        config_path = private_root / "opencode.json"
        config_dir = private_root / "config"
        if os.path.lexists(private_root):
            raise ValueError("run private directory contains a reserved OpenCode adapter path")

        permission: str | dict[str, object]
        if job.sandbox_mode == "danger-full-access":
            permission = "allow"
        else:
            # OpenCode's default permissions allow normal workspace tools while
            # asking before a tool touches an external directory. In headless
            # mode an ask is rejected, so make the intended boundary explicit.
            permission = {
                "*": "allow",
                "external_directory": "deny",
            }

        config_dir.mkdir(parents=True)
        config_path.write_text(
            json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "permission": permission,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self._active_config = config_path
        self._active_config_dir = config_dir
        try:
            yield
        finally:
            self._active_config = None
            self._active_config_dir = None
            if private_root.is_symlink():
                private_root.unlink()
            elif private_root.is_dir():
                shutil.rmtree(private_root)
            elif os.path.lexists(private_root):
                raise ValueError("OpenCode reserved private root changed type")

    def prepare_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        env = super().prepare_env(base)
        if self._active_config is not None and self._active_config_dir is not None:
            env["OPENCODE_CONFIG"] = str(self._active_config)
            env["OPENCODE_CONFIG_DIR"] = str(self._active_config_dir)
        return env

    def stdin_for(self, job: AgentJob) -> bytes | None:
        prompt = job.prompt_path.read_text(encoding="utf-8")
        return (prompt + self._COMPLETION_DIRECTIVE).encode("utf-8")

    def build_command(self, job: AgentJob) -> list[str]:
        native_model = self._native_model(job.model.model)
        allowed = OPENCODE_MODEL_EFFORTS[job.model.model]
        if job.model.effort not in allowed:
            wanted = " or ".join(f"'{effort}'" for effort in sorted(allowed))
            raise ValueError(
                f"OpenCode model '{job.model.model}' requires thinking effort {wanted}"
            )
        cmd = [
            self.resolve_binary(),
            "run",
            "--model",
            native_model,
            "--variant",
            job.model.effort,
            "--format",
            "json",
            "--dir",
            str(job.workdir),
            "--pure",
        ]
        if job.sandbox_mode == "danger-full-access":
            cmd.append("--auto")
        return cmd

    @staticmethod
    def _step_usage(tokens: Mapping[str, Any]) -> Usage:
        cache = tokens.get("cache")
        cache_read = 0
        cache_write = 0
        if isinstance(cache, Mapping):
            cache_read = _int_of(cache.get("read"))
            cache_write = _int_of(cache.get("write"))
        inclusive_input = _int_of(tokens.get("input"))
        return Usage(
            input_tokens=max(inclusive_input - cache_read - cache_write, 0),
            cached_input_tokens=cache_read,
            cache_write_tokens=cache_write,
            # OpenCode's output token count includes reasoning tokens.
            output_tokens=_int_of(tokens.get("output")),
        )

    @staticmethod
    def _error_message(value: Any) -> str:
        if isinstance(value, str) and value:
            return value
        if isinstance(value, Mapping):
            data = value.get("data")
            if isinstance(data, Mapping) and isinstance(data.get("message"), str):
                return data["message"]
            for key in ("message", "name"):
                if isinstance(value.get(key), str):
                    return value[key]
        return "unknown OpenCode error"

    def parse_output(self, job: AgentJob, stdout_text: str) -> ParsedOutput:
        del job
        usage: Usage | None = None
        cost: float | None = None
        last_message: str | None = None
        session_id: str | None = None
        text_parts: dict[str, str] = {}
        text_order: list[str] = []
        last_finish_reason: str | None = None

        for obj in _iter_json_objects(stdout_text):
            for key in ("sessionID", "sessionId", "session_id"):
                value = obj.get(key)
                if isinstance(value, str) and value:
                    session_id = value
                    break

            if obj.get("type") == "error":
                raise ValueError(
                    f"OpenCode execution failed: {self._error_message(obj.get('error'))}"
                )

            part = obj.get("part")
            if not isinstance(part, Mapping):
                continue
            part_type = part.get("type")
            if part_type == "tool":
                state = part.get("state")
                if isinstance(state, Mapping) and state.get("status") == "error":
                    metadata = state.get("metadata")
                    interrupted = (
                        isinstance(metadata, Mapping) and metadata.get("interrupted") is True
                    )
                    if interrupted and last_finish_reason == "length":
                        raise ValueError(
                            "OpenCode execution failed: model output hit the response length "
                            f"limit and the '{part.get('tool')}' tool call was aborted, so no "
                            "file was written"
                        )
                continue
            if part_type == "text" and isinstance(part.get("text"), str):
                part_id = part.get("id")
                if not isinstance(part_id, str) or not part_id:
                    part_id = f"text-{len(text_order)}"
                if part_id not in text_parts:
                    text_order.append(part_id)
                text_parts[part_id] = part["text"]
                continue

            if part_type != "step-finish":
                continue
            reason = part.get("reason")
            last_finish_reason = reason if isinstance(reason, str) else None
            tokens = part.get("tokens")
            if isinstance(tokens, Mapping):
                step_usage = self._step_usage(tokens)
                usage = step_usage if usage is None else usage.add(step_usage)
            step_cost = _float_of(part.get("cost"))
            if step_cost is not None:
                cost = step_cost if cost is None else cost + step_cost

        if last_finish_reason == "unknown":
            raise ValueError("OpenCode reported an unknown finish reason")
        if text_order:
            last_message = "\n".join(text_parts[key] for key in text_order)
        return ParsedOutput(
            usage=usage,
            reported_cost_usd=cost,
            last_message=last_message,
            session_id=session_id,
        )


DEFAULT_GROK_BINARY = "grok"

_GROK_DISALLOWED_TOOLS = (
    "ask_user_question",
    "image_edit",
    "image_gen",
    "lsp",
    "memory_get",
    "memory_search",
    "monitor",
    "scheduler_create",
    "scheduler_delete",
    "scheduler_list",
    "search_tool",
    "todo_write",
    "use_tool",
    "video_gen",
    "web_fetch",
    "web_search",
)


@register_harness
class GrokHarness(Harness):
    """Grok Build — ``--prompt-file`` + ``--cwd``, JSON output, scoped permissions."""

    name = "grok"

    def __init__(self, binary: str | None = None) -> None:
        # Community installs resolve from PATH; local config may pin an absolute
        # binary when multiple CLIs share the same command name.
        super().__init__(binary=DEFAULT_GROK_BINARY if binary is None else binary)

    def working_directory(self, job: AgentJob) -> Path | None:
        # Workspace is selected with ``--cwd``; process cwd optional.
        return None

    def stdin_for(self, job: AgentJob) -> bytes | None:
        # Prompt is carried via ``--prompt-file`` only.
        return None

    def prepare_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        env = super().prepare_env(base)
        # Keep each benchmark submission to one model agent. Unlike
        # ``--no-subagents``, this does not remove the task-output companions
        # that Grok's background-capable terminal requires at session startup.
        env["GROK_SUBAGENTS"] = "0"
        return env

    @contextmanager
    def execution_context(self, job: AgentJob) -> Iterator[None]:
        """Install a fail-closed custom OS sandbox profile for this job."""
        if job.sandbox_mode == "danger-full-access":
            yield
            return

        grok_dir = job.workdir / ".grok"
        profile_path = grok_dir / "sandbox.toml"
        prompt_copy = grok_dir / ".basecamp-bench-prompt.md"
        if grok_dir.is_symlink() or profile_path.is_symlink() or prompt_copy.is_symlink():
            raise ValueError("Grok sandbox configuration path must not be a symlink")

        created_dir = not grok_dir.exists()
        if created_dir:
            grok_dir.mkdir()
        elif not grok_dir.is_dir():
            raise ValueError("Grok sandbox configuration parent must be a directory")

        original = profile_path.read_bytes() if profile_path.exists() else None
        if original is not None and b"[profiles.basecamp_bench]" in original:
            raise ValueError("workspace already defines reserved Grok sandbox profile")
        if prompt_copy.exists():
            raise ValueError("workspace contains reserved Grok prompt path")

        read_only = list(job.evidence_dirs)
        if any(not path.is_dir() for path in read_only):
            raise ValueError("Grok evidence paths must be directories")
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            path = Path(entry)
            if entry and path.is_absolute() and path.is_dir() and path not in read_only:
                read_only.append(path)
        profile = (
            "\n[profiles.basecamp_bench]\n"
            'extends = "strict"\n'
            "restrict_network = false\n"
            f"read_only = {json.dumps([str(path) for path in read_only])}\n"
        ).encode()
        prefix = b"" if original is None or original.endswith(b"\n") else b"\n"
        profile_path.write_bytes((original or b"") + prefix + profile)
        prompt_copy.write_bytes(job.prompt_path.read_bytes())
        try:
            yield
        finally:
            if grok_dir.is_symlink():
                return
            if profile_path.is_symlink():
                profile_path.unlink()
            if prompt_copy.is_symlink() or prompt_copy.is_file():
                prompt_copy.unlink()
            if original is None:
                if profile_path.is_file():
                    profile_path.unlink()
            else:
                profile_path.write_bytes(original)
            if created_dir:
                try:
                    grok_dir.rmdir()
                except OSError:
                    pass

    def build_command(self, job: AgentJob) -> list[str]:
        cmd: list[str] = [
            self.resolve_binary(),
            "--prompt-file",
            str(
                job.prompt_path
                if job.sandbox_mode == "danger-full-access"
                else job.workdir / ".grok" / ".basecamp-bench-prompt.md"
            ),
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
                    "--sandbox",
                    "basecamp_bench",
                    "--permission-mode",
                    "dontAsk",
                    "--disallowed-tools",
                    # Grok 0.2.93 leaves Bash auto-backgrounding enabled while
                    # every positive --tools allowlist disables background
                    # support, causing session construction to fail. Start from
                    # its coherent default tool graph and remove capabilities
                    # the benchmark does not need. The OS sandbox and explicit
                    # permission rules below remain the filesystem boundary.
                    ",".join(_GROK_DISALLOWED_TOOLS),
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
                # Grok 1.0 documents the bare Bash prefix as the catch-all
                # permission rule. ``Bash(*)`` no longer authorizes native
                # ``run_terminal_command`` calls under ``dontAsk``.
                "Bash",
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


PI_MODEL_ALIASES: Mapping[str, str] = {
    # Benchmark identifiers must be safe path components, while OpenRouter's
    # native model identifiers contain a provider slash.
    "glm-5.2": "z-ai/glm-5.2",
}

# Pi lanes authenticate via $OPENROUTER_API_KEY in the generated catalog. The
# variable is only forwarded when the process launching the benchmark has it,
# which silently breaks runs started outside an interactive shell. This file
# is the durable fallback; the env var still wins when both are present.
OPENROUTER_KEY_FILE: Path = Path.home() / ".config" / "basecamp-bench" / "openrouter-api-key"


def _read_openrouter_key_file() -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(OPENROUTER_KEY_FILE, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            value = handle.read(16_385)
    except (OSError, UnicodeError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    if len(value) > 16_384:
        return None
    return value.strip() or None


@register_harness
class PiHarness(Harness):
    """Pi coding agent using an isolated OpenRouter model catalog."""

    name = "pi"
    requires_usage = True
    _PRIVATE_SUFFIX = ".pi-state"

    def __init__(self, binary: str | None = None) -> None:
        super().__init__(binary=binary)
        self._active_config_dir: Path | None = None

    @classmethod
    def _private_root(cls, job: AgentJob) -> Path:
        return job.log_path.parent / f".{job.log_path.name}{cls._PRIVATE_SUFFIX}"

    @classmethod
    def _session_dir(cls, job: AgentJob) -> Path:
        return cls._private_root(job) / "sessions"

    @staticmethod
    def _native_model(model: str) -> str:
        try:
            return PI_MODEL_ALIASES[model]
        except KeyError as exc:
            known = ", ".join(sorted(PI_MODEL_ALIASES))
            raise ValueError(
                f"Pi model '{model}' is unsupported; configured aliases: {known}"
            ) from exc

    @staticmethod
    def _session_token(usage: Mapping[str, Any], key: str) -> int:
        value = usage.get(key)
        if type(value) is not int or value < 0:
            raise ValueError("assistant message has invalid token counts")
        return value

    @contextmanager
    def execution_context(self, job: AgentJob) -> Iterator[None]:
        """Install a private model catalog and disposable native session ledger."""
        private_root = self._private_root(job)
        config_dir = private_root / "config"
        models_path = config_dir / "models.json"
        settings_path = config_dir / "settings.json"
        session_dir = self._session_dir(job)
        reserved = (private_root, config_dir, models_path, settings_path, session_dir)
        if any(path.is_symlink() for path in reserved):
            raise ValueError("Pi reserved configuration paths must not be symlinks")
        if os.path.lexists(private_root):
            raise ValueError("run private directory contains a reserved Pi adapter path")
        if any(not path.is_dir() for path in job.evidence_dirs):
            raise ValueError("Pi evidence paths must be directories")
        if not os.environ.get("OPENROUTER_API_KEY") and _read_openrouter_key_file() is None:
            raise ValueError(
                "Pi lane needs an OpenRouter key: export OPENROUTER_API_KEY in the "
                f"launching shell or write the key to {OPENROUTER_KEY_FILE}"
            )

        native_model = self._native_model(job.model.model)
        config_dir.mkdir(parents=True)
        self._active_config_dir = config_dir
        try:
            models_path.write_text(
                json.dumps(
                    {
                        "providers": {
                            "openrouter": {
                                "baseUrl": "https://openrouter.ai/api/v1",
                                "api": "openai-completions",
                                "apiKey": "$OPENROUTER_API_KEY",
                                "models": [
                                    {
                                        "id": native_model,
                                        "name": "GLM 5.2",
                                        "reasoning": True,
                                        "input": ["text"],
                                        "contextWindow": 1_048_576,
                                        "maxTokens": 131_072,
                                        # OpenRouter's default (price-ordered) routing
                                        # re-rolls the provider on every turn, and some
                                        # hosts (Z.AI, Venice, partially Novita) buffer
                                        # whole responses instead of streaming. A long
                                        # write turn on a buffering host emits no bytes
                                        # for its entire generation, which reads as a
                                        # hang and previously tripped Pi's idle timeout.
                                        # Pin to hosts verified to stream tool-call
                                        # deltas, serve fp8, and allow >=128k completion
                                        # tokens.
                                        "compat": {
                                            "openRouterRouting": {
                                                "order": [
                                                    "baidu",
                                                    "atlas-cloud",
                                                    "akashml",
                                                    "siliconflow",
                                                    "streamlake",
                                                ],
                                                "allow_fallbacks": False,
                                            }
                                        },
                                    }
                                ],
                            }
                        }
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            # The pinned providers above stream continuously (observed
            # inter-chunk gaps of a few seconds), so a genuine multi-minute
            # silence means the stream is dead. Fail the turn at ten minutes
            # instead of hanging until the benchmark's outer timeout.
            settings_path.write_text(
                json.dumps({"httpIdleTimeoutMs": 600_000}, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            yield
        finally:
            self._active_config_dir = None
            if private_root.is_symlink():
                private_root.unlink()
            elif private_root.is_dir():
                shutil.rmtree(private_root)
            elif os.path.lexists(private_root):
                raise ValueError("Pi reserved private root changed type")

    def prepare_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        env = super().prepare_env(base)
        # Version probes run outside a job context and need no private catalog.
        if self._active_config_dir is not None:
            env["PI_CODING_AGENT_DIR"] = str(self._active_config_dir)
        env["PI_TELEMETRY"] = "0"
        if not env.get("OPENROUTER_API_KEY"):
            key = _read_openrouter_key_file()
            if key is not None:
                env["OPENROUTER_API_KEY"] = key
        return env

    def build_command(self, job: AgentJob) -> list[str]:
        native_model = self._native_model(job.model.model)
        if job.model.effort != "high":
            raise ValueError("Pi model 'glm-5.2' requires thinking effort 'high'")
        return [
            self.resolve_binary(),
            "--mode",
            "text",
            "--provider",
            "openrouter",
            "--model",
            native_model,
            "--thinking",
            "high",
            "--session-dir",
            str(self._session_dir(job)),
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--approve",
            "--tools",
            "read,bash,edit,write,grep,find,ls",
            "-p",
            f"@{job.prompt_path}",
        ]

    def parse_output(self, job: AgentJob, stdout_text: str) -> ParsedOutput:
        del stdout_text  # Text mode is bounded; the native ledger is authoritative.
        session_dir = self._session_dir(job)
        if session_dir.is_symlink():
            raise ValueError("Pi session directory must not be a symlink")
        if not session_dir.is_dir():
            return ParsedOutput()
        files = sorted(session_dir.glob("*.jsonl"))
        if len(files) != 1:
            if not files:
                return ParsedOutput()
            raise ValueError("Pi usage capture must contain exactly one session")
        session_path = files[0]
        if session_path.is_symlink() or not session_path.is_file():
            raise ValueError("Pi usage capture must be a regular file")

        usage = Usage()
        assistant_messages = 0
        last_message: str | None = None
        session_id: str | None = None
        try:
            with session_path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        raise ValueError("session entry must be an object")
                    if entry.get("type") == "session":
                        native_id = entry.get("id")
                        if (
                            not isinstance(native_id, str)
                            or not native_id
                            or session_id is not None
                        ):
                            raise ValueError("session header is invalid")
                        session_id = native_id
                        continue
                    if entry.get("type") != "message":
                        continue
                    message = entry.get("message")
                    if not isinstance(message, dict) or message.get("role") != "assistant":
                        continue
                    native_usage = message.get("usage")
                    if not isinstance(native_usage, dict):
                        raise ValueError("assistant message omitted usage")
                    usage = usage.add(
                        Usage(
                            input_tokens=self._session_token(native_usage, "input"),
                            cached_input_tokens=self._session_token(native_usage, "cacheRead"),
                            cache_write_tokens=self._session_token(native_usage, "cacheWrite"),
                            output_tokens=self._session_token(native_usage, "output"),
                        )
                    )
                    assistant_messages += 1
                    content = message.get("content")
                    if isinstance(content, list):
                        text_parts = [
                            item["text"]
                            for item in content
                            if isinstance(item, dict)
                            and item.get("type") == "text"
                            and isinstance(item.get("text"), str)
                        ]
                        if text_parts:
                            last_message = "\n".join(text_parts)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Pi usage capture is unreadable") from exc
        if session_id is None or assistant_messages == 0:
            return ParsedOutput()
        return ParsedOutput(
            usage=usage,
            # Custom model catalogs cannot safely encode OpenRouter's dynamic
            # routing price. The runner resolves cost from its pinned pricing.
            reported_cost_usd=None,
            last_message=last_message,
            session_id=session_id,
        )


AGY_MODEL_ALIASES: Mapping[str, Mapping[str, str]] = {
    "gemini-3.1-pro": {
        "low": "Gemini 3.1 Pro (Low)",
        "high": "Gemini 3.1 Pro (High)",
    },
    "gemini-3.5-flash": {
        "low": "Gemini 3.5 Flash (Low)",
        "medium": "Gemini 3.5 Flash (Medium)",
        "high": "Gemini 3.5 Flash (High)",
    },
}


@register_harness
class AgyHarness(Harness):
    """Google Antigravity CLI with its native terminal sandbox enabled."""

    name = "agy"
    _COMPLETION_DIRECTIVE = (
        "\n\nWork only by reading and editing files inside the assigned workspace. "
        "Do not call artifact creation, artifact export, or artifact presentation tools. "
        "Treat the remaining context as a hard budget: inventory paths once, use targeted searches "
        "and bounded reads, never dump large files or command output, and do not reread unchanged "
        "files. Implement the required deliverable directly and reserve enough output capacity for "
        "the final response. Once the required implementation is complete, run only brief local "
        "checks and stop. "
        "When the implementation and local checks are complete, return a concise plain-text summary "
        "and exit successfully.\n"
    )
    _PRIVATE_SUFFIX = ".agy-state"
    _PROMPT_FILE = "prompt.md"
    _LAUNCHER_FILE = "launch.py"

    @classmethod
    def _private_root(cls, job: AgentJob) -> Path:
        return job.log_path.parent / f".{job.log_path.name}{cls._PRIVATE_SUFFIX}"

    @staticmethod
    def _native_model(model: str, effort: str) -> str:
        efforts = AGY_MODEL_ALIASES.get(model)
        if efforts is None:
            known = ", ".join(sorted(AGY_MODEL_ALIASES))
            raise ValueError(f"AGY model '{model}' is unsupported; configured aliases: {known}")
        try:
            return efforts[effort]
        except KeyError as exc:
            known = ", ".join(sorted(efforts))
            raise ValueError(
                f"AGY model '{model}' does not support effort '{effort}'; choose: {known}"
            ) from exc

    @contextmanager
    def execution_context(self, job: AgentJob) -> Iterator[None]:
        """Stage private control files and disposable evidence outside the submission tree."""
        state_dir = self._private_root(job)
        if state_dir.is_symlink() or state_dir.exists():
            raise ValueError("workspace contains a reserved AGY adapter path")
        state_dir.mkdir(parents=True)
        try:
            (state_dir / self._LAUNCHER_FILE).write_text(
                "import os\n"
                "import sys\n"
                "with open(sys.argv[1], encoding='utf-8') as fh:\n"
                "    prompt = fh.read()\n"
                "os.execv(sys.argv[2], [sys.argv[2], *sys.argv[3:], '-p', prompt])\n",
                encoding="utf-8",
            )
            prompt_text = job.prompt_path.read_text(encoding="utf-8")
            for index, source in enumerate(job.evidence_dirs):
                if source.is_symlink() or not source.is_dir():
                    raise ValueError("AGY evidence paths must be non-symlink directories")
                if any(path.is_symlink() for path in source.rglob("*")):
                    raise ValueError("AGY evidence trees must not contain symlinks")
                staged = state_dir / f"evidence-{index}"
                shutil.copytree(source, staged)
                prompt_text = prompt_text.replace(str(source), str(staged))
            (state_dir / self._PROMPT_FILE).write_text(
                prompt_text + self._COMPLETION_DIRECTIVE,
                encoding="utf-8",
            )
            yield
        finally:
            if state_dir.is_symlink():
                state_dir.unlink()
            elif state_dir.exists():
                shutil.rmtree(state_dir)

    def build_command(self, job: AgentJob) -> list[str]:
        native_model = self._native_model(job.model.model, job.model.effort)
        state_dir = self._private_root(job)
        native_log_path = job.log_path.with_name(f"{job.log_path.name}.agy")
        cmd = [
            sys.executable,
            str(state_dir / self._LAUNCHER_FILE),
            str(state_dir / self._PROMPT_FILE),
            self.resolve_binary(),
            "--model",
            native_model,
            "--mode",
            "accept-edits",
            "--output-format",
            "json",
            "--print-timeout",
            "24h",
            "--log-file",
            str(native_log_path),
            "--dangerously-skip-permissions",
            "--new-project",
            "--add-dir",
            str(job.workdir),
        ]
        for index in range(len(job.evidence_dirs)):
            cmd.extend(["--add-dir", str(state_dir / f"evidence-{index}")])
        if job.sandbox_mode != "danger-full-access":
            cmd.append("--sandbox")
        return cmd

    def parse_output(self, job: AgentJob, stdout_text: str) -> ParsedOutput:
        for obj in _iter_json_objects(stdout_text):
            status = obj.get("status")
            if isinstance(status, str) and status != "SUCCESS":
                error = obj.get("error")
                detail = error if isinstance(error, str) and error else status
                raise ValueError(f"AGY execution failed: {detail}")
            native_usage = obj.get("usage")
            usage = None
            if isinstance(native_usage, dict):
                usage = Usage(
                    input_tokens=_int_of(native_usage.get("input_tokens")),
                    output_tokens=_int_of(native_usage.get("output_tokens")),
                )
            response = obj.get("response")
            conversation_id = obj.get("conversation_id")
            return ParsedOutput(
                usage=usage,
                reported_cost_usd=None,
                last_message=response if isinstance(response, str) else None,
                session_id=conversation_id if isinstance(conversation_id, str) else None,
            )
        return ParsedOutput()
