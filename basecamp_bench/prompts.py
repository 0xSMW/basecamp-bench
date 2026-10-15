"""Prompt assembly with a strict builder/evaluator information boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def implementation_prompt_bytes(path: Path) -> bytes:
    """Return the user-authored implementation directive byte-for-byte."""
    prompt = Path(path)
    if prompt.is_symlink() or not prompt.is_file():
        raise ValueError(f"implementation prompt must be a regular file: {prompt}")
    data = prompt.read_bytes()
    if not data.strip():
        raise ValueError(f"implementation prompt is empty: {prompt}")
    return data


def build_evaluator_prompt(
    *,
    track: str,
    submission_id: str,
    evaluator_id: str,
    contract_sha256: str,
    contract: Mapping[str, Any],
    rubric: str,
    seed_dir: Path,
    submission_dir: Path,
    report_path: Path,
    result_path: Path,
    browser_url: str | None = None,
    browser_evidence_path: Path | None = None,
) -> str:
    """Build the evaluator directive (evidence boundary + exact result contract)."""
    if (browser_url is None) != (browser_evidence_path is None):
        raise ValueError("browser_url and browser_evidence_path must be supplied together")
    if browser_url is not None:
        if track != "fe":
            raise ValueError("browser evidence is supported only for the fe track")
        parsed_url = urlsplit(browser_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("browser_url must be an absolute HTTP URL")
    dimensions = contract.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        raise ValueError("contract.dimensions must be a nonempty list")
    dimension_ids: list[str] = []
    for index, dimension in enumerate(dimensions):
        if not isinstance(dimension, Mapping) or not isinstance(dimension.get("id"), str):
            raise ValueError(f"contract.dimensions[{index}].id must be a string")
        dimension_ids.append(dimension["id"])

    dim_placeholder = {
        "score": "number from 0 through 10",
        "notes": "concise evidence-backed assessment",
        "evidence": ["specific observed evidence"],
    }
    result_shape = {
        "schema_version": "1.0",
        "track": track,
        "submission_id": submission_id,
        "contract_sha256": contract_sha256,
        "judge_id": evaluator_id,
        "dimensions": {dim_id: dict(dim_placeholder) for dim_id in dimension_ids},
        "summary": "concise overall assessment",
    }

    browser_directive = ""
    output_boundary = "write only to the two output paths"
    path_rule = "in either output"
    result_condition = ""
    if browser_url is not None:
        assert browser_evidence_path is not None
        output_boundary = "write only to the three output paths"
        path_rule = "in any output"
        result_condition = "Only after successful browser verification, "
        browser_shape = {
            "schema_version": "1.0",
            "url": browser_url,
            "rendered": True,
            "status": "complete",
            "screenshots": [
                {"viewport": "desktop", "evidence": "specific screenshot/tool observation"},
                {"viewport": "mobile", "evidence": "specific screenshot/tool observation"},
            ],
            "interactions": [
                {"action": action, "observed": "specific observation", "passed": True}
                for action in ("click", "type", "reload")
            ],
        }
        browser_directive = (
            f"Browser evidence JSON path: {Path(browser_evidence_path)}\n"
            f"Runner-served browser URL: {browser_url}\n\n"
            "The runner serves the immutable submission snapshot at this exact HTTP URL. "
            "Do not start another server, use a headless browser, or open browser file URLs. "
            "You must use cua_repl with Chrome to open the supplied URL and inspect the served document "
            "before scoring. Read the current CUA documentation and bind the Chrome browser to discover "
            "its viewport capabilities; do not guess API methods. Source inspection and simulated DOM "
            "tests are not browser evidence. rendered=true means the actual served document was inspected "
            "in Chrome, not that the product works. A blank page or JavaScript error caused by the submission "
            "is a product defect: capture the actual page/error and score it with status 'complete'. "
            "Capture screenshots at desktop (approximately 1440px wide) and mobile (approximately "
            "390px wide) viewports. Exercise click, type, and reload actions in the rendered app; "
            "record specific observed outcomes, including failed product controls. A failed interaction "
            "is valid evidence: set passed to false and describe what actually happened. Include at "
            "least one interaction for each of click, type, and reload. If a needed control or input is "
            "absent, record the attempted action and the observed absence with passed=false.\n\n"
            "Only if an environmental browser/tool/setup failure prevents Chrome from loading or inspecting "
            "the served document, write browser evidence with schema_version '1.0', "
            "the exact supplied url, rendered false, status 'blocked', and a blocker field containing "
            "specific observed failure evidence. Keep a useful Markdown evidence report, then STOP "
            "without writing numerical result.json. Do not substitute source-only visual scores.\n\n"
            "After successful browser verification, write browser-evidence.json to the browser evidence "
            "path with this shape; preserve the exact URL and replace descriptive placeholders with "
            "actual observations and boolean passed values:\n\n"
            + json.dumps(browser_shape, indent=2, ensure_ascii=False)
            + "\n\n"
        )

    return (
        "Evaluate this submission against the complete rubric and contract below. "
        "Inspect its full delta from the seed, run it, and test it deeply enough to support every score. "
        "Choose the appropriate runtime, commands, tools, and test strategy from the implementation itself. "
        f"Treat the seed and submission directories as immutable evidence and {output_boundary}.\n\n"
        f"Track: {track}\n"
        f"Opaque submission ID: {submission_id}\n"
        f"Seed directory: {Path(seed_dir)}\n"
        f"Submission directory: {Path(submission_dir)}\n"
        f"Markdown report path: {Path(report_path)}\n"
        f"Result JSON path: {Path(result_path)}\n\n"
        "Score every contract dimension independently from direct evidence. Distinguish absent, present, "
        "stubbed, working, persistent, and production-shaped behavior where relevant. Do not infer credit from "
        "claims or code paths you did not verify. Cite files relative to the seed or submission root and never "
        f"include an absolute host path {path_rule}. Do not compute an overall score; the runner owns "
        "weighting and aggregation.\n\n"
        + browser_directive
        + "Write a useful evidence report to the Markdown path. "
        + result_condition
        + "Write exactly one JSON object to the result path "
        "with the exact keys and dimension IDs shown here; replace the descriptive placeholders with values:\n\n"
        + json.dumps(result_shape, indent=2, ensure_ascii=False)
        + "\n\nEvaluation contract:\n\n"
        + json.dumps(contract, indent=2, ensure_ascii=False, sort_keys=True)
        + "\n\nEvaluation rubric:\n\n"
        + rubric.strip()
        + "\n"
    )
