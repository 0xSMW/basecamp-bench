"""Prompt assembly with a strict builder/evaluator information boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


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
) -> str:
    """Build the evaluator directive.

    It defines the evidence boundary and parsed output, while leaving runtime,
    architecture, test strategy, and inspection method to the evaluator.
    """
    dimensions = contract.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        raise ValueError("contract.dimensions must be a nonempty list")
    dimension_ids: list[str] = []
    for index, dimension in enumerate(dimensions):
        if not isinstance(dimension, Mapping) or not isinstance(dimension.get("id"), str):
            raise ValueError(f"contract.dimensions[{index}].id must be a string")
        dimension_ids.append(dimension["id"])

    result_shape = {
        "schema_version": "1.0",
        "track": track,
        "submission_id": submission_id,
        "contract_sha256": contract_sha256,
        "judge_id": evaluator_id,
        "dimensions": {
            dimension_id: {
                "score": "number from 0 through 10",
                "notes": "concise evidence-backed assessment",
                "evidence": ["specific observed evidence"],
            }
            for dimension_id in dimension_ids
        },
        "summary": "concise overall assessment",
    }

    return (
        "Evaluate this submission against the complete rubric and contract below. "
        "Inspect its full delta from the seed, run it, and test it deeply enough to support every score. "
        "Choose the appropriate runtime, commands, tools, and test strategy from the implementation itself. "
        "Treat the seed and submission directories as immutable evidence and write only to the two output paths.\n\n"
        f"Track: {track}\n"
        f"Opaque submission ID: {submission_id}\n"
        f"Seed directory: {Path(seed_dir)}\n"
        f"Submission directory: {Path(submission_dir)}\n"
        f"Markdown report path: {Path(report_path)}\n"
        f"Result JSON path: {Path(result_path)}\n\n"
        "Score every contract dimension independently from direct evidence. Distinguish absent, present, "
        "stubbed, working, persistent, and production-shaped behavior where relevant. Do not infer credit from "
        "claims or code paths you did not verify. Cite files relative to the seed or submission root and never "
        "include an absolute host path in either output. Do not compute an overall score; the runner owns "
        "weighting and aggregation.\n\n"
        "Write a useful evidence report to the Markdown path. Write exactly one JSON object to the result path "
        "with the exact keys and dimension IDs shown here; replace the descriptive placeholders with values:\n\n"
        + json.dumps(result_shape, indent=2, ensure_ascii=False)
        + "\n\nEvaluation contract:\n\n"
        + json.dumps(contract, indent=2, ensure_ascii=False, sort_keys=True)
        + "\n\nEvaluation rubric:\n\n"
        + rubric.strip()
        + "\n"
    )
