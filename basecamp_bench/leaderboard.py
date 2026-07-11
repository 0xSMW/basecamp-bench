"""Deterministic attempt validation, aggregation, and leaderboard export.
Fail-closed aggregation of validated :class:`Attempt` rows into version-scoped
leaderboard roots compatible with :mod:`basecamp_bench.reporting`, plus atomic
JSON/CSV/Markdown export. Standard library only.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import statistics
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

from basecamp_bench.safety import resolve_within, validate_identifier
from basecamp_bench.validation import is_finite_number, is_sha256_hex

__all__ = [
    "Attempt",
    "aggregate_attempts",
    "attempt_from_raw",
    "write_leaderboards",
]
_SCHEMA_VERSION = "1.0"
_TRACKS = frozenset({"fe", "be"})
_MODES = frozenset({"local", "publication"})
_MIN_PUBLICATION_REPETITIONS = 3
_MIN_PUBLICATION_EVALUATORS = 2
_ROOT_KEYS = (
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
)
_PROVENANCE_HASH_KEYS = (
    "runner_source_sha256",
    "seed_tree_sha256",
    "reference_manifest_sha256",
    "reference_tree_sha256",
    "prompt_sha256",
    "rubric_sha256",
    "schema_bundle_sha256",
)
_ENTRY_FIELD_ORDER = (
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
)
_TABULAR_COLUMNS = tuple(name for name in _ENTRY_FIELD_ORDER if name != "raw_attempts")
_REASON_NO_VALID = "no_valid_attempts"
_REASON_DIM_MISMATCH = "dimension_key_mismatch"
_REASON_IMPL_COST_UNKNOWN = "implementation_cost_unknown"
_REASON_IMPL_COST_INCOMPLETE = "implementation_cost_incomplete"
_REASON_EVAL_COST_INCOMPLETE = "evaluation_cost_incomplete"
_REASON_DISPLAY_NAME = "display_name_inconsistent"
_REASON_INSUFFICIENT_REPS = "insufficient_repetitions"
_REASON_INSUFFICIENT_EVALS = "insufficient_evaluators"
_REASON_ATTEMPT_INELIGIBLE = "attempt_ineligible_reasons"
_REASON_LOCAL_MODE = "local_mode"


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be bool, got {type(value).__name__}")
    return value


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a positive int (bool excluded)")
    if value < 1:
        raise ValueError(f"{field} must be a positive int, got {value!r}")
    return value


def _require_nonneg_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a nonnegative int (bool excluded)")
    if value < 0:
        raise ValueError(f"{field} must be a nonnegative int, got {value!r}")
    return value


def _require_finite(value: Any, field: str) -> float:
    if not is_finite_number(value):
        raise ValueError(f"{field} must be a finite number (bool excluded)")
    return float(value)


def _require_nonneg_finite(value: Any, field: str) -> float:
    number = _require_finite(value, field)
    if number < 0.0:
        raise ValueError(f"{field} must be finite and nonnegative, got {number!r}")
    return number


def _require_score_0_10(value: Any, field: str) -> float:
    number = _require_finite(value, field)
    if number < 0.0 or number > 10.0:
        raise ValueError(f"{field} must be in 0..10, got {number!r}")
    return number


def _require_optional_nonneg_finite(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _require_nonneg_finite(value, field)


def _require_optional_score(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _require_score_0_10(value, field)


def _require_optional_nonneg_spread(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _require_nonneg_finite(value, field)


def _require_nonempty_string(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"{field} must be a nonempty string")
    if not value:
        raise ValueError(f"{field} must be a nonempty string")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{field} contains ASCII control characters")
    return value


def _require_sha256(value: Any, field: str) -> str:
    if not is_sha256_hex(value):
        raise ValueError(f"{field} must be a 64-char lowercase hex string")
    return value


def _normalize_model_key(value: str) -> str:
    """Normalize evaluator model IDs for exact distinct-count checks."""
    return value.lower().strip().replace(" ", "-")


def _population_stdev(values: Sequence[float]) -> float:
    if len(values) <= 1:
        return 0.0
    return float(statistics.pstdev(values))


def _median_float(values: Sequence[float]) -> float:
    return float(statistics.median(values))


def _mean_float(values: Sequence[float]) -> float:
    return float(statistics.fmean(values))


def _median_int(values: Sequence[int]) -> int:
    """Median of ints, rounded to nearest int for even-length averages."""
    return int(round(statistics.median(values)))


def _stable_reasons(reasons: Sequence[str]) -> tuple[str, ...]:
    """Deduplicate and sort reason identifiers deterministically."""
    return tuple(sorted(set(reasons)))


@dataclass(frozen=True, slots=True)
class Attempt:
    """One validated implementation/evaluation attempt for leaderboard aggregation."""

    run_id: str
    submission_id: str
    repetition: int
    track: str
    contract_version: str
    contract_sha256: str
    harness: str
    model_id: str
    display_name: str
    implementation_success: bool
    evaluation_success: bool
    score: float | None
    dimensions: Mapping[str, float]
    judge_spread: float | None
    implementation_cost_usd: float | None
    evaluation_cost_usd: float | None
    tokens: int
    duration_s: float
    evaluator_ids: tuple[str, ...]
    ineligible_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        run_id = validate_identifier(self.run_id, field="run_id")
        submission_id = validate_identifier(self.submission_id, field="submission_id")
        harness = validate_identifier(self.harness, field="harness")
        model_id = validate_identifier(self.model_id, field="model_id")
        contract_version = validate_identifier(self.contract_version, field="contract_version")
        contract_sha256 = _require_sha256(self.contract_sha256, "contract_sha256")
        display_name = _require_nonempty_string(self.display_name, "display_name")
        repetition = _require_positive_int(self.repetition, "repetition")
        track = self.track
        if track not in _TRACKS:
            raise ValueError(f"track must be 'fe' or 'be', got {track!r}")
        implementation_success = _require_bool(
            self.implementation_success, "implementation_success"
        )
        evaluation_success = _require_bool(self.evaluation_success, "evaluation_success")
        score = _require_optional_score(self.score, "score")
        judge_spread = _require_optional_nonneg_spread(self.judge_spread, "judge_spread")
        implementation_cost_usd = _require_optional_nonneg_finite(
            self.implementation_cost_usd, "implementation_cost_usd"
        )
        evaluation_cost_usd = _require_optional_nonneg_finite(
            self.evaluation_cost_usd, "evaluation_cost_usd"
        )
        tokens = _require_nonneg_int(self.tokens, "tokens")
        duration_s = _require_nonneg_finite(self.duration_s, "duration_s")
        raw_dims = self.dimensions
        if not isinstance(raw_dims, Mapping):
            raise ValueError("dimensions must be a mapping of str to float")
        dim_copy: dict[str, float] = {}
        for key, value in raw_dims.items():
            if not isinstance(key, str) or not key:
                raise ValueError("dimensions keys must be nonempty strings")
            dim_copy[key] = _require_score_0_10(value, f"dimensions[{key!r}]")
        dimensions = MappingProxyType(dim_copy)
        raw_evals = self.evaluator_ids
        if isinstance(raw_evals, (str, bytes)) or not isinstance(raw_evals, Sequence):
            raise ValueError("evaluator_ids must be a sequence of identifiers")
        evaluator_ids = tuple(
            validate_identifier(item, field=f"evaluator_ids[{index}]")
            for index, item in enumerate(raw_evals)
        )
        raw_reasons = self.ineligible_reasons
        if isinstance(raw_reasons, (str, bytes)) or not isinstance(raw_reasons, Sequence):
            raise ValueError("ineligible_reasons must be a sequence of strings")
        reasons_list: list[str] = []
        for index, item in enumerate(raw_reasons):
            if not isinstance(item, str):
                raise ValueError(
                    f"ineligible_reasons[{index}] must be a string, got {type(item).__name__}"
                )
            if any(ord(c) < 32 or ord(c) == 127 for c in item):
                raise ValueError(f"ineligible_reasons[{index}] contains control characters")
            reasons_list.append(item)
        ineligible_reasons = tuple(reasons_list)
        if evaluation_success and not implementation_success:
            raise ValueError("evaluation_success requires implementation_success")
        if evaluation_success:
            if score is None:
                raise ValueError("successful evaluation requires score in 0..10")
            if not dimensions:
                raise ValueError("successful evaluation requires nonempty dimensions")
            if judge_spread is None:
                raise ValueError("successful evaluation requires finite nonnegative judge_spread")
        else:
            if score is not None:
                raise ValueError("failed evaluation must have score None")
            if dimensions:
                raise ValueError("failed evaluation must have empty dimensions")
            if judge_spread is not None:
                raise ValueError("failed evaluation must have judge_spread None")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "submission_id", submission_id)
        object.__setattr__(self, "repetition", repetition)
        object.__setattr__(self, "track", track)
        object.__setattr__(self, "contract_version", contract_version)
        object.__setattr__(self, "contract_sha256", contract_sha256)
        object.__setattr__(self, "harness", harness)
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "implementation_success", implementation_success)
        object.__setattr__(self, "evaluation_success", evaluation_success)
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "dimensions", dimensions)
        object.__setattr__(self, "judge_spread", judge_spread)
        object.__setattr__(self, "implementation_cost_usd", implementation_cost_usd)
        object.__setattr__(self, "evaluation_cost_usd", evaluation_cost_usd)
        object.__setattr__(self, "tokens", tokens)
        object.__setattr__(self, "duration_s", duration_s)
        object.__setattr__(self, "evaluator_ids", evaluator_ids)
        object.__setattr__(self, "ineligible_reasons", ineligible_reasons)


def _attempt_to_raw(attempt: Attempt) -> dict[str, object]:
    """Convert an Attempt to portable JSON-friendly types with stable key order."""
    dims = {k: float(attempt.dimensions[k]) for k in sorted(attempt.dimensions)}
    return {
        "run_id": attempt.run_id,
        "submission_id": attempt.submission_id,
        "repetition": attempt.repetition,
        "track": attempt.track,
        "contract_version": attempt.contract_version,
        "contract_sha256": attempt.contract_sha256,
        "harness": attempt.harness,
        "model_id": attempt.model_id,
        "display_name": attempt.display_name,
        "implementation_success": attempt.implementation_success,
        "evaluation_success": attempt.evaluation_success,
        "score": attempt.score,
        "dimensions": dims,
        "judge_spread": attempt.judge_spread,
        "implementation_cost_usd": attempt.implementation_cost_usd,
        "evaluation_cost_usd": attempt.evaluation_cost_usd,
        "tokens": attempt.tokens,
        "duration_s": attempt.duration_s,
        "evaluator_ids": list(attempt.evaluator_ids),
        "ineligible_reasons": list(attempt.ineligible_reasons),
    }


def attempt_from_raw(raw: Mapping[str, Any]) -> Attempt:
    """Rebuild a validated :class:`Attempt` from its portable representation."""
    if not isinstance(raw, Mapping):
        raise ValueError("raw attempt must be a mapping")
    expected = {
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
    }
    if set(raw) != expected:
        raise ValueError("raw attempt keys do not match the attempt contract")
    return Attempt(**dict(raw))


def _aggregate_group(
    attempts: Sequence[Attempt],
    *,
    mode: str,
) -> dict[str, object]:
    """Aggregate one (harness, model_id) group into a leaderboard entry."""
    total = len(attempts)
    valid = [a for a in attempts if a.evaluation_success]
    valid_count = len(valid)
    reasons: list[str] = []
    if mode == "local":
        reasons.append(_REASON_LOCAL_MODE)
    success_rate = valid_count / total if total else 0.0
    if valid_count == 0:
        score = 0.0
        score_mean = 0.0
        score_stdev = 0.0
        score_min = score_max = score_range = 0.0
        dimensions: dict[str, float] = {}
        judge_spread = 0.0
        reasons.append(_REASON_NO_VALID)
    else:
        scores = [float(a.score) for a in valid]  # type: ignore[arg-type]
        score = _median_float(scores)
        score_mean = _mean_float(scores)
        score_stdev = _population_stdev(scores)
        score_min, score_max = min(scores), max(scores)
        score_range = score_max - score_min
        key_sets = [frozenset(a.dimensions.keys()) for a in valid]
        first_keys = key_sets[0]
        if any(ks != first_keys for ks in key_sets[1:]):
            reasons.append(_REASON_DIM_MISMATCH)
            consistent = set.intersection(*(set(ks) for ks in key_sets))
        else:
            consistent = set(first_keys)
        dimensions = {
            key: _median_float([float(a.dimensions[key]) for a in valid])
            for key in sorted(consistent)
        }
        spreads = [float(a.judge_spread) for a in valid]  # type: ignore[arg-type]
        judge_spread = _median_float(spreads)
    impl_costs = [
        float(a.implementation_cost_usd) for a in attempts if a.implementation_cost_usd is not None
    ]
    if not impl_costs:
        cost_per_attempt = 0.0
        implementation_cost_per_attempt = 0.0
        cost_mean = 0.0
        cost_stdev = 0.0
        cost_min = cost_max = cost_range = 0.0
        reasons.append(_REASON_IMPL_COST_UNKNOWN)
    else:
        cost_per_attempt = _median_float(impl_costs)
        implementation_cost_per_attempt = cost_per_attempt
        cost_mean = _mean_float(impl_costs)
        cost_stdev = _population_stdev(impl_costs)
        cost_min, cost_max = min(impl_costs), max(impl_costs)
        cost_range = cost_max - cost_min
        if len(impl_costs) != total:
            reasons.append(_REASON_IMPL_COST_INCOMPLETE)
    eval_costs = [
        float(a.evaluation_cost_usd) for a in attempts if a.evaluation_cost_usd is not None
    ]
    if not eval_costs:
        evaluation_cost_per_attempt = 0.0
        if mode == "publication":
            reasons.append(_REASON_EVAL_COST_INCOMPLETE)
    else:
        evaluation_cost_per_attempt = _median_float(eval_costs)
        if len(eval_costs) != total and mode == "publication":
            if _REASON_EVAL_COST_INCOMPLETE not in reasons:
                reasons.append(_REASON_EVAL_COST_INCOMPLETE)
    token_values = [a.tokens for a in attempts]
    tokens = _median_int(token_values)
    tokens_mean = _mean_float(token_values)
    tokens_min, tokens_max = min(token_values), max(token_values)
    tokens_range = tokens_max - tokens_min
    duration_values = [a.duration_s for a in attempts]
    duration_s = _median_float(duration_values)
    duration_mean_s = _mean_float(duration_values)
    duration_min_s, duration_max_s = min(duration_values), max(duration_values)
    duration_range_s = duration_max_s - duration_min_s
    run_ids = tuple(sorted({a.run_id for a in attempts}))
    names = {a.display_name for a in attempts}
    if len(names) == 1:
        display_name = next(iter(names))
    else:
        reasons.append(_REASON_DISPLAY_NAME)
        display_name = sorted(names)[0]
    model_id = attempts[0].model_id
    harness = attempts[0].harness
    if any(a.ineligible_reasons for a in attempts):
        reasons.append(_REASON_ATTEMPT_INELIGIBLE)
    if mode == "publication":
        if total < _MIN_PUBLICATION_REPETITIONS:
            reasons.append(_REASON_INSUFFICIENT_REPS)
        if valid and any(
            len({_normalize_model_key(eid) for eid in a.evaluator_ids})
            < _MIN_PUBLICATION_EVALUATORS
            for a in valid
        ):
            reasons.append(_REASON_INSUFFICIENT_EVALS)
    stable = _stable_reasons(reasons)
    eligible = valid_count > 0 and len(stable) == 0
    raw_rows = [_attempt_to_raw(a) for a in attempts]
    raw_attempts = sorted(
        raw_rows,
        key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
    )
    return {
        "model_id": model_id,
        "display_name": display_name,
        "harness": harness,
        "score": score,
        "score_mean": score_mean,
        "score_stdev": score_stdev,
        "score_min": score_min,
        "score_max": score_max,
        "score_range": score_range,
        "judge_spread": judge_spread,
        "cost_per_attempt": cost_per_attempt,
        "cost_mean": cost_mean,
        "cost_stdev": cost_stdev,
        "cost_min": cost_min,
        "cost_max": cost_max,
        "cost_range": cost_range,
        "success_rate": success_rate,
        "repetitions": total,
        "dimensions": dimensions,
        "tokens": tokens,
        "tokens_mean": tokens_mean,
        "tokens_min": tokens_min,
        "tokens_max": tokens_max,
        "tokens_range": tokens_range,
        "duration_s": duration_s,
        "duration_mean_s": duration_mean_s,
        "duration_min_s": duration_min_s,
        "duration_max_s": duration_max_s,
        "duration_range_s": duration_range_s,
        "eligible": eligible,
        "ineligible_reasons": list(stable),
        "run_ids": list(run_ids),
        "implementation_cost_per_attempt": implementation_cost_per_attempt,
        "evaluation_cost_per_attempt": evaluation_cost_per_attempt,
        "raw_attempts": raw_attempts,
    }


def aggregate_attempts(
    attempts: Sequence[Attempt],
    *,
    mode: Literal["local", "publication"],
    generated_at: str,
    comparison_provenance: Mapping[str, str] | Mapping[str, Mapping[str, str]] | None = None,
    dimension_profiles: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
) -> list[dict[str, object]]:
    """Aggregate attempts into version-scoped leaderboard root objects.
    Groups first by exact ``(track, contract_version, contract_sha256)``, then
    by ``(harness, model_id)``. Never combines FE/BE or distinct contract
    identities. Roots contain exactly the keys expected by reporting plus
    ``entries``.
    """
    if mode not in _MODES:
        raise ValueError(f"mode must be 'local' or 'publication', got {mode!r}")
    if isinstance(generated_at, bool) or not isinstance(generated_at, str):
        raise ValueError("generated_at must be a nonempty string")
    if not generated_at or any(ord(c) < 32 or ord(c) == 127 for c in generated_at):
        raise ValueError("generated_at must be a nonempty string without controls")
    if not isinstance(attempts, Sequence) or isinstance(attempts, (str, bytes)):
        raise ValueError("attempts must be a sequence of Attempt")
    for index, item in enumerate(attempts):
        if not isinstance(item, Attempt):
            raise ValueError(f"attempts[{index}] must be Attempt, got {type(item).__name__}")
    if comparison_provenance is None:
        # Kept for the low-level aggregation API and existing local callers. The
        # runner always supplies measured hashes; local output is ineligible.
        fallback = hashlib.sha256(b"unspecified-local-provenance-v1").hexdigest()
        provenance_by_track = {
            track: {key: fallback for key in _PROVENANCE_HASH_KEYS} for track in _TRACKS
        }
    else:
        if set(comparison_provenance) == set(_PROVENANCE_HASH_KEYS):
            flat = cast(Mapping[str, str], comparison_provenance)
            checked = {key: _require_sha256(flat[key], key) for key in _PROVENANCE_HASH_KEYS}
            provenance_by_track = {track: dict(checked) for track in _TRACKS}
        else:
            nested = cast(Mapping[str, Mapping[str, str]], comparison_provenance)
            provenance_by_track = {}
            for track, values in nested.items():
                if track not in _TRACKS or set(values) != set(_PROVENANCE_HASH_KEYS):
                    raise ValueError(
                        "comparison_provenance keys do not match the required identity"
                    )
                provenance_by_track[track] = {
                    key: _require_sha256(values[key], f"{track}.{key}")
                    for key in _PROVENANCE_HASH_KEYS
                }
    sections: dict[tuple[str, str, str], dict[tuple[str, str], list[Attempt]]] = {}
    for attempt in attempts:
        section_key = (
            attempt.track,
            attempt.contract_version,
            attempt.contract_sha256,
        )
        entry_key = (attempt.harness, attempt.model_id)
        sections.setdefault(section_key, {}).setdefault(entry_key, []).append(attempt)
    roots: list[dict[str, object]] = []
    for section_key in sorted(sections.keys()):
        track, contract_version, contract_sha256 = section_key
        if track not in provenance_by_track:
            raise ValueError(f"comparison_provenance missing track {track!r}")
        groups = sections[section_key]
        if dimension_profiles is None:
            inferred = sorted(
                {key for group in groups.values() for a in group for key in a.dimensions}
            )
            weight = 1.0 / len(inferred) if inferred else 1.0
            dimension_profile = [
                {"id": dim_id, "label": dim_id, "weight": weight} for dim_id in inferred
            ]
        else:
            raw_profile = dimension_profiles.get(track)
            if not isinstance(raw_profile, Sequence) or isinstance(raw_profile, (str, bytes)):
                raise ValueError(f"dimension_profiles missing track {track!r}")
            dimension_profile = []
            seen_dimensions: set[str] = set()
            for index, raw in enumerate(raw_profile):
                if not isinstance(raw, Mapping) or set(raw) != {"id", "label", "weight"}:
                    raise ValueError(f"dimension_profiles[{track}][{index}] is invalid")
                dim_id = validate_identifier(raw["id"], field="dimension id")
                label = _require_nonempty_string(raw["label"], "dimension label")
                weight = _require_nonneg_finite(raw["weight"], "dimension weight")
                if dim_id in seen_dimensions:
                    raise ValueError(f"duplicate dimension id {dim_id!r}")
                seen_dimensions.add(dim_id)
                dimension_profile.append({"id": dim_id, "label": label, "weight": weight})
            dimension_profile.sort(key=lambda row: str(row["id"]))
        entries: list[dict[str, object]] = []
        for entry_key in sorted(groups.keys()):
            entries.append(_aggregate_group(groups[entry_key], mode=mode))
        root: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "mode": mode,
            "track": track,
            "contract_version": contract_version,
            "contract_sha256": contract_sha256,
            "generated_at": generated_at,
            **provenance_by_track[track],
            "dimension_profile": dimension_profile,
            "entries": entries,
        }
        if frozenset(root.keys()) != frozenset(_ROOT_KEYS):
            raise RuntimeError("internal error: unexpected root keys")
        roots.append(root)
    return roots


def _leaderboard_basename(
    track: str,
    contract_version: str,
    contract_sha256: str,
) -> str:
    """Stable safe basename derived only from track and contract identity."""
    track_s = validate_identifier(track, field="track")
    version_s = validate_identifier(contract_version, field="contract_version")
    sha_s = _require_sha256(contract_sha256, "contract_sha256")
    return f"leaderboard_{track_s}_{version_s}_{sha_s}"


def _serialize_tabular_cell(column: str, value: object) -> str:
    if column == "dimensions":
        if not isinstance(value, Mapping):
            return json.dumps(value, sort_keys=True, ensure_ascii=False)
        ordered = {k: value[k] for k in sorted(value.keys(), key=str)}
        return json.dumps(ordered, sort_keys=True, ensure_ascii=False)
    if column in ("ineligible_reasons", "run_ids"):
        if value is None:
            return "[]"
        assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
        return json.dumps(list(value), ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _render_csv(entries: Sequence[Mapping[str, object]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_TABULAR_COLUMNS)
    for entry in entries:
        row = [_serialize_tabular_cell(col, entry.get(col)) for col in _TABULAR_COLUMNS]
        writer.writerow(row)
    return buffer.getvalue()


def _md_escape_cell(text: str) -> str:
    """Make a Markdown table cell single-row safe."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )
    return escaped


def _render_markdown(
    root: Mapping[str, object],
    entries: Sequence[Mapping[str, object]],
) -> str:
    track = root["track"]
    version = root["contract_version"]
    sha = root["contract_sha256"]
    generated = root["generated_at"]
    lines: list[str] = [
        f"# Leaderboard ({track} · {version})",
        "",
        f"- contract_sha256: `{sha}`",
        f"- generated_at: `{generated}`",
        f"- schema_version: `{root['schema_version']}`",
        "",
        "| " + " | ".join(_TABULAR_COLUMNS) + " |",
        "| " + " | ".join("---" for _ in _TABULAR_COLUMNS) + " |",
    ]
    for entry in entries:
        cells = [
            _md_escape_cell(_serialize_tabular_cell(col, entry.get(col)))
            for col in _TABULAR_COLUMNS
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(os.fspath(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write UTF-8 text; clean temporary file on failure."""
    parent = path.parent
    if not parent.is_dir():
        raise ValueError(f"parent directory does not exist: {parent}")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=os.fspath(parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, os.fspath(path))
        tmp_path = Path()
        _fsync_directory(parent)
    except Exception:
        if tmp_path != Path() and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def write_leaderboards(
    output_dir: Path,
    leaderboards: Sequence[Mapping[str, object]],
) -> list[Path]:
    """Atomically write JSON, CSV, and Markdown for each leaderboard root.
    Refuses to overwrite existing targets. Detects all collisions before any
    write so failure does not leave a partial new export. Returns all written
    paths in deterministic order.
    """
    if not isinstance(output_dir, Path):
        output_dir = Path(output_dir)
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError(f"output_dir is not a directory: {output_dir}")
    if not output_dir.exists():
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"cannot create output_dir {output_dir}: {exc}") from exc
    if not output_dir.is_dir():
        raise ValueError(f"output_dir is not a directory: {output_dir}")
    output_dir = output_dir.resolve()
    if not isinstance(leaderboards, Sequence) or isinstance(leaderboards, (str, bytes)):
        raise ValueError("leaderboards must be a sequence of root mappings")
    plans: list[tuple[Path, str, Mapping[str, object]]] = []
    seen_basenames: set[str] = set()
    for index, root in enumerate(leaderboards):
        if not isinstance(root, Mapping):
            raise ValueError(f"leaderboards[{index}]: expected mapping")
        actual_keys = frozenset(root.keys())
        expected_keys = frozenset(_ROOT_KEYS)
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            extra = sorted(actual_keys - expected_keys)
            parts: list[str] = []
            if missing:
                parts.append(f"missing keys {missing}")
            if extra:
                parts.append(f"unknown keys {extra}")
            raise ValueError(f"leaderboards[{index}]: " + "; ".join(parts))
        track = root["track"]
        contract_version = root["contract_version"]
        contract_sha256 = root["contract_sha256"]
        if not isinstance(track, str) or track not in _TRACKS:
            raise ValueError(f"leaderboards[{index}].track: expected 'fe' or 'be'")
        if not isinstance(contract_version, str):
            raise ValueError(f"leaderboards[{index}].contract_version: expected string")
        if not isinstance(contract_sha256, str):
            raise ValueError(f"leaderboards[{index}].contract_sha256: expected string")
        basename = _leaderboard_basename(track, contract_version, contract_sha256)
        if basename in seen_basenames:
            raise ValueError(f"leaderboards[{index}]: duplicate export basename {basename!r}")
        seen_basenames.add(basename)
        entries = root["entries"]
        if not isinstance(entries, list):
            raise ValueError(f"leaderboards[{index}].entries: expected list")
        for ext, kind in (("json", "json"), ("csv", "csv"), ("md", "md")):
            name = f"{basename}.{ext}"
            target = resolve_within(output_dir, name)
            try:
                target.relative_to(output_dir)
            except ValueError as exc:
                raise ValueError(f"target escapes output_dir: {target}") from exc
            plans.append((target, kind, root))
    plans.sort(key=lambda item: item[0].as_posix())
    collisions = [p for p, _, _ in plans if os.path.lexists(p)]
    if collisions:
        names = ", ".join(sorted(str(p) for p in collisions))
        raise ValueError(f"refusing to overwrite existing files: {names}")
    written: list[Path] = []
    try:
        for target, kind, root in plans:
            entries = root["entries"]
            assert isinstance(entries, list)
            if kind == "json":
                text = json.dumps(root, sort_keys=True, ensure_ascii=False) + "\n"
            elif kind == "csv":
                text = _render_csv(entries)
            else:
                text = _render_markdown(root, entries)
            _atomic_write_text(target, text)
            written.append(target)
    except Exception:
        raise
    return written
