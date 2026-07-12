"""Shared raw-attempt shape and deterministic ordering for report modules."""

from __future__ import annotations

import json
from collections.abc import Mapping

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
    portable = dict(raw)
    dimensions = portable["dimensions"]
    assert isinstance(dimensions, Mapping)
    portable["dimensions"] = dict(dimensions)
    serialized = json.dumps(portable, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    repetition = raw["repetition"]
    assert isinstance(repetition, int) and not isinstance(repetition, bool)
    return (
        str(raw["run_id"]),
        str(raw["submission_id"]),
        repetition,
        serialized,
    )
