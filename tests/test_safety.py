"""Unit tests for basecamp_bench.safety (stdlib unittest only)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from basecamp_bench.safety import (
    IDENTIFIER_RE,
    atomic_snapshot,
    atomic_write_json,
    create_unique_directory,
    portable_path,
    redact_text,
    resolve_within,
    sha256_file,
    tree_manifest,
    validate_identifier,
    verify_tree_manifest,
)


def _can_symlink() -> bool:
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


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()


class ValidateIdentifierTests(TempDirTestCase):
    def test_accepts_valid_identifiers(self) -> None:
        for value in ("a", "a1", "foo-bar", "foo_bar", "file.name", "x9._-"):
            with self.subTest(value=value):
                self.assertEqual(validate_identifier(value), value)
                self.assertIsNotNone(IDENTIFIER_RE.fullmatch(value))

    def test_rejects_traversal_and_separators(self) -> None:
        for value in (
            "../../../escape",
            "foo/bar",
            "foo\\bar",
            "..",
            "a..b",
            "x/../y",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    validate_identifier(value, field="name")
                self.assertIn("name", str(ctx.exception))

    def test_rejects_bool_and_non_string(self) -> None:
        for value in (True, False, 1, 1.5, None, b"abc", ["a"], {"a": 1}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    validate_identifier(value, field="id")
                self.assertIn("id", str(ctx.exception))

    def test_rejects_controls_empty_overlong(self) -> None:
        with self.assertRaises(ValueError):
            validate_identifier("", field="x")
        with self.assertRaises(ValueError):
            validate_identifier("a\nb", field="x")
        with self.assertRaises(ValueError):
            validate_identifier("a\x00b", field="x")
        with self.assertRaises(ValueError):
            validate_identifier("a" * 65, field="x")
        self.assertEqual(validate_identifier("a" * 64), "a" * 64)

    def test_rejects_absolute_posix_and_windows(self) -> None:
        for value in ("/etc/passwd", "\\Windows", "C:\\Windows", "C:/Windows", "D:foo"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as ctx:
                    validate_identifier(value, field="path")
                self.assertIn("path", str(ctx.exception))

    def test_rejects_disallowed_characters(self) -> None:
        for value in ("A1", "-leading", ".dot", "has space", "foo@bar", "café"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_identifier(value)


class ResolveWithinTests(TempDirTestCase):
    def test_allows_internal_and_missing_leaf(self) -> None:
        nested = self.root / "a" / "b"
        nested.mkdir(parents=True)
        resolved = resolve_within(self.root, "a", "b", "missing.txt")
        self.assertEqual(resolved, (self.root / "a" / "b" / "missing.txt").resolve())

    def test_rejects_traversal(self) -> None:
        (self.root / "inside").mkdir()
        with self.assertRaises(ValueError):
            resolve_within(self.root, "..", "escape")
        with self.assertRaises(ValueError):
            resolve_within(self.root, "inside", "..", "..", "escape")


class CreateUniqueDirectoryTests(TempDirTestCase):
    def test_creates_and_refuses_collision(self) -> None:
        path = self.root / "unique"
        created = create_unique_directory(path)
        self.assertTrue(created.is_dir())
        self.assertEqual(created, path)
        with self.assertRaises(ValueError):
            create_unique_directory(path)


class ManifestTests(TempDirTestCase):
    def test_sorted_manifest_and_stable_hashes(self) -> None:
        (self.root / "b.txt").write_text("b", encoding="utf-8")
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "c.txt").write_text("c", encoding="utf-8")

        m1 = tree_manifest(self.root)
        m2 = tree_manifest(self.root)
        self.assertEqual(list(m1.keys()), sorted(m1.keys()))
        self.assertEqual(list(m1.keys()), ["a.txt", "b.txt", "sub/c.txt"])
        self.assertEqual(m1, m2)
        expected_a = hashlib.sha256(b"a").hexdigest()
        self.assertEqual(m1["a.txt"], expected_a)
        self.assertEqual(sha256_file(self.root / "a.txt"), expected_a)

    def test_ignore_prunes_directory(self) -> None:
        keep = self.root / "keep.txt"
        keep.write_text("k", encoding="utf-8")
        skip = self.root / "skip"
        skip.mkdir()
        (skip / "secret.txt").write_text("s", encoding="utf-8")
        (self.root / "x.tmp").write_text("t", encoding="utf-8")
        manifest = tree_manifest(self.root, ignore=("skip", "*.tmp"))
        self.assertEqual(list(manifest.keys()), ["keep.txt"])

    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_symlink_file_rejected_in_manifest(self) -> None:
        target = self.root / "real.txt"
        target.write_text("data", encoding="utf-8")
        link = self.root / "link.txt"
        link.symlink_to(target)
        with self.assertRaises(ValueError) as ctx:
            tree_manifest(self.root)
        self.assertIn("symlink", str(ctx.exception).lower())

    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_symlink_dir_rejected_in_manifest(self) -> None:
        real_dir = self.root / "real"
        real_dir.mkdir()
        (real_dir / "f.txt").write_text("f", encoding="utf-8")
        link = self.root / "linkdir"
        link.symlink_to(real_dir)
        with self.assertRaises(ValueError) as ctx:
            tree_manifest(self.root)
        self.assertIn("symlink", str(ctx.exception).lower())

    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_symlink_root_rejected_in_manifest(self) -> None:
        real = self.root / "real"
        real.mkdir()
        (real / "f.txt").write_text("f", encoding="utf-8")
        link_root = self.root / "linkroot"
        link_root.symlink_to(real)
        with self.assertRaises(ValueError) as ctx:
            tree_manifest(link_root)
        self.assertIn("real directory", str(ctx.exception).lower())

    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_ignored_symlink_file_rejected_in_manifest(self) -> None:
        target = self.root / "real.txt"
        target.write_text("data", encoding="utf-8")
        link = self.root / "skip.link"
        link.symlink_to(target)
        with self.assertRaises(ValueError) as ctx:
            tree_manifest(self.root, ignore=("*.link",))
        self.assertIn("symlink", str(ctx.exception).lower())

    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_ignored_symlink_dir_rejected_in_manifest(self) -> None:
        real_dir = self.root / "real"
        real_dir.mkdir()
        (real_dir / "f.txt").write_text("f", encoding="utf-8")
        link = self.root / "skipdir"
        link.symlink_to(real_dir)
        with self.assertRaises(ValueError) as ctx:
            tree_manifest(self.root, ignore=("skipdir",))
        self.assertIn("symlink", str(ctx.exception).lower())


class VerifyManifestTests(TempDirTestCase):
    def test_missing_unexpected_changed_sorted(self) -> None:
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        (self.root / "b.txt").write_text("b", encoding="utf-8")
        (self.root / "c.txt").write_text("c", encoding="utf-8")
        base = tree_manifest(self.root)

        (self.root / "a.txt").write_text("changed", encoding="utf-8")
        (self.root / "b.txt").unlink()
        (self.root / "d.txt").write_text("d", encoding="utf-8")

        expected = dict(base)
        expected.pop("c.txt")  # leave c as unexpected in tree vs expected without it
        # expected has a(old), b, c — actual has a(new), c, d
        # Simpler: use base as expected after mutations above.
        errors = verify_tree_manifest(self.root, base)
        self.assertEqual(errors, sorted(errors))
        self.assertIn("hash mismatch: a.txt", errors)
        self.assertIn("missing: b.txt", errors)
        self.assertIn("unexpected: d.txt", errors)

    def test_invalid_keys_and_collection_failure(self) -> None:
        (self.root / "ok.txt").write_text("ok", encoding="utf-8")
        errors = verify_tree_manifest(
            self.root,
            {
                "../escape": "abc",
                "/abs": "abc",
                "ok.txt": "deadbeef",
            },
        )
        self.assertTrue(any(e.startswith("invalid manifest key:") for e in errors))
        self.assertIn("hash mismatch: ok.txt", errors)
        self.assertEqual(errors, sorted(errors))

        if _can_symlink():
            link = self.root / "sym"
            link.symlink_to(self.root / "ok.txt")
            # Symlink causes collection to fail closed with a deterministic error.
            fail_errors = verify_tree_manifest(self.root, {"ok.txt": "x" * 64})
            self.assertTrue(any(e.startswith("manifest collection failed:") for e in fail_errors))
            self.assertEqual(fail_errors, sorted(fail_errors))


class AtomicSnapshotTests(TempDirTestCase):
    def test_snapshot_ignore_and_manifest_equality(self) -> None:
        src = self.root / "src"
        src.mkdir()
        (src / "keep.txt").write_text("keep", encoding="utf-8")
        junk = src / "junk"
        junk.mkdir()
        (junk / "n.txt").write_text("n", encoding="utf-8")
        (src / "x.log").write_text("log", encoding="utf-8")
        dest = self.root / "dest"
        manifest = atomic_snapshot(src, dest, ignore_patterns=("junk", "*.log"))
        self.assertTrue(dest.is_dir())
        self.assertFalse((dest / "junk").exists())
        self.assertFalse((dest / "x.log").exists())
        self.assertEqual(manifest, tree_manifest(dest))
        self.assertEqual(list(manifest.keys()), ["keep.txt"])
        with self.assertRaises(ValueError):
            atomic_snapshot(src, dest)

    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_snapshot_rejects_symlinks(self) -> None:
        src = self.root / "src"
        src.mkdir()
        real = src / "real.txt"
        real.write_text("r", encoding="utf-8")
        (src / "link.txt").symlink_to(real)
        dest = self.root / "dest"
        with self.assertRaises(ValueError):
            atomic_snapshot(src, dest)
        self.assertFalse(dest.exists())

    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_snapshot_rejects_ignored_symlink_no_dest(self) -> None:
        src = self.root / "src"
        src.mkdir()
        (src / "keep.txt").write_text("k", encoding="utf-8")
        real = src / "real.txt"
        real.write_text("r", encoding="utf-8")
        (src / "skip.link").symlink_to(real)
        dest = self.root / "dest"
        with self.assertRaises(ValueError) as ctx:
            atomic_snapshot(src, dest, ignore_patterns=("*.link",))
        self.assertIn("symlink", str(ctx.exception).lower())
        self.assertFalse(dest.exists())
        self.assertFalse(os.path.lexists(dest))

    @unittest.skipUnless(_can_symlink(), "platform cannot create symlinks")
    def test_snapshot_refuses_dangling_destination_symlink(self) -> None:
        src = self.root / "src"
        src.mkdir()
        (src / "keep.txt").write_text("k", encoding="utf-8")
        dest = self.root / "dest"
        dest.symlink_to(self.root / "missing-target")
        self.assertTrue(dest.is_symlink())
        self.assertFalse(dest.exists())
        with self.assertRaises(ValueError) as ctx:
            atomic_snapshot(src, dest)
        self.assertIn("already exists", str(ctx.exception).lower())
        self.assertTrue(dest.is_symlink())
        self.assertFalse(dest.exists())
        self.assertEqual(os.readlink(dest), str(self.root / "missing-target"))

    def test_snapshot_copy_failure_cleans_temp_no_dest(self) -> None:
        src = self.root / "src"
        src.mkdir()
        (src / "f.txt").write_text("f", encoding="utf-8")
        dest = self.root / "dest"
        before = set(self.root.iterdir())
        with mock.patch("basecamp_bench.safety.shutil.copy2", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_snapshot(src, dest)
        self.assertFalse(dest.exists())
        after = set(self.root.iterdir())
        leftovers = [p for p in after - before if p.name.startswith(".dest.tmp.")]
        self.assertEqual(leftovers, [])
        # Only src should remain from our work (plus any unrelated).
        self.assertIn(src, after)
        self.assertNotIn(dest, after)


class AtomicWriteJsonTests(TempDirTestCase):
    def test_atomic_replace_and_deterministic_content(self) -> None:
        path = self.root / "data.json"
        atomic_write_json(path, {"b": 2, "a": 1})
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps({"a": 1, "b": 2}, sort_keys=True) + "\n")
        atomic_write_json(path, {"z": 0, "a": 3})
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            json.dumps({"a": 3, "z": 0}, sort_keys=True) + "\n",
        )
        # No temp leftovers.
        self.assertEqual([p.name for p in self.root.iterdir()], ["data.json"])


class PortablePathTests(TempDirTestCase):
    def test_internal_and_external(self) -> None:
        inner = self.root / "sub" / "f.txt"
        inner.parent.mkdir()
        inner.write_text("x", encoding="utf-8")
        self.assertEqual(portable_path(inner, self.root), "sub/f.txt")
        outside = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(portable_path(outside, self.root), "<external>")
        finally:
            shutil.rmtree(outside, ignore_errors=True)


class RedactTextTests(TempDirTestCase):
    def test_redacts_home_roots_secrets_and_empty(self) -> None:
        home = str(Path.home())
        root = self.root.resolve()
        secret = "super-secret-token-xyz"
        text = f"home={home} root={root} secret={secret} empty="
        redacted = redact_text(text, roots=(root,), secret_values=(secret, ""))
        self.assertIn("<home>", redacted)
        self.assertIn("<root>", redacted)
        self.assertIn("<secret>", redacted)
        self.assertNotIn(home, redacted)
        self.assertNotIn(str(root), redacted)
        self.assertNotIn(secret, redacted)
        # Empty secret must not explode the string into labels.
        self.assertNotEqual(redacted, "<secret>" * len(text))

    def test_longer_secret_first(self) -> None:
        long = "abcdefghij"
        short = "abcde"
        text = f"x={long} y={short}"
        redacted = redact_text(text, secret_values=(short, long))
        self.assertEqual(redacted, "x=<secret> y=<secret>")


if __name__ == "__main__":
    unittest.main()
