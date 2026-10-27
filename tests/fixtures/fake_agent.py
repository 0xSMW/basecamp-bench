#!/usr/bin/env python3
"""Credential-free subprocess fixture for Basecamp Bench end-to-end tests."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("implement", "evaluate"), required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--last-message", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    return parser


def _emit(kind: str) -> None:
    usage = (
        {"input_tokens": 11, "cached_input_tokens": 2, "cache_write_tokens": 1, "output_tokens": 5}
        if kind == "implement"
        else {
            "input_tokens": 7,
            "cached_input_tokens": 1,
            "cache_write_tokens": 0,
            "output_tokens": 3,
        }
    )
    print(
        json.dumps(
            {
                "usage": usage,
                "reported_cost_usd": 0.012 if kind == "implement" else 0.004,
                "last_message": f"fake {kind} complete",
                "session_id": f"fake-{kind}",
            },
            sort_keys=True,
        ),
        flush=True,
    )


def _implement(args: argparse.Namespace) -> int:
    artifact = args.workdir / "artifact.py"
    artifact.write_text(
        "#!/usr/bin/env python3\nprint('fake artifact works')\n",
        encoding="utf-8",
    )
    artifact.chmod(0o755)
    (args.workdir / "index.html").write_text(
        "<!doctype html><title>fake artifact</title><main>fake artifact works</main>\n",
        encoding="utf-8",
    )
    args.last_message.parent.mkdir(parents=True, exist_ok=True)
    args.last_message.write_text("implemented\n", encoding="utf-8")
    if args.model == "fake-partial":
        _emit("implement")
        return 9
    if args.model == "fake-timeout":
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)",
            ]
        )
        (args.workdir / "grandchild.pid").write_text(str(child.pid), encoding="ascii")
        time.sleep(120)
        return 0
    _emit("implement")
    return 0


def _field(prompt: str, label: str) -> str:
    match = re.search(rf"^{re.escape(label)}:\s*(.+)$", prompt, re.MULTILINE)
    if match is None:
        raise ValueError(f"missing evaluator prompt field: {label}")
    return match.group(1).strip()


def _judge_id(prompt: str) -> str:
    match = re.search(r'"judge_id"\s*:\s*"([a-z0-9._-]+)"', prompt)
    if match is None:
        raise ValueError("missing judge_id")
    return match.group(1)


def _evaluate(args: argparse.Namespace) -> int:
    prompt = args.prompt.read_text(encoding="utf-8")
    seed = Path(_field(prompt, "Seed directory"))
    submission = Path(_field(prompt, "Submission directory"))
    report = Path(_field(prompt, "Markdown report path"))
    result = Path(_field(prompt, "Result JSON path"))
    submission_id = _field(prompt, "Opaque submission ID")
    track = _field(prompt, "Track")
    contract_match = re.search(r'"contract_sha256"\s*:\s*"([a-f0-9]{64})"', prompt)
    if contract_match is None:
        raise ValueError("missing contract_sha256")
    browser_url_match = re.search(r"^Runner-served browser URL:\s*(.+)$", prompt, re.MULTILINE)
    browser_path_match = re.search(r"^Browser evidence JSON path:\s*(.+)$", prompt, re.MULTILINE)
    browser_url = browser_url_match.group(1).strip() if browser_url_match else None
    browser_evidence = Path(browser_path_match.group(1).strip()) if browser_path_match else None
    if (browser_url is None) != (browser_evidence is None):
        raise ValueError("browser URL and evidence path must be supplied together")
    if (seed / "artifact.py").exists() or not (submission / "artifact.py").is_file():
        raise ValueError("submission delta is not isolated")
    executed = subprocess.run(
        [sys.executable, os.fspath(submission / "artifact.py")],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if executed.stdout.strip() != "fake artifact works":
        raise ValueError("artifact output mismatch")
    if browser_url is not None:
        request = Request(browser_url, headers={"User-Agent": "Chrome/test"})
        with urlopen(request, timeout=5) as response:
            if response.status != 200 or b"fake artifact" not in response.read():
                raise ValueError("runner-served frontend did not load")
        assert browser_evidence is not None
        browser_evidence.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "url": browser_url,
                    "rendered": True,
                    "status": "complete",
                    "screenshots": [
                        {"viewport": "desktop", "evidence": "fixture desktop page loaded"},
                        {"viewport": "mobile", "evidence": "fixture mobile page loaded"},
                    ],
                    "interactions": [
                        {
                            "action": action,
                            "observed": f"fixture observed {action}",
                            "passed": True,
                        }
                        for action in ("click", "type", "reload")
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    if args.model == "fake-judge-mutate-seed":
        (seed / "mutation.txt").write_text("mutated\n", encoding="utf-8")
    if args.model == "fake-judge-mutate-submission":
        (submission / "mutation.txt").write_text("mutated\n", encoding="utf-8")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Fake evaluation\n\nObserved and ran artifact.py.\n", encoding="utf-8")
    if args.model == "fake-judge-missing-result":
        _emit("evaluate")
        return 0
    if args.model == "fake-judge-malformed":
        result.write_text("{malformed", encoding="utf-8")
        _emit("evaluate")
        return 0
    score = 8.0 if args.model.endswith("-a") else 6.0
    result.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "track": track,
                "submission_id": submission_id,
                "contract_sha256": contract_match.group(1),
                "judge_id": _judge_id(prompt),
                "dimensions": {
                    "craft": {
                        "score": score,
                        "notes": "Artifact executed and produced the expected output.",
                        "evidence": ["artifact.py executed successfully"],
                    }
                },
                "summary": "Runnable implementation with directly observed behavior.",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    args.last_message.parent.mkdir(parents=True, exist_ok=True)
    args.last_message.write_text("evaluated\n", encoding="utf-8")
    _emit("evaluate")
    return 5 if args.model == "fake-judge-nonzero" else 0


def main() -> int:
    args = _parser().parse_args()
    return _implement(args) if args.kind == "implement" else _evaluate(args)


if __name__ == "__main__":
    raise SystemExit(main())
