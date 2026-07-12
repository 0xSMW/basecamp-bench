"""Publication verifier and portable exporter (acyclic: depends on manifest).

Owns on-disk strict verification, shareability/secret scanning, and
deterministic ZIP export. Ordinary local evaluation/reporting must not import
this module; stable facades on :mod:`basecamp_bench.manifest` lazy-load it.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from basecamp_bench.manifest import (
    _PRIVATE_ARTIFACT_ROOTS,
    DEFAULT_MAX_EXPORT_ARTIFACT_BYTES,
    DEFAULT_MAX_EXPORT_MEMBERS,
    DEFAULT_MAX_EXPORT_TOTAL_BYTES,
    REDACTED,
    _fsync_dir,
    _is_safe_relpath,
    _read_bytes_bounded,
    _sha256_file,
    _validate_manifest_data,
)
from basecamp_bench.validation import is_sha256_hex

__all__ = ["verify_run", "export_run", "scan_secrets"]

_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_ZIP_EXTERNAL_ATTR = 0o644 << 16
_ZIP_CREATE_SYSTEM = 3
_ZIP_COMPRESS_TYPE = zipfile.ZIP_DEFLATED
_ZIP_COMPRESSLEVEL = 9

# ---------------------------------------------------------------------------
# Shareability / secret scanning constants
# ---------------------------------------------------------------------------

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
    re.compile(
        r"(?<![A-Za-z0-9_:])\\{2,}[A-Za-z0-9$_.-]+\\+[A-Za-z0-9$_.-]+"
        r"(?:\\+[^\\/\s\"'<>]+)?"
    ),
    re.compile(
        r"(?<![:/])//(?!Applications/)[A-Za-z0-9_.-]+/[A-Za-z0-9$_.-]+"
        r"(?:/[^\s\"'<>]+)?"
    ),
)

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
        match = pattern.search(text)
        if match is None:
            continue
        if reason in {"credential_assignment", "password_assignment"}:
            assigned = match.group(2)
            if re.fullmatch(
                r"(?:\{[A-Za-z0-9_.-]+\}|<[^<>]+>|\$\{[A-Za-z0-9_.-]+\})",
                assigned,
            ):
                continue
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

    from basecamp_bench.layout import verify_readable_layout

    errors.extend(verify_readable_layout(root, data))

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
    """Verify, secret-scan, and export a portable deterministic ZIP.

    Includes only ``run-manifest.json`` and manifest-declared artifacts.
    Artifacts above *max_artifact_bytes*, archives above *max_total_bytes*, or
    archives above *max_members* fail closed before capture. Defaults are 256
    MiB per artifact, 256 MiB total, and 10,000 members. Temporary files are
    never created inside *run_dir*. Returns *output_zip*.
    """
    root = Path(run_dir)
    dest = Path(output_zip)

    if (
        isinstance(max_artifact_bytes, bool)
        or not isinstance(max_artifact_bytes, int)
        or max_artifact_bytes < 1
    ):
        raise ValueError("max_artifact_bytes must be a positive integer")
    if (
        isinstance(max_total_bytes, bool)
        or not isinstance(max_total_bytes, int)
        or max_total_bytes < 1
    ):
        raise ValueError("max_total_bytes must be a positive integer")
    if isinstance(max_members, bool) or not isinstance(max_members, int) or max_members < 1:
        raise ValueError("max_members must be a positive integer")

    errors = verify_run(
        root,
        max_artifact_bytes=max_artifact_bytes,
        max_total_bytes=max_total_bytes,
        max_members=max_members,
    )
    if errors:
        raise ValueError("run verification failed:\n" + "\n".join(errors))

    if dest.is_symlink() or dest.parent.is_symlink():
        raise ValueError(f"output zip path must not involve symlinks: {dest}")
    if os.path.lexists(dest):
        raise ValueError(f"output zip already exists: {dest}")
    if not dest.parent.is_dir():
        raise ValueError(f"output zip parent is not a directory: {dest.parent}")

    manifest_path = root / "run-manifest.json"
    try:
        if manifest_path.stat().st_size > max_total_bytes:
            raise ValueError(
                f"export bytes exceed configured total limit ({max_total_bytes} bytes)"
            )
        manifest_bytes = _read_bytes_bounded(manifest_path, max_total_bytes)
        data = json.loads(manifest_bytes.decode("utf-8"))
    except ValueError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot reload run-manifest.json: {exc}") from exc
    validation_errors = _validate_manifest_data(data)
    if validation_errors:
        raise ValueError("manifest changed after verification:\n" + "\n".join(validation_errors))

    artifacts = data.get("artifacts") if isinstance(data, dict) else None
    if not isinstance(artifacts, dict):
        raise ValueError("artifacts missing from verified manifest")
    if 1 + len(artifacts) > max_members:
        raise ValueError(f"export member count exceeds configured limit ({max_members})")
    total_bytes = len(manifest_bytes)
    if total_bytes > max_total_bytes:
        raise ValueError(f"export bytes exceed configured total limit ({max_total_bytes} bytes)")

    # Collect (archive_name, bytes) with stable ordering.
    members: list[tuple[str, bytes]] = [("run-manifest.json", manifest_bytes)]

    for rel in sorted(artifacts.keys(), key=lambda k: str(k)):
        if not _is_safe_relpath(rel):
            raise ValueError(f"unsafe artifact path in export: {rel!r}")
        if rel.split("/", 1)[0] in _PRIVATE_ARTIFACT_ROOTS:
            raise ValueError(f"private artifact path in export: {rel!r}")
        file_path = root.joinpath(*rel.split("/"))
        if file_path.is_symlink() or not file_path.is_file():
            raise ValueError(f"artifact missing or not a regular file: {rel}")
        try:
            declared_size = file_path.stat().st_size
            if declared_size > max_artifact_bytes:
                raise ValueError(
                    f"artifact exceeds configured size limit ({max_artifact_bytes} bytes): {rel}"
                )
            if total_bytes + declared_size > max_total_bytes:
                raise ValueError(
                    f"export bytes exceed configured total limit ({max_total_bytes} bytes)"
                )
            capture_limit = min(max_artifact_bytes, max_total_bytes - total_bytes)
            payload = _read_bytes_bounded(file_path, capture_limit)
        except OSError as exc:
            raise ValueError(f"cannot read artifact {rel}: {exc}") from exc
        except ValueError as exc:
            raise ValueError(f"artifact exceeds configured capture limit: {rel}") from exc
        if len(payload) > max_artifact_bytes:
            raise ValueError(
                f"artifact exceeds configured size limit ({max_artifact_bytes} bytes): {rel}"
            )
        total_bytes += len(payload)
        if total_bytes > max_total_bytes:
            raise ValueError(
                f"export bytes exceed configured total limit ({max_total_bytes} bytes)"
            )
        if hashlib.sha256(payload).hexdigest() != artifacts[rel]:
            raise ValueError(f"artifact changed after verification: {rel}")
        members.append((rel, payload))

    members.sort(key=lambda item: item[0])
    findings: list[dict[str, Any]] = []
    for name, payload in members:
        findings.extend(_scan_bytes(name, payload, enforce_shareability=True))
    findings = _sort_findings(findings)
    if findings:
        summary = ", ".join(f"{f['path']}:{f['reason']}" for f in findings[:10])
        more = "" if len(findings) <= 10 else f" (+{len(findings) - 10} more)"
        raise ValueError(f"shareability/secret scan failed: {summary}{more}")

    # Write ZIP to a temp file beside the destination, then os.replace.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.",
        suffix=".tmp.zip",
        dir=os.fspath(dest.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        _write_deterministic_zip(tmp_path, members)
        os.replace(os.fspath(tmp_path), os.fspath(dest))
        tmp_path = Path()
        _fsync_dir(dest.parent)
    except Exception:
        if tmp_path != Path() and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise

    return dest


def _write_deterministic_zip(path: Path, members: Sequence[tuple[str, bytes]]) -> None:
    """Write a byte-stable ZIP archive to *path*."""
    # Build in memory then write once so file metadata is fully controlled.
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=_ZIP_COMPRESS_TYPE,
        compresslevel=_ZIP_COMPRESSLEVEL,
        allowZip64=False,
    ) as zf:
        for name, payload in members:
            info = zipfile.ZipInfo(filename=name, date_time=_ZIP_DATE_TIME)
            info.compress_type = _ZIP_COMPRESS_TYPE
            info.create_system = _ZIP_CREATE_SYSTEM
            info.external_attr = _ZIP_EXTERNAL_ATTR
            info.internal_attr = 0
            info.comment = b""
            info.extra = b""
            # Flag bit 11 (UTF-8 names) is fine for ASCII/UTF-8 paths; leave default.
            zf.writestr(
                info, payload, compress_type=_ZIP_COMPRESS_TYPE, compresslevel=_ZIP_COMPRESSLEVEL
            )

    data = buffer.getvalue()
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
