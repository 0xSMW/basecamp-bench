# Security

## Threat model

Implementation and judge CLIs execute model-generated actions and must be treated as untrusted code. Directory separation and prompt instructions are audit aids, not security boundaries. Host credentials, environment variables, files, processes, and network access are at risk when a harness receives unrestricted permissions.

Workspace-only execution under `runs/<run-id>/workspaces` is the local default. Some vendor CLIs provide their own OS sandbox and others rely only on that directory boundary. Full host access requires an explicit configuration setting plus either `--allow-unsafe-host-execution` for local acknowledgement or `--confirmed-isolated-environment` inside an external boundary. Publication runs require the documented disposable VM/container boundary.

## Optional hardening for local runs; required for publication

- Disposable VM or container with no personal files or ambient cloud credentials.
- Read-only seed/reference mounts and one writable job workspace.
- Dedicated, least-privilege vendor credentials with spending limits.
- Explicit outbound-network allowlist.
- CPU, memory, disk, process-count, and wall-clock limits.
- No host Docker socket, SSH agent, browser profile, keychain, or home-directory mount.

The runner validates paths, hashes evidence, detects mutation, bounds captured logs, redacts portable exports, scans promoted artifacts for likely secrets and host-specific absolute paths, and terminates complete process groups. Export scans the complete captured bytes of every manifest-declared artifact before writing the archive; findings that cross internal I/O boundaries are therefore covered. JSON artifacts are parsed after UTF-8 decoding so escaped strings receive the same secret and host-path checks as literal text, and malformed JSON fails closed. Export never silently truncates or skips a large artifact. It fails closed above configurable limits of 256 MiB per artifact, 256 MiB total captured bytes, or 10,000 archive members; callers may lower or explicitly raise `max_artifact_bytes`, `max_total_bytes`, and `max_members`. Declared textual formats must be valid UTF-8. Binary artifacts such as screenshots remain exportable and are hash-verified without text decoding. Undeclared logs, workspaces, and other private files are never read by export. These controls reduce mistakes and produce evidence; they do not replace OS isolation.

Official `baseline/` releases must pass `verify-run`, portable export scanning,
an exact declared-file inventory, deterministic report regeneration, generated
snapshot inspection, and maintainer review. Commit only the unpacked portable
export; never promote raw run directories, logs, prompts, private files, or
workspaces.

See [Isolated execution](ISOLATION.md) for the disposable VM workflow and
container reference.

## Reporting vulnerabilities

Do not open a public issue containing credentials, exploit payloads, or private benchmark artifacts. Use GitHub's private vulnerability-reporting form for this repository. Include the runner version, contract hash, minimal reproduction, and impact. Rotate any credential exposed to a harness or log immediately.
