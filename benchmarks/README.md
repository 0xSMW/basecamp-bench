# Benchmark inputs

Each track directory is a versioned evaluation unit:

- `prompt.md` is passed byte-for-byte to implementation agents.
- `eval.md` gives evaluator agents the complete assessment context.
- `contract.json` defines the canonical dimension IDs, anchors, weights, and
  runner-owned scoring policy.

The shared `reference-pack.json` records the source, license, distributability,
and SHA-256 digest of every file under `Repo/reference/`.
