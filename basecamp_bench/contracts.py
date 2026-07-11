"""Evaluation contract loading, validation, scoring, and aggregation.

Machine-readable contracts define dimensions, anchors, weights, and overall
policy. Judges return per-dimension scores only; the runner validates shapes
and computes weighted totals using contract-owned weights exclusively.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeGuard

from basecamp_bench.validation import is_finite_number, is_sha256_hex

__all__ = [
    "Dimension",
    "EvaluationContract",
    "load_contract",
    "contract_sha256",
    "validate_contract_data",
    "validate_judge_result",
    "compute_weighted_score",
    "aggregate_judges",
    "aggregate_repetitions",
]

_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_WEIGHT_SUM_TOLERANCE = 1e-9
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
_REP_KEYS = frozenset({"score", "success"})


@dataclass(frozen=True, slots=True)
class Dimension:
    """One scored evaluation dimension with label, weight, and anchors."""

    id: str
    label: str
    weight: float
    anchors: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class EvaluationContract:
    """Versioned evaluation contract for one track."""

    schema_version: str
    contract_version: str
    track: str
    description: str
    dimensions: tuple[Dimension, ...]
    overall_policy: Mapping[str, Any]


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
    """Require an object with exactly the given keys. Append errors if not."""
    if not isinstance(obj, dict):
        errors.append(f"{path}: expected object")
        return False
    expected_set = set(expected)
    actual = set(obj.keys())
    for key in sorted(expected_set - actual):
        errors.append(f"{path}: missing key {key!r}")
    for key in sorted(actual - expected_set):
        errors.append(f"{path}: unknown key {key!r}")
    return actual == expected_set


def _validate_nonempty_string_list(
    value: Any,
    path: str,
    errors: list[str],
) -> None:
    if not isinstance(value, list):
        errors.append(f"{path}: expected nonempty list of nonempty strings")
        return
    if not value:
        errors.append(f"{path}: expected nonempty list of nonempty strings")
        return
    for index, item in enumerate(value):
        if not _is_nonempty_string(item):
            errors.append(f"{path}[{index}]: expected nonempty string")


def validate_contract_data(data: Any) -> list[str]:
    """Validate contract JSON data; return all deterministic errors."""
    errors: list[str] = []
    if not _check_exact_keys(data, _ROOT_KEYS, "contract", errors):
        # Still attempt partial validation when the root is a dict.
        if not isinstance(data, dict):
            return errors

    if isinstance(data, dict):
        schema_version = data.get("schema_version")
        if "schema_version" in data and schema_version != "1.0":
            errors.append("contract.schema_version: expected exactly '1.0'")

        contract_version = data.get("contract_version")
        if "contract_version" in data and not _is_nonempty_string(contract_version):
            errors.append("contract.contract_version: expected nonempty string")

        track = data.get("track")
        if "track" in data and track not in _TRACKS:
            errors.append("contract.track: expected exactly 'fe' or 'be'")

        description = data.get("description")
        if "description" in data and not _is_nonempty_string(description):
            errors.append("contract.description: expected nonempty string")

        overall_policy = data.get("overall_policy")
        if "overall_policy" in data:
            if _check_exact_keys(overall_policy, _POLICY_KEYS, "contract.overall_policy", errors):
                method = overall_policy.get("method")
                if method != "weighted_sum":
                    errors.append("contract.overall_policy.method: expected exactly 'weighted_sum'")
                precision = overall_policy.get("precision")
                if (
                    isinstance(precision, bool)
                    or not isinstance(precision, int)
                    or not 0 <= precision <= 12
                ):
                    errors.append("contract.overall_policy.precision: expected integer 0..12")
                if overall_policy.get("missing") != "invalidate":
                    errors.append("contract.overall_policy.missing: expected exactly 'invalidate'")

        dimensions = data.get("dimensions")
        dim_ids: list[str] = []
        weight_sum = 0.0
        weights_ok = True
        if "dimensions" in data:
            if not isinstance(dimensions, list) or not dimensions:
                errors.append("contract.dimensions: expected nonempty list")
            else:
                for index, dim in enumerate(dimensions):
                    dpath = f"contract.dimensions[{index}]"
                    if not _check_exact_keys(dim, _DIMENSION_KEYS, dpath, errors):
                        continue
                    dim_id = dim.get("id")
                    if not _is_safe_identifier(dim_id):
                        errors.append(
                            f"{dpath}.id: expected safe identifier "
                            "(lowercase letter/digit start; lowercase letters, "
                            "digits, dot, underscore, or hyphen thereafter)"
                        )
                    else:
                        dim_ids.append(dim_id)

                    label = dim.get("label")
                    if not _is_nonempty_string(label):
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


def _freeze_anchors(anchors: Mapping[str, Any]) -> Mapping[str, str]:
    return MappingProxyType({key: str(anchors[key]) for key in _ANCHOR_KEYS})


def _freeze_policy(policy: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "method": policy["method"],
            "precision": int(policy["precision"]),
            "missing": policy["missing"],
        }
    )


def _build_contract(data: Mapping[str, Any]) -> EvaluationContract:
    dimensions: list[Dimension] = []
    for raw in data["dimensions"]:
        dimensions.append(
            Dimension(
                id=raw["id"],
                label=raw["label"],
                weight=float(raw["weight"]),
                anchors=_freeze_anchors(raw["anchors"]),
            )
        )
    return EvaluationContract(
        schema_version=data["schema_version"],
        contract_version=data["contract_version"],
        track=data["track"],
        description=data["description"],
        dimensions=tuple(dimensions),
        overall_policy=_freeze_policy(data["overall_policy"]),
    )


def load_contract(path: str | Path) -> EvaluationContract:
    """Load and validate a UTF-8 JSON evaluation contract from *path*."""
    contract_path = Path(path)
    text = contract_path.read_text(encoding="utf-8")
    data = json.loads(text)
    errors = validate_contract_data(data)
    if errors:
        raise ValueError("Invalid contract:\n" + "\n".join(errors))
    return _build_contract(data)


def contract_sha256(path: str | Path) -> str:
    """Return lowercase SHA-256 hex digest of the exact file bytes at *path*."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _all_dimension_ids(contract: EvaluationContract) -> list[str]:
    return [dim.id for dim in contract.dimensions]


def _dimension_weights(contract: EvaluationContract) -> dict[str, float]:
    return {dim.id: float(dim.weight) for dim in contract.dimensions}


def _validate_score_value(value: Any, path: str, errors: list[str]) -> None:
    if not is_finite_number(value):
        errors.append(f"{path}: expected finite number 0..10 (bool excluded)")
        return
    number = float(value)
    if number < 0.0 or number > 10.0:
        errors.append(f"{path}: expected finite number 0..10 (bool excluded)")


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

    Always returns a list of human-readable errors; never raises for malformed
    input. ``schema_version`` and ``track`` must match the contract; identity
    and hash fields are also checked against the explicit expected arguments.
    """
    errors: list[str] = []
    if not _check_exact_keys(data, _JUDGE_ROOT_KEYS, "judge_result", errors):
        if not isinstance(data, dict):
            return errors

    if not isinstance(data, dict):
        return errors

    schema_version = data.get("schema_version")
    if "schema_version" in data:
        if not _is_nonempty_string(schema_version):
            errors.append("judge_result.schema_version: expected nonempty string")
        elif schema_version != contract.schema_version:
            errors.append(
                "judge_result.schema_version: expected "
                f"{contract.schema_version!r}, got {schema_version!r}"
            )

    track = data.get("track")
    if "track" in data:
        if track != contract.track:
            errors.append(
                f"judge_result.track: expected contract track {contract.track!r}, got {track!r}"
            )
        if track != expected_track:
            errors.append(f"judge_result.track: expected {expected_track!r}, got {track!r}")

    submission_id = data.get("submission_id")
    if "submission_id" in data:
        if not _is_safe_identifier(submission_id):
            errors.append(
                "judge_result.submission_id: expected safe identifier "
                "(lowercase letter/digit start; lowercase letters, "
                "digits, dot, underscore, or hyphen thereafter)"
            )
        if submission_id != expected_submission_id:
            errors.append(
                "judge_result.submission_id: expected "
                f"{expected_submission_id!r}, got {submission_id!r}"
            )

    contract_hash = data.get("contract_sha256")
    if "contract_sha256" in data:
        if not is_sha256_hex(contract_hash):
            errors.append("judge_result.contract_sha256: expected lowercase 64-char hex SHA-256")
        if contract_hash != expected_contract_sha256:
            errors.append(
                "judge_result.contract_sha256: expected "
                f"{expected_contract_sha256!r}, got {contract_hash!r}"
            )

    judge_id = data.get("judge_id")
    if "judge_id" in data:
        if not _is_safe_identifier(judge_id):
            errors.append(
                "judge_result.judge_id: expected safe identifier "
                "(lowercase letter/digit start; lowercase letters, "
                "digits, dot, underscore, or hyphen thereafter)"
            )
        if judge_id != expected_judge_id:
            errors.append(
                f"judge_result.judge_id: expected {expected_judge_id!r}, got {judge_id!r}"
            )

    summary = data.get("summary")
    if "summary" in data and not _is_nonempty_string(summary):
        errors.append("judge_result.summary: expected nonempty string")

    expected_dims = set(_all_dimension_ids(contract))
    dimensions = data.get("dimensions")
    if "dimensions" in data:
        if not isinstance(dimensions, dict):
            errors.append("judge_result.dimensions: expected object keyed by dimension id")
        else:
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
                _validate_score_value(entry.get("score"), f"{dpath}.score", errors)
                if not _is_nonempty_string(entry.get("notes")):
                    errors.append(f"{dpath}.notes: expected nonempty string")
                _validate_nonempty_string_list(entry.get("evidence"), f"{dpath}.evidence", errors)

    return errors


def compute_weighted_score(
    scores: Mapping[str, Any],
    contract: EvaluationContract,
) -> float:
    """Compute runner-owned weighted overall score using contract precision.

    *scores* must cover every contract dimension exactly. Invalid input raises
    ``ValueError``.
    """
    if not isinstance(scores, Mapping):
        raise ValueError("scores: expected mapping of dimension id to score")

    expected = _all_dimension_ids(contract)
    expected_set = set(expected)
    actual_set = set(scores.keys())
    missing = sorted(expected_set - actual_set)
    extra = sorted(actual_set - expected_set)
    problems: list[str] = []
    if missing:
        problems.append(f"missing dimension ids: {missing}")
    if extra:
        problems.append(f"extra dimension ids: {extra}")
    if problems:
        raise ValueError("; ".join(problems))

    total = 0.0
    weights = _dimension_weights(contract)
    for dim_id in expected:
        value = scores[dim_id]
        if not is_finite_number(value):
            raise ValueError(f"scores[{dim_id!r}]: expected finite number 0..10 (bool excluded)")
        number = float(value)
        if number < 0.0 or number > 10.0:
            raise ValueError(f"scores[{dim_id!r}]: expected finite number 0..10 (bool excluded)")
        total += number * weights[dim_id]
    return round(total, int(contract.overall_policy["precision"]))


def _extract_judge_scores(
    result: Any,
    contract: EvaluationContract,
    index: int,
) -> tuple[str, dict[str, float]]:
    """Extract judge_id and complete dimension scores from one aggregation result."""
    if not isinstance(result, Mapping):
        raise ValueError(f"results[{index}]: expected object")

    judge_id = result.get("judge_id")
    if not _is_safe_identifier(judge_id):
        raise ValueError(
            f"results[{index}].judge_id: expected safe identifier "
            "(lowercase letter/digit start; lowercase letters, "
            "digits, dot, underscore, or hyphen thereafter)"
        )

    dimensions = result.get("dimensions")
    if not isinstance(dimensions, Mapping):
        raise ValueError(f"results[{index}].dimensions: expected object keyed by dimension id")

    expected = _all_dimension_ids(contract)
    expected_set = set(expected)
    actual_set = set(dimensions.keys())
    missing = sorted(expected_set - actual_set)
    extra = sorted(actual_set - expected_set)
    if missing or extra:
        parts: list[str] = []
        if missing:
            parts.append(f"missing dimensions {missing}")
        if extra:
            parts.append(f"extra dimensions {extra}")
        raise ValueError(f"results[{index}].dimensions: " + "; ".join(parts))

    scores: dict[str, float] = {}
    for dim_id in expected:
        entry = dimensions[dim_id]
        if isinstance(entry, Mapping):
            if "score" not in entry:
                raise ValueError(f"results[{index}].dimensions[{dim_id!r}]: missing score")
            raw = entry["score"]
        else:
            raw = entry
        if not is_finite_number(raw):
            raise ValueError(
                f"results[{index}].dimensions[{dim_id!r}].score: "
                "expected finite number 0..10 (bool excluded)"
            )
        number = float(raw)
        if number < 0.0 or number > 10.0:
            raise ValueError(
                f"results[{index}].dimensions[{dim_id!r}].score: "
                "expected finite number 0..10 (bool excluded)"
            )
        scores[dim_id] = number
    assert isinstance(judge_id, str)
    return judge_id, scores


def _population_stdev(values: Sequence[float]) -> float:
    if len(values) == 1:
        return 0.0
    return float(statistics.pstdev(values))


def aggregate_judges(
    results: Sequence[Any],
    contract: EvaluationContract,
) -> dict[str, Any]:
    """Aggregate multi-judge results with runner-owned weights.

    Each result must provide:
    - ``judge_id`` (safe identifier; unique across *results*)
    - ``dimensions`` covering exactly all contract dimension IDs, each entry
      either a finite score or an object containing ``score``

    Return shape (deterministic key order by contract dimension order)::

        {
          "dimensions": {
            "<dim_id>": {
              "median": float,
              "stdev": float,   # population standard deviation
              "min": float,
              "max": float,
            },
            ...
          },
          "judges": [
            {
              "judge_id": str,
              "scores": {dim_id: float, ...},  # full dimension map
              "overall": float,                # weighted, contract precision
            },
            ...
          ],
          "overall": float,  # weighted sum of per-dimension medians
        }
    """
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        raise ValueError("results: expected nonempty sequence of judge results")
    if not results:
        raise ValueError("results: expected at least one judge result")

    per_dim_values: dict[str, list[float]] = {dim.id: [] for dim in contract.dimensions}
    judges_out: list[dict[str, Any]] = []
    seen_judge_ids: set[str] = set()

    for index, result in enumerate(results):
        judge_id, judge_scores = _extract_judge_scores(result, contract, index)
        if judge_id in seen_judge_ids:
            raise ValueError(f"results[{index}].judge_id: duplicate judge id {judge_id!r}")
        seen_judge_ids.add(judge_id)

        # Preserve contract dimension order.
        ordered = {dim_id: judge_scores[dim_id] for dim_id in _all_dimension_ids(contract)}
        for dim_id, value in ordered.items():
            per_dim_values[dim_id].append(value)
        judges_out.append(
            {
                "judge_id": judge_id,
                "scores": ordered,
                "overall": compute_weighted_score(ordered, contract),
            }
        )

    dimensions_out: dict[str, dict[str, float]] = {}
    median_scores: dict[str, float] = {}
    for dim_id in _all_dimension_ids(contract):
        values = per_dim_values[dim_id]
        median = float(statistics.median(values))
        median_scores[dim_id] = median
        dimensions_out[dim_id] = {
            "median": median,
            "stdev": _population_stdev(values),
            "min": float(min(values)),
            "max": float(max(values)),
        }

    return {
        "dimensions": dimensions_out,
        "judges": judges_out,
        "overall": compute_weighted_score(median_scores, contract),
    }


def aggregate_repetitions(rows: Sequence[Any]) -> dict[str, Any]:
    """Aggregate repetition rows into deterministic summary statistics.

    Each row must be an object with exact keys ``score`` and ``success``:
    - ``score``: finite numeric excluding bool
    - ``success``: bool

    Return shape::

        {
          "count": int,
          "median": float,
          "mean": float,
          "stdev": float,        # population standard deviation
          "min": float,
          "max": float,
          "success_rate": float, # successes / count
        }
    """
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError("rows: expected nonempty sequence")
    if not rows:
        raise ValueError("rows: expected nonempty sequence")

    scores: list[float] = []
    successes = 0
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"rows[{index}]: expected object with keys score, success")
        keys = set(row.keys())
        if keys != _REP_KEYS:
            missing = sorted(_REP_KEYS - keys)
            extra = sorted(keys - _REP_KEYS)
            parts = []
            if missing:
                parts.append(f"missing keys {missing}")
            if extra:
                parts.append(f"unknown keys {extra}")
            raise ValueError(f"rows[{index}]: " + "; ".join(parts))
        score = row["score"]
        if not is_finite_number(score):
            raise ValueError(f"rows[{index}].score: expected finite number (bool excluded)")
        success = row["success"]
        if not isinstance(success, bool):
            raise ValueError(f"rows[{index}].success: expected bool")
        scores.append(float(score))
        if success:
            successes += 1

    count = len(scores)
    return {
        "count": count,
        "median": float(statistics.median(scores)),
        "mean": float(statistics.fmean(scores)),
        "stdev": _population_stdev(scores),
        "min": float(min(scores)),
        "max": float(max(scores)),
        "success_rate": successes / count,
    }
