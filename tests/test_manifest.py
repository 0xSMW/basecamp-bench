"""Unit tests for basecamp_bench.manifest (stdlib unittest only)."""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from basecamp_bench.manifest import (
    REDACTED,
    SCHEMA_VERSION,
    build_manifest,
    collect_environment,
    export_run,
    git_provenance,
    hash_inputs,
    redact_config,
    scan_secrets,
    verify_run,
    write_manifest,
)
from tests._support import TempDirTestCase
from tests._support import can_symlink as _can_symlink
from tests._support import minimal_manifest_kwargs as _minimal_manifest_kwargs
from tests._support import sha256_text as _sha256_text


class CollectEnvironmentTests(unittest.TestCase):
    def test_portable_keys_only_no_host_identity(self) -> None:
        env = collect_environment()
        self.assertIsInstance(env, dict)
        forbidden_substrings = (
            str(Path.home()),
            os.path.expanduser("~"),
        )
        # Username / hostname probes — values must not appear as env values.
        identity_candidates = []
        for key in ("USER", "LOGNAME", "USERNAME", "HOSTNAME", "HOST", "HOME"):
            val = os.environ.get(key)
            if val and len(val) > 1:
                identity_candidates.append(val)
        try:
            import getpass

            identity_candidates.append(getpass.getuser())
        except Exception:
            pass
        try:
            import socket

            identity_candidates.append(socket.gethostname())
        except Exception:
            pass

        blob = json.dumps(env, sort_keys=True)
        for forbidden in forbidden_substrings:
            if forbidden and forbidden not in ("/", "~"):
                self.assertNotIn(forbidden, blob)
        for identity in identity_candidates:
            if identity and identity not in ("/", ".", "null"):
                self.assertNotIn(identity, blob)

        forbidden_keys = {
            "username",
            "user",
            "home",
            "hostname",
            "host",
            "node",
            "login",
            "getcwd",
            "cwd",
            "path",
        }
        lowered = {k.lower() for k in env}
        self.assertTrue(forbidden_keys.isdisjoint(lowered))
        self.assertIn("python_version", env)
        self.assertIn("platform_system", env)
        self.assertIn("timezone_offset", env)
        # JSON-compatible types only.
        json.dumps(env, allow_nan=False)


class GitProvenanceTests(TempDirTestCase):
    def test_nonrepository(self) -> None:
        prov = git_provenance(self.root)
        self.assertEqual(set(prov.keys()), {"commit", "dirty", "error"})
        self.assertIsNone(prov["commit"])
        self.assertIsNone(prov["dirty"])
        self.assertIsInstance(prov["error"], str)
        self.assertTrue(prov["error"])

    def test_missing_git_executable(self) -> None:
        with mock.patch(
            "basecamp_bench.manifest.subprocess.run",
            side_effect=FileNotFoundError("git"),
        ):
            prov = git_provenance(self.root)
        self.assertIsNone(prov["commit"])
        self.assertIsNone(prov["dirty"])
        self.assertIn("git", prov["error"].lower())


class HashInputsTests(TempDirTestCase):
    def test_file_and_tree_determinism(self) -> None:
        a = self.root / "a.txt"
        a.write_text("alpha", encoding="utf-8")
        tree = self.root / "tree"
        tree.mkdir()
        (tree / "b.txt").write_text("beta", encoding="utf-8")
        sub = tree / "sub"
        sub.mkdir()
        (sub / "c.txt").write_text("gamma", encoding="utf-8")

        paths = {"file": a, "dir": tree}
        h1 = hash_inputs(paths)
        h2 = hash_inputs(paths)
        self.assertEqual(h1, h2)
        self.assertEqual(list(h1.keys()), ["dir", "file"])  # sorted names
        self.assertRegex(h1["file"], r"^[a-f0-9]{64}$")
        self.assertRegex(h1["dir"], r"^[a-f0-9]{64}$")
        self.assertEqual(h1["file"], _sha256_text("alpha"))

        # Content change changes tree hash.
        (sub / "c.txt").write_text("gamma!", encoding="utf-8")
        h3 = hash_inputs(paths)
        self.assertNotEqual(h1["dir"], h3["dir"])
        self.assertEqual(h1["file"], h3["file"])

    def test_symlink_root_rejected(self) -> None:
        if not _can_symlink():
            self.skipTest("symlinks not supported")
        target = self.root / "real.txt"
        target.write_text("x", encoding="utf-8")
        link = self.root / "link.txt"
        link.symlink_to(target)
        with self.assertRaises(ValueError) as ctx:
            hash_inputs({"x": link})
        self.assertIn("symlink", str(ctx.exception).lower())

    def test_symlink_inside_tree_rejected(self) -> None:
        if not _can_symlink():
            self.skipTest("symlinks not supported")
        tree = self.root / "tree"
        tree.mkdir()
        real = tree / "real.txt"
        real.write_text("x", encoding="utf-8")
        (tree / "link.txt").symlink_to(real)
        with self.assertRaises(ValueError) as ctx:
            hash_inputs({"t": tree})
        self.assertIn("symlink", str(ctx.exception).lower())

    def test_missing_path_rejected(self) -> None:
        with self.assertRaises(ValueError):
            hash_inputs({"missing": self.root / "nope"})


class RedactConfigTests(unittest.TestCase):
    def test_recursive_redaction_without_mutation(self) -> None:
        original = {
            "model": "gpt",
            "api_token": "sekrit",
            "nested": {
                "password": "p@ss",
                "ok": 1,
                "list": [
                    {"client_secret": "abc", "name": "x"},
                    "plain",
                ],
            },
            "AUTH_KEY": "k",
            "credential": "c",
        }
        snapshot = json.loads(json.dumps(original))
        redacted = redact_config(original)
        self.assertEqual(original, snapshot)  # no mutation
        self.assertEqual(redacted["model"], "gpt")
        self.assertEqual(redacted["api_token"], REDACTED)
        self.assertEqual(redacted["nested"]["password"], REDACTED)
        self.assertEqual(redacted["nested"]["ok"], 1)
        self.assertEqual(redacted["nested"]["list"][0]["client_secret"], REDACTED)
        self.assertEqual(redacted["nested"]["list"][0]["name"], "x")
        self.assertEqual(redacted["nested"]["list"][1], "plain")
        self.assertEqual(redacted["AUTH_KEY"], REDACTED)
        self.assertEqual(redacted["credential"], REDACTED)

    def test_explicit_secret_keys(self) -> None:
        data = {"custom_field": "hide-me", "public": "ok"}
        redacted = redact_config(data, secret_keys=("custom_field",))
        self.assertEqual(redacted["custom_field"], REDACTED)
        self.assertEqual(redacted["public"], "ok")
        self.assertEqual(data["custom_field"], "hide-me")

    def test_requires_mapping(self) -> None:
        with self.assertRaises(TypeError):
            redact_config([1, 2, 3])


class ScanSecretsTests(TempDirTestCase):
    def test_documented_credential_placeholders_are_not_secrets(self) -> None:
        (self.root / "example.md").write_text(
            "client_secret={client_secret}\napi_key=<api-key>\ntoken=${TOKEN}\n",
            encoding="utf-8",
        )
        self.assertEqual(scan_secrets(self.root), [])

    def test_risky_name_and_content_without_leaking_values(self) -> None:
        secret_value = "SUPER_SECRET_VALUE_9f3a2b1c"
        env_file = self.root / ".env"
        env_file.write_text(f"API_KEY={secret_value}\n", encoding="utf-8")
        normal = self.root / "readme.txt"
        normal.write_text("hello world\n", encoding="utf-8")
        key_file = self.root / "nested" / "note.md"
        key_file.parent.mkdir()
        key_file.write_text(
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEfake\n-----END RSA PRIVATE KEY-----\n",
            encoding="utf-8",
        )
        findings = scan_secrets(self.root)
        self.assertTrue(findings)
        blob = json.dumps(findings)
        self.assertNotIn(secret_value, blob)
        self.assertNotIn("MIIEfake", blob)
        reasons = {f["reason"] for f in findings}
        self.assertIn("risky_filename", reasons)
        self.assertTrue(
            any(
                r in reasons
                for r in ("private_key_block", "credential_assignment", "password_assignment")
            )
            or "private_key_block" in reasons
        )
        for finding in findings:
            self.assertEqual(set(finding.keys()), {"path", "line", "reason"})
            self.assertNotIn("content", finding)
            self.assertNotIn("match", finding)

    def test_symlink_reported_not_followed(self) -> None:
        if not _can_symlink():
            self.skipTest("symlinks not supported")
        outside = self.root / "outside_secret"
        outside.write_text("password=should-not-read\n", encoding="utf-8")
        link = self.root / "link-secret"
        link.symlink_to(outside)
        findings = scan_secrets(self.root)
        self.assertTrue(any(f["reason"] == "symlink_rejected" for f in findings))
        # Must not report content findings for the linked file via the link.
        content_on_link = [
            f for f in findings if f["path"] == "link-secret" and f["reason"] != "symlink_rejected"
        ]
        self.assertEqual(content_on_link, [])

    def test_large_file_is_scanned_to_the_end(self) -> None:
        payload = (b"clean\n" * 200_000) + b"auth_token=abc1234567890\n"
        self.assertGreater(len(payload), 1024 * 1024)
        (self.root / "large.txt").write_bytes(payload)
        findings = scan_secrets(self.root)
        self.assertTrue(any(item["reason"] == "credential_assignment" for item in findings))


class BuildAndWriteManifestTests(TempDirTestCase):
    def test_exact_root_and_redacted_config(self) -> None:
        manifest = build_manifest(**_minimal_manifest_kwargs())
        self.assertEqual(
            set(manifest.keys()),
            {
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
            },
        )
        self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["config"]["mode"], "local")
        self.assertEqual(manifest["run"]["id"], "run-test-001")
        self.assertEqual(manifest["runner"]["version"], "1.0.0a1")
        self.assertIn("python_version", manifest["environment"])
        self.assertEqual(
            manifest["costs"],
            {
                "known_implementation_usd": 0.1,
                "known_evaluation_usd": 0.0,
                "known_total_usd": 0.1,
                "complete": True,
                "unknown_job_count": 0,
            },
        )

    def test_cost_summary_includes_evaluators_and_surfaces_unknown_costs(self) -> None:
        jobs = list(_minimal_manifest_kwargs()["jobs"])
        evaluation = dict(jobs[0])
        evaluation.update({"id": "job-2", "kind": "evaluate", "cost_usd": 0.025})
        unknown = dict(evaluation)
        unknown.update({"id": "job-3", "cost_usd": None})
        manifest = build_manifest(**_minimal_manifest_kwargs(jobs=[*jobs, evaluation, unknown]))
        self.assertEqual(manifest["costs"]["known_implementation_usd"], 0.1)
        self.assertEqual(manifest["costs"]["known_evaluation_usd"], 0.025)
        self.assertEqual(manifest["costs"]["known_total_usd"], 0.125)
        self.assertFalse(manifest["costs"]["complete"])
        self.assertEqual(manifest["costs"]["unknown_job_count"], 1)

    def test_cost_summary_excludes_reused_implementation(self) -> None:
        reused = dict(_minimal_manifest_kwargs()["jobs"][0])
        reused["command_preview"] = "reuse prior_run=prior snapshot_tree_sha256=abc"
        manifest = build_manifest(**_minimal_manifest_kwargs(jobs=[reused]))
        self.assertEqual(manifest["costs"]["known_total_usd"], 0.0)
        self.assertTrue(manifest["costs"]["complete"])

    def test_invalid_status_and_hashes(self) -> None:
        with self.assertRaises(ValueError):
            build_manifest(**_minimal_manifest_kwargs(status="done"))
        with self.assertRaises(ValueError):
            build_manifest(**_minimal_manifest_kwargs(inputs={"x": "not-a-hash"}))
        with self.assertRaises(ValueError):
            build_manifest(**_minimal_manifest_kwargs(artifacts={"../escape": _sha256_text("x")}))
        with self.assertRaisesRegex(ValueError, "private run area"):
            build_manifest(
                **_minimal_manifest_kwargs(artifacts={"logs/private.log": _sha256_text("x")})
            )

    def test_tooling_shape_is_strict(self) -> None:
        kwargs = _minimal_manifest_kwargs()
        tooling = [dict(kwargs["tooling"][0])]
        tooling[0]["extra"] = True
        with self.assertRaisesRegex(ValueError, "tooling"):
            build_manifest(**_minimal_manifest_kwargs(tooling=tooling))
        tooling = [dict(kwargs["tooling"][0])]
        tooling[0]["deterministic_seed"] = {"supported": True, "limitation": "none"}
        with self.assertRaisesRegex(ValueError, "supported must be false"):
            build_manifest(**_minimal_manifest_kwargs(tooling=tooling))
        tooling = [dict(kwargs["tooling"][0])]
        tooling[0]["version_error"] = "also set"
        with self.assertRaisesRegex(ValueError, "exactly one"):
            build_manifest(**_minimal_manifest_kwargs(tooling=tooling))

    def test_absolute_pricing_cache_path_is_made_portable(self) -> None:
        kwargs = _minimal_manifest_kwargs()
        pricing = dict(kwargs["pricing"])
        pricing["cache_path"] = "/Users/example/.pricing-cache.json"
        manifest = build_manifest(**_minimal_manifest_kwargs(pricing=pricing))
        self.assertEqual(manifest["pricing"]["cache_path"], ".pricing-cache.json")

    def test_atomic_deterministic_write(self) -> None:
        manifest = build_manifest(**_minimal_manifest_kwargs())
        path = self.root / "run-manifest.json"
        write_manifest(path, manifest)
        write_manifest(path, manifest)
        text1 = path.read_text(encoding="utf-8")
        write_manifest(path, manifest)
        text2 = path.read_text(encoding="utf-8")
        self.assertEqual(text1, text2)
        self.assertTrue(text1.endswith("\n"))
        # sorted keys → schema_version before status alphabetically among roots
        loaded = json.loads(text1)
        self.assertEqual(loaded, json.loads(json.dumps(manifest, sort_keys=True)))
        # Pretty indentation present.
        self.assertIn('\n  "', text1)

    def test_write_refuses_symlink_destination(self) -> None:
        if not _can_symlink():
            self.skipTest("symlinks not supported")
        real = self.root / "real.json"
        real.write_text("{}\n", encoding="utf-8")
        link = self.root / "link.json"
        link.symlink_to(real)
        with self.assertRaises(ValueError):
            write_manifest(link, {"a": 1})


class VerifyRunTests(TempDirTestCase):
    def _seed_run(
        self,
        *,
        run_name: str = "run",
        artifact_text: str = "hello",
        mutate_manifest: object | None = None,
        skip_artifact: bool = False,
        artifact_rel: str = "results/out.txt",
    ) -> Path:
        run_dir = self.root / run_name
        run_dir.mkdir()
        art = run_dir.joinpath(*artifact_rel.split("/"))
        art.parent.mkdir(parents=True, exist_ok=True)
        if not skip_artifact:
            art.write_text(artifact_text, encoding="utf-8")
        digest = _sha256_text(artifact_text)
        manifest = build_manifest(
            **_minimal_manifest_kwargs(
                inputs={"seed": digest},
                artifacts={artifact_rel: digest},
            )
        )
        if mutate_manifest is not None:
            if callable(mutate_manifest):
                mutate_manifest(manifest)
            elif isinstance(mutate_manifest, dict):
                manifest.update(mutate_manifest)
        write_manifest(run_dir / "run-manifest.json", manifest)
        return run_dir

    def test_happy_path(self) -> None:
        run_dir = self._seed_run()
        # Undeclared files are ignored.
        (run_dir / "noise.log").write_text("ignore me", encoding="utf-8")
        self.assertEqual(verify_run(run_dir), [])

    def test_legacy_schema_1_manifest_without_cost_summary_remains_valid(self) -> None:
        run_dir = self._seed_run()
        path = run_dir / "run-manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["costs"]
        path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(verify_run(run_dir), [])

    def test_malformed_job_costs_report_errors_without_crashing(self) -> None:
        for index, invalid_cost in enumerate(("oops", {}, 10**400), start=1):
            with self.subTest(invalid_cost=type(invalid_cost).__name__):
                run_dir = self._seed_run(
                    run_name=f"run-invalid-cost-{index}",
                    artifact_rel=f"results/out-{index}.txt",
                )
                path = run_dir / "run-manifest.json"
                data = json.loads(path.read_text(encoding="utf-8"))
                data["jobs"][0]["cost_usd"] = invalid_cost
                path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
                errors = verify_run(run_dir)
                self.assertTrue(any("cost_usd" in error for error in errors))

    def test_missing_manifest(self) -> None:
        run_dir = self.root / "empty"
        run_dir.mkdir()
        errors = verify_run(run_dir)
        self.assertTrue(any("missing run-manifest" in e for e in errors))

    def test_hash_mismatch_and_missing_artifact(self) -> None:
        run_dir = self._seed_run(artifact_text="v1")
        art = run_dir / "results" / "out.txt"
        art.write_text("mutated", encoding="utf-8")
        errors = verify_run(run_dir)
        self.assertTrue(any(e.startswith("hash mismatch:") for e in errors))

        run_dir2 = self.root / "run-missing"
        run_dir2.mkdir()
        digest = _sha256_text("gone")
        manifest = build_manifest(
            **_minimal_manifest_kwargs(
                inputs={"seed": digest},
                artifacts={"results/out.txt": digest},
            )
        )
        write_manifest(run_dir2 / "run-manifest.json", manifest)
        errors2 = verify_run(run_dir2)
        self.assertTrue(any(e.startswith("missing artifact:") for e in errors2))

    def test_absolute_and_traversal_paths_rejected(self) -> None:
        run_dir = self.root / "run-trav"
        run_dir.mkdir()
        digest = _sha256_text("x")
        # Build valid then poke unsafe paths into the on-disk JSON.
        manifest = build_manifest(
            **_minimal_manifest_kwargs(
                inputs={"seed": digest},
                artifacts={"ok.txt": digest},
            )
        )
        (run_dir / "ok.txt").write_text("x", encoding="utf-8")
        write_manifest(run_dir / "run-manifest.json", manifest)
        raw = json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))
        raw["artifacts"] = {
            "/etc/passwd": digest,
            "../outside": digest,
            "foo/../../etc/passwd": digest,
            "a\\b": digest,
            "C:/Windows/system.ini": digest,
            "": digest,
            ".": digest,
        }
        (run_dir / "run-manifest.json").write_text(
            json.dumps(raw, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        errors = verify_run(run_dir)
        self.assertTrue(any("invalid artifact path" in e for e in errors))
        self.assertGreaterEqual(sum(1 for e in errors if "invalid artifact path" in e), 4)

    def test_artifact_symlink_and_component_rejection(self) -> None:
        if not _can_symlink():
            self.skipTest("symlinks not supported")
        run_dir = self.root / "run-sym"
        run_dir.mkdir()
        real = run_dir / "real.txt"
        real.write_text("payload", encoding="utf-8")
        digest = _sha256_text("payload")
        link = run_dir / "link.txt"
        link.symlink_to(real)
        manifest = build_manifest(
            **_minimal_manifest_kwargs(
                inputs={"seed": digest},
                artifacts={"link.txt": digest},
            )
        )
        write_manifest(run_dir / "run-manifest.json", manifest)
        errors = verify_run(run_dir)
        self.assertTrue(any("symlink" in e for e in errors))

        # Symlink as intermediate directory component.
        run_dir2 = self.root / "run-sym-comp"
        run_dir2.mkdir()
        target_dir = run_dir2 / "target"
        target_dir.mkdir()
        (target_dir / "file.txt").write_text("payload", encoding="utf-8")
        (run_dir2 / "via").symlink_to(target_dir)
        manifest2 = build_manifest(
            **_minimal_manifest_kwargs(
                inputs={"seed": digest},
                artifacts={"via/file.txt": digest},
            )
        )
        write_manifest(run_dir2 / "run-manifest.json", manifest2)
        errors2 = verify_run(run_dir2)
        self.assertTrue(any("symlink" in e for e in errors2))

    def test_exact_root_validation(self) -> None:
        run_dir = self._seed_run()
        path = run_dir / "run-manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["extra_root"] = True
        del data["pricing"]
        path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        errors = verify_run(run_dir)
        self.assertTrue(any("unexpected field: extra_root" in e for e in errors))
        self.assertTrue(any("missing required field: pricing" in e for e in errors))

    def test_nested_required_fields_and_numeric_types(self) -> None:
        run_dir = self._seed_run()
        path = run_dir / "run-manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["runner"]["version"]
        data["jobs"][0]["duration_s"] = -1
        data["config"]["run_root"] = "/absolute/runs"
        path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        errors = verify_run(run_dir)
        self.assertTrue(any("runner missing required field: version" in e for e in errors))
        self.assertTrue(any("duration_s" in e for e in errors))
        self.assertTrue(any("config.run_root" in e for e in errors))

    def test_nonfinite_json_number_rejected(self) -> None:
        run_dir = self._seed_run()
        path = run_dir / "run-manifest.json"
        text = path.read_text(encoding="utf-8").replace('"duration_s": 1.0', '"duration_s": NaN')
        path.write_text(text, encoding="utf-8")
        self.assertTrue(any("non-finite" in error for error in verify_run(run_dir)))

    def test_malformed_json(self) -> None:
        run_dir = self.root / "bad"
        run_dir.mkdir()
        (run_dir / "run-manifest.json").write_text("{not-json", encoding="utf-8")
        errors = verify_run(run_dir)
        self.assertTrue(any("malformed JSON" in e for e in errors))


class ExportRunTests(TempDirTestCase):
    def _ready_run(self) -> Path:
        run_dir = self.root / "run"
        run_dir.mkdir()
        art_dir = run_dir / "results"
        art_dir.mkdir()
        (art_dir / "out.txt").write_text("hello", encoding="utf-8")
        digest = _sha256_text("hello")
        # Keep config free of secret-like *filenames* under run_dir.
        manifest = build_manifest(
            **_minimal_manifest_kwargs(
                inputs={"seed": digest},
                artifacts={"results/out.txt": digest},
            )
        )
        write_manifest(run_dir / "run-manifest.json", manifest)
        # Extra undeclared file must not appear in the archive.
        (run_dir / "scratch.tmp").write_text("nope", encoding="utf-8")
        return run_dir

    def _replace_artifacts(self, run_dir: Path, artifacts: dict[str, bytes]) -> None:
        manifest_path = run_dir / "run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"] = {}
        for rel, payload in artifacts.items():
            path = run_dir.joinpath(*rel.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            manifest["artifacts"][rel] = hashlib.sha256(payload).hexdigest()
        write_manifest(manifest_path, manifest)

    def test_deterministic_bytes_across_two_exports(self) -> None:
        run_dir = self._ready_run()
        out1 = self.root / "a.zip"
        out2 = self.root / "b.zip"
        export_run(run_dir, out1)
        export_run(run_dir, out2)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())
        # Only declared members.
        import zipfile

        with zipfile.ZipFile(out1, "r") as zf:
            names = sorted(zf.namelist())
        self.assertEqual(names, ["results/out.txt", "run-manifest.json"])

    def test_existing_output_collision(self) -> None:
        run_dir = self._ready_run()
        out = self.root / "out.zip"
        out.write_bytes(b"preexisting")
        with self.assertRaises(ValueError) as ctx:
            export_run(run_dir, out)
        self.assertIn("already exists", str(ctx.exception).lower())

    def test_rejects_failed_verification(self) -> None:
        run_dir = self._ready_run()
        (run_dir / "results" / "out.txt").write_text("mutated", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            export_run(run_dir, self.root / "x.zip")
        self.assertIn("verification failed", str(ctx.exception).lower())

    def test_rejects_secret_findings(self) -> None:
        run_dir = self._ready_run()
        secret = "API_KEY=abc1234567890\n"
        (run_dir / ".env").write_text(secret, encoding="utf-8")
        manifest_path = run_dir / "run-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"][".env"] = _sha256_text(secret)
        write_manifest(manifest_path, manifest)
        with self.assertRaises(ValueError) as ctx:
            export_run(run_dir, self.root / "y.zip")
        msg = str(ctx.exception).lower()
        self.assertIn("secret", msg)
        self.assertNotIn("abc1234567890", msg)

    def test_rejects_secret_after_one_mib_and_across_io_boundary(self) -> None:
        run_dir = self._ready_run()
        boundary = 1024 * 1024
        prefix = b"x\n" + (b" " * (boundary - len(b"API_") - 2))
        payload = prefix + b"API_KEY=abc1234567890\n" + (b"y" * 4096)
        self._replace_artifacts(run_dir, {"results/large.txt": payload})

        with self.assertRaisesRegex(ValueError, "credential_assignment"):
            export_run(run_dir, self.root / "large-secret.zip")

    def test_large_clean_declared_text_is_exportable(self) -> None:
        run_dir = self._ready_run()
        payload = (b"clean benchmark evidence\n" * 100_000) + b"complete\n"
        self.assertGreater(len(payload), 1024 * 1024)
        self._replace_artifacts(run_dir, {"results/large.txt": payload})

        out = self.root / "large-clean.zip"
        export_run(run_dir, out)
        self.assertTrue(out.is_file())

    def test_configured_export_scan_limit_fails_closed(self) -> None:
        run_dir = self._ready_run()
        payload = b"clean evidence that is intentionally over the test limit\n"
        self._replace_artifacts(run_dir, {"results/evidence.txt": payload})
        with self.assertRaisesRegex(ValueError, "exceeds configured size limit"):
            export_run(run_dir, self.root / "over-limit.zip", max_artifact_bytes=16)

    def test_export_scan_limit_must_be_positive_integer(self) -> None:
        run_dir = self._ready_run()
        for value in (0, -1, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    export_run(
                        run_dir,
                        self.root / f"invalid-limit-{value}.zip",
                        max_artifact_bytes=value,
                    )

    def test_cumulative_limits_must_be_positive_integers(self) -> None:
        run_dir = self._ready_run()
        for field in ("max_total_bytes", "max_members"):
            for value in (0, -1, True):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, "positive integer"):
                        export_run(
                            run_dir,
                            self.root / f"invalid-{field}-{value}.zip",
                            **{field: value},
                        )

    def test_rejects_host_home_paths_in_evaluator_reports_and_results(self) -> None:
        cases = {
            "evaluator-reports/eval.md": b"Evidence: /Users/alice/private/repo/file.py\n",
            "results/judge-result.json": b'{"evidence":"/home/alice/work/result.json"}\n',
            "results/root.json": b'{"evidence":"/root/bench/result.json"}\n',
            "results/temp.json": b'{"evidence":"/private/var/folders/aa/private/result"}\n',
        }
        for index, (rel, payload) in enumerate(cases.items()):
            with self.subTest(rel=rel):
                run_dir = self.root / f"host-path-{index}"
                run_dir.mkdir()
                (run_dir / "results").mkdir()
                (run_dir / "results" / "out.txt").write_text("hello", encoding="utf-8")
                manifest = build_manifest(**_minimal_manifest_kwargs())
                write_manifest(run_dir / "run-manifest.json", manifest)
                self._replace_artifacts(run_dir, {rel: payload})
                with self.assertRaisesRegex(ValueError, "host_absolute_path"):
                    export_run(run_dir, self.root / f"host-path-{index}.zip")

    def test_rejects_windows_drive_and_unc_paths(self) -> None:
        cases = (
            b"C:\\Users\\alice\\private\\report.json\n",
            b"\\\\server\\private-share\\result.json\n",
            b"//server/private-share/result.json\n",
            b'{"path":"C:\\\\Users\\\\alice\\\\private\\\\report.json"}\n',
        )
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                run_dir = self.root / f"windows-path-{index}"
                run_dir.mkdir()
                (run_dir / "results").mkdir()
                (run_dir / "results" / "out.txt").write_text("hello", encoding="utf-8")
                write_manifest(
                    run_dir / "run-manifest.json",
                    build_manifest(**_minimal_manifest_kwargs()),
                )
                self._replace_artifacts(run_dir, {"results/judge-result.json": payload})
                with self.assertRaisesRegex(ValueError, "host_absolute_path"):
                    export_run(run_dir, self.root / f"windows-path-{index}.zip")

    def test_allows_javascript_regexes_and_macos_application_paths(self) -> None:
        run_dir = self._ready_run()
        payload = (
            b'let source = template.replace(/[|\\\\()[\\]^$+*?.]/g, "\\\\$&");\n'
            b"binary=//Applications/Example.app/Contents/MacOS/example\n"
        )
        self._replace_artifacts(run_dir, {"results/evidence.js": payload})
        out = self.root / "portable-system-paths.zip"
        export_run(run_dir, out)
        self.assertTrue(out.is_file())

    def test_rejects_json_escaped_host_paths(self) -> None:
        cases = (
            b'{"evidence":"\\/Users\\/alice\\/private\\/result.json"}\n',
            b'{"evidence":"\\u002fhome\\u002falice\\u002fprivate\\u002fresult.json"}\n',
            b'{"evidence":"\\u0043:\\u005cUsers\\u005calice\\u005cresult.json"}\n',
            b'{"evidence":"\\u005c\\u005cserver\\u005cshare\\u005cresult.json"}\n',
        )
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                run_dir = self.root / f"escaped-host-{index}"
                run_dir.mkdir()
                (run_dir / "results").mkdir()
                (run_dir / "results" / "out.txt").write_text("hello", encoding="utf-8")
                write_manifest(
                    run_dir / "run-manifest.json",
                    build_manifest(**_minimal_manifest_kwargs()),
                )
                self._replace_artifacts(run_dir, {"results/judge-result.json": payload})
                with self.assertRaisesRegex(ValueError, "host_absolute_path"):
                    export_run(run_dir, self.root / f"escaped-host-{index}.zip")

    def test_rejects_json_escaped_secrets_and_credential_assignments(self) -> None:
        cases = (
            b'{"api\\u005fkey":"abc1234567890"}\n',
            b'{"value":"\\u0073\\u006b-abcdefghijklmnopqrstuv"}\n',
        )
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                run_dir = self.root / f"escaped-secret-{index}"
                run_dir.mkdir()
                (run_dir / "results").mkdir()
                (run_dir / "results" / "out.txt").write_text("hello", encoding="utf-8")
                write_manifest(
                    run_dir / "run-manifest.json",
                    build_manifest(**_minimal_manifest_kwargs()),
                )
                self._replace_artifacts(run_dir, {"results/judge-result.json": payload})
                with self.assertRaisesRegex(
                    ValueError,
                    "credential_assignment|api_secret_token",
                ):
                    export_run(run_dir, self.root / f"escaped-secret-{index}.zip")

    def test_malformed_declared_json_fails_closed(self) -> None:
        run_dir = self._ready_run()
        self._replace_artifacts(run_dir, {"results/judge-result.json": b'{"broken": true\n'})
        with self.assertRaisesRegex(ValueError, "malformed_json"):
            export_run(run_dir, self.root / "malformed-json.zip")

    def test_cumulative_byte_limit_and_exact_boundary(self) -> None:
        run_dir = self._ready_run()
        artifacts = {
            f"results/part-{index}.txt": (f"clean-{index}".encode("ascii") * 8)
            for index in range(4)
        }
        self._replace_artifacts(run_dir, artifacts)
        exact_total = (run_dir / "run-manifest.json").stat().st_size + sum(
            len(payload) for payload in artifacts.values()
        )
        self.assertTrue(all(len(payload) < exact_total for payload in artifacts.values()))

        with self.assertRaisesRegex(ValueError, "configured total limit"):
            export_run(
                run_dir,
                self.root / "aggregate-over.zip",
                max_artifact_bytes=exact_total,
                max_total_bytes=exact_total - 1,
            )
        export_run(
            run_dir,
            self.root / "aggregate-exact.zip",
            max_artifact_bytes=exact_total,
            max_total_bytes=exact_total,
        )

    def test_member_limit_and_exact_boundary(self) -> None:
        run_dir = self._ready_run()
        artifacts = {f"results/part-{index}.txt": b"clean" for index in range(3)}
        self._replace_artifacts(run_dir, artifacts)
        exact_members = 1 + len(artifacts)
        with self.assertRaisesRegex(ValueError, "member count"):
            export_run(run_dir, self.root / "members-over.zip", max_members=exact_members - 1)
        export_run(run_dir, self.root / "members-exact.zip", max_members=exact_members)

    def test_documented_placeholder_paths_and_urls_are_allowed(self) -> None:
        run_dir = self._ready_run()
        payload = (
            b"Examples: /Users/... /Users/<user>/repo /home/{user}/repo "
            b"https://example.test/server/share/file\n"
        )
        self._replace_artifacts(run_dir, {"results/notes.md": payload})
        export_run(run_dir, self.root / "placeholder-paths.zip")

    def test_binary_declared_artifact_is_hash_verified_and_exportable(self) -> None:
        run_dir = self._ready_run()
        payload = b"\x89PNG\r\n\x1a\n\x00\xff\xfe" + b"/Users/alice/private" + bytes(range(256))
        self._replace_artifacts(run_dir, {"submissions/screenshot.png": payload})
        out = self.root / "binary.zip"
        export_run(run_dir, out)
        import zipfile

        with zipfile.ZipFile(out) as archive:
            self.assertEqual(archive.read("submissions/screenshot.png"), payload)

    def test_binary_artifact_cannot_hide_a_high_signal_secret(self) -> None:
        run_dir = self._ready_run()
        payload = b"\x89PNG\r\n\x1a\n\x00\xff" + b"auth_token=abc1234567890\n"
        self._replace_artifacts(run_dir, {"submissions/screenshot.png": payload})
        with self.assertRaisesRegex(ValueError, "credential_assignment"):
            export_run(run_dir, self.root / "binary-secret.zip")

    def test_undecodable_declared_text_fails_closed(self) -> None:
        run_dir = self._ready_run()
        self._replace_artifacts(run_dir, {"results/judge-result.json": b"{\xff}\n"})
        with self.assertRaisesRegex(ValueError, "undecodable_text"):
            export_run(run_dir, self.root / "undecodable.zip")

    def test_undeclared_private_secret_is_never_read_or_exported(self) -> None:
        run_dir = self._ready_run()
        private = run_dir / "logs" / "agent.log"
        private.parent.mkdir()
        private.write_text("API_KEY=abc1234567890\n", encoding="utf-8")
        out = self.root / "public.zip"
        export_run(run_dir, out)
        import zipfile

        with zipfile.ZipFile(out) as archive:
            self.assertNotIn("logs/agent.log", archive.namelist())


class PublicationBoundaryArchitectureTests(unittest.TestCase):
    """Prove local core imports stay free of ZIP/export/shareability code."""

    def test_runner_and_reporting_do_not_load_publication_implementation(self) -> None:
        import subprocess
        import sys

        for module in ("basecamp_bench.runner", "basecamp_bench.reporting"):
            with self.subTest(module=module):
                script = (
                    "import sys\n"
                    f"import {module}\n"
                    "blocked = [\n"
                    '    name for name in ("basecamp_bench.manifest_export", "zipfile")\n'
                    "    if name in sys.modules\n"
                    "]\n"
                    "raise SystemExit(0 if not blocked else f'loaded: {blocked}')\n"
                )
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_manifest_import_does_not_load_export_module(self) -> None:
        import subprocess
        import sys

        script = (
            "import sys\n"
            "import basecamp_bench.manifest\n"
            "assert 'basecamp_bench.manifest_export' not in sys.modules\n"
            "assert 'zipfile' not in sys.modules\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_public_facades_delegate_to_publication_module(self) -> None:
        import basecamp_bench.manifest as provenance
        import basecamp_bench.manifest_export as publication

        self.assertEqual(provenance.verify_run.__module__, "basecamp_bench.manifest")
        self.assertEqual(publication.verify_run.__module__, "basecamp_bench.manifest_export")
        self.assertEqual(provenance.export_run.__module__, "basecamp_bench.manifest")
        self.assertEqual(publication.export_run.__module__, "basecamp_bench.manifest_export")
        self.assertEqual(provenance.scan_secrets.__module__, "basecamp_bench.manifest")
        self.assertEqual(publication.scan_secrets.__module__, "basecamp_bench.manifest_export")


class BuildManifestGitIntegrationTests(TempDirTestCase):
    def test_repo_none_leaves_null_git_fields(self) -> None:
        m = build_manifest(**_minimal_manifest_kwargs(repo=None))
        self.assertIsNone(m["runner"]["commit"])
        self.assertIsNone(m["runner"]["dirty"])
        self.assertIsNone(m["runner"]["error"])

    def test_repo_non_git_records_error(self) -> None:
        m = build_manifest(**_minimal_manifest_kwargs(repo=self.root))
        self.assertIsNone(m["runner"]["commit"])
        self.assertIsNotNone(m["runner"]["error"])

    def test_explicit_runner_git_uses_frozen_provenance(self) -> None:
        frozen = {"commit": "b" * 40, "dirty": False, "error": None}
        with mock.patch("basecamp_bench.manifest.git_provenance") as probe:
            manifest = build_manifest(**_minimal_manifest_kwargs(repo=None, runner_git=frozen))
        probe.assert_not_called()
        self.assertEqual(manifest["runner"], {"version": "1.0.0a1", **frozen})

    def test_repo_and_explicit_runner_git_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            build_manifest(
                **_minimal_manifest_kwargs(
                    repo=self.root,
                    runner_git={"commit": "b" * 40, "dirty": False, "error": None},
                )
            )


if __name__ == "__main__":
    unittest.main()
