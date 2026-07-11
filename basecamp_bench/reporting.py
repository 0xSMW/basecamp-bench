"""Deterministic HTML benchmark report generation (stdlib only).

Loads version-scoped leaderboard JSON, classifies Pareto frontiers, and
renders a self-contained offline HTML report. FE and BE tracks and distinct
contract revisions are never mixed.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

from basecamp_bench.leaderboard import aggregate_attempts, attempt_from_raw

__all__ = [
    "ReportPoint",
    "expected_cost",
    "pareto_frontier",
    "load_leaderboards",
    "build_report_payload",
    "render_report_html",
    "write_report",
]

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "mode",
        "track",
        "contract_version",
        "contract_sha256",
        "generated_at",
        "runner_source_sha256",
        "seed_tree_sha256",
        "reference_manifest_sha256",
        "reference_tree_sha256",
        "prompt_sha256",
        "rubric_sha256",
        "schema_bundle_sha256",
        "dimension_profile",
        "entries",
    }
)
_ENTRY_KEYS = frozenset(
    {
        "model_id",
        "display_name",
        "harness",
        "score",
        "score_mean",
        "score_stdev",
        "score_min",
        "score_max",
        "score_range",
        "judge_spread",
        "cost_per_attempt",
        "cost_mean",
        "cost_stdev",
        "cost_min",
        "cost_max",
        "cost_range",
        "success_rate",
        "repetitions",
        "dimensions",
        "tokens",
        "tokens_mean",
        "tokens_min",
        "tokens_max",
        "tokens_range",
        "duration_s",
        "duration_mean_s",
        "duration_min_s",
        "duration_max_s",
        "duration_range_s",
        "eligible",
        "ineligible_reasons",
        "run_ids",
        "implementation_cost_per_attempt",
        "evaluation_cost_per_attempt",
        "raw_attempts",
    }
)

# Portable raw-attempt key set matching leaderboard.Attempt / _attempt_to_raw.
_RAW_ATTEMPT_KEY_ORDER: tuple[str, ...] = (
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
_RAW_ATTEMPT_KEYS = frozenset(_RAW_ATTEMPT_KEY_ORDER)

# Portable identifier: ASCII alnum first, then alnum / . / _ / - (max 64).
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_IDENTIFIER_LEN = 64
_MAX_DISPLAY_NAME_LEN = 256
_MAX_REASON_LEN = 256
_MAX_SAFE_STRING_LEN = 256

_ZERO_SUCCESS_REASON = "success_rate is zero"


@dataclass(frozen=True, slots=True)
class ReportPoint:
    """One model entry on a version-scoped leaderboard."""

    track: str
    contract_version: str
    contract_sha256: str
    model_id: str
    display_name: str
    harness: str
    score: float
    score_mean: float
    score_stdev: float
    score_min: float
    score_max: float
    score_range: float
    judge_spread: float
    cost_per_attempt: float
    cost_mean: float
    cost_stdev: float
    cost_min: float
    cost_max: float
    cost_range: float
    success_rate: float
    repetitions: int
    dimensions: Mapping[str, float]
    tokens: int
    tokens_mean: float
    tokens_min: int
    tokens_max: int
    tokens_range: int
    duration_s: float
    duration_mean_s: float
    duration_min_s: float
    duration_max_s: float
    duration_range_s: float
    eligible: bool
    ineligible_reasons: tuple[str, ...]
    run_ids: tuple[str, ...]
    implementation_cost_per_attempt: float
    evaluation_cost_per_attempt: float
    raw_attempts: tuple[Mapping[str, object], ...]
    mode: str = "local"
    runner_source_sha256: str = "0" * 64
    seed_tree_sha256: str = "0" * 64
    reference_manifest_sha256: str = "0" * 64
    reference_tree_sha256: str = "0" * 64
    prompt_sha256: str = "0" * 64
    rubric_sha256: str = "0" * 64
    schema_bundle_sha256: str = "0" * 64
    dimension_profile_json: str = "[]"
    schema_version: str | None = None
    generated_at_values: tuple[str, ...] = ()
    source_run_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"local", "publication"}:
            raise ValueError("mode must be 'local' or 'publication'")
        dims = self.dimensions
        if not isinstance(dims, MappingProxyType):
            object.__setattr__(
                self,
                "dimensions",
                MappingProxyType({str(k): float(v) for k, v in dict(dims).items()}),
            )
        reasons = self.ineligible_reasons
        if not isinstance(reasons, tuple):
            object.__setattr__(self, "ineligible_reasons", tuple(reasons))
        run_ids = self.run_ids
        if not isinstance(run_ids, tuple):
            object.__setattr__(self, "run_ids", tuple(run_ids))
        raw = self.raw_attempts
        if not isinstance(raw, tuple) or any(
            not isinstance(item, MappingProxyType) for item in raw
        ):
            object.__setattr__(
                self,
                "raw_attempts",
                tuple(_freeze_raw_attempt(item) for item in raw),
            )
        object.__setattr__(self, "generated_at_values", tuple(self.generated_at_values))
        object.__setattr__(self, "source_run_ids", tuple(self.source_run_ids))


def expected_cost(point: ReportPoint) -> float | None:
    """Return expected implementation cost: implementation_cost_per_attempt / success_rate.

    Returns None when success_rate is zero or inputs are non-finite, negative,
    or otherwise invalid for normalization. Evaluation overhead is never used.
    """
    cost = point.implementation_cost_per_attempt
    rate = point.success_rate
    if isinstance(cost, bool) or isinstance(rate, bool):
        return None
    if not isinstance(cost, (int, float)) or not isinstance(rate, (int, float)):
        return None
    cost_f = float(cost)
    rate_f = float(rate)
    if not math.isfinite(cost_f) or not math.isfinite(rate_f):
        return None
    if cost_f < 0.0 or rate_f < 0.0 or rate_f > 1.0:
        return None
    if rate_f == 0.0:
        return None
    result = cost_f / rate_f
    if not math.isfinite(result):
        return None
    return result


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _is_nonneg_finite(value: Any) -> bool:
    return _is_finite_number(value) and float(value) >= 0.0


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _section_key(point: ReportPoint) -> tuple[str, ...]:
    return (
        point.track,
        point.contract_version,
        point.contract_sha256,
        point.mode,
        point.runner_source_sha256,
        point.seed_tree_sha256,
        point.reference_manifest_sha256,
        point.reference_tree_sha256,
        point.prompt_sha256,
        point.rubric_sha256,
        point.schema_bundle_sha256,
        point.dimension_profile_json,
    )


def _frontier_eligible(point: ReportPoint) -> bool:
    if point.mode != "publication":
        return False
    if not point.eligible:
        return False
    if not _is_nonneg_finite(point.score):
        return False
    cost = expected_cost(point)
    return cost is not None and cost >= 0.0 and math.isfinite(cost)


def _dominates(a: ReportPoint, b: ReportPoint, cost_a: float, cost_b: float) -> bool:
    """Return True when *a* dominates *b* (including exact-tie lex rule)."""
    sa, sb = float(a.score), float(b.score)
    if sa >= sb and cost_a <= cost_b and (sa > sb or cost_a < cost_b):
        return True
    if sa == sb and cost_a == cost_b and _point_identity(a) < _point_identity(b):
        return True
    return False


def _point_identity(point: ReportPoint) -> tuple[str, str]:
    return (point.harness, point.model_id)


def _point_id(point: ReportPoint) -> str:
    return f"{point.harness}:{point.model_id}"


def _dominator_sort_key(point: ReportPoint, cost: float) -> tuple[float, float, str, str]:
    # Lowest expected cost, then highest score, then lexicographically smallest id.
    return (cost, -float(point.score), point.harness, point.model_id)


def pareto_frontier(
    points: Sequence[ReportPoint],
) -> tuple[set[tuple[str, str]], dict[tuple[str, str], tuple[str, str] | None]]:
    """Compute Pareto frontier and deterministic dominator map by model_id.

    Returns ``(frontier_ids, dominator_by_model_id)``. Ineligible or invalid
    points map to None and are never on the frontier. Exact score/cost ties
    keep the lexicographically smaller model_id on the frontier.
    """
    by_id: dict[tuple[str, str], ReportPoint] = {}
    for point in points:
        identity = _point_identity(point)
        if identity in by_id:
            raise ValueError(f"duplicate report point identity: {identity!r}")
        by_id[identity] = point

    dominator: dict[tuple[str, str], tuple[str, str] | None] = {mid: None for mid in by_id}
    candidates: list[tuple[ReportPoint, float]] = []
    for point in by_id.values():
        if not _frontier_eligible(point):
            continue
        cost = expected_cost(point)
        assert cost is not None
        candidates.append((point, cost))

    frontier: set[tuple[str, str]] = set()
    for point, cost in candidates:
        doms: list[tuple[ReportPoint, float]] = []
        for other, other_cost in candidates:
            if _point_identity(other) == _point_identity(point):
                continue
            if _dominates(other, point, other_cost, cost):
                doms.append((other, other_cost))
        if not doms:
            frontier.add(_point_identity(point))
            dominator[_point_identity(point)] = None
        else:
            best, _ = min(doms, key=lambda item: _dominator_sort_key(item[0], item[1]))
            dominator[_point_identity(point)] = _point_identity(best)

    return frontier, dominator


def _exact_keys(obj: Mapping[str, Any], expected: frozenset[str], path: str) -> None:
    actual = frozenset(obj.keys())
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    parts: list[str] = []
    if missing:
        parts.append(f"missing keys {missing}")
    if extra:
        parts.append(f"unknown keys {extra}")
    if parts:
        raise ValueError(f"{path}: " + "; ".join(parts))


def _require_nonempty_string(value: Any, path: str) -> str:
    if not _is_nonempty_string(value):
        raise ValueError(f"{path}: expected nonempty string")
    return value


def _require_finite_number(value: Any, path: str) -> float:
    if not _is_finite_number(value):
        raise ValueError(f"{path}: expected finite number (bool excluded)")
    return float(value)


def _require_nonneg_finite(value: Any, path: str) -> float:
    if not _is_nonneg_finite(value):
        raise ValueError(f"{path}: expected finite nonnegative number (bool excluded)")
    return float(value)


def _require_nonneg_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path}: expected nonnegative integer (bool excluded)")
    if value < 0:
        raise ValueError(f"{path}: expected nonnegative integer")
    return value


def _require_success_rate(value: Any, path: str) -> float:
    rate = _require_finite_number(value, path)
    if rate < 0.0 or rate > 1.0:
        raise ValueError(f"{path}: expected number in [0, 1], got {rate!r}")
    return rate


def _validate_distribution(
    *,
    median: float,
    mean: float,
    stdev: float | None,
    minimum: float,
    maximum: float,
    value_range: float,
    path: str,
) -> None:
    def close(left: float, right: float) -> bool:
        return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)

    if minimum > maximum and not close(minimum, maximum):
        raise ValueError(f"{path}: minimum exceeds maximum")
    if (median < minimum and not close(median, minimum)) or (
        median > maximum and not close(median, maximum)
    ):
        raise ValueError(f"{path}: median is outside min/max")
    if (mean < minimum and not close(mean, minimum)) or (
        mean > maximum and not close(mean, maximum)
    ):
        raise ValueError(f"{path}: mean is outside min/max")
    if not close(value_range, maximum - minimum):
        raise ValueError(f"{path}: range must equal max - min")
    if close(minimum, maximum) and stdev is not None and not close(stdev, 0.0):
        raise ValueError(f"{path}: stdev must be zero for a constant distribution")


def _require_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path}: expected bool")
    return value


def _require_string_list(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{path}: expected list of strings")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"{path}[{index}]: expected string")
        out.append(item)
    return tuple(out)


def _require_dimensions(value: Any, path: str) -> Mapping[str, float]:
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object mapping strings to finite numbers")
    result: dict[str, float] = {}
    for key in sorted(value.keys(), key=lambda k: (str(type(k)), str(k))):
        if not isinstance(key, str):
            raise ValueError(f"{path}: dimension keys must be strings")
        raw = value[key]
        if not _is_finite_number(raw):
            raise ValueError(f"{path}[{key!r}]: expected finite number (bool excluded)")
        result[key] = float(raw)
    return MappingProxyType(result)


def _has_control_chars(value: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


def _looks_like_absolute_path(value: str) -> bool:
    if value.startswith("/") or value.startswith("\\"):
        return True
    if len(value) >= 2 and value[0].isalpha() and value[1] == ":":
        if len(value) == 2 or value[2] in "/\\":
            return True
    return False


def _looks_like_path_or_command(value: str) -> bool:
    """Reject absolute paths, file URLs, traversal, and command/argv/prompt shapes."""
    lower = value.lower().strip()
    if lower.startswith("file:"):
        return True
    if _looks_like_absolute_path(value):
        return True
    if "/../" in value or "\\..\\" in value or value in (".", ".."):
        return True
    if value.startswith("../") or value.startswith("..\\"):
        return True
    # Command-line / argv / prompt provenance (targeted; allows normal labels).
    if re.search(r"(^|[\s;|&])(?:argv|prompt)\s*[=:]", lower):
        return True
    if re.match(
        r"^(?:python|python3|bash|sh|zsh|cmd|powershell|node|ruby|perl)\s+",
        lower,
    ):
        return True
    if re.search(r"\s--?[a-z0-9][\w-]*\b", lower) and ("/" in value or "\\" in value):
        return True
    return False


def _require_safe_identifier(value: Any, path: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"{path}: expected nonempty safe string")
    if not value:
        raise ValueError(f"{path}: expected nonempty safe string")
    if len(value) > _MAX_IDENTIFIER_LEN:
        raise ValueError(f"{path}: identifier exceeds {_MAX_IDENTIFIER_LEN} characters")
    if _has_control_chars(value):
        raise ValueError(f"{path}: contains control characters")
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"{path}: path-shaped identifier rejected")
    if _looks_like_absolute_path(value) or _looks_like_path_or_command(value):
        raise ValueError(f"{path}: path or command-shaped value rejected")
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{path}: not a portable identifier")
    return value


def _require_display_name(value: Any, path: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"{path}: expected nonempty string")
    if not value:
        raise ValueError(f"{path}: expected nonempty string")
    if len(value) > _MAX_DISPLAY_NAME_LEN:
        raise ValueError(f"{path}: display name exceeds {_MAX_DISPLAY_NAME_LEN} characters")
    if _has_control_chars(value):
        raise ValueError(f"{path}: contains control characters")
    if _looks_like_path_or_command(value):
        raise ValueError(f"{path}: path or command-shaped value rejected")
    return value


def _require_safe_label(value: Any, path: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"{path}: expected string")
    if len(value) > _MAX_REASON_LEN:
        raise ValueError(f"{path}: exceeds {_MAX_REASON_LEN} characters")
    if _has_control_chars(value):
        raise ValueError(f"{path}: contains control characters")
    if _looks_like_path_or_command(value):
        raise ValueError(f"{path}: path or command-shaped value rejected")
    return value


def _require_optional_nonneg_finite(value: Any, path: str) -> float | None:
    if value is None:
        return None
    return _require_nonneg_finite(value, path)


def _require_score_0_10(value: Any, path: str) -> float:
    number = _require_finite_number(value, path)
    if number < 0.0 or number > 10.0:
        raise ValueError(f"{path}: expected number in 0..10, got {number!r}")
    return number


def _require_positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path}: expected positive integer (bool excluded)")
    if value < 1:
        raise ValueError(f"{path}: expected positive integer")
    return value


def _require_raw_dimensions_success(value: Any, path: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object mapping strings to finite numbers")
    if not value:
        raise ValueError(f"{path}: successful evaluation requires nonempty dimensions")
    result: dict[str, float] = {}
    for key in sorted(value.keys(), key=lambda k: (str(type(k)), str(k))):
        if not isinstance(key, str) or not key:
            raise ValueError(f"{path}: dimension keys must be nonempty strings")
        if _has_control_chars(key) or _looks_like_path_or_command(key):
            raise ValueError(f"{path}: unsafe dimension key {key!r}")
        result[key] = _require_score_0_10(value[key], f"{path}[{key!r}]")
    return result


def _freeze_raw_attempt(raw: Mapping[str, Any] | Mapping[str, object]) -> Mapping[str, object]:
    """Deeply freeze one raw attempt into a MappingProxyType tree."""
    data = dict(raw)
    dims_src = data.get("dimensions", {})
    if isinstance(dims_src, Mapping):
        dims = MappingProxyType(
            {str(k): float(dims_src[k]) for k in sorted(dims_src.keys(), key=str)}
        )
    else:
        dims = MappingProxyType({})
    evals = data.get("evaluator_ids", ())
    reasons = data.get("ineligible_reasons", ())
    frozen: dict[str, object] = {}
    for key in _RAW_ATTEMPT_KEY_ORDER:
        if key == "dimensions":
            frozen[key] = dims
        elif key == "evaluator_ids":
            frozen[key] = tuple(evals)
        elif key == "ineligible_reasons":
            frozen[key] = tuple(reasons)
        else:
            frozen[key] = data[key]
    return MappingProxyType(frozen)


def _raw_attempt_sort_key(raw: Mapping[str, object]) -> tuple[str, str, int, str]:
    portable: dict[str, object] = {}
    for key in _RAW_ATTEMPT_KEY_ORDER:
        value = raw[key]
        if key == "dimensions":
            assert isinstance(value, Mapping)
            portable[key] = dict(value)
        elif key in ("evaluator_ids", "ineligible_reasons"):
            assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
            portable[key] = list(value)
        else:
            portable[key] = value
    if isinstance(portable["dimensions"], dict):
        portable["dimensions"] = {
            k: portable["dimensions"][k] for k in sorted(portable["dimensions"].keys(), key=str)
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


def _parse_raw_attempt(
    raw: Any,
    *,
    track: str,
    contract_version: str,
    contract_sha256: str,
    model_id: str,
    harness: str,
    path: str,
) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected object")
    _exact_keys(raw, _RAW_ATTEMPT_KEYS, path)

    run_id = _require_safe_identifier(raw["run_id"], f"{path}.run_id")
    submission_id = _require_safe_identifier(raw["submission_id"], f"{path}.submission_id")
    repetition = _require_positive_int(raw["repetition"], f"{path}.repetition")

    raw_track = _require_nonempty_string(raw["track"], f"{path}.track")
    if raw_track != track:
        raise ValueError(f"{path}.track: must match root track {track!r}, got {raw_track!r}")
    raw_cv = _require_safe_identifier(raw["contract_version"], f"{path}.contract_version")
    if raw_cv != contract_version:
        raise ValueError(
            f"{path}.contract_version: must match root identity "
            f"{contract_version!r}, got {raw_cv!r}"
        )
    raw_sha = _require_nonempty_string(raw["contract_sha256"], f"{path}.contract_sha256")
    if _has_control_chars(raw_sha) or _looks_like_path_or_command(raw_sha):
        raise ValueError(f"{path}.contract_sha256: path or command-shaped value rejected")
    if len(raw_sha) > _MAX_SAFE_STRING_LEN:
        raise ValueError(f"{path}.contract_sha256: exceeds bound")
    if raw_sha != contract_sha256:
        raise ValueError(f"{path}.contract_sha256: must match root identity, got {raw_sha!r}")

    raw_harness = _require_safe_identifier(raw["harness"], f"{path}.harness")
    if raw_harness != harness:
        raise ValueError(
            f"{path}.harness: must match entry harness {harness!r}, got {raw_harness!r}"
        )
    raw_model = _require_safe_identifier(raw["model_id"], f"{path}.model_id")
    if raw_model != model_id:
        raise ValueError(
            f"{path}.model_id: must match entry model_id {model_id!r}, got {raw_model!r}"
        )
    display_name = _require_display_name(raw["display_name"], f"{path}.display_name")

    implementation_success = _require_bool(
        raw["implementation_success"], f"{path}.implementation_success"
    )
    evaluation_success = _require_bool(raw["evaluation_success"], f"{path}.evaluation_success")
    if evaluation_success and not implementation_success:
        raise ValueError(f"{path}: evaluation_success requires implementation_success")

    if evaluation_success:
        score = _require_score_0_10(raw["score"], f"{path}.score")
        dimensions = _require_raw_dimensions_success(raw["dimensions"], f"{path}.dimensions")
        judge_spread = _require_nonneg_finite(raw["judge_spread"], f"{path}.judge_spread")
    else:
        if raw["score"] is not None:
            raise ValueError(f"{path}.score: failed evaluation must have score None")
        score = None
        dims_raw = raw["dimensions"]
        if not isinstance(dims_raw, dict) or dims_raw:
            raise ValueError(f"{path}.dimensions: failed evaluation must have empty dimensions")
        dimensions = {}
        if raw["judge_spread"] is not None:
            raise ValueError(f"{path}.judge_spread: failed evaluation must have judge_spread None")
        judge_spread = None

    implementation_cost_usd = _require_optional_nonneg_finite(
        raw["implementation_cost_usd"], f"{path}.implementation_cost_usd"
    )
    evaluation_cost_usd = _require_optional_nonneg_finite(
        raw["evaluation_cost_usd"], f"{path}.evaluation_cost_usd"
    )
    tokens = _require_nonneg_int(raw["tokens"], f"{path}.tokens")
    duration_s = _require_nonneg_finite(raw["duration_s"], f"{path}.duration_s")

    evals_raw = raw["evaluator_ids"]
    if not isinstance(evals_raw, list):
        raise ValueError(f"{path}.evaluator_ids: expected list of safe strings")
    evaluator_ids: list[str] = []
    for i, item in enumerate(evals_raw):
        evaluator_ids.append(_require_safe_identifier(item, f"{path}.evaluator_ids[{i}]"))

    reasons_raw = raw["ineligible_reasons"]
    if not isinstance(reasons_raw, list):
        raise ValueError(f"{path}.ineligible_reasons: expected list of safe strings")
    ineligible_reasons: list[str] = []
    for i, item in enumerate(reasons_raw):
        ineligible_reasons.append(_require_safe_label(item, f"{path}.ineligible_reasons[{i}]"))

    ordered: dict[str, object] = {
        "run_id": run_id,
        "submission_id": submission_id,
        "repetition": repetition,
        "track": raw_track,
        "contract_version": raw_cv,
        "contract_sha256": raw_sha,
        "harness": raw_harness,
        "model_id": raw_model,
        "display_name": display_name,
        "implementation_success": implementation_success,
        "evaluation_success": evaluation_success,
        "score": score,
        "dimensions": dimensions,
        "judge_spread": judge_spread,
        "implementation_cost_usd": implementation_cost_usd,
        "evaluation_cost_usd": evaluation_cost_usd,
        "tokens": tokens,
        "duration_s": duration_s,
        "evaluator_ids": evaluator_ids,
        "ineligible_reasons": ineligible_reasons,
    }
    return _freeze_raw_attempt(ordered)


def _parse_raw_attempts(
    value: Any,
    *,
    track: str,
    contract_version: str,
    contract_sha256: str,
    model_id: str,
    harness: str,
    path: str,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{path}: expected list of raw attempt objects")
    parsed: list[Mapping[str, object]] = []
    seen_ids: dict[tuple[str, str, int], str] = {}
    for index, item in enumerate(value):
        apath = f"{path}[{index}]"
        raw = _parse_raw_attempt(
            item,
            track=track,
            contract_version=contract_version,
            contract_sha256=contract_sha256,
            model_id=model_id,
            harness=harness,
            path=apath,
        )
        repetition = raw["repetition"]
        assert isinstance(repetition, int) and not isinstance(repetition, bool)
        identity = (
            str(raw["run_id"]),
            str(raw["submission_id"]),
            repetition,
        )
        serialized = _raw_attempt_sort_key(raw)[3]
        if identity in seen_ids and seen_ids[identity] != serialized:
            raise ValueError(
                f"{apath}: conflicting duplicate raw identity "
                f"run_id={identity[0]!r} submission_id={identity[1]!r} "
                f"repetition={identity[2]!r}"
            )
        if identity not in seen_ids:
            seen_ids[identity] = serialized
            parsed.append(raw)
    parsed.sort(key=_raw_attempt_sort_key)
    return tuple(parsed)


def _parse_entry(
    entry: Any,
    *,
    track: str,
    contract_version: str,
    contract_sha256: str,
    path: str,
    index: int,
    provenance: Mapping[str, str],
    section_meta: Mapping[str, Any] | None = None,
) -> ReportPoint:
    epath = f"{path}:entries[{index}]"
    if not isinstance(entry, dict):
        raise ValueError(f"{epath}: expected object")
    _exact_keys(entry, _ENTRY_KEYS, epath)

    model_id = _require_nonempty_string(entry["model_id"], f"{epath}.model_id")
    display_name = _require_nonempty_string(entry["display_name"], f"{epath}.display_name")
    harness = _require_nonempty_string(entry["harness"], f"{epath}.harness")
    score = _require_finite_number(entry["score"], f"{epath}.score")
    score_mean = _require_nonneg_finite(entry["score_mean"], f"{epath}.score_mean")
    score_stdev = _require_nonneg_finite(entry["score_stdev"], f"{epath}.score_stdev")
    score_min = _require_nonneg_finite(entry["score_min"], f"{epath}.score_min")
    score_max = _require_nonneg_finite(entry["score_max"], f"{epath}.score_max")
    score_range = _require_nonneg_finite(entry["score_range"], f"{epath}.score_range")
    if max(score, score_mean, score_min, score_max) > 10.0:
        raise ValueError(f"{epath}: score distribution values must be in 0..10")
    _validate_distribution(
        median=score,
        mean=score_mean,
        stdev=score_stdev,
        minimum=score_min,
        maximum=score_max,
        value_range=score_range,
        path=f"{epath}.score_distribution",
    )
    judge_spread = _require_nonneg_finite(entry["judge_spread"], f"{epath}.judge_spread")
    cost_per_attempt = _require_nonneg_finite(
        entry["cost_per_attempt"], f"{epath}.cost_per_attempt"
    )
    cost_mean = _require_nonneg_finite(entry["cost_mean"], f"{epath}.cost_mean")
    cost_stdev = _require_nonneg_finite(entry["cost_stdev"], f"{epath}.cost_stdev")
    cost_min = _require_nonneg_finite(entry["cost_min"], f"{epath}.cost_min")
    cost_max = _require_nonneg_finite(entry["cost_max"], f"{epath}.cost_max")
    cost_range = _require_nonneg_finite(entry["cost_range"], f"{epath}.cost_range")
    _validate_distribution(
        median=cost_per_attempt,
        mean=cost_mean,
        stdev=cost_stdev,
        minimum=cost_min,
        maximum=cost_max,
        value_range=cost_range,
        path=f"{epath}.implementation_cost_distribution",
    )
    success_rate = _require_success_rate(entry["success_rate"], f"{epath}.success_rate")
    repetitions = _require_nonneg_int(entry["repetitions"], f"{epath}.repetitions")
    dimensions = _require_dimensions(entry["dimensions"], f"{epath}.dimensions")
    tokens = _require_nonneg_int(entry["tokens"], f"{epath}.tokens")
    tokens_mean = _require_nonneg_finite(entry["tokens_mean"], f"{epath}.tokens_mean")
    tokens_min = _require_nonneg_int(entry["tokens_min"], f"{epath}.tokens_min")
    tokens_max = _require_nonneg_int(entry["tokens_max"], f"{epath}.tokens_max")
    tokens_range = _require_nonneg_int(entry["tokens_range"], f"{epath}.tokens_range")
    _validate_distribution(
        median=float(tokens),
        mean=tokens_mean,
        stdev=None,
        minimum=float(tokens_min),
        maximum=float(tokens_max),
        value_range=float(tokens_range),
        path=f"{epath}.tokens_distribution",
    )
    duration_s = _require_nonneg_finite(entry["duration_s"], f"{epath}.duration_s")
    duration_mean_s = _require_nonneg_finite(entry["duration_mean_s"], f"{epath}.duration_mean_s")
    duration_min_s = _require_nonneg_finite(entry["duration_min_s"], f"{epath}.duration_min_s")
    duration_max_s = _require_nonneg_finite(entry["duration_max_s"], f"{epath}.duration_max_s")
    duration_range_s = _require_nonneg_finite(
        entry["duration_range_s"], f"{epath}.duration_range_s"
    )
    _validate_distribution(
        median=duration_s,
        mean=duration_mean_s,
        stdev=None,
        minimum=duration_min_s,
        maximum=duration_max_s,
        value_range=duration_range_s,
        path=f"{epath}.duration_distribution",
    )
    eligible = _require_bool(entry["eligible"], f"{epath}.eligible")
    ineligible_reasons = _require_string_list(
        entry["ineligible_reasons"], f"{epath}.ineligible_reasons"
    )
    run_ids = _require_string_list(entry["run_ids"], f"{epath}.run_ids")
    implementation_cost_per_attempt = _require_nonneg_finite(
        entry["implementation_cost_per_attempt"],
        f"{epath}.implementation_cost_per_attempt",
    )
    evaluation_cost_per_attempt = _require_nonneg_finite(
        entry["evaluation_cost_per_attempt"],
        f"{epath}.evaluation_cost_per_attempt",
    )
    if float(cost_per_attempt) != float(implementation_cost_per_attempt):
        raise ValueError(
            f"{epath}: cost_per_attempt ({cost_per_attempt!r}) must equal "
            f"implementation_cost_per_attempt "
            f"({implementation_cost_per_attempt!r})"
        )
    raw_attempts = _parse_raw_attempts(
        entry["raw_attempts"],
        track=track,
        contract_version=contract_version,
        contract_sha256=contract_sha256,
        model_id=model_id,
        harness=harness,
        path=f"{epath}.raw_attempts",
    )

    if success_rate == 0.0:
        eligible = False
        reasons = list(ineligible_reasons)
        if _ZERO_SUCCESS_REASON not in reasons:
            reasons.append(_ZERO_SUCCESS_REASON)
        ineligible_reasons = tuple(reasons)

    meta = section_meta or {}
    return ReportPoint(
        track=track,
        contract_version=contract_version,
        contract_sha256=contract_sha256,
        model_id=model_id,
        display_name=display_name,
        harness=harness,
        score=score,
        score_mean=score_mean,
        score_stdev=score_stdev,
        score_min=score_min,
        score_max=score_max,
        score_range=score_range,
        judge_spread=judge_spread,
        cost_per_attempt=cost_per_attempt,
        cost_mean=cost_mean,
        cost_stdev=cost_stdev,
        cost_min=cost_min,
        cost_max=cost_max,
        cost_range=cost_range,
        success_rate=success_rate,
        repetitions=repetitions,
        dimensions=dimensions,
        tokens=tokens,
        tokens_mean=tokens_mean,
        tokens_min=tokens_min,
        tokens_max=tokens_max,
        tokens_range=tokens_range,
        duration_s=duration_s,
        duration_mean_s=duration_mean_s,
        duration_min_s=duration_min_s,
        duration_max_s=duration_max_s,
        duration_range_s=duration_range_s,
        eligible=eligible,
        ineligible_reasons=ineligible_reasons,
        run_ids=run_ids,
        implementation_cost_per_attempt=implementation_cost_per_attempt,
        evaluation_cost_per_attempt=evaluation_cost_per_attempt,
        raw_attempts=raw_attempts,
        mode=provenance["mode"],
        runner_source_sha256=provenance["runner_source_sha256"],
        seed_tree_sha256=provenance["seed_tree_sha256"],
        reference_manifest_sha256=provenance["reference_manifest_sha256"],
        reference_tree_sha256=provenance["reference_tree_sha256"],
        prompt_sha256=provenance["prompt_sha256"],
        rubric_sha256=provenance["rubric_sha256"],
        schema_bundle_sha256=provenance["schema_bundle_sha256"],
        dimension_profile_json=provenance["dimension_profile_json"],
        schema_version=cast(str | None, meta.get("schema_version")),
        generated_at_values=tuple(cast(Sequence[str], meta.get("generated_at_values", ()))),
        source_run_ids=tuple(cast(Sequence[str], meta.get("source_run_ids", ()))),
    )


def load_leaderboards(paths: Sequence[Path]) -> list[ReportPoint]:
    """Load, compatibility-section, deduplicate, and recompute leaderboard rows."""
    collected: dict[tuple[str, ...], dict[str, Any]] = {}

    for raw_path in paths:
        path = Path(raw_path)
        label = os.fspath(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"{label}: cannot read leaderboard file: {exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label}: invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{label}: root must be a JSON object")
        _exact_keys(data, _ROOT_KEYS, label)

        schema_version = _require_nonempty_string(data["schema_version"], f"{label}.schema_version")
        if schema_version != "1.0":
            raise ValueError(f"{label}.schema_version: unsupported version")
        mode = _require_nonempty_string(data["mode"], f"{label}.mode")
        if mode not in {"local", "publication"}:
            raise ValueError(f"{label}.mode: expected local or publication")
        track = _require_nonempty_string(data["track"], f"{label}.track")
        if track not in {"fe", "be"}:
            raise ValueError(f"{label}.track: expected fe or be")
        contract_version = _require_nonempty_string(
            data["contract_version"], f"{label}.contract_version"
        )
        contract_sha256 = _require_nonempty_string(
            data["contract_sha256"], f"{label}.contract_sha256"
        )
        generated_at = _require_nonempty_string(data["generated_at"], f"{label}.generated_at")
        provenance: dict[str, str] = {"mode": mode}
        for key in (
            "runner_source_sha256",
            "seed_tree_sha256",
            "reference_manifest_sha256",
            "reference_tree_sha256",
            "prompt_sha256",
            "rubric_sha256",
            "schema_bundle_sha256",
        ):
            value = _require_nonempty_string(data[key], f"{label}.{key}")
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"{label}.{key}: expected lowercase SHA-256")
            provenance[key] = value
        raw_profile = data["dimension_profile"]
        if not isinstance(raw_profile, list) or not raw_profile:
            raise ValueError(f"{label}.dimension_profile: expected nonempty array")
        profile: list[dict[str, Any]] = []
        profile_ids: set[str] = set()
        for pindex, row in enumerate(raw_profile):
            ppath = f"{label}.dimension_profile[{pindex}]"
            if not isinstance(row, dict) or set(row) != {"id", "label", "weight"}:
                raise ValueError(f"{ppath}: invalid dimension metadata")
            dim_id = _require_safe_identifier(row["id"], f"{ppath}.id")
            dim_label = _require_display_name(row["label"], f"{ppath}.label")
            weight = _require_nonneg_finite(row["weight"], f"{ppath}.weight")
            if weight <= 0 or dim_id in profile_ids:
                raise ValueError(f"{ppath}: duplicate id or nonpositive weight")
            profile_ids.add(dim_id)
            profile.append({"id": dim_id, "label": dim_label, "weight": weight})
        if abs(sum(float(row["weight"]) for row in profile) - 1.0) > 1e-9:
            raise ValueError(f"{label}.dimension_profile: weights must sum to 1")
        profile.sort(key=lambda row: str(row["id"]))
        profile_json = json.dumps(
            profile, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        provenance["dimension_profile_json"] = profile_json
        entries = data["entries"]
        if not isinstance(entries, list):
            raise ValueError(f"{label}.entries: expected list")

        section = (
            track,
            contract_version,
            contract_sha256,
            mode,
            provenance["runner_source_sha256"],
            provenance["seed_tree_sha256"],
            provenance["reference_manifest_sha256"],
            provenance["reference_tree_sha256"],
            provenance["prompt_sha256"],
            provenance["rubric_sha256"],
            provenance["schema_bundle_sha256"],
            profile_json,
        )
        bucket = collected.setdefault(
            section,
            {
                "schema_version": schema_version,
                "timestamps": set(),
                "attempts": {},
                "model_identity": {},
                "provenance": provenance,
                "profile": profile,
            },
        )
        bucket["timestamps"].add(generated_at)

        for index, entry in enumerate(entries):
            point = _parse_entry(
                entry,
                track=track,
                contract_version=contract_version,
                contract_sha256=contract_sha256,
                path=label,
                index=index,
                provenance=provenance,
            )
            identity = (point.harness, point.model_id)
            prior_name = bucket["model_identity"].setdefault(identity, point.display_name)
            if prior_name != point.display_name:
                raise ValueError(f"{label}: inconsistent display identity for {identity!r}")
            for frozen in point.raw_attempts:
                raw = _raw_attempt_payload(frozen)
                if set(raw["dimensions"]) != profile_ids and raw["evaluation_success"]:
                    raise ValueError(
                        f"{label}: raw attempt dimensions differ from dimension profile"
                    )
                canonical = json.dumps(
                    raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                )
                logical = (raw["run_id"], raw["submission_id"], raw["repetition"])
                existing = bucket["attempts"].get(logical)
                if existing is not None and existing[0] != canonical:
                    raise ValueError(f"{label}: conflicting non-identical duplicate raw attempt")
                bucket["attempts"][logical] = (canonical, attempt_from_raw(raw))

    points: list[ReportPoint] = []
    for compat_key in sorted(collected):
        bucket = collected[compat_key]
        attempts = [item[1] for _, item in sorted(bucket["attempts"].items())]
        provenance = bucket["provenance"]
        mode = cast(Literal["local", "publication"], provenance["mode"])
        roots = aggregate_attempts(
            attempts,
            mode=mode,
            generated_at=max(bucket["timestamps"]),
            comparison_provenance={
                key: provenance[key]
                for key in provenance
                if key != "mode" and key != "dimension_profile_json"
            },
            dimension_profiles={compat_key[0]: bucket["profile"]},
        )
        if len(roots) != 1:
            raise ValueError("compatible leaderboard inputs produced multiple sections")
        section_meta = {
            "schema_version": bucket["schema_version"],
            "generated_at": max(bucket["timestamps"]),
            "generated_at_values": sorted(bucket["timestamps"]),
            "source_run_ids": sorted({attempt.run_id for attempt in attempts}),
            "mode": mode,
            "dimension_profile": bucket["profile"],
            **{k: provenance[k] for k in provenance if k not in {"mode", "dimension_profile_json"}},
        }
        combined_entries = cast(list[dict[str, Any]], roots[0]["entries"])
        for index, entry in enumerate(combined_entries):
            points.append(
                _parse_entry(
                    entry,
                    track=compat_key[0],
                    contract_version=compat_key[1],
                    contract_sha256=compat_key[2],
                    path="combined leaderboards",
                    index=index,
                    provenance=provenance,
                    section_meta=section_meta,
                )
            )
    return points


def _classification(
    point: ReportPoint,
    frontier: set[tuple[str, str]],
    dominator: dict[tuple[str, str], tuple[str, str] | None],
) -> str:
    if not _frontier_eligible(point):
        return "ineligible"
    if _point_identity(point) in frontier:
        return "frontier"
    return "dominated"


def _frontier_sort_key(point: ReportPoint) -> tuple[float, float, str, str]:
    cost = expected_cost(point)
    assert cost is not None
    # Increasing score; then lower cost; then lex model_id.
    return (float(point.score), cost, point.harness, point.model_id)


def _marginals(
    ordered_frontier: list[ReportPoint],
) -> dict[tuple[str, str], float | None]:
    out: dict[tuple[str, str], float | None] = {}
    for index, point in enumerate(ordered_frontier):
        if index == 0:
            out[_point_identity(point)] = None
            continue
        prev = ordered_frontier[index - 1]
        cost_cur = expected_cost(point)
        cost_prev = expected_cost(prev)
        assert cost_cur is not None and cost_prev is not None
        d_score = float(point.score) - float(prev.score)
        if d_score <= 0.0:
            out[_point_identity(point)] = None
        else:
            out[_point_identity(point)] = (cost_cur - cost_prev) / d_score
    return out


def _raw_attempt_payload(raw: Mapping[str, object]) -> dict[str, Any]:
    dims_src = raw.get("dimensions") or {}
    if isinstance(dims_src, Mapping):
        dims = {str(k): float(dims_src[k]) for k in sorted(dims_src.keys(), key=str)}
    else:
        dims = {}
    evals = raw.get("evaluator_ids") or ()
    reasons = raw.get("ineligible_reasons") or ()
    assert isinstance(evals, Sequence) and not isinstance(evals, (str, bytes))
    assert isinstance(reasons, Sequence) and not isinstance(reasons, (str, bytes))
    return {
        "run_id": raw["run_id"],
        "submission_id": raw["submission_id"],
        "repetition": raw["repetition"],
        "track": raw["track"],
        "contract_version": raw["contract_version"],
        "contract_sha256": raw["contract_sha256"],
        "harness": raw["harness"],
        "model_id": raw["model_id"],
        "display_name": raw["display_name"],
        "implementation_success": raw["implementation_success"],
        "evaluation_success": raw["evaluation_success"],
        "score": raw["score"],
        "dimensions": dims,
        "judge_spread": raw["judge_spread"],
        "implementation_cost_usd": raw["implementation_cost_usd"],
        "evaluation_cost_usd": raw["evaluation_cost_usd"],
        "tokens": raw["tokens"],
        "duration_s": raw["duration_s"],
        "evaluator_ids": list(evals),
        "ineligible_reasons": list(reasons),
    }


def _point_payload(
    point: ReportPoint,
    *,
    classification: str,
    dominator: str | None,
    marginal: float | None,
) -> dict[str, Any]:
    dims = {k: point.dimensions[k] for k in sorted(point.dimensions.keys())}
    raw_attempts = [
        _raw_attempt_payload(raw) for raw in sorted(point.raw_attempts, key=_raw_attempt_sort_key)
    ]
    return {
        "point_id": _point_id(point),
        "model_id": point.model_id,
        "display_name": point.display_name,
        "harness": point.harness,
        "score": point.score,
        "score_mean": point.score_mean,
        "score_stdev": point.score_stdev,
        "score_min": point.score_min,
        "score_max": point.score_max,
        "score_range": point.score_range,
        "judge_spread": point.judge_spread,
        "cost_per_attempt": point.cost_per_attempt,
        "cost_mean": point.cost_mean,
        "implementation_cost_per_attempt": point.implementation_cost_per_attempt,
        "evaluation_cost_per_attempt": point.evaluation_cost_per_attempt,
        "cost_stdev": point.cost_stdev,
        "cost_min": point.cost_min,
        "cost_max": point.cost_max,
        "cost_range": point.cost_range,
        "success_rate": point.success_rate,
        "repetitions": point.repetitions,
        "dimensions": dims,
        "tokens": point.tokens,
        "tokens_mean": point.tokens_mean,
        "tokens_min": point.tokens_min,
        "tokens_max": point.tokens_max,
        "tokens_range": point.tokens_range,
        "duration_s": point.duration_s,
        "duration_mean_s": point.duration_mean_s,
        "duration_min_s": point.duration_min_s,
        "duration_max_s": point.duration_max_s,
        "duration_range_s": point.duration_range_s,
        "eligible": point.eligible,
        "ineligible_reasons": list(point.ineligible_reasons),
        "run_ids": list(point.run_ids),
        "raw_attempts": raw_attempts,
        "expected_cost": expected_cost(point),
        "classification": classification,
        "dominator": dominator,
        "marginal_cost_per_quality": marginal,
    }


def build_report_payload(points: Sequence[ReportPoint]) -> dict[str, Any]:
    """Build a JSON-serializable, deterministic report payload.

    Groups points by (track, contract_version, contract_sha256). Every point
    appears, including ineligible ones. Provenance comes only from loaded
    leaderboard metadata (or null when absent).
    """
    sections_map: dict[tuple[str, ...], list[ReportPoint]] = {}
    for point in points:
        sections_map.setdefault(_section_key(point), []).append(point)

    sections: list[dict[str, Any]] = []
    for key in sorted(sections_map.keys()):
        track, contract_version, contract_sha256 = key[:3]
        group = list(sections_map[key])
        # Deterministic model order within section.
        group.sort(key=lambda p: p.model_id)

        frontier_ids, dominator_map = pareto_frontier(group)
        frontier_points = [p for p in group if _point_identity(p) in frontier_ids]
        frontier_points.sort(key=_frontier_sort_key)
        ordered_frontier_ids = [_point_id(p) for p in frontier_points]
        marginals = _marginals(frontier_points)

        schema_versions = {
            point.schema_version for point in group if point.schema_version is not None
        }
        if len(schema_versions) > 1:
            raise ValueError("inconsistent schema versions within report section")
        generated_at_values = sorted(
            {stamp for point in group for stamp in point.generated_at_values}
        )
        source_run_ids = sorted({run_id for point in group for run_id in point.source_run_ids})
        models: list[dict[str, Any]] = []
        for point in group:
            cls = _classification(point, frontier_ids, dominator_map)
            dominator_identity = dominator_map.get(_point_identity(point))
            models.append(
                _point_payload(
                    point,
                    classification=cls,
                    dominator=":".join(dominator_identity) if dominator_identity else None,
                    marginal=marginals.get(_point_identity(point)) if cls == "frontier" else None,
                )
            )

        sections.append(
            {
                "track": track,
                "contract_version": contract_version,
                "contract_sha256": contract_sha256,
                "schema_version": next(iter(schema_versions), None),
                "generated_at": max(generated_at_values) if generated_at_values else None,
                "generated_at_values": generated_at_values,
                "source_run_ids": source_run_ids,
                "mode": key[3],
                "runner_source_sha256": key[4],
                "seed_tree_sha256": key[5],
                "reference_manifest_sha256": key[6],
                "reference_tree_sha256": key[7],
                "prompt_sha256": key[8],
                "rubric_sha256": key[9],
                "schema_bundle_sha256": key[10],
                "dimension_profile": json.loads(key[11]),
                "frontier": ordered_frontier_ids,
                "models": models,
            }
        )

    return {"sections": sections}


def _escape(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _fmt_num(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _escape(value)
    if not math.isfinite(float(value)):
        return "—"
    return f"{float(value):.{digits}f}".rstrip("0").rstrip(".") if digits else str(value)


def _model_color(model_id: str) -> str:
    """Stable contrast-conscious HSL from SHA-256 of model_id."""
    digest = hashlib.sha256(model_id.encode("utf-8")).hexdigest()
    hue = int(digest[:8], 16) % 360
    # Mid saturation/lightness for readable dark and light backgrounds.
    sat = 55 + (int(digest[8:10], 16) % 21)  # 55–75
    light = 42 + (int(digest[10:12], 16) % 17)  # 42–58
    return f"hsl({hue}, {sat}%, {light}%)"


def _embed_json(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # Prevent </script> breakout and HTML parsing of raw < characters.
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _chart_svg(section: Mapping[str, Any], section_index: int) -> str:
    models: list[dict[str, Any]] = list(section["models"])
    width, height = 720, 420
    margin_l, margin_r, margin_t, margin_b = 64, 28, 28, 56
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    plottable = [
        m
        for m in models
        if m.get("expected_cost") is not None
        and _is_finite_number(m["expected_cost"])
        and _is_finite_number(m["score"])
    ]

    track = section["track"]
    title = f"Cost vs quality for track {track}, contract {section['contract_version']}"
    aria = (
        f"Scatter chart of expected implementation cost versus score for {track}. "
        f"Expected implementation cost on the horizontal axis with cheaper "
        f"to the right. Score on the vertical axis. Includes error bars, "
        f"point labels, and a frontier polyline."
    )

    if not plottable:
        return (
            f'<svg class="chart" role="img" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" aria-label="{_escape(aria)}">'
            f"<title>{_escape(title)}</title>"
            f"<desc>{_escape(aria)}</desc>"
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            f'class="muted">No plottable points</text></svg>'
        )

    scores = [float(m["score"]) for m in plottable]
    costs = [float(m["expected_cost"]) for m in plottable]
    score_lo = min(scores)
    score_hi = max(scores)
    cost_lo = min(costs)
    cost_hi = max(costs)
    if score_hi == score_lo:
        score_lo -= 1.0
        score_hi += 1.0
    if cost_hi == cost_lo:
        cost_lo = max(0.0, cost_lo - 1.0)
        cost_hi = cost_hi + 1.0
    # Pad for error bars.
    score_pad = max((score_hi - score_lo) * 0.08, 0.05)
    cost_pad = max((cost_hi - cost_lo) * 0.08, 0.05)
    score_lo -= score_pad
    score_hi += score_pad
    cost_lo = max(0.0, cost_lo - cost_pad)
    cost_hi += cost_pad

    def x_of(cost: float) -> float:
        # Cheaper (lower expected cost) maps to the right.
        t = (cost - cost_lo) / (cost_hi - cost_lo)
        return margin_l + (1.0 - t) * plot_w

    def y_of(score: float) -> float:
        t = (score - score_lo) / (score_hi - score_lo)
        return margin_t + (1.0 - t) * plot_h

    parts: list[str] = [
        f'<svg class="chart" role="img" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" aria-label="{_escape(aria)}">',
        f"<title>{_escape(title)}</title>",
        f"<desc>{_escape(aria)}</desc>",
        f'<rect x="0" y="0" width="{width}" height="{height}" class="chart-bg"/>',
        f'<line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" '
        f'y2="{margin_t + plot_h}" class="axis"/>',
        f'<line x1="{margin_l}" y1="{margin_t + plot_h}" '
        f'x2="{margin_l + plot_w}" y2="{margin_t + plot_h}" class="axis"/>',
        f'<text x="{margin_l + plot_w / 2}" y="{height - 12}" text-anchor="middle" '
        f'class="axis-label">Expected implementation cost (cheaper to the right)</text>',
        f'<text x="16" y="{margin_t + plot_h / 2}" text-anchor="middle" '
        f'class="axis-label" transform="rotate(-90 16 {margin_t + plot_h / 2})">'
        f"Score (quality)</text>",
        f'<text x="{margin_l}" y="{height - 30}" text-anchor="start" class="muted">'
        f"higher cost →</text>",
        f'<text x="{margin_l + plot_w}" y="{height - 30}" text-anchor="end" '
        f'class="muted">← lower cost (cheaper)</text>',
    ]

    # Frontier polyline in increasing score order.
    frontier_ids: list[str] = list(section.get("frontier") or [])
    by_id = {m["point_id"]: m for m in models}
    f_pts: list[str] = []
    for mid in frontier_ids:
        m = by_id.get(mid)
        if not m or m.get("expected_cost") is None:
            continue
        f_pts.append(f"{x_of(float(m['expected_cost'])):.2f},{y_of(float(m['score'])):.2f}")
    if len(f_pts) >= 2:
        parts.append(f'<polyline class="frontier-line" fill="none" points="{" ".join(f_pts)}"/>')
    elif len(f_pts) == 1:
        # Single frontier point still marked via point styling.
        pass

    for m in sorted(plottable, key=lambda item: item["point_id"]):
        mid = m["point_id"]
        score = float(m["score"])
        cost = float(m["expected_cost"])
        cx, cy = x_of(cost), y_of(score)
        color = _model_color(mid)
        cls = m.get("classification", "ineligible")
        if cls == "frontier":
            marker = "frontier-point"
            r = 6
        elif cls == "dominated":
            marker = "dominated-point"
            r = 5
        else:
            marker = "ineligible-point"
            r = 4

        s_err = float(m.get("score_stdev") or 0.0)
        c_err = float(m.get("cost_stdev") or 0.0)
        # Vertical error bar (score).
        y1, y2 = y_of(score + s_err), y_of(score - s_err)
        parts.append(
            f'<line class="error-bar" x1="{cx:.2f}" y1="{y1:.2f}" '
            f'x2="{cx:.2f}" y2="{y2:.2f}" stroke="{_escape(color)}"/>'
        )
        # Horizontal error bar in data space (cost); cheaper-right mapping.
        x1, x2 = x_of(cost - c_err), x_of(cost + c_err)
        parts.append(
            f'<line class="error-bar" x1="{x1:.2f}" y1="{cy:.2f}" '
            f'x2="{x2:.2f}" y2="{cy:.2f}" stroke="{_escape(color)}"/>'
        )
        parts.append(
            f'<circle class="{marker}" cx="{cx:.2f}" cy="{cy:.2f}" r="{r}" '
            f'fill="{_escape(color)}" data-model="{_escape(mid)}">'
            f"<title>{_escape(m.get('display_name') or mid)}: "
            f"score={score:.4g}, expected_cost={cost:.4g}</title></circle>"
        )
        parts.append(
            f'<text class="point-label" x="{cx + 8:.2f}" y="{cy - 8:.2f}">'
            f"{_escape(m.get('display_name') or mid)}</text>"
        )

    parts.append("</svg>")
    return "\n".join(parts)


def _dimension_table(models: Sequence[Mapping[str, Any]]) -> str:
    dim_keys: set[str] = set()
    for m in models:
        dims = m.get("dimensions") or {}
        if isinstance(dims, dict):
            dim_keys.update(str(k) for k in dims.keys())
    keys = sorted(dim_keys)
    if not keys:
        return '<p class="muted">No dimension scores.</p>'

    head = "".join(f'<th scope="col">{_escape(k)}</th>' for k in keys)
    rows: list[str] = []
    for m in models:
        dims = m.get("dimensions") or {}
        cells = "".join(f"<td>{_escape(_fmt_num(dims.get(k)))}</td>" for k in keys)
        rows.append(
            f'<tr><th scope="row">{_escape(m.get("display_name") or m["model_id"])}'
            f"</th>{cells}</tr>"
        )
    return (
        '<table class="dim-table">'
        "<caption>Per-model dimension scores</caption>"
        f'<thead><tr><th scope="col">Model</th>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _models_table(models: Sequence[Mapping[str, Any]]) -> str:
    headers = [
        "Model",
        "Score median",
        "Score mean",
        "Score stdev",
        "Score min",
        "Score max",
        "Score range",
        "Expected implementation cost per valid result",
        "Implementation cost median per attempt",
        "Implementation cost mean",
        "Implementation cost stdev",
        "Implementation cost min",
        "Implementation cost max",
        "Implementation cost range",
        "Evaluation overhead per attempt",
        "Success rate",
        "Repetitions",
        "Judge spread",
        "Tokens median",
        "Tokens mean",
        "Tokens min",
        "Tokens max",
        "Tokens range",
        "Duration median (s)",
        "Duration mean (s)",
        "Duration min (s)",
        "Duration max (s)",
        "Duration range (s)",
        "Classification",
        "Dominator",
        "Marginal cost/quality",
        "Eligible",
        "Ineligible reasons",
        "Run IDs",
        "Harness",
    ]
    thead = "".join(f'<th scope="col">{_escape(h)}</th>' for h in headers)
    rows: list[str] = []
    for m in models:
        reasons = ", ".join(m.get("ineligible_reasons") or []) or "—"
        run_ids = ", ".join(m.get("run_ids") or []) or "—"
        cells = [
            _escape(m.get("display_name") or m["model_id"]),
            _escape(_fmt_num(m.get("score"))),
            _escape(_fmt_num(m.get("score_mean"))),
            _escape(_fmt_num(m.get("score_stdev"))),
            _escape(_fmt_num(m.get("score_min"))),
            _escape(_fmt_num(m.get("score_max"))),
            _escape(_fmt_num(m.get("score_range"))),
            _escape(_fmt_num(m.get("expected_cost"))),
            _escape(_fmt_num(m.get("implementation_cost_per_attempt"))),
            _escape(_fmt_num(m.get("cost_mean"))),
            _escape(_fmt_num(m.get("cost_stdev"))),
            _escape(_fmt_num(m.get("cost_min"))),
            _escape(_fmt_num(m.get("cost_max"))),
            _escape(_fmt_num(m.get("cost_range"))),
            _escape(_fmt_num(m.get("evaluation_cost_per_attempt"))),
            _escape(_fmt_num(m.get("success_rate"))),
            _escape(m.get("repetitions")),
            _escape(_fmt_num(m.get("judge_spread"))),
            _escape(m.get("tokens")),
            _escape(_fmt_num(m.get("tokens_mean"))),
            _escape(m.get("tokens_min")),
            _escape(m.get("tokens_max")),
            _escape(m.get("tokens_range")),
            _escape(_fmt_num(m.get("duration_s"))),
            _escape(_fmt_num(m.get("duration_mean_s"))),
            _escape(_fmt_num(m.get("duration_min_s"))),
            _escape(_fmt_num(m.get("duration_max_s"))),
            _escape(_fmt_num(m.get("duration_range_s"))),
            _escape(m.get("classification")),
            _escape(m.get("dominator") if m.get("dominator") is not None else "—"),
            _escape(_fmt_num(m.get("marginal_cost_per_quality"))),
            _escape("yes" if m.get("eligible") else "no"),
            _escape(reasons),
            _escape(run_ids),
            _escape(m.get("harness")),
        ]
        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    return (
        '<table class="raw-table">'
        "<caption>Aggregate model metrics for this contract section</caption>"
        f"<thead><tr>{thead}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _raw_attempts_table(models: Sequence[Mapping[str, Any]]) -> str:
    headers = [
        "Run ID",
        "Submission ID",
        "Repetition",
        "Model",
        "Harness",
        "Implementation success",
        "Evaluation success",
        "Score",
        "Implementation cost",
        "Evaluation cost",
        "Evaluator count",
        "Tokens",
        "Duration",
        "Reason labels",
    ]
    thead = "".join(f'<th scope="col">{_escape(h)}</th>' for h in headers)
    rows: list[str] = []
    for m in models:
        raw_value = m.get("raw_attempts") or []
        assert isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes))
        raw_list = list(raw_value)
        assert all(isinstance(raw, Mapping) for raw in raw_list)
        raw_list.sort(key=_raw_attempt_sort_key)
        for raw in raw_list:
            reasons = ", ".join(raw.get("ineligible_reasons") or []) or "—"
            evals = raw.get("evaluator_ids") or []
            cells = [
                _escape(raw.get("run_id")),
                _escape(raw.get("submission_id")),
                _escape(raw.get("repetition")),
                _escape(raw.get("display_name") or raw.get("model_id") or m.get("model_id")),
                _escape(raw.get("harness")),
                _escape("yes" if raw.get("implementation_success") else "no"),
                _escape("yes" if raw.get("evaluation_success") else "no"),
                _escape(_fmt_num(raw.get("score"))),
                _escape(_fmt_num(raw.get("implementation_cost_usd"))),
                _escape(_fmt_num(raw.get("evaluation_cost_usd"))),
                _escape(len(evals) if isinstance(evals, (list, tuple)) else 0),
                _escape(raw.get("tokens")),
                _escape(_fmt_num(raw.get("duration_s"))),
                _escape(reasons),
            ]
            rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    if not rows:
        rows.append('<tr><td colspan="14" class="muted">No raw attempts.</td></tr>')
    return (
        '<table class="attempts-table">'
        "<caption>Per-attempt raw results including failures "
        "(missing scores and costs shown as —)</caption>"
        f"<thead><tr>{thead}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _methodology_html() -> str:
    return """
<section class="methodology" id="methodology">
  <h2>Methodology and provenance</h2>
  <ul>
    <li><strong>Expected implementation cost per valid result</strong> is
      <code>implementation_cost_per_attempt / success_rate</code>
      (equal to <code>cost_per_attempt / success_rate</code> when aggregates
      are consistent). It estimates the expected implementation cost per valid
      submission. When success rate is zero, or inputs are non-finite or
      negative, expected cost is undefined and the point cannot join the
      Pareto frontier.</li>
    <li><strong>Evaluation overhead per attempt</strong>
      (<code>evaluation_cost_per_attempt</code>) is reported for transparency
      only. It never changes Pareto membership or expected implementation
      cost.</li>
    <li><strong>Success handling</strong>: a success rate of zero forces the
      entry ineligible even if the source marked it eligible. Ineligible and
      failed attempts remain visible in tables and denominators.</li>
    <li><strong>Pareto frontier</strong>: among eligible points with finite
      nonnegative score and expected implementation cost, point A dominates B
      when A has score ≥ B and expected cost ≤ B, with at least one strict
      inequality. Exact score/cost ties keep the lexicographically smaller
      <code>model_id</code> as the frontier representative. Among multiple
      dominators, prefer lowest expected cost, then highest score, then
      lexicographically smallest model id.</li>
    <li><strong>Marginal cost per quality</strong> is computed only between
      adjacent frontier points ordered by increasing score:
      Δexpected_cost / Δscore when Δscore &gt; 0.</li>
    <li><strong>Contracts and hashes</strong>: FE and BE tracks are never
      combined. Distinct <code>contract_version</code> /
      <code>contract_sha256</code> values form separate report sections.</li>
    <li><strong>Schema version and generated_at</strong> are carried from the
      source leaderboard files only; this report does not inject wall-clock
      time.</li>
    <li><strong>Harness</strong> identifies the agent/tooling configuration
      that produced the attempts.</li>
    <li><strong>Judge spread and error bars</strong>: score standard deviation
      and judge spread quantify observed variance; cost standard deviation is
      shown as horizontal error bars. These do not make nondeterministic
      models deterministic.</li>
    <li><strong>Colors</strong> are stable HSL values derived from
      SHA-256(<code>model_id</code>). Classification is also labeled in text.</li>
    <li>Only run IDs/hashes are shown; filesystem paths are never rendered.</li>
  </ul>
</section>
"""


def render_report_html(payload: Mapping[str, Any]) -> str:
    """Render a self-contained offline HTML5 report for *payload*."""
    sections = list(payload.get("sections") or [])
    body_parts: list[str] = [
        "<header><h1>Basecamp Bench Report</h1>",
        '<p class="lede">Deterministic cost-vs-quality report. '
        "FE and BE tracks and contract revisions are shown in separate "
        "sections. Expected implementation cost uses implementation cost "
        "per attempt divided by success rate; cheaper models appear to the "
        "right on charts. Evaluation overhead is shown separately and does "
        "not affect the Pareto frontier.</p></header>",
        _methodology_html(),
    ]

    for index, section in enumerate(sections):
        track = section.get("track", "")
        cv = section.get("contract_version", "")
        sha = section.get("contract_sha256", "")
        schema = section.get("schema_version")
        generated = section.get("generated_at")
        models = list(section.get("models") or [])
        sid = f"section-{index}-{track}-{cv}"

        body_parts.append(f'<section class="track-section" id="{_escape(sid)}">')
        body_parts.append(f"<h2>Track {_escape(track)} · contract {_escape(cv)}</h2>")
        body_parts.append('<dl class="provenance">')
        body_parts.append(f"<dt>contract_sha256</dt><dd><code>{_escape(sha)}</code></dd>")
        body_parts.append(
            f"<dt>schema_version</dt><dd>{_escape(schema if schema is not None else 'null')}</dd>"
        )
        body_parts.append(
            f"<dt>generated_at</dt><dd>{_escape(generated if generated is not None else 'null')}</dd>"
        )
        for name in (
            "mode",
            "runner_source_sha256",
            "seed_tree_sha256",
            "reference_manifest_sha256",
            "reference_tree_sha256",
            "prompt_sha256",
            "rubric_sha256",
            "schema_bundle_sha256",
        ):
            body_parts.append(
                f"<dt>{name}</dt><dd><code>{_escape(section.get(name, 'null'))}</code></dd>"
            )
        body_parts.append(
            f"<dt>source timestamps</dt><dd>{_escape(', '.join(section.get('generated_at_values') or []) or '—')}</dd>"
        )
        body_parts.append(
            f"<dt>source run IDs</dt><dd>{_escape(', '.join(section.get('source_run_ids') or []) or '—')}</dd>"
        )
        body_parts.append(
            f"<dt>frontier</dt><dd>{_escape(', '.join(section.get('frontier') or []) or '—')}</dd>"
        )
        body_parts.append("</dl>")

        body_parts.append("<h3>Cost vs quality</h3>")
        body_parts.append(_chart_svg(section, index))

        body_parts.append("<h3>Dimension profile</h3>")
        profile = section.get("dimension_profile") or []
        body_parts.append(
            "<table><thead><tr><th>ID</th><th>Label</th><th>Weight</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td><code>{_escape(row.get('id'))}</code></td><td>{_escape(row.get('label'))}</td><td>{_fmt_num(row.get('weight'))}</td></tr>"
                for row in profile
                if isinstance(row, Mapping)
            )
            + "</tbody></table>"
        )
        body_parts.append(_dimension_table(models))

        body_parts.append("<h3>Aggregate model table</h3>")
        body_parts.append(_models_table(models))

        body_parts.append("<h3>Raw attempt table</h3>")
        body_parts.append(_raw_attempts_table(models))
        body_parts.append("</section>")

    embedded = _embed_json(payload)
    css = """
:root { color-scheme: light dark; --bg:#f7f7f5; --fg:#1a1a1a; --muted:#5c5c5c;
  --card:#fff; --border:#d0d0cc; --frontier:#0b6e4f; --dom:#8a6d3b; --inel:#8a8a8a; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141414; --fg:#f0f0f0; --muted:#a0a0a0; --card:#1e1e1e;
    --border:#333; --frontier:#3dcea0; --dom:#e0b15c; --inel:#888; }
}
* { box-sizing: border-box; }
body { margin:0; font: 15px/1.45 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
header, section { max-width: 1080px; margin: 0 auto; padding: 1.25rem 1rem; }
header { border-bottom: 1px solid var(--border); }
h1 { font-size: 1.6rem; margin: 0 0 .5rem; }
h2 { font-size: 1.25rem; margin-top: 1.5rem; }
h3 { font-size: 1.05rem; margin-top: 1.25rem; }
.lede, .muted { color: var(--muted); }
.track-section { background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; margin: 1rem auto; }
.provenance { display:grid; grid-template-columns: 12rem 1fr; gap:.25rem .75rem; }
.provenance dt { font-weight:600; }
.provenance dd { margin:0; word-break: break-all; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; font-size: 13px; }
th, td { border: 1px solid var(--border); padding: .35rem .5rem; text-align: left; vertical-align: top; }
th { background: rgba(127,127,127,.12); }
.chart { width: 100%; max-width: 720px; height: auto; background: transparent; }
.chart-bg { fill: transparent; }
.axis { stroke: var(--fg); stroke-width: 1.2; }
.axis-label { fill: var(--fg); font-size: 12px; }
.frontier-line { stroke: var(--frontier); stroke-width: 2; stroke-dasharray: 4 2; }
.frontier-point { stroke: #000; stroke-width: 1.2; }
.dominated-point { opacity: .75; stroke: var(--dom); stroke-width: 1; stroke-dasharray: 2 2; }
.ineligible-point { opacity: .45; stroke: var(--inel); stroke-width: 1; fill-opacity: .5; }
.error-bar { stroke-width: 1; opacity: .7; }
.point-label { font-size: 11px; fill: var(--fg); }
.methodology ul { padding-left: 1.2rem; }
table caption { caption-side: top; text-align: left; font-weight: 600; margin-bottom: .35rem; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .92em; }
"""

    doc = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8"/>\n'
        "<title>Basecamp Bench Report</title>\n"
        f"<style>\n{css}\n</style>\n"
        "</head>\n"
        "<body>\n"
        f"{''.join(body_parts)}\n"
        f'<script type="application/json" id="report-payload">{embedded}</script>\n'
        "</body>\n"
        "</html>\n"
    )
    return doc


def write_report(paths: Sequence[Path], output: Path) -> Path:
    """Load leaderboards, build payload, render HTML, atomically write *output*."""
    output = Path(output)
    points = load_leaderboards(paths)
    payload = build_report_payload(points)
    html_text = render_report_html(payload)

    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=os.fspath(parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(html_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, os.fspath(output))
        tmp_path = Path()  # successfully replaced
    except Exception:
        if tmp_path != Path() and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
    return output
