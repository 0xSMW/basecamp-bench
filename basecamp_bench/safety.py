"""Filesystem and text safety helpers for Basecamp Bench.

All helpers fail closed on ambiguous or unsafe state and use only the
Python standard library.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

# Portable directory/file identifier: ASCII alnum first, then alnum / . / _ / -.
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Documented maximum length for validate_identifier.
_MAX_IDENTIFIER_LEN = 64


def validate_identifier(value: object, field: str = "identifier") -> str:
    """Return *value* unchanged if it is a safe portable identifier.

    Rejects non-strings (including bool), empty/overlong values (max 64),
    absolute paths, separators, ``..``, control characters, and strings that
    do not match :data:`IDENTIFIER_RE`.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"{field} must be a string, got {type(value).__name__}")
    if not value:
        raise ValueError(f"{field} must not be empty")
    if len(value) > _MAX_IDENTIFIER_LEN:
        raise ValueError(
            f"{field} must be at most {_MAX_IDENTIFIER_LEN} characters, got {len(value)}"
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{field} contains ASCII control characters")
    if "/" in value or "\\" in value:
        raise ValueError(f"{field} must not contain path separators: {value!r}")
    if ".." in value:
        raise ValueError(f"{field} must not contain '..': {value!r}")
    # POSIX absolute (leading / already caught) and Windows drive-absolute.
    if value.startswith("/") or value.startswith("\\"):
        raise ValueError(f"{field} must not be an absolute path: {value!r}")
    if len(value) >= 2 and value[0].isalpha() and value[1] == ":":
        raise ValueError(f"{field} must not be a Windows absolute path: {value!r}")
    if IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{field} is not a valid identifier: {value!r}")
    return value


def resolve_within(root: os.PathLike[str] | str, *parts: os.PathLike[str] | str) -> Path:
    """Resolve *parts* under *root*, rejecting any path outside *root*.

    Normalizes ``..`` and symlink components via :meth:`Path.resolve` even when
    the leaf does not yet exist. Containment is checked with
    :meth:`Path.relative_to`, not string-prefix comparison.
    """
    root_resolved = Path(root).resolve()
    if not root_resolved.is_dir():
        raise ValueError(f"root is not a directory: {root_resolved}")
    candidate = root_resolved.joinpath(*parts)
    # strict=False so missing leaves still normalize traversal segments.
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes root {root_resolved}: {resolved}") from exc
    return resolved


def create_unique_directory(path: os.PathLike[str] | str) -> Path:
    """Atomically create exactly *path*; parents must already exist.

    Refuses an existing path rather than reusing it. Returns the created path.
    """
    target = Path(path)
    try:
        os.mkdir(target)
    except FileExistsError as exc:
        raise ValueError(f"directory already exists: {target}") from exc
    except FileNotFoundError as exc:
        raise ValueError(f"parent directory does not exist: {target.parent}") from exc
    return target


def sha256_file(path: os.PathLike[str] | str) -> str:
    """Stream a regular file and return its lowercase SHA-256 hex digest."""
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"not a regular file: {target}")
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _as_posix_rel(path: str) -> str:
    return path.replace("\\", "/")


def _is_ignored(rel_posix: str, patterns: Iterable[str]) -> bool:
    """Return True if *rel_posix* matches any shell-style *patterns*."""
    for pattern in patterns:
        if fnmatch.fnmatch(rel_posix, pattern):
            return True
    return False


def _reject_if_outside(path: Path, root: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path resolves outside root {root}: {path}") from exc


def tree_manifest(
    root: os.PathLike[str] | str,
    ignore: Iterable[str] = (),
) -> dict[str, str]:
    """Build a sorted POSIX-relative path → SHA-256 map for files under *root*.

    Never follows symlinks; rejects every symlink. Paths that resolve outside
    *root* are rejected. *ignore* entries are shell-style relative POSIX
    patterns (``fnmatch``); ignored directories prune their subtrees.
    Directories themselves are not included in the result.
    """
    original_root = Path(root)
    if original_root.is_symlink():
        raise ValueError(f"root must be a real directory: {original_root}")
    root_path = original_root.resolve()
    if not root_path.is_dir():
        raise ValueError(f"root must be a real directory: {root_path}")
    patterns = tuple(ignore)
    entries: dict[str, str] = {}

    for dirpath, dirnames, filenames in os.walk(root_path, topdown=True, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink():
            raise ValueError(f"symlink rejected: {current}")
        _reject_if_outside(current, root_path)

        rel_dir = current.relative_to(root_path).as_posix()
        if rel_dir == ".":
            rel_dir = ""

        # Reject symlink directories before ignore pruning; never follow them.
        kept: list[str] = []
        for name in sorted(dirnames):
            child_path = current / name
            if child_path.is_symlink():
                raise ValueError(f"symlink rejected: {child_path}")
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            child_rel = _as_posix_rel(child_rel)
            if _is_ignored(child_rel, patterns):
                continue
            _reject_if_outside(child_path, root_path)
            kept.append(name)
        dirnames[:] = kept

        for name in filenames:
            file_path = current / name
            if file_path.is_symlink():
                raise ValueError(f"symlink rejected: {file_path}")
            file_rel = f"{rel_dir}/{name}" if rel_dir else name
            file_rel = _as_posix_rel(file_rel)
            if _is_ignored(file_rel, patterns):
                continue
            if not file_path.is_file():
                raise ValueError(f"not a regular file: {file_path}")
            _reject_if_outside(file_path, root_path)
            entries[file_rel] = sha256_file(file_path)

    return {key: entries[key] for key in sorted(entries)}


def _is_safe_manifest_key(key: object) -> bool:
    if not isinstance(key, str) or not key:
        return False
    if "\\" in key or key.startswith("/"):
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in key):
        return False
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return True


def verify_tree_manifest(
    root: os.PathLike[str] | str,
    manifest: Mapping[str, str],
    ignore: Iterable[str] = (),
) -> list[str]:
    """Compare *root* to *manifest* and return sorted human-readable errors.

    Reports missing, unexpected, and hash-mismatched paths. Invalid expected
    keys and failures while collecting the live manifest are reported as
    deterministic error entries. *ignore* uses :func:`tree_manifest` patterns
    and must match the patterns used to create *manifest*.
    """
    errors: list[str] = []
    expected: dict[str, str] = {}
    for key, digest in manifest.items():
        if not _is_safe_manifest_key(key):
            errors.append(f"invalid manifest key: {key!r}")
            continue
        if not isinstance(digest, str) or not digest:
            errors.append(f"invalid manifest digest for {key!r}")
            continue
        expected[key] = digest

    try:
        actual = tree_manifest(root, ignore=ignore)
    except Exception as exc:  # noqa: BLE001 — surface any collection failure
        errors.append(f"manifest collection failed: {exc}")
        return sorted(errors)

    for path in sorted(set(expected) | set(actual)):
        if path not in actual:
            errors.append(f"missing: {path}")
        elif path not in expected:
            errors.append(f"unexpected: {path}")
        elif actual[path] != expected[path]:
            errors.append(f"hash mismatch: {path}")

    return sorted(errors)


def _fsync_directory(directory: Path) -> None:
    """Best-effort fsync of a directory (supported on POSIX)."""
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


def atomic_write_json(path: os.PathLike[str] | str, data: Any) -> None:
    """Write *data* as deterministic JSON via a same-directory atomic replace.

    Serializes with sorted keys, UTF-8, and a trailing newline. Parent must
    already exist. On failure, only the temporary file created by this call is
    removed.
    """
    target = Path(path)
    parent = target.parent
    if not parent.is_dir():
        raise ValueError(f"parent directory does not exist: {parent}")

    payload = json.dumps(data, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=os.fspath(parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, os.fspath(target))
        tmp_path = Path()  # replaced; do not unlink
        _fsync_directory(parent)
    except Exception:
        if tmp_path != Path() and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _copy_tree_safe(
    source: Path,
    destination: Path,
    ignore_patterns: tuple[str, ...],
) -> None:
    """Copy *source* into *destination* without following symlinks."""
    source = source.resolve()
    for dirpath, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink():
            raise ValueError(f"symlink rejected: {current}")
        _reject_if_outside(current, source)

        rel_dir = current.relative_to(source).as_posix()
        if rel_dir == ".":
            rel_dir = ""

        dest_dir = destination / rel_dir if rel_dir else destination
        dest_dir.mkdir(parents=True, exist_ok=True)

        kept: list[str] = []
        for name in sorted(dirnames):
            child_path = current / name
            if child_path.is_symlink():
                raise ValueError(f"symlink rejected: {child_path}")
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            child_rel = _as_posix_rel(child_rel)
            if _is_ignored(child_rel, ignore_patterns):
                continue
            _reject_if_outside(child_path, source)
            kept.append(name)
        dirnames[:] = kept

        for name in filenames:
            src_file = current / name
            if src_file.is_symlink():
                raise ValueError(f"symlink rejected: {src_file}")
            file_rel = f"{rel_dir}/{name}" if rel_dir else name
            file_rel = _as_posix_rel(file_rel)
            if _is_ignored(file_rel, ignore_patterns):
                continue
            if not src_file.is_file():
                raise ValueError(f"not a regular file: {src_file}")
            _reject_if_outside(src_file, source)
            dest_file = dest_dir / name
            shutil.copy2(src_file, dest_file)


def atomic_snapshot(
    source: os.PathLike[str] | str,
    destination: os.PathLike[str] | str,
    ignore_patterns: Iterable[str] = (),
) -> dict[str, str]:
    """Copy *source* into a new *destination* directory atomically.

    Refuses an existing destination. Source must be a real (non-symlink)
    directory. Copies into a uniquely created sibling temporary directory,
    rejects symlinks and escaped paths, applies ignore patterns with the same
    semantics as :func:`tree_manifest`, then renames into place. On any
    failure, destination is left absent and only this call's temporary
    directory is cleaned up.
    """
    src = Path(source)
    dest = Path(destination)
    # Path.exists() is False for dangling symlinks; refuse any dirent including those.
    if os.path.lexists(dest):
        raise ValueError(f"destination already exists: {dest}")
    if src.is_symlink() or not src.is_dir():
        raise ValueError(f"source must be a real directory: {src}")

    parent = dest.parent
    if not parent.is_dir():
        raise ValueError(f"destination parent does not exist: {parent}")

    patterns = tuple(ignore_patterns)
    tmp_dir: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{dest.name}.tmp.", dir=os.fspath(parent))
    )
    try:
        assert tmp_dir is not None
        _copy_tree_safe(src, tmp_dir, patterns)
        manifest = tree_manifest(tmp_dir)
        os.replace(os.fspath(tmp_dir), os.fspath(dest))
        tmp_dir = None
        _fsync_directory(parent)
        return manifest
    except Exception:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def portable_path(path: os.PathLike[str] | str, root: os.PathLike[str] | str) -> str:
    """Return a POSIX path relative to *root*, or ``<external>`` if outside."""
    try:
        resolved = Path(path).resolve()
        root_resolved = Path(root).resolve()
        return resolved.relative_to(root_resolved).as_posix()
    except (ValueError, OSError):
        return "<external>"


def redact_text(
    text: str,
    roots: Iterable[os.PathLike[str] | str] = (),
    secret_values: Iterable[str] = (),
) -> str:
    """Redact home, filesystem roots, and explicit secrets from *text*.

    Replacement labels are stable and non-disclosing: ``<home>``, ``<root>``,
    ``<secret>``. Both native and POSIX renderings of roots are replaced when
    they differ. Empty secrets are ignored. Longer sensitive strings are
    replaced first to avoid partial leakage.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")

    pairs: list[tuple[str, str]] = []

    home = Path.home()
    home_native = str(home)
    home_posix = home.as_posix()
    if home_native:
        pairs.append((home_native, "<home>"))
    if home_posix and home_posix != home_native:
        pairs.append((home_posix, "<home>"))

    for root in roots:
        root_path = Path(root)
        candidates = {str(root_path), root_path.as_posix()}
        try:
            resolved = root_path.resolve()
            candidates.add(str(resolved))
            candidates.add(resolved.as_posix())
        except OSError:
            pass
        for candidate in candidates:
            if candidate:
                pairs.append((candidate, "<root>"))

    for secret in secret_values:
        if secret:
            pairs.append((str(secret), "<secret>"))

    # Longer first so prefixes of longer secrets do not leak.
    pairs.sort(key=lambda item: len(item[0]), reverse=True)

    result = text
    for value, label in pairs:
        if value:
            result = result.replace(value, label)
    return result
