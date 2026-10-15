from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from basecamp_bench.browser_runtime import (
    BrowserRuntimeError,
    FrontendServer,
    validate_browser_evidence,
)
from basecamp_bench.safety import tree_manifest


class FrontendServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "snapshot"
        self.root.mkdir()
        (self.root / "index.html").write_text("<!doctype html><button>Click</button>")
        (self.root / "asset.css").write_text("body{color:black}")

    def test_live_readonly_runtime_and_cleanup_preserve_snapshot(self) -> None:
        before = tree_manifest(self.root)
        with FrontendServer(self.root) as server:
            url = server.url
            self.assertFalse(server.chrome_loaded())
            with urlopen(Request(url, headers={"User-Agent": "Chrome/test"}), timeout=2) as r:
                self.assertEqual(r.status, 200)
                self.assertIn(b"<button>", r.read())
            with urlopen(Request(url, method="HEAD"), timeout=2) as r:
                self.assertEqual(r.read(), b"")
            with urlopen(url.rsplit("/", 1)[0] + "/asset.css", timeout=2) as r:
                self.assertIn(b"color:black", r.read())
            self.assertTrue(server.evidence()["chrome_loaded"])
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(method=method), self.assertRaises(HTTPError) as error:
                    urlopen(Request(url, data=b"overwrite", method=method), timeout=2)
                self.assertEqual(error.exception.code, 405)
        self.assertEqual(before, tree_manifest(self.root))
        with self.assertRaises(URLError):
            urlopen(url, timeout=1)

    def test_scoped_paths_symlinks_host_and_directory_listing(self) -> None:
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret")
        (self.root / "escape.txt").symlink_to(outside)
        (self.root / "dir").mkdir()
        (self.root / "linked").symlink_to(Path(self.tmp.name), target_is_directory=True)
        with FrontendServer(self.root) as server:
            base = server.url.rsplit("/", 1)[0] + "/"
            for suffix in (
                "",
                "dir/",
                "escape.txt",
                "linked/outside.txt",
                "%2e%2e/outside.txt",
                "../outside.txt",
            ):
                with self.subTest(suffix=suffix), self.assertRaises(HTTPError) as error:
                    urlopen(base + suffix, timeout=2)
                self.assertEqual(error.exception.code, 404)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(server.url, headers={"Host": "external.example"}), timeout=2)
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError):
                urlopen(server.url.replace(server.prefix, "/wrong/"), timeout=2)

    def test_parallel_servers_have_distinct_ports_and_urls(self) -> None:
        with FrontendServer(self.root) as first, FrontendServer(self.root) as second:
            self.assertNotEqual(first.url, second.url)
            with urlopen(first.url) as one, urlopen(second.url) as two:
                self.assertEqual(one.read(), two.read())

    def test_cleanup_on_evaluator_exception(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "evaluation failed"):
            with FrontendServer(self.root) as server:
                url = server.url
                raise RuntimeError("evaluation failed")
        with self.assertRaises(URLError):
            urlopen(url, timeout=1)

    def test_entry_selection_and_missing_runtime(self) -> None:
        # A single root HTML file may have a different builder-chosen name.
        root = Path(self.tmp.name) / "alternate"
        root.mkdir()
        with self.assertRaises(BrowserRuntimeError):
            FrontendServer(root)
        (root / "prototype page.html").write_text("<h1>Prototype</h1>")
        with FrontendServer(root) as server:
            self.assertTrue(server.url.endswith("/prototype%20page.html"))
        (root / "other.html").write_text("<p>Ambiguous</p>")
        with self.assertRaises(BrowserRuntimeError):
            FrontendServer(root)


class BrowserEvidenceTests(unittest.TestCase):
    def test_requires_browser_load_screenshots_and_observed_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "browser-evidence.json"
            url = "http://127.0.0.1:1234/token/index.html"
            valid = {
                "schema_version": "1.0",
                "url": url,
                "rendered": True,
                "status": "complete",
                "screenshots": [
                    {"viewport": v, "evidence": "Screenshot captured"}
                    for v in ("desktop", "mobile")
                ],
                "interactions": [
                    {"action": a, "observed": "Product control failed", "passed": False}
                    for a in ("click", "type", "reload")
                ],
            }
            self.assertEqual(
                validate_browser_evidence(path, url=url, chrome_loaded=True),
                ["missing_browser_evidence"],
            )
            path.write_text(json.dumps(valid))
            self.assertEqual(
                validate_browser_evidence(path, url=url, chrome_loaded=False),
                ["browser_not_loaded"],
            )
            self.assertEqual(validate_browser_evidence(path, url=url, chrome_loaded=True), [])
            for change in (
                {"status": "blocked"},
                {"url": url + "wrong"},
                {"rendered": False},
                {"screenshots": []},
                {"interactions": []},
                {"screenshots": [{"viewport": [], "evidence": "bad"}]},
                {"interactions": [{"action": {}, "observed": "bad", "passed": True}]},
            ):
                with self.subTest(change=change):
                    path.write_text(json.dumps(dict(valid, **change)))
                    self.assertEqual(
                        validate_browser_evidence(path, url=url, chrome_loaded=True),
                        ["incomplete_browser_evidence"],
                    )
            path.write_text("not json")
            self.assertEqual(
                validate_browser_evidence(path, url=url, chrome_loaded=True),
                ["malformed_browser_evidence"],
            )


if __name__ == "__main__":
    unittest.main()
