"""Evaluation contract loading, validation, scoring, and aggregation."""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeGuard

from basecamp_bench.validation import is_finite_number, is_sha256_hex

__all__ = [
    "Dimension",
    "EvaluationContract",
    "ValidatedJudgeScores",
    "load_contract",
    "contract_sha256",
    "validate_contract_data",
    "validate_judge_result",
    "normalize_validated_judge_result",
    "compute_weighted_score",
    "aggregate_judges",
]

_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_WEIGHT_SUM_TOLERANCE = 1e-9
_SCHEMA_VERSION = "1.0"
_ANCHOR_KEYS = ("0", "5", "10")
_TRACKS = frozenset({"fe", "be"})
_ROOT_KEYS = (
    "schema_version",
    "contract_version",
    "track",
    "description",
    "dimensions",
    "overall_policy",
)
_DIMENSION_KEYS = ("id", "label", "weight", "anchors")
_POLICY_KEYS = ("method", "precision", "missing")
_JUDGE_ROOT_KEYS = (
    "schema_version",
    "track",
    "submission_id",
    "contract_sha256",
    "judge_id",
    "dimensions",
    "summary",
)
_JUDGE_DIM_KEYS = ("score", "notes", "evidence")
_SAFE_ID_HINT = (
    "expected safe identifier "
    "(lowercase letter/digit start; lowercase letters, "
    "digits, dot, underscore, or hyphen thereafter)"
)
_SCORE_HINT = "expected finite number 0..10 (bool excluded)"


@dataclass(frozen=True, slots=True)
class Dimension:
    id: str
    label: str
    weight: float
    anchors: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class EvaluationContract:
    schema_version: str
    contract_version: str
    track: str
    description: str
    dimensions: tuple[Dimension, ...]
    overall_policy: Mapping[str, Any]


_VALIDATED_JUDGE_TOKEN = object()


@dataclass(frozen=True, slots=True)
class ValidatedJudgeScores:
    """Immutable judge scores produced only after full judge-result validation.

    Construct exclusively via :func:`normalize_validated_judge_result`. Direct
    construction is rejected so callers cannot pass raw score projections into
    aggregation by accident.
    """

    judge_id: str
    scores: Mapping[str, float]
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _VALIDATED_JUDGE_TOKEN:
            raise TypeError(
                "ValidatedJudgeScores cannot be constructed directly; "
                "use normalize_validated_judge_result(...)"
            )
        if not _is_safe_identifier(self.judge_id):
            raise ValueError(f"judge_id: {_SAFE_ID_HINT}")
        if not isinstance(self.scores, Mapping):
            raise ValueError("scores: expected mapping of dimension id to score")
        frozen_scores: dict[str, float] = {}
        for dim_id, score in self.scores.items():
            if not isinstance(dim_id, str):
                raise ValueError("scores: expected string dimension ids")
            frozen_scores[dim_id] = _require_score(score, f"scores[{dim_id!r}]")
        object.__setattr__(self, "scores", MappingProxyType(frozen_scores))


def _is_safe_identifier(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and bool(_IDENTIFIER_RE.fullmatch(value))


def _is_nonempty_string(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and value != ""


def _check_exact_keys(
    obj: Any,
    expected: Sequence[str],
    path: str,
    errors: list[str],
) -> TypeGuard[dict[str, Any]]:
    if not isinstance(obj, dict):
        errors.append(f"{path}: expected object")
        return False
    expected_set, actual = set(expected), set(obj.keys())
    for key in sorted(expected_set - actual):
        errors.append(f"{path}: missing key {key!r}")
    for key in sorted(actual - expected_set):
        errors.append(f"{path}: unknown key {key!r}")
    return actual == expected_set


def _check_nonempty_string_list(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{path}: expected nonempty list of nonempty strings")
        return
    for index, item in enumerate(value):
        if not _is_nonempty_string(item):
            errors.append(f"{path}[{index}]: expected nonempty string")


def _dim_ids(contract: EvaluationContract) -> list[str]:
    return [dim.id for dim in contract.dimensions]


def _as_score(value: Any) -> float | None:
    if not is_finite_number(value):
        return None
    number = float(value)
    return number if 0.0 <= number <= 10.0 else None


def _require_score(value: Any, path: str) -> float:
    number = _as_score(value)
    if number is None:
        raise ValueError(f"{path}: {_SCORE_HINT}")
    return number


def _mismatch_parts(
    actual: set[str],
    expected: Sequence[str],
    *,
    missing_label: str,
    extra_label: str,
) -> list[str]:
    expected_set = set(expected)
    missing, extra = sorted(expected_set - actual), sorted(actual - expected_set)
    parts: list[str] = []
    if missing:
        parts.append(f"{missing_label} {missing}")
    if extra:
        parts.append(f"{extra_label} {extra}")
    return parts


def validate_contract_data(data: Any) -> list[str]:
    """Validate contract JSON data; return all deterministic errors."""
    errors: list[str] = []
    if not _check_exact_keys(data, _ROOT_KEYS, "contract", errors):
        if not isinstance(data, dict):
            return errors
    if not isinstance(data, dict):
        return errors

    if "schema_version" in data and data.get("schema_version") != _SCHEMA_VERSION:
        errors.append("contract.schema_version: expected exactly '1.0'")
    if "contract_version" in data and not _is_nonempty_string(data.get("contract_version")):
        errors.append("contract.contract_version: expected nonempty string")
    if "track" in data and data.get("track") not in _TRACKS:
        errors.append("contract.track: expected exactly 'fe' or 'be'")
    if "description" in data and not _is_nonempty_string(data.get("description")):
        errors.append("contract.description: expected nonempty string")

    if "overall_policy" in data:
        policy = data.get("overall_policy")
        if _check_exact_keys(policy, _POLICY_KEYS, "contract.overall_policy", errors):
            if policy.get("method") != "weighted_sum":
                errors.append("contract.overall_policy.method: expected exactly 'weighted_sum'")
            precision = policy.get("precision")
            if (
                isinstance(precision, bool)
                or not isinstance(precision, int)
                or not 0 <= precision <= 12
            ):
                errors.append("contract.overall_policy.precision: expected integer 0..12")
            if policy.get("missing") != "invalidate":
                errors.append("contract.overall_policy.missing: expected exactly 'invalidate'")

    if "dimensions" not in data:
        return errors
    dimensions = data.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        errors.append("contract.dimensions: expected nonempty list")
        return errors

    dim_ids: list[str] = []
    weight_sum = 0.0
    weights_ok = True
    for index, dim in enumerate(dimensions):
        dpath = f"contract.dimensions[{index}]"
        if not _check_exact_keys(dim, _DIMENSION_KEYS, dpath, errors):
            continue
        dim_id = dim.get("id")
        if not _is_safe_identifier(dim_id):
            errors.append(f"{dpath}.id: {_SAFE_ID_HINT}")
        else:
            dim_ids.append(dim_id)
        if not _is_nonempty_string(dim.get("label")):
            errors.append(f"{dpath}.label: expected nonempty string")
        weight = dim.get("weight")
        if not is_finite_number(weight) or float(weight) <= 0:
            errors.append(f"{dpath}.weight: expected finite number > 0 (bool excluded)")
            weights_ok = False
        else:
            weight_sum += float(weight)
        anchors = dim.get("anchors")
        if _check_exact_keys(anchors, _ANCHOR_KEYS, f"{dpath}.anchors", errors):
            for key in _ANCHOR_KEYS:
                if not _is_nonempty_string(anchors.get(key)):
                    errors.append(f"{dpath}.anchors[{key!r}]: expected nonempty string")

    if len(dim_ids) != len(set(dim_ids)):
        seen: set[str] = set()
        for dim_id in dim_ids:
            if dim_id in seen:
                errors.append(f"contract.dimensions: duplicate dimension id {dim_id!r}")
            seen.add(dim_id)
    if weights_ok and dim_ids and abs(weight_sum - 1.0) > _WEIGHT_SUM_TOLERANCE:
        errors.append(
            f"contract.dimensions: weights must sum to 1.0 "
            f"(within {_WEIGHT_SUM_TOLERANCE}); got {weight_sum!r}"
        )
    return errors


def _build_contract(data: Mapping[str, Any]) -> EvaluationContract:
    dimensions = tuple(
        Dimension(
            id=raw["id"],
            label=raw["label"],
            weight=float(raw["weight"]),
            anchors=MappingProxyType({key: str(raw["anchors"][key]) for key in _ANCHOR_KEYS}),
        )
        for raw in data["dimensions"]
    )
    policy = data["overall_policy"]
    return EvaluationContract(
        schema_version=data["schema_version"],
        contract_version=data["contract_version"],
        track=data["track"],
        description=data["description"],
        dimensions=dimensions,
        overall_policy=MappingProxyType(
            {
                "method": policy["method"],
                "precision": int(policy["precision"]),
                "missing": policy["missing"],
            }
        ),
    )


def load_contract(path: str | Path) -> EvaluationContract:
    """Load and validate a UTF-8 JSON evaluation contract from *path*."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate_contract_data(data)
    if errors:
        raise ValueError("Invalid contract:\n" + "\n".join(errors))
    return _build_contract(data)


def contract_sha256(path: str | Path) -> str:
    """Return lowercase SHA-256 hex digest of the exact file bytes at *path*."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_judge_result(
    data: Any,
    contract: EvaluationContract,
    *,
    expected_track: str,
    expected_submission_id: str,
    expected_contract_sha256: str,
    expected_judge_id: str,
) -> list[str]:
    """Validate a judge result against *contract* and expected identities.

    Returns descriptive errors; never raises for malformed input.
    """
    errors: list[str] = []
    if not _check_exact_keys(data, _JUDGE_ROOT_KEYS, "judge_result", errors):
        if not isinstance(data, dict):
            return errors
    if not isinstance(data, dict):
        return errors

    if "schema_version" in data:
        schema_version = data.get("schema_version")
        if not _is_nonempty_string(schema_version):
            errors.append("judge_result.schema_version: expected nonempty string")
        elif schema_version != contract.schema_version:
            errors.append(
                "judge_result.schema_version: expected "
                f"{contract.schema_version!r}, got {schema_version!r}"
            )

    if "track" in data:
        track = data.get("track")
        if track != contract.track:
            errors.append(
                f"judge_result.track: expected contract track {contract.track!r}, got {track!r}"
            )
        if track != expected_track:
            errors.append(f"judge_result.track: expected {expected_track!r}, got {track!r}")

    if "submission_id" in data:
        submission_id = data.get("submission_id")
        if not _is_safe_identifier(submission_id):
            errors.append(f"judge_result.submission_id: {_SAFE_ID_HINT}")
        if submission_id != expected_submission_id:
            errors.append(
                "judge_result.submission_id: expected "
                f"{expected_submission_id!r}, got {submission_id!r}"
            )

    if "contract_sha256" in data:
        contract_hash = data.get("contract_sha256")
        if not is_sha256_hex(contract_hash):
            errors.append("judge_result.contract_sha256: expected lowercase 64-char hex SHA-256")
        if contract_hash != expected_contract_sha256:
            errors.append(
                "judge_result.contract_sha256: expected "
                f"{expected_contract_sha256!r}, got {contract_hash!r}"
            )

    if "judge_id" in data:
        judge_id = data.get("judge_id")
        if not _is_safe_identifier(judge_id):
            errors.append(f"judge_result.judge_id: {_SAFE_ID_HINT}")
        if judge_id != expected_judge_id:
            errors.append(
                f"judge_result.judge_id: expected {expected_judge_id!r}, got {judge_id!r}"
            )

    if "summary" in data and not _is_nonempty_string(data.get("summary")):
        errors.append("judge_result.summary: expected nonempty string")

    if "dimensions" not in data:
        return errors
    dimensions = data.get("dimensions")
    if not isinstance(dimensions, dict):
        errors.append("judge_result.dimensions: expected object keyed by dimension id")
        return errors

    expected_dims = set(_dim_ids(contract))
    actual_dims = set(dimensions.keys())
    for dim_id in sorted(expected_dims - actual_dims):
        errors.append(f"judge_result.dimensions: missing dimension {dim_id!r}")
    for dim_id in sorted(actual_dims - expected_dims):
        errors.append(f"judge_result.dimensions: unknown dimension {dim_id!r}")
    for dim_id in sorted(actual_dims & expected_dims):
        entry = dimensions[dim_id]
        dpath = f"judge_result.dimensions[{dim_id!r}]"
        if not _check_exact_keys(entry, _JUDGE_DIM_KEYS, dpath, errors):
            continue
        if _as_score(entry.get("score")) is None:
            errors.append(f"{dpath}.score: {_SCORE_HINT}")
        if not _is_nonempty_string(entry.get("notes")):
            errors.append(f"{dpath}.notes: expected nonempty string")
        _check_nonempty_string_list(entry.get("evidence"), f"{dpath}.evidence", errors)
    return errors


def _weighted_sum(scores: Mapping[str, float], contract: EvaluationContract) -> float:
    total = sum(float(scores[dim.id]) * float(dim.weight) for dim in contract.dimensions)
    return round(total, int(contract.overall_policy["precision"]))


def compute_weighted_score(scores: Mapping[str, Any], contract: EvaluationContract) -> float:
    """Compute runner-owned weighted overall score using contract precision."""
    if not isinstance(scores, Mapping):
        raise ValueError("scores: expected mapping of dimension id to score")
    expected = _dim_ids(contract)
    parts = _mismatch_parts(
        set(scores.keys()),
        expected,
        missing_label="missing dimension ids:",
        extra_label="extra dimension ids:",
    )
    if parts:
        raise ValueError("; ".join(parts))
    ordered = {dim_id: _require_score(scores[dim_id], f"scores[{dim_id!r}]") for dim_id in expected}
    return _weighted_sum(ordered, contract)


def normalize_validated_judge_result(
    data: Any,
    contract: EvaluationContract,
    *,
    expected_track: str,
    expected_submission_id: str,
    expected_contract_sha256: str,
    expected_judge_id: str,
) -> ValidatedJudgeScores:
    """Run full judge-result validation and return normalized scores.

    The only construction path for :class:`ValidatedJudgeScores`. Raises
    :class:`ValueError` with all validator errors when the raw result is invalid.
    """
    errors = validate_judge_result(
        data,
        contract,
        expected_track=expected_track,
        expected_submission_id=expected_submission_id,
        expected_contract_sha256=expected_contract_sha256,
        expected_judge_id=expected_judge_id,
    )
    if errors:
        raise ValueError("Invalid judge result:\n" + "\n".join(errors))
    assert isinstance(data, dict)
    dimensions = data["dimensions"]
    assert isinstance(dimensions, dict)
    order = _dim_ids(contract)
    scores: dict[str, float] = {}
    for dim_id in order:
        entry = dimensions[dim_id]
        scores[dim_id] = _require_score(
            entry["score"], f"judge_result.dimensions[{dim_id!r}].score"
        )
    return ValidatedJudgeScores(
        judge_id=str(data["judge_id"]),
        scores=MappingProxyType(scores),
        _token=_VALIDATED_JUDGE_TOKEN,
    )


def aggregate_judges(
    results: Sequence[ValidatedJudgeScores],
    contract: EvaluationContract,
) -> dict[str, Any]:
    """Aggregate multi-judge scores already normalized via full validation.

    Rejects raw full judge mappings and bare score mappings at runtime; only
    :class:`ValidatedJudgeScores` instances are accepted.
    """
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        raise ValueError("results: expected nonempty sequence of ValidatedJudgeScores")
    if not results:
        raise ValueError("results: expected at least one ValidatedJudgeScores")

    per_dim: dict[str, list[float]] = {dim.id: [] for dim in contract.dimensions}
    judges_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    order = _dim_ids(contract)

    for index, result in enumerate(results):
        if not isinstance(result, ValidatedJudgeScores):
            raise TypeError(
                f"results[{index}]: expected ValidatedJudgeScores from "
                f"normalize_validated_judge_result(...), got {type(result).__name__}"
            )
        if result.judge_id in seen:
            raise ValueError(f"results[{index}].judge_id: duplicate judge id {result.judge_id!r}")
        seen.add(result.judge_id)
        missing = [dim_id for dim_id in order if dim_id not in result.scores]
        extra = sorted(set(result.scores) - set(order))
        if missing or extra:
            parts: list[str] = []
            if missing:
                parts.append(f"missing dimensions {missing}")
            if extra:
                parts.append(f"extra dimensions {extra}")
            raise ValueError(f"results[{index}].scores: " + "; ".join(parts))
        ordered = {dim_id: float(result.scores[dim_id]) for dim_id in order}
        for dim_id, value in ordered.items():
            per_dim[dim_id].append(value)
        judges_out.append(
            {
                "judge_id": result.judge_id,
                "scores": ordered,
                "overall": _weighted_sum(ordered, contract),
            }
        )

    dimensions_out: dict[str, dict[str, float]] = {}
    medians: dict[str, float] = {}
    for dim_id in order:
        values = per_dim[dim_id]
        median = float(statistics.median(values))
        medians[dim_id] = median
        dimensions_out[dim_id] = {
            "median": median,
            "stdev": 0.0 if len(values) == 1 else float(statistics.pstdev(values)),
            "min": float(min(values)),
            "max": float(max(values)),
        }
    return {
        "dimensions": dimensions_out,
        "judges": judges_out,
        "overall": _weighted_sum(medians, contract),
    }
