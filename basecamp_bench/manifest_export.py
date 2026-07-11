"""Portable, deterministic archive export for verified benchmark runs.

The manifest module owns provenance and verification. This module performs the
post-verification capture, shareability scan, and byte-stable ZIP assembly.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from basecamp_bench.manifest import (
    _PRIVATE_ARTIFACT_ROOTS,
    DEFAULT_MAX_EXPORT_ARTIFACT_BYTES,
    DEFAULT_MAX_EXPORT_MEMBERS,
    DEFAULT_MAX_EXPORT_TOTAL_BYTES,
    _fsync_dir,
    _is_safe_relpath,
    _read_bytes_bounded,
    _scan_bytes,
    _sort_findings,
    _validate_manifest_data,
    verify_run,
)

__all__ = ["export_run"]

_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_ZIP_EXTERNAL_ATTR = 0o644 << 16
_ZIP_CREATE_SYSTEM = 3
_ZIP_COMPRESS_TYPE = zipfile.ZIP_DEFLATED
_ZIP_COMPRESSLEVEL = 9


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
