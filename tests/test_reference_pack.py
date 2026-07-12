"""Unit tests for basecamp_bench.reference_pack (stdlib unittest only)."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path

from basecamp_bench.reference_pack import (
    Asset,
    ReferencePack,
    load_reference_pack,
)
from tests._support import can_symlink as _can_symlink

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKED_IN_MANIFEST = REPO_ROOT / "benchmarks" / "reference-pack.json"
CHECKED_IN_ROOT = REPO_ROOT / "Repo" / "reference"

# Unique marker used only to assert it never appears in error messages.
_SECRET_MARKER = "SECRET_CONTENT_DO_NOT_LEAK_9f3a2c1b7e"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _independent_tree_sha256(assets: list[tuple[str, str]]) -> str:
    """Reproduce tree_sha256: sorted path records as path\\0digest\\n UTF-8."""
    hasher = hashlib.sha256()
    for path, digest in sorted(assets, key=lambda item: item[0]):
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(digest.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _asset_dict(
    path: str,
    content: bytes,
    *,
    owner: str = "Owner",
    source: str = "Source",
    license: str = "MIT",
    modifications: str = "None",
    distributable: bool = True,
    sha256: str | None = None,
) -> dict:
    return {
        "path": path,
        "sha256": sha256 if sha256 is not None else _sha256_bytes(content),
        "owner": owner,
        "source": source,
        "license": license,
        "modifications": modifications,
        "distributable": distributable,
    }


def _manifest_dict(
    assets: list[dict],
    *,
    schema_version: str = "1.0",
    pack_id: str = "test-pack",
    pack_version: str = "1",
    distributable: bool = True,
) -> dict:
    return {
        "schema_version": schema_version,
        "pack_id": pack_id,
        "pack_version": pack_version,
        "distributable": distributable,
        "assets": assets,
    }


class TempPackTestCase(unittest.TestCase):
    """Temporary root + manifest fixtures; never mutates the repository tree."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self._tmpdir.name)
        self.root = self.base / "root"
        self.root.mkdir()
        self.manifest_path = self.base / "manifest.json"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def write_file(self, rel: str, data: bytes | str = b"data") -> bytes:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = data if isinstance(data, bytes) else data.encode("utf-8")
        path.write_bytes(raw)
        return raw

    def write_manifest(self, data: dict) -> Path:
        payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
        self.manifest_path.write_text(payload, encoding="utf-8")
        return self.manifest_path

    def write_pack(
        self,
        files: dict[str, bytes | str],
        *,
        pack_distributable: bool = True,
        asset_distributable: bool | dict[str, bool] = True,
        schema_version: str = "1.0",
        pack_id: str = "test-pack",
        pack_version: str = "1",
    ) -> tuple[Path, dict]:
        asset_list: list[dict] = []
        for rel, content in files.items():
            raw = self.write_file(rel, content)
            if isinstance(asset_distributable, dict):
                dist = asset_distributable.get(rel, True)
            else:
                dist = bool(asset_distributable)
            asset_list.append(_asset_dict(rel, raw, distributable=dist))
        manifest = _manifest_dict(
            asset_list,
            schema_version=schema_version,
            pack_id=pack_id,
            pack_version=pack_version,
            distributable=pack_distributable,
        )
        self.write_manifest(manifest)
        return self.manifest_path, manifest

    def load(self, *, publication: bool = False) -> ReferencePack:
        return load_reference_pack(self.manifest_path, self.root, publication=publication)


class CheckedInPackTests(unittest.TestCase):
    def test_loads_checked_in_reference_pack(self) -> None:
        self.assertTrue(CHECKED_IN_MANIFEST.is_file())
        self.assertTrue(CHECKED_IN_ROOT.is_dir())
        pack = load_reference_pack(CHECKED_IN_MANIFEST, CHECKED_IN_ROOT)
        self.assertIsInstance(pack, ReferencePack)
        self.assertEqual(pack.schema_version, "1.0")
        self.assertEqual(pack.pack_id, "basecamp5-reference")
        self.assertTrue(pack.distributable)
        self.assertGreater(len(pack.assets), 0)
        self.assertEqual(
            list(a.path for a in pack.assets),
            sorted(a.path for a in pack.assets),
        )

        raw = CHECKED_IN_MANIFEST.read_bytes()
        self.assertEqual(pack.manifest_sha256, _sha256_bytes(raw))
        expected_tree = _independent_tree_sha256([(a.path, a.sha256) for a in pack.assets])
        self.assertEqual(pack.tree_sha256, expected_tree)

        for asset in pack.assets:
            self.assertIsInstance(asset, Asset)
            self.assertTrue(asset.distributable)
            file_path = CHECKED_IN_ROOT / asset.path
            self.assertTrue(file_path.is_file())
            self.assertEqual(asset.sha256, _sha256_file(file_path))

    def test_publication_accepted_for_distributable_checked_in_pack(self) -> None:
        pack = load_reference_pack(CHECKED_IN_MANIFEST, CHECKED_IN_ROOT, publication=True)
        self.assertTrue(pack.distributable)


class ImmutableReturnTests(TempPackTestCase):
    def test_frozen_dataclasses_and_tuple_assets(self) -> None:
        self.write_pack({"a.txt": b"hello", "b/c.txt": b"world"})
        pack = self.load()
        self.assertTrue(is_dataclass(pack))
        self.assertTrue(is_dataclass(pack.assets[0]))
        self.assertIsInstance(pack.assets, tuple)
        self.assertEqual([a.path for a in pack.assets], ["a.txt", "b/c.txt"])

        with self.assertRaises((FrozenInstanceError, AttributeError)):
            pack.pack_id = "mutated"  # type: ignore[misc]
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            pack.assets[0].path = "x"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            pack.assets[0] = pack.assets[0]  # type: ignore[index]
        with self.assertRaises(AttributeError):
            pack.assets.append(pack.assets[0])  # type: ignore[attr-defined]

    def test_manifest_and_tree_hashes_independently_verified(self) -> None:
        files = {"z.txt": b"z-bytes", "a.txt": b"a-bytes"}
        self.write_pack(files)
        pack = self.load()
        self.assertEqual(pack.manifest_sha256, _sha256_bytes(self.manifest_path.read_bytes()))
        expected = _independent_tree_sha256([(a.path, a.sha256) for a in pack.assets])
        self.assertEqual(pack.tree_sha256, expected)
        # Sorting: a before z regardless of write order.
        self.assertEqual([a.path for a in pack.assets], ["a.txt", "z.txt"])


class ManifestShapeTests(TempPackTestCase):
    def test_malformed_json(self) -> None:
        self.write_file("a.txt", b"a")
        self.manifest_path.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            self.load()
        msg = str(ctx.exception)
        self.assertIn("JSON parse error", msg)
        self.assertNotIn("{not-json", msg)

    def test_root_not_object(self) -> None:
        self.manifest_path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("object", str(ctx.exception).lower())

    def test_missing_root_keys(self) -> None:
        self.write_file("a.txt", b"a")
        data = _manifest_dict([_asset_dict("a.txt", b"a")])
        del data["pack_version"]
        self.write_manifest(data)
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("missing", str(ctx.exception).lower())
        self.assertIn("pack_version", str(ctx.exception))

    def test_unknown_root_keys(self) -> None:
        self.write_file("a.txt", b"a")
        data = _manifest_dict([_asset_dict("a.txt", b"a")])
        data["extra_root"] = True
        self.write_manifest(data)
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("unknown", str(ctx.exception).lower())
        self.assertIn("extra_root", str(ctx.exception))

    def test_missing_asset_keys(self) -> None:
        self.write_file("a.txt", b"a")
        asset = _asset_dict("a.txt", b"a")
        del asset["owner"]
        self.write_manifest(_manifest_dict([asset]))
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("missing", str(ctx.exception).lower())
        self.assertIn("owner", str(ctx.exception))

    def test_unknown_asset_keys(self) -> None:
        self.write_file("a.txt", b"a")
        asset = _asset_dict("a.txt", b"a")
        asset["extra"] = "nope"
        self.write_manifest(_manifest_dict([asset]))
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("unknown", str(ctx.exception).lower())
        self.assertIn("extra", str(ctx.exception))

    def test_fixed_schema_version_rejection(self) -> None:
        self.write_file("a.txt", b"a")
        data = _manifest_dict([_asset_dict("a.txt", b"a")], schema_version="2.0")
        self.write_manifest(data)
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("schema_version", str(ctx.exception))

    def test_unsafe_pack_id(self) -> None:
        self.write_file("a.txt", b"a")
        for bad_id in (
            "",
            "Bad",
            "-leading",
            "has space",
            "a" * 65,
            "foo/bar",
            "../x",
        ):
            with self.subTest(pack_id=bad_id):
                data = _manifest_dict([_asset_dict("a.txt", b"a")], pack_id=bad_id or "x")
                data["pack_id"] = bad_id
                self.write_manifest(data)
                with self.assertRaises(ValueError) as ctx:
                    self.load()
                self.assertIn("pack_id", str(ctx.exception))

    def test_bad_types_fail_closed(self) -> None:
        self.write_file("a.txt", b"a")
        base_asset = _asset_dict("a.txt", b"a")

        cases: list[tuple[str, dict]] = [
            ("pack_version_bool", {"pack_version": True}),
            ("pack_version_int", {"pack_version": 1}),
            ("pack_version_empty", {"pack_version": ""}),
            ("distributable_int", {"distributable": 1}),
            ("distributable_string", {"distributable": "true"}),
            ("assets_not_list", {"assets": {"path": "a.txt"}}),
            ("schema_version_bool", {"schema_version": True}),
        ]
        for name, overrides in cases:
            with self.subTest(name=name):
                data = _manifest_dict([dict(base_asset)])
                data.update(overrides)
                self.write_manifest(data)
                with self.assertRaises(ValueError):
                    self.load()

        # Asset-level type failures.
        asset_cases: list[tuple[str, dict]] = [
            ("path_bool", {**base_asset, "path": True}),
            ("sha256_bool", {**base_asset, "sha256": True}),
            ("owner_empty", {**base_asset, "owner": ""}),
            ("owner_int", {**base_asset, "owner": 1}),
            ("source_empty", {**base_asset, "source": ""}),
            ("license_empty", {**base_asset, "license": ""}),
            ("modifications_null", {**base_asset, "modifications": None}),
            ("modifications_int", {**base_asset, "modifications": 0}),
            ("distributable_int", {**base_asset, "distributable": 0}),
            ("distributable_str", {**base_asset, "distributable": "yes"}),
            ("asset_not_object", "not-an-object"),  # type: ignore[list-item]
        ]
        for name, asset in asset_cases:
            with self.subTest(asset=name):
                if name == "asset_not_object":
                    data = _manifest_dict([])
                    data["assets"] = [asset]  # type: ignore[list-item]
                else:
                    data = _manifest_dict([asset])  # type: ignore[list-item]
                self.write_manifest(data)
                with self.assertRaises(ValueError):
                    self.load()

    def test_invalid_digest(self) -> None:
        self.write_file("a.txt", b"a")
        base = _asset_dict("a.txt", b"a")
        for bad in (
            "",
            "abc",
            "A" * 64,  # uppercase
            "g" * 64,
            "a" * 63,
            "a" * 65,
        ):
            with self.subTest(digest=bad):
                asset = dict(base)
                asset["sha256"] = bad
                self.write_manifest(_manifest_dict([asset]))
                with self.assertRaises(ValueError) as ctx:
                    self.load()
                self.assertIn("sha256", str(ctx.exception).lower())

    def test_duplicate_path(self) -> None:
        raw = self.write_file("a.txt", b"dup")
        asset = _asset_dict("a.txt", raw)
        self.write_manifest(_manifest_dict([asset, dict(asset)]))
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_empty_modifications_allowed(self) -> None:
        raw = self.write_file("a.txt", b"a")
        asset = _asset_dict("a.txt", raw, modifications="")
        self.write_manifest(_manifest_dict([asset]))
        pack = self.load()
        self.assertEqual(pack.assets[0].modifications, "")


class PathValidationTests(TempPackTestCase):
    def test_rejects_empty_absolute_dot_dotdot_backslash_control_non_normalized(
        self,
    ) -> None:
        # Manifest-only path checks: file need not exist for path rejection.
        raw = b"x"
        digest = _sha256_bytes(raw)
        bad_paths = [
            "",
            "/abs/file.txt",
            "foo/./bar.txt",
            "foo/../bar.txt",
            "./foo.txt",
            "../foo.txt",
            "foo//bar.txt",
            "foo/bar/",
            "foo\\bar.txt",
            "a\nb.txt",
            "a\x00b.txt",
            "a\x1fb.txt",
            "C:/windows/file.txt",
            "C:foo.txt",
        ]
        for path in bad_paths:
            with self.subTest(path=path):
                asset = {
                    "path": path,
                    "sha256": digest,
                    "owner": "O",
                    "source": "S",
                    "license": "L",
                    "modifications": "M",
                    "distributable": True,
                }
                self.write_manifest(_manifest_dict([asset]))
                with self.assertRaises(ValueError) as ctx:
                    self.load()
                msg = str(ctx.exception).lower()
                self.assertTrue(
                    "path" in msg
                    or "relative" in msg
                    or "normalized" in msg
                    or "control" in msg
                    or "empty" in msg
                    or "posix" in msg
                    or "component" in msg
                    or ".." in msg
                    or "'.'" in msg
                    or "separator" in msg
                    or "backslash" in msg
                    or "must not" in msg,
                    msg=str(ctx.exception),
                )


class FilesystemTests(TempPackTestCase):
    def test_missing_file(self) -> None:
        asset = _asset_dict("missing.txt", b"missing")
        self.write_manifest(_manifest_dict([asset]))
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("does not exist", str(ctx.exception).lower())

    def test_non_regular_file(self) -> None:
        dir_path = self.root / "notafile"
        dir_path.mkdir()
        asset = _asset_dict("notafile", b"x")
        self.write_manifest(_manifest_dict([asset]))
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("regular file", str(ctx.exception).lower())

    def test_undeclared_file(self) -> None:
        self.write_pack({"a.txt": b"a"})
        self.write_file("extra.txt", b"extra")
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("undeclared", str(ctx.exception).lower())
        self.assertIn("extra.txt", str(ctx.exception))

    def test_ds_store_allowed_undeclared(self) -> None:
        self.write_pack({"a.txt": b"a"})
        self.write_file(".DS_Store", b"junk")
        nested = self.root / "sub"
        nested.mkdir()
        (nested / ".DS_Store").write_bytes(b"more-junk")
        pack = self.load()
        self.assertEqual([a.path for a in pack.assets], ["a.txt"])

    def test_asset_hash_mismatch(self) -> None:
        self.write_file("a.txt", b"actual-bytes")
        asset = _asset_dict("a.txt", b"other-bytes")
        self.write_manifest(_manifest_dict([asset]))
        with self.assertRaises(ValueError) as ctx:
            self.load()
        msg = str(ctx.exception)
        self.assertIn("SHA-256 mismatch", msg)
        self.assertIn("a.txt", msg)
        self.assertNotIn("actual-bytes", msg)
        self.assertNotIn("other-bytes", msg)


class SymlinkTests(TempPackTestCase):
    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_manifest_symlink_rejected(self) -> None:
        self.write_pack({"a.txt": b"a"})
        real_manifest = self.base / "real-manifest.json"
        real_manifest.write_bytes(self.manifest_path.read_bytes())
        self.manifest_path.unlink()
        self.manifest_path.symlink_to(real_manifest)
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("symlink", str(ctx.exception).lower())
        self.assertIn("manifest", str(ctx.exception).lower())

    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_root_symlink_rejected(self) -> None:
        self.write_pack({"a.txt": b"a"})
        real_root = self.base / "real-root"
        shutil.copytree(self.root, real_root)
        # Replace root with symlink.
        shutil.rmtree(self.root)
        self.root.symlink_to(real_root)
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("symlink", str(ctx.exception).lower())

    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_intermediate_symlink_rejected(self) -> None:
        real_dir = self.root / "realdir"
        real_dir.mkdir()
        (real_dir / "file.txt").write_bytes(b"payload")
        link_dir = self.root / "linkdir"
        link_dir.symlink_to(real_dir)
        digest = _sha256_bytes(b"payload")
        asset = _asset_dict("linkdir/file.txt", b"payload", sha256=digest)
        self.write_manifest(_manifest_dict([asset]))
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("symlink", str(ctx.exception).lower())

    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_final_file_symlink_rejected(self) -> None:
        real = self.root / "real.txt"
        real.write_bytes(b"payload")
        link = self.root / "link.txt"
        link.symlink_to(real)
        asset = _asset_dict("link.txt", b"payload")
        self.write_manifest(_manifest_dict([asset]))
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("symlink", str(ctx.exception).lower())

    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_undeclared_symlink_rejected_not_ignored(self) -> None:
        self.write_pack({"a.txt": b"a"})
        target = self.root / "a.txt"
        link = self.root / "sneaky"
        link.symlink_to(target)
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("symlink", str(ctx.exception).lower())

    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_ds_store_symlink_rejected(self) -> None:
        self.write_pack({"a.txt": b"a"})
        target = self.root / "a.txt"
        link = self.root / ".DS_Store"
        link.symlink_to(target)
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertIn("symlink", str(ctx.exception).lower())


class DistributableTests(TempPackTestCase):
    def test_pack_distributable_requires_all_assets(self) -> None:
        files = {"a.txt": b"a", "b.txt": b"b"}
        self.write_pack(
            files,
            pack_distributable=True,
            asset_distributable={"a.txt": True, "b.txt": False},
        )
        with self.assertRaises(ValueError) as ctx:
            self.load()
        msg = str(ctx.exception).lower()
        self.assertIn("distributable", msg)
        self.assertIn("b.txt", str(ctx.exception))

    def test_pack_not_distributable_allows_mixed_assets(self) -> None:
        self.write_pack(
            {"a.txt": b"a", "b.txt": b"b"},
            pack_distributable=False,
            asset_distributable={"a.txt": True, "b.txt": False},
        )
        pack = self.load()
        self.assertFalse(pack.distributable)
        by_path = {a.path: a.distributable for a in pack.assets}
        self.assertEqual(by_path, {"a.txt": True, "b.txt": False})

    def test_publication_requires_pack_and_assets_distributable(self) -> None:
        self.write_pack(
            {"a.txt": b"a"},
            pack_distributable=False,
            asset_distributable=True,
        )
        with self.assertRaises(ValueError) as ctx:
            self.load(publication=True)
        self.assertIn("publication", str(ctx.exception).lower())

        self.write_pack(
            {"a.txt": b"a"},
            pack_distributable=True,
            asset_distributable=False,
        )
        # Pack distributable true with non-distributable asset fails even
        # without publication.
        with self.assertRaises(ValueError):
            self.load(publication=False)

    def test_publication_ok_when_all_distributable(self) -> None:
        self.write_pack({"a.txt": b"a"}, pack_distributable=True)
        pack = self.load(publication=True)
        self.assertTrue(pack.distributable)
        self.assertTrue(pack.assets[0].distributable)


class ErrorMessageSafetyTests(TempPackTestCase):
    def test_error_messages_omit_file_contents(self) -> None:
        secret = _SECRET_MARKER.encode("utf-8")
        self.write_file("secret.txt", secret)
        # Hash mismatch: wrong digest.
        asset = _asset_dict("secret.txt", b"different")
        self.write_manifest(_manifest_dict([asset]))
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertNotIn(_SECRET_MARKER, str(ctx.exception))
        self.assertNotIn("different", str(ctx.exception))

    def test_json_error_omits_source_content(self) -> None:
        payload = '{"schema_version": "1.0", "bad": ' + json.dumps(_SECRET_MARKER)
        self.manifest_path.write_text(payload, encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            self.load()
        self.assertNotIn(_SECRET_MARKER, str(ctx.exception))

    def test_hash_mismatch_message_has_paths_and_digests_only(self) -> None:
        content = (_SECRET_MARKER + "-body").encode("utf-8")
        self.write_file("a.txt", content)
        wrong = "ab" * 32
        asset = _asset_dict("a.txt", content, sha256=wrong)
        self.write_manifest(_manifest_dict([asset]))
        with self.assertRaises(ValueError) as ctx:
            self.load()
        msg = str(ctx.exception)
        self.assertIn("a.txt", msg)
        self.assertIn(wrong, msg)
        self.assertIn(_sha256_bytes(content), msg)
        self.assertNotIn(_SECRET_MARKER, msg)


class EmptyPackTests(TempPackTestCase):
    def test_empty_assets_tree_hash(self) -> None:
        self.write_manifest(_manifest_dict([], distributable=True))
        pack = self.load()
        self.assertEqual(pack.assets, ())
        self.assertEqual(pack.tree_sha256, _sha256_bytes(b""))
        self.assertEqual(pack.manifest_sha256, _sha256_bytes(self.manifest_path.read_bytes()))


if __name__ == "__main__":
    unittest.main()
