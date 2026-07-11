"""Run manifest provenance, hashing, verification, and portable export.

Standard-library only. Produces and validates ``run-manifest.json`` objects
compatible with ``schemas/run-manifest.schema.json``. Never records host
identity (username, home, hostname) or follows symlinks into untrusted trees.
"""

from __future__ import annotations

import hashlib
import json
import locale
import math
import os
import platform
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TypeGuard

from basecamp_bench.validation import is_finite_number, is_sha256_hex

__all__ = [
    "SCHEMA_VERSION",
    "STATUS_VALUES",
    "REDACTED",
    "DEFAULT_MAX_EXPORT_ARTIFACT_BYTES",
    "DEFAULT_MAX_EXPORT_TOTAL_BYTES",
    "DEFAULT_MAX_EXPORT_MEMBERS",
    "collect_environment",
    "git_provenance",
    "hash_inputs",
    "redact_config",
    "scan_secrets",
    "build_manifest",
    "write_manifest",
    "verify_run",
    "export_run",
]

SCHEMA_VERSION = "1.0"
STATUS_VALUES = frozenset({"planned", "running", "complete", "failed", "ineligible"})
ROOT_KEYS = (
    "schema_version",
    "runner",
    "run",
    "environment",
    "config",
    "inputs",
    "pricing",
    "costs",
    "tooling",
    "jobs",
    "artifacts",
    "status",
)
REQUIRED_ROOT_KEYS = frozenset(ROOT_KEYS) - {"costs"}
ALLOWED_ROOT_KEYS = frozenset(ROOT_KEYS)

REDACTED = "<redacted>"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PRIVATE_ARTIFACT_ROOTS = frozenset({"logs", "workspaces", "private", "prompts"})
_ENV_KEYS = frozenset(
    {
        "python_version",
        "python_implementation",
        "python_compiler",
        "platform_system",
        "platform_release",
        "platform_version",
        "platform_machine",
        "platform_architecture",
        "locale_language",
        "locale_encoding",
        "timezone_name",
        "timezone_offset",
    }
)
_CONFIG_KEYS = frozenset(
    {
        "mode",
        "run_root",
        "seed_root",
        "reference_root",
        "reference_manifest",
        "timeout_s",
        "full_access",
        "repetitions",
        "harnesses",
        "evaluators",
        "tracks",
        "pricing",
    }
)
_SECRET_KEY_TOKENS = frozenset({"token", "key", "secret", "password", "credential"})
_READ_CHUNK = 1024 * 1024
DEFAULT_MAX_EXPORT_ARTIFACT_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_EXPORT_TOTAL_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_EXPORT_MEMBERS = 10_000


_TEXT_ARTIFACT_SUFFIXES = frozenset(
    {
        ".css",
        ".csv",
        ".htm",
        ".html",
        ".js",
        ".json",
        ".jsonl",
        ".jsx",
        ".md",
        ".mjs",
        ".py",
        ".rb",
        ".rst",
        ".svg",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)

# Filename basenames / suffixes that commonly hold credentials.
_RISKY_BASENAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".env.test",
        ".env.staging",
        "credentials.json",
        "credentials.yml",
        "credentials.yaml",
        "secrets.json",
        "secrets.yml",
        "secrets.yaml",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "auth.json",
        "service-account.json",
        "service_account.json",
    }
)
_RISKY_NAME_RE = re.compile(
    r"(^|/)("
    r"\.env(\.[A-Za-z0-9._-]+)?|"
    r".*\.pem|"
    r".*\.p12|"
    r".*\.pfx|"
    r".*\.key|"
    r".*(^|[/_.-])(secret|secrets|credential|credentials)([/_.-]|$)"
    r")$",
    re.IGNORECASE,
)

# Content patterns: conservative, high-signal only. Matched text is never stored.
_CONTENT_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |ENCRYPTED )?PRIVATE KEY-----"),
        "private_key_block",
    ),
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "aws_access_key_id",
    ),
    (
        re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
        "github_pat",
    ),
    (
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
        "github_pat",
    ),
    (
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        "slack_token",
    ),
    (
        re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
        "api_secret_token",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|"
            r"client[_-]?secret|private[_-]?key)\b\s*[:=]\s*['\"]?([^\s'\"#]{8,})"
        ),
        "credential_assignment",
    ),
    (
        re.compile(r"(?i)\b(password|passwd|pwd)\b\s*[:=]\s*['\"]?([^\s'\"#]{4,})"),
        "password_assignment",
    ),
)

# Concrete host paths only. Placeholder-looking path components are excluded
# to avoid rejecting documentation and schemas that describe path syntax.
_CONCRETE_PATH_COMPONENT = r"(?!\.\.\.(?=[/\\\s]|$)|<|\{|\$|\*|\[)[A-Za-z0-9_.-]+"
_HOST_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?<![A-Za-z0-9_])/(?:Users|home)/{_CONCRETE_PATH_COMPONENT}"
        rf"(?:/{_CONCRETE_PATH_COMPONENT})*"
    ),
    re.compile(rf"(?<![A-Za-z0-9_])/root(?:/{_CONCRETE_PATH_COMPONENT})*"),
    re.compile(
        rf"(?<![A-Za-z0-9_])/(?:private/var/folders|var/folders|private/var/tmp|var/tmp|private/tmp|tmp)"
        rf"(?:/{_CONCRETE_PATH_COMPONENT})+"
    ),
    # Direct text and JSON-escaped Windows drive paths are both rejected.
    re.compile(r"(?<![A-Za-z0-9_])\b[A-Za-z]:[\\/]+[^\s\"'<>]+"),
    # Backslash and forward-slash UNC forms, excluding URL-style ``://``.
    re.compile(r"(?<![A-Za-z0-9_:])\\{2,}[^\\/\s\"'<>]+\\+[^\\/\s\"'<>]+"),
    re.compile(r"(?<![:/])//[A-Za-z0-9_.-]+/[A-Za-z0-9$_.-]+(?:/[^\s\"'<>]+)?"),
)


# ---------------------------------------------------------------------------
# Environment and Git provenance
# ---------------------------------------------------------------------------


def collect_environment() -> dict[str, Any]:
    """Return portable Python/platform/locale/timezone metadata only.

    Never includes username, home directory, hostname, or absolute local paths.
    Values are JSON-compatible and deterministic for a given interpreter/host
    configuration (not wall-clock).
    """
    lang, enc = locale.getlocale()
    preferred = locale.getpreferredencoding(False)
    # Prefer POSIX TZ abbreviation / offset without host identity.
    tz_name = time.tzname[0] if time.tzname else None
    tz_offset = time.strftime("%z") or None

    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_compiler": platform.python_compiler(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "platform_machine": platform.machine(),
        "platform_architecture": platform.architecture()[0] or None,
        "locale_language": lang,
        "locale_encoding": enc or preferred or None,
        "timezone_name": tz_name,
        "timezone_offset": tz_offset,
    }


def git_provenance(repo: Path) -> dict[str, Any]:
    """Return ``{commit, dirty, error}`` for *repo* without raising.

    Uses argv-form read-only Git commands only (no shell). *commit* is a full
    SHA or ``None``; *dirty* is a bool or ``None`` when undetermined; *error*
    is ``None`` or a concise sanitized message.
    """
    result: dict[str, Any] = {"commit": None, "dirty": None, "error": None}
    repo_path = Path(repo)
    try:
        repo_arg = os.fspath(repo_path)
    except (TypeError, ValueError) as exc:
        result["error"] = f"invalid repo path: {type(exc).__name__}"
        return result

    try:
        head = subprocess.run(
            ["git", "-C", repo_arg, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            shell=False,
        )
    except FileNotFoundError:
        result["error"] = "git executable not found"
        return result
    except subprocess.TimeoutExpired:
        result["error"] = "git rev-parse timed out"
        return result
    except OSError as exc:
        result["error"] = f"git rev-parse failed: {exc.strerror or type(exc).__name__}"
        return result

    if head.returncode != 0:
        result["error"] = _sanitize_git_error(head.stderr or head.stdout or "not a git repository")
        return result

    commit = (head.stdout or "").strip()
    if not commit or any(c.isspace() for c in commit):
        result["error"] = "invalid git commit output"
        return result
    result["commit"] = commit

    try:
        status = subprocess.run(
            ["git", "-C", repo_arg, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            shell=False,
        )
    except FileNotFoundError:
        result["error"] = "git executable not found"
        result["dirty"] = None
        return result
    except subprocess.TimeoutExpired:
        result["error"] = "git status timed out"
        result["dirty"] = None
        return result
    except OSError as exc:
        result["error"] = f"git status failed: {exc.strerror or type(exc).__name__}"
        result["dirty"] = None
        return result

    if status.returncode != 0:
        result["error"] = _sanitize_git_error(status.stderr or status.stdout or "git status failed")
        result["dirty"] = None
        return result

    result["dirty"] = bool((status.stdout or "").strip())
    return result


def _sanitize_git_error(message: str) -> str:
    """Collapse a git stderr blob to a short non-disclosing message."""
    text = " ".join(str(message).split())
    if not text:
        return "git command failed"
    lower = text.lower()
    if "not a git repository" in lower:
        return "not a git repository"
    if "not found" in lower and "git" in lower:
        return "git executable not found"
    # Strip absolute paths that often appear in git errors.
    text = re.sub(r"(/[^\s:]+)+", "<path>", text)
    text = re.sub(r"[A-Za-z]:\\[^\s:]+", "<path>", text)
    if len(text) > 160:
        text = text[:157] + "..."
    return text


# ---------------------------------------------------------------------------
# Input hashing
# ---------------------------------------------------------------------------


def hash_inputs(paths: Mapping[str, Path]) -> dict[str, str]:
    """Return a sorted mapping of logical input name → SHA-256 hex digest.

    Ordinary files are content-hashed. Directories use a deterministic tree
    hash over sorted relative POSIX paths, entry types, and child digests.
    Symlinks (including the root path), missing paths, and unsupported
    filesystem entries are rejected with :class:`ValueError`.
    """
    if not isinstance(paths, Mapping):
        raise TypeError("paths must be a mapping of name to Path")

    out: dict[str, str] = {}
    for name in sorted(paths.keys(), key=lambda k: str(k)):
        if not isinstance(name, str) or not name:
            raise ValueError(f"input name must be a nonempty string: {name!r}")
        raw = paths[name]
        path = Path(raw)
        out[name] = _hash_path(path)
    return out


def _hash_path(path: Path) -> str:
    if path.is_symlink():
        raise ValueError(f"symlink rejected: {path}")
    if not path.exists():
        raise ValueError(f"path does not exist: {path}")
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"cannot stat path: {path}") from exc

    if stat.S_ISLNK(mode):
        raise ValueError(f"symlink rejected: {path}")
    if stat.S_ISREG(mode):
        return _sha256_file(path)
    if stat.S_ISDIR(mode):
        return _sha256_tree(path)
    raise ValueError(f"unsupported filesystem entry: {path}")


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_bytes_bounded(path: Path, max_bytes: int) -> bytes:
    """Read at most *max_bytes*, failing if the file grows beyond the bound."""
    with path.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"file exceeds configured read limit ({max_bytes} bytes)")
    return payload


def _sha256_tree(root: Path) -> str:
    """Deterministic directory tree hash (never follows symlinks)."""
    if root.is_symlink():
        raise ValueError(f"symlink rejected: {root}")
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")

    records: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink():
            raise ValueError(f"symlink rejected: {current}")

        rel_dir = current.relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""

        # Directories and files in sorted name order for stable walk + records.
        dirnames.sort()
        filenames.sort()

        kept_dirs: list[str] = []
        for name in dirnames:
            child = current / name
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            child_rel = child_rel.replace("\\", "/")
            if child.is_symlink():
                raise ValueError(f"symlink rejected: {child}")
            try:
                mode = child.lstat().st_mode
            except OSError as exc:
                raise ValueError(f"cannot stat path: {child}") from exc
            if not stat.S_ISDIR(mode):
                raise ValueError(f"unsupported filesystem entry: {child}")
            # Directory marker recorded with empty content digest placeholder;
            # nested files contribute their own records. Tree digest covers all.
            records.append(f"dir:{child_rel}:")
            kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in filenames:
            child = current / name
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            child_rel = child_rel.replace("\\", "/")
            if child.is_symlink():
                raise ValueError(f"symlink rejected: {child}")
            try:
                mode = child.lstat().st_mode
            except OSError as exc:
                raise ValueError(f"cannot stat path: {child}") from exc
            if not stat.S_ISREG(mode):
                raise ValueError(f"unsupported filesystem entry: {child}")
            digest = _sha256_file(child)
            records.append(f"file:{child_rel}:{digest}")

    records.sort()
    payload = "\n".join(records).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Config redaction
# ---------------------------------------------------------------------------


def redact_config(data: Any, secret_keys: Sequence[str] = ()) -> dict[str, Any]:
    """Deep-copy *data* redacting secret-shaped mapping values.

    Keys match when a normalized name token is one of ``token``, ``key``,
    ``secret``, ``password``, or ``credential`` (separator/case insensitive),
    or when the key equals an entry in *secret_keys*. Structure and
    JSON-compatible values are preserved. Input is never mutated.
    """
    if not isinstance(data, Mapping):
        raise TypeError("config data must be a mapping")
    explicit = {str(k) for k in secret_keys}
    redacted = _redact_value(data, explicit)
    if not isinstance(redacted, dict):
        raise TypeError("redacted config must be a mapping")
    return redacted


def _normalize_key_tokens(name: str) -> list[str]:
    # Split camelCase / separators into lowercase tokens.
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", spaced)
    parts = re.split(r"[^A-Za-z0-9]+", spaced)
    return [p.lower() for p in parts if p]


def _is_secret_key(name: object, explicit: set[str]) -> bool:
    if not isinstance(name, str):
        return False
    if name in explicit:
        return True
    # Also match explicit keys with case/separator normalization.
    name_tokens = _normalize_key_tokens(name)
    name_norm = "_".join(name_tokens)
    for key in explicit:
        if "_".join(_normalize_key_tokens(key)) == name_norm:
            return True
    return any(tok in _SECRET_KEY_TOKENS for tok in name_tokens)


def _redact_value(value: Any, explicit: set[str]) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = key if isinstance(key, str) else str(key)
            if _is_secret_key(key, explicit):
                out[key_str] = REDACTED
            else:
                out[key_str] = _redact_value(item, explicit)
        return out
    if isinstance(value, list):
        return [_redact_value(item, explicit) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, explicit) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if value is None or isinstance(value, (bool, int, float, str)):
        # Reject non-JSON numbers (bool is int subclass — checked first).
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError("non-finite float is not JSON-compatible")
        return value
    if isinstance(value, bytes):
        raise TypeError("bytes values are not JSON-compatible in config")
    raise TypeError(f"unsupported config value type: {type(value).__name__}")


# ---------------------------------------------------------------------------
# Secret scanning
# ---------------------------------------------------------------------------


def scan_secrets(root: Path) -> list[dict[str, Any]]:
    """Scan *root* for likely secrets; return deterministic finding dicts.

    Each finding has ``path`` (POSIX relative), ``line`` (int or ``None``), and
    ``reason`` only — never matched secret content. Symlinks are reported and
    never followed. Binary content is skipped; regular files of every size are
    fully scanned. Results are sorted by path, then line (``None`` first), then
    reason.
    """
    root_path = Path(root)
    findings: list[dict[str, Any]] = []

    if root_path.is_symlink():
        findings.append({"path": ".", "line": None, "reason": "symlink_rejected"})
        return _sort_findings(findings)
    if not root_path.is_dir():
        findings.append({"path": ".", "line": None, "reason": "not_a_directory"})
        return _sort_findings(findings)

    for dirpath, dirnames, filenames in os.walk(root_path, topdown=True, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink():
            rel = _rel_posix(current, root_path)
            findings.append({"path": rel or ".", "line": None, "reason": "symlink_rejected"})
            dirnames[:] = []
            continue

        dirnames.sort()
        filenames.sort()

        # Report symlink directories without descending.
        kept: list[str] = []
        for name in dirnames:
            child = current / name
            rel = _rel_posix(child, root_path)
            if child.is_symlink():
                findings.append({"path": rel, "line": None, "reason": "symlink_rejected"})
                continue
            kept.append(name)
        dirnames[:] = kept

        for name in filenames:
            child = current / name
            rel = _rel_posix(child, root_path)
            if child.is_symlink():
                findings.append({"path": rel, "line": None, "reason": "symlink_rejected"})
                continue
            if not child.is_file():
                findings.append(
                    {
                        "path": rel,
                        "line": None,
                        "reason": "unsupported_entry",
                    }
                )
                continue
            findings.extend(_scan_file(child, rel))

    return _sort_findings(findings)


def _rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def sort_key(item: dict[str, Any]) -> tuple[str, int, int, str]:
        line = item.get("line")
        # None lines sort before numbered lines.
        line_rank = -1 if line is None else int(line)
        return (
            str(item.get("path", "")),
            0 if line is None else 1,
            line_rank,
            str(item.get("reason", "")),
        )

    return sorted(findings, key=sort_key)


def _is_risky_filename(rel_posix: str) -> bool:
    base = rel_posix.rsplit("/", 1)[-1]
    if base in _RISKY_BASENAMES:
        return True
    if _RISKY_NAME_RE.search(rel_posix):
        return True
    return False


def _scan_file(path: Path, rel: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if _is_risky_filename(rel):
        out.append({"path": rel, "line": None, "reason": "risky_filename"})

    try:
        raw = path.read_bytes()
    except OSError:
        return out

    out.extend(_scan_bytes(rel, raw, include_risky_name=False))
    return out


def _scan_bytes(
    rel: str,
    raw: bytes,
    *,
    include_risky_name: bool = True,
    enforce_shareability: bool = False,
) -> list[dict[str, Any]]:
    """Scan captured bytes completely so verification and export cannot race.

    Export already captures each declared artifact for hash verification and
    deterministic ZIP creation. Scanning that exact buffer avoids a size-based
    blind spot and ensures matches spanning any I/O chunk boundary are found.
    """
    out: list[dict[str, Any]] = []
    if include_risky_name and _is_risky_filename(rel):
        out.append({"path": rel, "line": None, "reason": "risky_filename"})

    # High-signal secret patterns are ASCII. Replacement decoding preserves
    # all ASCII runs while making non-ASCII bytes regex boundaries, so binary
    # members are scanned too and cannot hide content behind invalid UTF-8.
    scan_text = raw.decode("ascii", errors="replace")
    for line_no, line in enumerate(scan_text.splitlines(), start=1):
        out.extend(_secret_findings(rel, line, line=line_no))

    if not enforce_shareability:
        return out

    expected_text = Path(rel).suffix.lower() in _TEXT_ARTIFACT_SUFFIXES
    if b"\x00" in raw:
        if expected_text:
            out.append({"path": rel, "line": None, "reason": "undecodable_text"})
        return out
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        if expected_text:
            out.append({"path": rel, "line": None, "reason": "undecodable_text"})
        return out
    if text and _control_ratio(text) > 0.05:
        if expected_text:
            out.append({"path": rel, "line": None, "reason": "undecodable_text"})
        return out

    for line_no, line in enumerate(text.splitlines(), start=1):
        if any(pattern.search(line) for pattern in _HOST_PATH_PATTERNS):
            out.append({"path": rel, "line": line_no, "reason": "host_absolute_path"})

    if Path(rel).suffix.lower() == ".json":
        try:
            decoded = json.loads(
                text,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number: {value}")
                ),
            )
        except (json.JSONDecodeError, ValueError, RecursionError):
            out.append({"path": rel, "line": None, "reason": "malformed_json"})
            return out
        for value in _decoded_json_strings(decoded):
            out.extend(_secret_findings(rel, value, line=None))
            if any(pattern.search(value) for pattern in _HOST_PATH_PATTERNS):
                out.append({"path": rel, "line": None, "reason": "host_absolute_path"})
    return out


def _secret_findings(rel: str, text: str, *, line: int | None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for pattern, reason in _CONTENT_SECRET_PATTERNS:
        if REDACTED in text and reason in {"credential_assignment", "password_assignment"}:
            continue
        if pattern.search(text):
            findings.append({"path": rel, "line": line, "reason": reason})
    return findings


def _decoded_json_strings(value: Any) -> Iterator[str]:
    """Return decoded strings plus key/value assignments from parsed JSON."""

    def walk(item: Any) -> Iterator[str]:
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(key, str):
                    yield key
                    if isinstance(child, str):
                        yield f"{key}={child}"
                yield from walk(child)
        elif isinstance(item, list):
            for child in item:
                yield from walk(child)
        elif isinstance(item, str):
            yield item

    yield from walk(value)


def _control_ratio(text: str) -> float:
    if not text:
        return 0.0
    bad = sum(1 for c in text if ord(c) < 9 or (13 < ord(c) < 32) or ord(c) == 127)
    return bad / len(text)


# ---------------------------------------------------------------------------
# Manifest construction and serialization
# ---------------------------------------------------------------------------


def build_manifest(
    *,
    runner_version: str,
    run_id: str,
    mode: str,
    config: Mapping[str, Any],
    inputs: Mapping[str, Any],
    pricing: Mapping[str, Any],
    tooling: Sequence[Any],
    jobs: Sequence[Any],
    artifacts: Mapping[str, Any],
    status: str,
    started_at: str,
    finished_at: str | None = None,
    repo: Path | None = None,
    runner_git: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an exact-root run manifest compatible with the schema.

    Root keys are exactly those required by
    ``schemas/run-manifest.schema.json``. Config is redacted; Path and other
    non-JSON types are rejected or converted. Invalid status/hash/path values
    raise :class:`ValueError` or :class:`TypeError`.
    """
    if not isinstance(runner_version, str) or not runner_version:
        raise ValueError("runner_version must be a nonempty string")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a nonempty string")
    if not isinstance(mode, str) or not mode:
        raise ValueError("mode must be a nonempty string")
    if not isinstance(status, str) or status not in STATUS_VALUES:
        raise ValueError(f"status must be one of {sorted(STATUS_VALUES)}, got {status!r}")
    if not isinstance(started_at, str) or not started_at:
        raise ValueError("started_at must be a nonempty ISO date-time string")
    if finished_at is not None and (not isinstance(finished_at, str) or not finished_at):
        raise ValueError("finished_at must be None or a nonempty ISO date-time string")

    if not isinstance(config, Mapping):
        raise TypeError("config must be a mapping")
    if not isinstance(inputs, Mapping):
        raise TypeError("inputs must be a mapping")
    if not isinstance(pricing, Mapping):
        raise TypeError("pricing must be a mapping")
    if not isinstance(tooling, Sequence) or isinstance(tooling, (str, bytes)):
        raise TypeError("tooling must be a sequence")
    if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)):
        raise TypeError("jobs must be a sequence")
    if not isinstance(artifacts, Mapping):
        raise TypeError("artifacts must be a mapping")

    safe_inputs = _validate_hash_map(inputs, field="inputs")
    safe_artifacts = _validate_artifact_map(artifacts, field="artifacts")
    safe_config = redact_config(config)
    safe_pricing = _portable_pricing(pricing)
    safe_tooling = _jsonable(list(tooling), field="tooling")
    if not isinstance(safe_tooling, list):
        raise TypeError("tooling must serialize to a list")
    safe_jobs = _jsonable(list(jobs), field="jobs")
    if not isinstance(safe_jobs, list):
        raise TypeError("jobs must serialize to a list")

    if runner_git is not None:
        if repo is not None:
            raise ValueError("repo and runner_git are mutually exclusive")
        if set(runner_git) != {"commit", "dirty", "error"}:
            raise ValueError("runner_git must contain exactly commit, dirty, and error")
        commit = runner_git["commit"]
        dirty = runner_git["dirty"]
        error = runner_git["error"]
        if commit is not None and (
            not isinstance(commit, str)
            or re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", commit) is None
        ):
            raise ValueError("runner_git.commit must be a full lowercase Git hash or None")
        if dirty is not None and type(dirty) is not bool:
            raise TypeError("runner_git.dirty must be bool or None")
        if error is not None and (not isinstance(error, str) or not error):
            raise ValueError("runner_git.error must be a nonempty string or None")
        git = {"commit": commit, "dirty": dirty, "error": error}
    elif repo is None:
        git = {"commit": None, "dirty": None, "error": None}
    else:
        git = git_provenance(Path(repo))

    runner: dict[str, Any] = {
        "version": runner_version,
        "commit": git["commit"],
        "dirty": git["dirty"],
        "error": git["error"],
    }

    run_block: dict[str, Any] = {
        "id": run_id,
        "mode": mode,
        "started_at": started_at,
        "finished_at": finished_at,
    }

    costs = _summarize_incurred_costs(safe_jobs)

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "runner": runner,
        "run": run_block,
        "environment": collect_environment(),
        "config": safe_config,
        "inputs": safe_inputs,
        "pricing": safe_pricing,
        "costs": costs,
        "tooling": safe_tooling,
        "jobs": safe_jobs,
        "artifacts": safe_artifacts,
        "status": status,
    }

    # Exact root contract.
    if set(manifest.keys()) != ALLOWED_ROOT_KEYS:
        raise RuntimeError("internal error: manifest root keys drifted from schema")
    validation_errors = _validate_manifest_data(manifest)
    if validation_errors:
        raise ValueError("invalid run manifest:\n" + "\n".join(validation_errors))
    return manifest


def _summarize_incurred_costs(jobs: Sequence[Any]) -> dict[str, Any]:
    """Summarize known spend for calls executed by this run.

    Reevaluation manifests contain an implementation record for provenance,
    identified by their stable ``reuse prior_run=`` command preview. That
    historical cost remains attributed to the attempt but is not incurred
    again by the reevaluation run.
    """
    implementation = 0.0
    evaluation = 0.0
    unknown_job_count = 0
    for job in jobs:
        if not isinstance(job, Mapping) or job.get("skipped") is True:
            continue
        preview = job.get("command_preview")
        if (
            job.get("kind") == "implement"
            and isinstance(preview, str)
            and preview.startswith("reuse prior_run=")
        ):
            continue
        kind = job.get("kind")
        if kind not in {"implement", "evaluate"}:
            continue
        cost = job.get("cost_usd")
        if cost is None:
            unknown_job_count += 1
        elif not is_finite_number(cost) or float(cost) < 0:
            unknown_job_count += 1
        elif kind == "implement":
            implementation += float(cost)
        else:
            evaluation += float(cost)
    return {
        "known_implementation_usd": implementation,
        "known_evaluation_usd": evaluation,
        "known_total_usd": implementation + evaluation,
        "complete": unknown_job_count == 0,
        "unknown_job_count": unknown_job_count,
    }


def _portable_pricing(pricing: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize pricing provenance without leaking an absolute cache path."""
    safe = _jsonable(pricing, field="pricing")
    if not isinstance(safe, dict):
        raise TypeError("pricing must serialize to an object")
    cache_path = safe.get("cache_path")
    if isinstance(cache_path, str) and (
        cache_path.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", cache_path))
    ):
        safe["cache_path"] = Path(cache_path).name or None
    return safe


def _validate_hash_map(data: Mapping[str, Any], *, field: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in sorted(data.keys(), key=lambda k: str(k)):
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field} keys must be nonempty strings: {key!r}")
        digest = data[key]
        if not is_sha256_hex(digest):
            raise ValueError(f"{field}[{key!r}] must be a lowercase SHA-256 hex digest")
        out[key] = digest
    return out


def _validate_artifact_map(data: Mapping[str, Any], *, field: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in sorted(data.keys(), key=lambda k: str(k)):
        if not _is_safe_relpath(key):
            raise ValueError(f"{field} path is not a safe relative POSIX path: {key!r}")
        if key == "run-manifest.json":
            raise ValueError(f"{field} may not self-declare run-manifest.json")
        if key.split("/", 1)[0] in _PRIVATE_ARTIFACT_ROOTS:
            raise ValueError(f"{field} path targets a private run area: {key!r}")
        digest = data[key]
        if not is_sha256_hex(digest):
            raise ValueError(f"{field}[{key!r}] must be a lowercase SHA-256 hex digest")
        out[key] = digest
    return out


def _is_safe_relpath(key: object) -> bool:
    if not isinstance(key, str) or not key:
        return False
    if "\\" in key or key.startswith("/") or re.match(r"^[A-Za-z]:", key):
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in key):
        return False
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return True


def _jsonable(value: Any, *, field: str) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{field}: non-finite float is not JSON-compatible")
        return value
    if isinstance(value, Path):
        raise TypeError(f"{field}: Path objects are not portable JSON values")
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"{field}: mapping keys must be strings")
            out[k] = _jsonable(v, field=f"{field}.{k}")
        return out
    if isinstance(value, list):
        return [_jsonable(v, field=f"{field}[]") for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v, field=f"{field}[]") for v in value]
    raise TypeError(f"{field}: unsupported type {type(value).__name__} for JSON manifest")


def _validate_manifest_data(data: Any) -> list[str]:
    """Deeply validate the standard-library run-manifest 1.0 contract."""
    errors: list[str] = []

    def exact(value: Any, keys: set[str] | frozenset[str], path: str) -> TypeGuard[dict[str, Any]]:
        if not isinstance(value, dict):
            errors.append(f"{path} must be an object")
            return False
        for key in sorted(keys - set(value)):
            errors.append(f"{path} missing required field: {key}")
        for key in sorted(set(value) - keys):
            errors.append(f"{path} has unexpected field: {key}")
        return True

    def string(value: Any, path: str, *, nullable: bool = False) -> bool:
        if nullable and value is None:
            return True
        if not isinstance(value, str) or not value:
            errors.append(f"{path} must be a nonempty string" + (" or null" if nullable else ""))
            return False
        return True

    def identifier(value: Any, path: str, *, nullable: bool = False) -> bool:
        if nullable and value is None:
            return True
        if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
            errors.append(
                f"{path} must be a safe lowercase identifier" + (" or null" if nullable else "")
            )
            return False
        return True

    def boolean(value: Any, path: str, *, nullable: bool = False) -> bool:
        if nullable and value is None:
            return True
        if not isinstance(value, bool):
            errors.append(f"{path} must be a boolean" + (" or null" if nullable else ""))
            return False
        return True

    def integer(value: Any, path: str, minimum: int = 0, *, nullable: bool = False) -> bool:
        if nullable and value is None:
            return True
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            errors.append(
                f"{path} must be an integer >= {minimum}" + (" or null" if nullable else "")
            )
            return False
        return True

    def number(
        value: Any,
        path: str,
        minimum: float = 0.0,
        maximum: float | None = None,
        *,
        nullable: bool = False,
    ) -> bool:
        if nullable and value is None:
            return True
        try:
            finite = (
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value))
            )
        except (OverflowError, ValueError):
            finite = False
        if not finite:
            errors.append(f"{path} must be a finite number" + (" or null" if nullable else ""))
            return False
        if float(value) < minimum or (maximum is not None and float(value) > maximum):
            errors.append(
                f"{path} must be in {minimum}..{maximum if maximum is not None else 'infinity'}"
            )
            return False
        return True

    def timestamp(value: Any, path: str, *, nullable: bool = False) -> bool:
        if nullable and value is None:
            return True
        if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
            errors.append(
                f"{path} must be a UTC date-time ending in Z" + (" or null" if nullable else "")
            )
            return False
        try:
            datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError:
            errors.append(f"{path} must be a valid UTC date-time")
            return False
        return True

    def relpath(value: Any, path: str) -> bool:
        if not _is_safe_relpath(value):
            errors.append(f"{path} must be a safe relative POSIX path")
            return False
        return True

    if not isinstance(data, dict):
        errors.append("manifest must be an object")
        return errors
    for key in sorted(REQUIRED_ROOT_KEYS - set(data)):
        errors.append(f"manifest missing required field: {key}")
    for key in sorted(set(data) - ALLOWED_ROOT_KEYS):
        errors.append(f"manifest has unexpected field: {key}")
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"manifest.schema_version must be {SCHEMA_VERSION!r}")
    if data.get("status") not in STATUS_VALUES:
        errors.append("manifest.status is invalid")

    runner = data.get("runner")
    if exact(runner, {"version", "commit", "dirty", "error"}, "manifest.runner"):
        string(runner.get("version"), "manifest.runner.version")
        commit = runner.get("commit")
        if commit is not None and (
            not isinstance(commit, str)
            or re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit) is None
        ):
            errors.append("manifest.runner.commit must be a lowercase Git hash or null")
        boolean(runner.get("dirty"), "manifest.runner.dirty", nullable=True)
        string(runner.get("error"), "manifest.runner.error", nullable=True)

    run = data.get("run")
    if exact(run, {"id", "mode", "started_at", "finished_at"}, "manifest.run"):
        identifier(run.get("id"), "manifest.run.id")
        if run.get("mode") not in {"local", "publication"}:
            errors.append("manifest.run.mode must be 'local' or 'publication'")
        timestamp(run.get("started_at"), "manifest.run.started_at")
        timestamp(run.get("finished_at"), "manifest.run.finished_at", nullable=True)

    costs = data.get("costs")
    cost_keys = {
        "known_implementation_usd",
        "known_evaluation_usd",
        "known_total_usd",
        "complete",
        "unknown_job_count",
    }
    if "costs" in data and exact(costs, cost_keys, "manifest.costs"):
        number(costs.get("known_implementation_usd"), "manifest.costs.known_implementation_usd")
        number(costs.get("known_evaluation_usd"), "manifest.costs.known_evaluation_usd")
        number(costs.get("known_total_usd"), "manifest.costs.known_total_usd")
        boolean(costs.get("complete"), "manifest.costs.complete")
        integer(costs.get("unknown_job_count"), "manifest.costs.unknown_job_count")
        implementation = costs.get("known_implementation_usd")
        evaluation = costs.get("known_evaluation_usd")
        total = costs.get("known_total_usd")
        if all(is_finite_number(value) for value in (implementation, evaluation, total)):
            assert implementation is not None and evaluation is not None and total is not None
            if not math.isclose(
                float(total),
                float(implementation) + float(evaluation),
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                errors.append(
                    "manifest.costs.known_total_usd must equal implementation plus evaluation"
                )
        unknown = costs.get("unknown_job_count")
        complete = costs.get("complete")
        if (
            isinstance(unknown, int)
            and not isinstance(unknown, bool)
            and isinstance(complete, bool)
        ):
            if complete != (unknown == 0):
                errors.append("manifest.costs.complete must match unknown_job_count == 0")

    env = data.get("environment")
    if exact(env, _ENV_KEYS, "manifest.environment"):
        for key in sorted(_ENV_KEYS):
            value = env.get(key)
            if key in {"python_version", "python_implementation", "platform_system"}:
                string(value, f"manifest.environment.{key}")
            elif value is not None and not isinstance(value, str):
                errors.append(f"manifest.environment.{key} must be a string or null")

    config = data.get("config")
    if exact(config, _CONFIG_KEYS, "manifest.config"):
        if config.get("mode") not in {"local", "publication"}:
            errors.append("manifest.config.mode must be 'local' or 'publication'")
        for key in ("run_root", "seed_root", "reference_root", "reference_manifest"):
            relpath(config.get(key), f"manifest.config.{key}")
        integer(config.get("timeout_s"), "manifest.config.timeout_s", 1)
        boolean(config.get("full_access"), "manifest.config.full_access")
        integer(config.get("repetitions"), "manifest.config.repetitions", 1)
        harnesses = config.get("harnesses")
        if not isinstance(harnesses, dict) or not harnesses:
            errors.append("manifest.config.harnesses must be a nonempty object")
        else:
            hkeys = {"adapter", "model", "effort", "provider_family", "display_name", "enabled"}
            for hid, spec in sorted(harnesses.items(), key=lambda item: str(item[0])):
                identifier(hid, f"manifest.config.harnesses[{hid!r}] id")
                if exact(spec, hkeys, f"manifest.config.harnesses[{hid!r}]"):
                    for key in hkeys - {"enabled"}:
                        string(spec.get(key), f"manifest.config.harnesses[{hid!r}].{key}")
                    boolean(spec.get("enabled"), f"manifest.config.harnesses[{hid!r}].enabled")
        evaluators = config.get("evaluators")
        if not isinstance(evaluators, list) or not evaluators:
            errors.append("manifest.config.evaluators must be a nonempty array")
        else:
            ekeys = {"id", "harness", "model", "effort", "provider_family", "enabled"}
            seen: set[str] = set()
            for index, spec in enumerate(evaluators):
                path = f"manifest.config.evaluators[{index}]"
                if exact(spec, ekeys, path):
                    identifier(spec.get("id"), f"{path}.id")
                    identifier(spec.get("harness"), f"{path}.harness")
                    if (
                        isinstance(spec.get("harness"), str)
                        and isinstance(harnesses, dict)
                        and spec["harness"] not in harnesses
                    ):
                        errors.append(f"{path}.harness references an unknown harness")
                    for key in ("model", "effort", "provider_family"):
                        string(spec.get(key), f"{path}.{key}")
                    boolean(spec.get("enabled"), f"{path}.enabled")
                    evaluator_id = spec.get("id")
                    if isinstance(evaluator_id, str):
                        if evaluator_id in seen:
                            errors.append(f"{path}.id is duplicated")
                        seen.add(evaluator_id)
        tracks = config.get("tracks")
        if not isinstance(tracks, dict) or not tracks:
            errors.append("manifest.config.tracks must be a nonempty object")
        else:
            for tid, spec in sorted(tracks.items(), key=lambda item: str(item[0])):
                identifier(tid, f"manifest.config.tracks[{tid!r}] id")
                if exact(
                    spec, {"prompt", "rubric", "contract"}, f"manifest.config.tracks[{tid!r}]"
                ):
                    for key in ("prompt", "rubric", "contract"):
                        relpath(spec.get(key), f"manifest.config.tracks[{tid!r}].{key}")
        pricing_overrides = config.get("pricing")
        if not isinstance(pricing_overrides, dict):
            errors.append("manifest.config.pricing must be an object")
        else:
            for mid, rates in sorted(pricing_overrides.items(), key=lambda item: str(item[0])):
                identifier(mid, f"manifest.config.pricing[{mid!r}] id")
                if exact(
                    rates,
                    {"input", "output", "cache_read", "cache_write"},
                    f"manifest.config.pricing[{mid!r}]",
                ):
                    for key in ("input", "output", "cache_read", "cache_write"):
                        number(rates.get(key), f"manifest.config.pricing[{mid!r}].{key}")

    inputs = data.get("inputs")
    if not isinstance(inputs, dict):
        errors.append("manifest.inputs must be an object")
    else:
        for key, digest in inputs.items():
            string(key, "manifest.inputs key")
            if not is_sha256_hex(digest):
                errors.append(f"manifest.inputs[{key!r}] must be a lowercase SHA-256 digest")

    pricing = data.get("pricing")
    public_pricing_keys = {
        "source",
        "retrieved_at",
        "stale",
        "error",
        "complete",
        "limitations",
        "url",
        "cache_path",
    }
    if isinstance(pricing, dict) and set(pricing) == {"error"}:
        string(pricing.get("error"), "manifest.pricing.error")
    elif exact(pricing, public_pricing_keys, "manifest.pricing"):
        for key in ("source", "error", "url", "cache_path"):
            string(pricing.get(key), f"manifest.pricing.{key}", nullable=True)
        timestamp(pricing.get("retrieved_at"), "manifest.pricing.retrieved_at", nullable=True)
        boolean(pricing.get("stale"), "manifest.pricing.stale")
        boolean(pricing.get("complete"), "manifest.pricing.complete")
        limitations = pricing.get("limitations")
        if not isinstance(limitations, list) or any(
            not isinstance(item, str) or not item for item in limitations
        ):
            errors.append("manifest.pricing.limitations must be an array of nonempty strings")
        cache_path = pricing.get("cache_path")
        if isinstance(cache_path, str):
            relpath(cache_path, "manifest.pricing.cache_path")

    tooling = data.get("tooling")
    tooling_keys = {
        "role",
        "config_id",
        "evaluator_id",
        "adapter",
        "model_id",
        "provider_family",
        "effort",
        "executable_version",
        "version_error",
        "adapter_version",
        "runner_version",
        "deterministic_seed",
    }
    if not isinstance(tooling, list) or not tooling:
        errors.append("manifest.tooling must be a nonempty array")
    else:
        seen_tooling: set[tuple[Any, ...]] = set()
        for index, record in enumerate(tooling):
            path = f"manifest.tooling[{index}]"
            if not exact(record, tooling_keys, path):
                continue
            role = record.get("role")
            if role not in {"implementation", "evaluator"}:
                errors.append(f"{path}.role is invalid")
            identifier(record.get("config_id"), f"{path}.config_id")
            evaluator_id = record.get("evaluator_id")
            if role == "implementation":
                if evaluator_id is not None:
                    errors.append(f"{path}.evaluator_id must be null for implementation")
            elif role == "evaluator":
                identifier(evaluator_id, f"{path}.evaluator_id")
            for key in (
                "adapter",
                "model_id",
                "provider_family",
                "effort",
                "adapter_version",
                "runner_version",
            ):
                string(record.get(key), f"{path}.{key}")
            version, version_error = record.get("executable_version"), record.get("version_error")
            string(version, f"{path}.executable_version", nullable=True)
            string(version_error, f"{path}.version_error", nullable=True)
            if (version is None) == (version_error is None):
                errors.append(
                    f"{path} must contain exactly one of executable_version or version_error"
                )
            seed = record.get("deterministic_seed")
            if exact(seed, {"supported", "limitation"}, f"{path}.deterministic_seed"):
                if seed.get("supported") is not False:
                    errors.append(f"{path}.deterministic_seed.supported must be false")
                string(seed.get("limitation"), f"{path}.deterministic_seed.limitation")
            identity = (role, record.get("config_id"), evaluator_id, record.get("model_id"))
            if identity in seen_tooling:
                errors.append(f"{path} duplicates a tooling role")
            seen_tooling.add(identity)

    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        errors.append("manifest.jobs must be an array")
    else:
        seen_job_ids: set[str] = set()
        for index, job in enumerate(jobs):
            _validate_job(job, index, errors, exact, identifier, string, boolean, integer, number)
            if isinstance(job, dict) and isinstance(job.get("id"), str):
                if job["id"] in seen_job_ids:
                    errors.append(f"manifest.jobs[{index}].id is duplicated")
                seen_job_ids.add(job["id"])
        if isinstance(costs, dict) and costs != _summarize_incurred_costs(jobs):
            errors.append("manifest.costs must match incurred job costs")

    artifacts = data.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("manifest.artifacts must be an object")
    else:
        for rel, digest in artifacts.items():
            if not _is_safe_relpath(rel):
                errors.append(f"manifest.artifacts has invalid artifact path: {rel!r}")
            elif rel == "run-manifest.json":
                errors.append("manifest.artifacts may not self-declare run-manifest.json")
            elif rel.split("/", 1)[0] in _PRIVATE_ARTIFACT_ROOTS:
                errors.append(f"manifest.artifacts targets private run area: {rel!r}")
            if not is_sha256_hex(digest):
                errors.append(f"manifest.artifacts[{rel!r}] must be a lowercase SHA-256 digest")
    if isinstance(run, dict) and isinstance(config, dict) and run.get("mode") != config.get("mode"):
        errors.append("manifest.run.mode must match manifest.config.mode")
    return sorted(set(errors))


def _validate_job(
    job: Any,
    index: int,
    errors: list[str],
    exact: Any,
    identifier: Any,
    string: Any,
    boolean: Any,
    integer: Any,
    number: Any,
) -> None:
    path = f"manifest.jobs[{index}]"
    if not isinstance(job, dict):
        errors.append(f"{path} must be an object")
        return
    skip_keys = {
        "id",
        "kind",
        "harness",
        "track",
        "repetition",
        "submission_id",
        "evaluator_id",
        "skipped",
        "reason",
    }
    base_keys = {
        "id",
        "kind",
        "harness",
        "track",
        "repetition",
        "submission_id",
        "command_preview",
        "returncode",
        "duration_s",
        "timed_out",
        "interrupted",
        "error",
        "cost_usd",
        "reported_cost_usd",
        "usage",
    }
    eval_keys = base_keys | {"evaluator_id", "eval_attempt_id", "valid", "invalid_reasons"}
    keys = set(job)
    expected = (
        skip_keys
        if keys == skip_keys
        else eval_keys
        if keys & {"evaluator_id", "eval_attempt_id", "valid", "invalid_reasons"}
        else base_keys
    )
    exact(job, expected, path)
    identifier(job.get("id"), f"{path}.id")
    if job.get("kind") not in {"implement", "evaluate"}:
        errors.append(f"{path}.kind must be 'implement' or 'evaluate'")
    identifier(job.get("harness"), f"{path}.harness")
    if job.get("track") not in {"fe", "be"}:
        errors.append(f"{path}.track must be 'fe' or 'be'")
    integer(job.get("repetition"), f"{path}.repetition", 1)
    identifier(job.get("submission_id"), f"{path}.submission_id", nullable=True)
    if expected == skip_keys:
        identifier(job.get("evaluator_id"), f"{path}.evaluator_id")
        if job.get("skipped") is not True:
            errors.append(f"{path}.skipped must be true")
        string(job.get("reason"), f"{path}.reason")
        return
    string(job.get("command_preview"), f"{path}.command_preview")
    integer(job.get("returncode"), f"{path}.returncode", -2147483648, nullable=True)
    number(job.get("duration_s"), f"{path}.duration_s")
    boolean(job.get("timed_out"), f"{path}.timed_out")
    boolean(job.get("interrupted"), f"{path}.interrupted")
    string(job.get("error"), f"{path}.error", nullable=True)
    number(job.get("cost_usd"), f"{path}.cost_usd", nullable=True)
    number(job.get("reported_cost_usd"), f"{path}.reported_cost_usd", nullable=True)
    usage = job.get("usage")
    if usage is not None:
        ukeys = {"input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens"}
        if exact(usage, ukeys, f"{path}.usage"):
            for key in ukeys:
                integer(usage.get(key), f"{path}.usage.{key}")
    if expected == eval_keys:
        identifier(job.get("evaluator_id"), f"{path}.evaluator_id")
        identifier(job.get("eval_attempt_id"), f"{path}.eval_attempt_id")
        boolean(job.get("valid"), f"{path}.valid")
        reasons = job.get("invalid_reasons")
        if not isinstance(reasons, list) or any(
            not isinstance(item, str) or not item for item in reasons
        ):
            errors.append(f"{path}.invalid_reasons must be an array of nonempty strings")


def write_manifest(path: Path, data: Any) -> None:
    """Atomically write *data* as deterministic UTF-8 JSON.

    Uses a same-directory tempfile and :func:`os.replace` (no ``mv``).
    Serialization uses sorted keys, two-space indentation, and a trailing
    newline. Refuses symlink destinations and symlink parents.
    """
    target = Path(path)
    parent = target.parent

    if parent.is_symlink():
        raise ValueError(f"manifest parent must not be a symlink: {parent}")
    if not parent.is_dir():
        raise ValueError(f"manifest parent is not a directory: {parent}")
    if target.is_symlink():
        raise ValueError(f"manifest destination must not be a symlink: {target}")
    if target.exists() and not target.is_file():
        raise ValueError(f"manifest destination is not a regular file: {target}")

    payload = (
        json.dumps(
            data,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=os.fspath(parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, os.fspath(target))
        tmp_path = Path()  # replaced
        _fsync_dir(parent)
    except Exception:
        if tmp_path != Path() and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(os.fspath(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Verification and export
# ---------------------------------------------------------------------------


def verify_run(
    run_dir: Path,
    *,
    max_artifact_bytes: int | None = None,
    max_total_bytes: int | None = None,
    max_members: int | None = None,
) -> list[str]:
    """Validate a run directory's manifest and declared artifacts.

    Never raises for ordinary invalid run contents; returns a sorted list of
    human-readable errors. Unrelated undeclared files are not rejected.
    """
    if max_artifact_bytes is not None and (
        isinstance(max_artifact_bytes, bool)
        or not isinstance(max_artifact_bytes, int)
        or max_artifact_bytes < 1
    ):
        return ["max_artifact_bytes must be a positive integer or None"]
    if max_total_bytes is not None and (
        isinstance(max_total_bytes, bool)
        or not isinstance(max_total_bytes, int)
        or max_total_bytes < 1
    ):
        return ["max_total_bytes must be a positive integer or None"]
    if max_members is not None and (
        isinstance(max_members, bool) or not isinstance(max_members, int) or max_members < 1
    ):
        return ["max_members must be a positive integer or None"]

    errors: list[str] = []
    root = Path(run_dir)

    if root.is_symlink():
        return sorted(["run directory must not be a symlink"])
    if not root.is_dir():
        return sorted(["run directory does not exist or is not a directory"])

    manifest_path = root / "run-manifest.json"
    if manifest_path.is_symlink():
        return sorted(["run-manifest.json must not be a symlink"])
    if not manifest_path.is_file():
        return sorted(["missing run-manifest.json"])
    if max_total_bytes is not None:
        try:
            if manifest_path.lstat().st_size > max_total_bytes:
                return sorted(
                    [f"export bytes exceed configured total limit ({max_total_bytes} bytes)"]
                )
        except OSError as exc:
            return sorted([f"cannot stat run-manifest.json: {exc.strerror or type(exc).__name__}"])

    try:
        manifest_bytes = (
            _read_bytes_bounded(manifest_path, max_total_bytes)
            if max_total_bytes is not None
            else manifest_path.read_bytes()
        )
        raw = manifest_bytes.decode("utf-8")
        data = json.loads(
            raw,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except UnicodeDecodeError:
        return sorted(["run-manifest.json is not valid UTF-8"])
    except json.JSONDecodeError as exc:
        return sorted([f"run-manifest.json is malformed JSON: {exc.msg}"])
    except ValueError as exc:
        return sorted([f"run-manifest.json is invalid JSON: {exc}"])
    except OSError as exc:
        return sorted([f"cannot read run-manifest.json: {exc.strerror or type(exc).__name__}"])

    if not isinstance(data, dict):
        return sorted(["run-manifest.json root must be a JSON object"])

    errors.extend(_validate_manifest_data(data))

    artifacts = data.get("artifacts")
    if "artifacts" not in data:
        return sorted(errors)
    if not isinstance(artifacts, dict):
        errors.append("artifacts must be a mapping")
        return sorted(errors)

    if max_members is not None and 1 + len(artifacts) > max_members:
        errors.append(f"export member count exceeds configured limit ({max_members})")
    try:
        total_bytes = manifest_path.lstat().st_size
    except OSError:
        total_bytes = 0
    if max_total_bytes is not None and total_bytes > max_total_bytes:
        errors.append(f"export bytes exceed configured total limit ({max_total_bytes} bytes)")

    for rel, digest in artifacts.items():
        if not _is_safe_relpath(rel):
            errors.append(f"invalid artifact path: {rel!r}")
            continue
        if rel.split("/", 1)[0] in _PRIVATE_ARTIFACT_ROOTS:
            errors.append(f"artifact path targets private run area: {rel!r}")
            continue
        if not is_sha256_hex(digest):
            errors.append(f"artifacts[{rel!r}] must be a lowercase SHA-256 hex digest")
            continue
        artifact_path = root.joinpath(*rel.split("/"))
        try:
            size = artifact_path.lstat().st_size
        except OSError:
            size = 0
        total_bytes += size
        if max_total_bytes is not None and total_bytes > max_total_bytes:
            errors.append(f"export bytes exceed configured total limit ({max_total_bytes} bytes)")
            continue
        errors.extend(
            _verify_artifact_file(
                root,
                rel,
                digest,
                max_artifact_bytes=max_artifact_bytes,
            )
        )

    return sorted(set(errors))


def _verify_artifact_file(
    run_dir: Path,
    rel: str,
    expected: str,
    *,
    max_artifact_bytes: int | None = None,
) -> list[str]:
    errors: list[str] = []
    # Walk components without following symlinks.
    current = run_dir
    if current.is_symlink():
        return [f"artifact path has symlink component: {rel}"]

    parts = rel.split("/")
    for i, part in enumerate(parts):
        current = current / part
        try:
            st = current.lstat()
        except FileNotFoundError:
            return [f"missing artifact: {rel}"]
        except OSError as exc:
            return [f"cannot stat artifact {rel}: {exc.strerror or type(exc).__name__}"]
        if stat.S_ISLNK(st.st_mode):
            return [f"artifact path has symlink component: {rel}"]
        if i < len(parts) - 1:
            if not stat.S_ISDIR(st.st_mode):
                return [f"artifact path component is not a directory: {rel}"]
        else:
            if not stat.S_ISREG(st.st_mode):
                return [f"artifact is not a regular file: {rel}"]
            if max_artifact_bytes is not None and st.st_size > max_artifact_bytes:
                return [
                    f"artifact exceeds configured size limit ({max_artifact_bytes} bytes): {rel}"
                ]

    # Containment: pure join of safe relative parts under run_dir is sufficient
    # when no symlinks and no .. segments; also check resolve(strict) when possible.
    try:
        resolved = current.resolve(strict=True)
        root_resolved = run_dir.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except FileNotFoundError:
        return [f"missing artifact: {rel}"]
    except ValueError:
        return [f"artifact path escapes run directory: {rel}"]
    except OSError as exc:
        return [f"cannot resolve artifact {rel}: {exc.strerror or type(exc).__name__}"]

    try:
        actual = _sha256_file(current)
    except ValueError as exc:
        return [f"artifact hash failed for {rel}: {exc}"]
    except OSError as exc:
        return [f"cannot read artifact {rel}: {exc.strerror or type(exc).__name__}"]

    if actual != expected:
        errors.append(f"hash mismatch: {rel}")
    return errors


def export_run(
    run_dir: Path,
    output_zip: Path,
    *,
    max_artifact_bytes: int = DEFAULT_MAX_EXPORT_ARTIFACT_BYTES,
    max_total_bytes: int = DEFAULT_MAX_EXPORT_TOTAL_BYTES,
    max_members: int = DEFAULT_MAX_EXPORT_MEMBERS,
) -> Path:
    """Verify and export a run through the dedicated archive boundary.

    The lazy import keeps :mod:`basecamp_bench.manifest_export` independently
    importable while preserving this module's established public API.
    """
    from basecamp_bench.manifest_export import export_run as export_verified_run

    return export_verified_run(
        run_dir,
        output_zip,
        max_artifact_bytes=max_artifact_bytes,
        max_total_bytes=max_total_bytes,
        max_members=max_members,
    )
