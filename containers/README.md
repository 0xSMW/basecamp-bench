# Disposable runner image

The included Dockerfile is a minimal, non-root runner image. Extend it with
only the agent CLIs you intend to benchmark and pin their versions. The base
image contains no model credentials or agent CLIs.

Build it from the repository root:

```sh
docker build --pull -f containers/Dockerfile -t basecamp-bench:local .
```

For a publication run, pass an image digest captured by your release process:

```sh
docker build --build-arg BASE_IMAGE='python:3.13-slim@sha256:<digest>' \
  -f containers/Dockerfile -t basecamp-bench:release .
```

Run with a writable output volume, explicit resource limits, and only the
credential files required by the selected harnesses:

```sh
docker run --rm --init \
  --cpus 4 --memory 12g --pids-limit 512 \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=2g \
  --mount type=volume,src=basecamp-bench-runs,dst=/bench/runs \
  basecamp-bench:local show-config
```

Real model runs require outbound network access. Apply an egress allowlist at
the host or cluster layer for the selected providers. Never mount a home
directory, Docker socket, SSH agent, browser profile, or general-purpose cloud
credential directory. A disposable VM remains the stronger boundary when an
agent CLI requires unrestricted host tools.
