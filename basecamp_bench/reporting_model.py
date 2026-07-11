"""Shared raw-attempt shape and deterministic ordering for report modules."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

__all__ = ["RAW_ATTEMPT_KEY_ORDER", "raw_attempt_sort_key"]

RAW_ATTEMPT_KEY_ORDER: tuple[str, ...] = (
    "run_id",
    "submission_id",
    "repetition",
    "track",
    "contract_version",
    "contract_sha256",
    "harness",
    "model_id",
    "display_name",
    "implementation_success",
    "evaluation_success",
    "score",
    "dimensions",
    "judge_spread",
    "implementation_cost_usd",
    "evaluation_cost_usd",
    "tokens",
    "duration_s",
    "evaluator_ids",
    "ineligible_reasons",
)


def raw_attempt_sort_key(raw: Mapping[str, object]) -> tuple[str, str, int, str]:
    """Return the canonical identity and serialization ordering for an attempt."""
    portable: dict[str, object] = {}
    for key in RAW_ATTEMPT_KEY_ORDER:
        value = raw[key]
        if key == "dimensions":
            assert isinstance(value, Mapping)
            portable[key] = dict(value)
        elif key in ("evaluator_ids", "ineligible_reasons"):
            assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
            portable[key] = list(value)
        else:
            portable[key] = value
    dimensions = portable["dimensions"]
    if isinstance(dimensions, dict):
        portable["dimensions"] = {
            key: dimensions[key] for key in sorted(dimensions.keys(), key=str)
        }
    serialized = json.dumps(portable, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    repetition = raw["repetition"]
    assert isinstance(repetition, int) and not isinstance(repetition, bool)
    return (
        str(raw["run_id"]),
        str(raw["submission_id"]),
        repetition,
        serialized,
    )
