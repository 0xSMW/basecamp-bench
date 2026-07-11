# Isolated execution

Agent CLIs execute model-directed commands and generated code. Run paid or
publication benchmarks in a disposable VM with no personal data, ambient
credentials, or access to production systems.

## Reference boundary

- Start from a newly created VM or dedicated CI worker.
- Install pinned Python and agent-CLI versions.
- Clone the benchmark into a dedicated unprivileged account.
- Supply short-lived, spend-limited credentials for selected providers only.
- Allow outbound traffic only to those provider endpoints and required package
  registries; disable inbound traffic.
- Limit CPU, memory, disk, process count, and wall-clock time outside the
  runner.
- Export only with `basecamp-bench export-run`; copy the resulting archive out,
  then destroy the VM.

The container recipe in [`containers/`](../containers/) provides a repeatable
non-root base and resource-limit example. Containers share the host kernel, so
use a disposable VM around the container for publication runs or any harness
that needs broad permissions.

Directory separation, prompt instructions, environment allowlists, snapshot
hashes, and mutation checks are integrity controls. They do not provide an OS
security boundary.
