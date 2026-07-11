"""Reference-pack manifest loading, validation, and integrity hashing.

Standard-library only. Validates manifests against the keys and constraints of
``schemas/reference-pack.schema.json``, verifies filesystem safety under a pack
root without following symlinks, and returns an immutable pack record.

Tree hash encoding
------------------
``tree_sha256`` is SHA-256 over the concatenation of one record per declared
asset, sorted by path (lexicographic on the path string). Each record is:

    UTF-8(path) + 0x00 + UTF-8(lowercase_hex_sha256) + 0x0a

where ``path`` is the relative POSIX path from the manifest and
``lowercase_hex_sha256`` is the 64-character digest declared for that asset
(after it has been verified against the file bytes). Empty packs hash the
empty byte string. Paths are validated to exclude NUL and other control
characters, so the encoding is unambiguous.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "Asset",
    "ReferencePack",
    "load_reference_pack",
]

SCHEMA_VERSION = "1.0"

_ROOT_KEYS = (
    "schema_version",
    "pack_id",
    "pack_version",
    "distributable",
    "assets",
)
_ASSET_KEYS = (
    "path",
    "sha256",
    "owner",
    "source",
    "license",
    "modifications",
    "distributable",
)
_ROOT_KEY_SET = frozenset(_ROOT_KEYS)
_ASSET_KEY_SET = frozenset(_ASSET_KEYS)

_PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")
_READ_CHUNK = 1024 * 1024
_DS_STORE = ".DS_Store"


@dataclass(frozen=True, slots=True)
class Asset:
    """One declared reference-pack asset with provenance and integrity."""

    path: str
    sha256: str
    owner: str
    source: str
    license: str
    modifications: str
    distributable: bool


@dataclass(frozen=True, slots=True)
class ReferencePack:
    """Validated, immutable reference pack with integrity digests."""

    schema_version: str
    pack_id: str
    pack_version: str
    distributable: bool
    assets: tuple[Asset, ...]
    manifest_sha256: str
    tree_sha256: str


def load_reference_pack(
    manifest_path: Path,
    root: Path,
    publication: bool = False,
) -> ReferencePack:
    """Load and validate a reference-pack manifest against *root*.

    Returns a frozen :class:`ReferencePack`. Raises :class:`ValueError` with
    actionable path/reason text for malformed manifests or unsafe/mismatching
    filesystem state. Error messages never include asset or manifest file
    contents.
    """
    manifest_path = Path(manifest_path)
    root = Path(root)

    _reject_symlink_path(manifest_path, "manifest_path")
    if not manifest_path.is_file():
        raise ValueError(f"manifest_path is not a regular file: {manifest_path}")

    _reject_symlink_path(root, "root")
    if not root.is_dir():
        raise ValueError(f"root is not a real directory: {root}")

    raw = _read_file_bytes(manifest_path)
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    data = _parse_manifest_json(raw, manifest_path)
    assets = _validate_manifest_object(data)

    if publication:
        if not data["distributable"]:
            raise ValueError("publication requires pack distributable to be true")
        nondist = [a.path for a in assets if not a.distributable]
        if nondist:
            raise ValueError(
                "publication requires every asset distributable true; "
                f"non-distributable: {', '.join(nondist)}"
            )

    declared = {asset.path for asset in assets}
    for asset in assets:
        file_path = _resolve_asset_under_root(root, asset.path)
        actual = _sha256_file(file_path)
        if actual != asset.sha256:
            raise ValueError(
                f"SHA-256 mismatch for asset {asset.path!r}: "
                f"manifest declares {asset.sha256}, file has {actual}"
            )

    _reject_undeclared_and_symlinks(root, declared)

    tree_sha256 = _compute_tree_sha256(assets)
    return ReferencePack(
        schema_version=data["schema_version"],
        pack_id=data["pack_id"],
        pack_version=data["pack_version"],
        distributable=data["distributable"],
        assets=assets,
        manifest_sha256=manifest_sha256,
        tree_sha256=tree_sha256,
    )


# ---------------------------------------------------------------------------
# Manifest parsing and structural validation
# ---------------------------------------------------------------------------


def _parse_manifest_json(raw: bytes, manifest_path: Path) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"manifest is not valid UTF-8: {manifest_path} ({exc.reason} at byte {exc.start})"
        ) from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"manifest JSON parse error in {manifest_path}: "
            f"{exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from None


def _validate_manifest_object(data: Any) -> tuple[Asset, ...]:
    if not isinstance(data, dict):
        raise ValueError(f"manifest root must be a JSON object, got {type(data).__name__}")

    actual_keys = set(data.keys())
    missing = _ROOT_KEY_SET - actual_keys
    unknown = actual_keys - _ROOT_KEY_SET
    if missing or unknown:
        parts: list[str] = []
        if missing:
            parts.append("missing keys: " + ", ".join(repr(k) for k in sorted(missing)))
        if unknown:
            parts.append("unknown keys: " + ", ".join(repr(k) for k in sorted(unknown)))
        raise ValueError("manifest root: " + "; ".join(parts))

    schema_version = data["schema_version"]
    if not _is_str(schema_version) or schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"schema_version must be exactly {SCHEMA_VERSION!r}, "
            f"got {type(schema_version).__name__}"
            + (f" value {schema_version!r}" if _is_str(schema_version) else "")
        )

    pack_id = data["pack_id"]
    if not _is_str(pack_id):
        raise ValueError(f"pack_id must be a string, got {type(pack_id).__name__}")
    if _PACK_ID_RE.fullmatch(pack_id) is None:
        raise ValueError(f"pack_id is not a safe lowercase identifier: {pack_id!r}")

    pack_version = data["pack_version"]
    if not _is_nonempty_str(pack_version):
        raise ValueError(
            f"pack_version must be a nonempty string, got {type(pack_version).__name__}"
        )

    distributable = data["distributable"]
    if not _is_bool(distributable):
        raise ValueError(f"distributable must be a boolean, got {type(distributable).__name__}")

    raw_assets = data["assets"]
    if not isinstance(raw_assets, list):
        raise ValueError(f"assets must be an array, got {type(raw_assets).__name__}")

    assets: list[Asset] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(raw_assets):
        asset = _validate_asset_object(item, index)
        if asset.path in seen_paths:
            raise ValueError(f"duplicate asset path: {asset.path!r}")
        seen_paths.add(asset.path)
        assets.append(asset)

    assets.sort(key=lambda a: a.path)

    if distributable and any(not a.distributable for a in assets):
        bad = [a.path for a in assets if not a.distributable]
        raise ValueError(
            "pack distributable may be true only when every asset is "
            f"distributable; non-distributable assets: {', '.join(bad)}"
        )

    return tuple(assets)


def _validate_asset_object(item: Any, index: int) -> Asset:
    prefix = f"assets[{index}]"
    if not isinstance(item, dict):
        raise ValueError(f"{prefix} must be an object, got {type(item).__name__}")

    actual_keys = set(item.keys())
    missing = _ASSET_KEY_SET - actual_keys
    unknown = actual_keys - _ASSET_KEY_SET
    if missing or unknown:
        parts: list[str] = []
        if missing:
            parts.append("missing keys: " + ", ".join(repr(k) for k in sorted(missing)))
        if unknown:
            parts.append("unknown keys: " + ", ".join(repr(k) for k in sorted(unknown)))
        raise ValueError(f"{prefix}: " + "; ".join(parts))

    path = item["path"]
    if not _is_str(path):
        raise ValueError(f"{prefix}.path must be a string, got {type(path).__name__}")
    _validate_asset_path(path, field=f"{prefix}.path")

    digest = item["sha256"]
    if not _is_str(digest):
        raise ValueError(f"{prefix}.sha256 must be a string, got {type(digest).__name__}")
    if _SHA256_HEX_RE.fullmatch(digest) is None:
        raise ValueError(f"{prefix}.sha256 must be a lowercase 64-hex digest")

    owner = item["owner"]
    if not _is_nonempty_str(owner):
        raise ValueError(f"{prefix}.owner must be a nonempty string, got {type(owner).__name__}")

    source = item["source"]
    if not _is_nonempty_str(source):
        raise ValueError(f"{prefix}.source must be a nonempty string, got {type(source).__name__}")

    license_value = item["license"]
    if not _is_nonempty_str(license_value):
        raise ValueError(
            f"{prefix}.license must be a nonempty string, got {type(license_value).__name__}"
        )

    modifications = item["modifications"]
    if not _is_str(modifications):
        raise ValueError(
            f"{prefix}.modifications must be a string, got {type(modifications).__name__}"
        )

    asset_dist = item["distributable"]
    if not _is_bool(asset_dist):
        raise ValueError(
            f"{prefix}.distributable must be a boolean, got {type(asset_dist).__name__}"
        )

    return Asset(
        path=path,
        sha256=digest,
        owner=owner,
        source=source,
        license=license_value,
        modifications=modifications,
        distributable=asset_dist,
    )


def _validate_asset_path(path: str, *, field: str) -> None:
    """Reject empty, absolute, traversal, backslash, control, non-normalized."""
    if path == "":
        raise ValueError(f"{field} must not be empty")
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise ValueError(f"{field} contains control characters: {path!r}")
    if "\\" in path:
        raise ValueError(f"{field} must use POSIX separators only: {path!r}")
    if path.startswith("/"):
        raise ValueError(f"{field} must be a relative path: {path!r}")
    # Windows drive-absolute forms are not relative POSIX paths.
    if len(path) >= 2 and path[0].isalpha() and path[1] == ":":
        raise ValueError(f"{field} must be a relative POSIX path: {path!r}")

    parts = path.split("/")
    if any(part == "" for part in parts):
        raise ValueError(f"{field} is not normalized (empty path component): {path!r}")
    if any(part == "." for part in parts):
        raise ValueError(f"{field} must not contain '.' components: {path!r}")
    if any(part == ".." for part in parts):
        raise ValueError(f"{field} must not contain '..' components: {path!r}")

    normalized = posixpath.normpath(path)
    if normalized != path:
        raise ValueError(f"{field} is not a normalized relative POSIX path: {path!r}")
    if normalized in (".", "..") or normalized.startswith("../"):
        raise ValueError(f"{field} is not a safe relative path: {path!r}")


# ---------------------------------------------------------------------------
# Filesystem safety (no symlink following)
# ---------------------------------------------------------------------------


def _reject_symlink_path(path: Path, label: str) -> None:
    try:
        if path.is_symlink():
            raise ValueError(f"{label} must not be a symlink: {path}")
    except OSError as exc:
        raise ValueError(f"cannot stat {label}: {path}") from exc


def _resolve_asset_under_root(root: Path, rel_path: str) -> Path:
    """Join *rel_path* under *root* without following any symlink component.

    Each intermediate component must be a real directory; the final component
    must be a regular file. Containment is lexical (validated path parts only).
    """
    current = root
    parts = rel_path.split("/")
    for index, part in enumerate(parts):
        # Lexical containment: only join validated relative components.
        candidate = current / part
        try:
            st = os.lstat(candidate)
        except FileNotFoundError as exc:
            raise ValueError(f"declared asset does not exist: {rel_path}") from exc
        except OSError as exc:
            raise ValueError(f"cannot stat asset path component {part!r} of {rel_path!r}") from exc

        if stat.S_ISLNK(st.st_mode):
            raise ValueError(f"symlink rejected in asset path {rel_path!r} (component {part!r})")

        is_last = index == len(parts) - 1
        if is_last:
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"declared asset is not a regular file: {rel_path}")
        else:
            if not stat.S_ISDIR(st.st_mode):
                raise ValueError(
                    f"asset path component is not a directory: {'/'.join(parts[: index + 1])}"
                )
        current = candidate
    return current


def _reject_undeclared_and_symlinks(root: Path, declared: set[str]) -> None:
    """Walk *root* without following links; reject extras and all symlinks."""
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        try:
            if current.is_symlink():
                rel = _rel_posix(current, root)
                raise ValueError(f"symlink rejected under root: {rel}")
        except OSError as exc:
            raise ValueError(f"cannot stat directory under root: {current}") from exc

        rel_dir = _rel_posix(current, root)
        if rel_dir == ".":
            rel_dir = ""

        dirnames.sort()
        filenames.sort()

        for name in dirnames:
            child = current / name
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            try:
                st = os.lstat(child)
            except OSError as exc:
                raise ValueError(f"cannot stat path under root: {child_rel}") from exc
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(f"symlink rejected under root: {child_rel}")
            if not stat.S_ISDIR(st.st_mode):
                raise ValueError(f"unsupported non-directory entry under root: {child_rel}")

        for name in filenames:
            child = current / name
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            try:
                st = os.lstat(child)
            except OSError as exc:
                raise ValueError(f"cannot stat path under root: {child_rel}") from exc
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(f"symlink rejected under root: {child_rel}")
            if name == _DS_STORE:
                # Undeclared AppleDouble metadata is ignored; declared ones
                # are still subject to the asset checks above.
                continue
            if child_rel not in declared:
                if not stat.S_ISREG(st.st_mode):
                    raise ValueError(f"unsupported undeclared entry under root: {child_rel}")
                raise ValueError(f"undeclared file under root: {child_rel}")
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"declared asset is not a regular file: {child_rel}")


def _rel_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"path escapes root {root}: {path}") from exc


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _read_file_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read file: {path}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_READ_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise ValueError(f"cannot read file for hashing: {path}") from exc
    return digest.hexdigest()


def _compute_tree_sha256(assets: tuple[Asset, ...] | list[Asset]) -> str:
    """Hash declared assets with the documented path+digest record encoding."""
    # Assets are expected already sorted by path; sort defensively.
    ordered = sorted(assets, key=lambda a: a.path)
    hasher = hashlib.sha256()
    for asset in ordered:
        hasher.update(asset.path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(asset.sha256.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Type helpers (fail closed; never treat bool as int/str)
# ---------------------------------------------------------------------------


def _is_str(value: Any) -> bool:
    return isinstance(value, str) and not isinstance(value, bool)


def _is_nonempty_str(value: Any) -> bool:
    return _is_str(value) and value != ""


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)
