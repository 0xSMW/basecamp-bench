"""Canonical attempt codec, ledger I/O, and derived aggregation.

Raw attempts are the sole persisted evaluation record. Statistics are derived
via :func:`aggregate_attempts`. Legacy schema 1.0 ``entries`` JSON remains
readable; new output is schema 2.0 with a flat ``attempts`` list.

CSV and Markdown leaderboard files are optional projections only. Generate them
explicitly via :func:`write_tabular_views` or ``basecamp-bench export-tabular``;
normal runs never write them.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import statistics
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, fields
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

from basecamp_bench.reporting_model import RAW_ATTEMPT_KEY_ORDER, raw_attempt_sort_key
from basecamp_bench.safety import resolve_within, validate_identifier
from basecamp_bench.validation import is_finite_number, is_sha256_hex

__all__ = [
    "Attempt",
    "AttemptLedger",
    "aggregate_attempts",
    "atomic_write_text",
    "attempt_from_raw",
    "attempt_to_raw",
    "build_attempt_ledgers",
    "load_attempt_ledger",
    "profile_to_raw",
    "require_display_name",
    "write_attempt_ledgers",
    "write_leaderboards",
    "write_tabular_views",
]

_SCHEMA_VERSION_LEGACY = "1.0"
_SCHEMA_VERSION = "2.0"
_TRACKS = frozenset({"fe", "be"})
_MODES = frozenset({"local", "publication"})
_MIN_PUBLICATION_REPETITIONS = 3
_MIN_PUBLICATION_EVALUATORS = 2
_RAW_ATTEMPT_KEYS = frozenset(RAW_ATTEMPT_KEY_ORDER)
_LEDGER_KEYS = (
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
    "attempts",
)
_LEGACY_ROOT_KEYS = frozenset(
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
_PROVENANCE_HASH_KEYS = (
    "runner_source_sha256",
    "seed_tree_sha256",
    "reference_manifest_sha256",
    "reference_tree_sha256",
    "prompt_sha256",
    "rubric_sha256",
    "schema_bundle_sha256",
)
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
_MAX_DISPLAY_NAME_LEN = 256
_MAX_REASON_LEN = 256
_WEIGHT_SUM_TOLERANCE = 1e-9
_DIMENSION_PROFILE_KEYS = frozenset({"id", "label", "weight"})
# Flat aggregate entry fields for optional CSV/Markdown projections (no raw_attempts).
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
    lower = value.lower().strip()
    if lower.startswith("file:"):
        return True
    if _looks_like_absolute_path(value):
        return True
    if "/../" in value or "\\..\\" in value or value in (".", ".."):
        return True
    if value.startswith("../") or value.startswith("..\\"):
        return True
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


def require_display_name(value: Any, field: str = "display_name") -> str:
    """Validate a display/label name under Attempt and report-rename rules.

    Accepts a nonempty string (literal spaces and Unicode preserved; blank or
    whitespace-only rejected), enforces the max length, and rejects ASCII
    control characters and path/command-shaped values.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"{field} must be a nonempty string")
    if not value or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    if len(value) > _MAX_DISPLAY_NAME_LEN:
        raise ValueError(f"{field} exceeds {_MAX_DISPLAY_NAME_LEN} characters")
    if _has_control_chars(value):
        raise ValueError(f"{field} contains ASCII control characters")
    if _looks_like_path_or_command(value):
        raise ValueError(f"{field} path or command-shaped value rejected")
    return value


def _require_reason_label(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if len(value) > _MAX_REASON_LEN:
        raise ValueError(f"{field} exceeds {_MAX_REASON_LEN} characters")
    if _has_control_chars(value):
        raise ValueError(f"{field} contains ASCII control characters")
    if _looks_like_path_or_command(value):
        raise ValueError(f"{field} path or command-shaped value rejected")
    return value


def _require_sha256(value: Any, field: str) -> str:
    if not is_sha256_hex(value):
        raise ValueError(f"{field} must be a 64-char lowercase hex string")
    return value


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


@dataclass(frozen=True, slots=True)
class Attempt:
    """One validated implementation/evaluation attempt."""

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
        display_name = require_display_name(self.display_name, "display_name")
        repetition = _require_positive_int(self.repetition, "repetition")
        track = self.track
        if track not in _TRACKS:
            raise ValueError(f"track must be 'fe' or 'be', got {track!r}")
        implementation_success = _require_bool(
            self.implementation_success, "implementation_success"
        )
        evaluation_success = _require_bool(self.evaluation_success, "evaluation_success")
        score = None if self.score is None else _require_score_0_10(self.score, "score")
        judge_spread = (
            None
            if self.judge_spread is None
            else _require_nonneg_finite(self.judge_spread, "judge_spread")
        )
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
            if _has_control_chars(key) or _looks_like_path_or_command(key):
                raise ValueError(f"dimensions: unsafe dimension key {key!r}")
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
            reasons_list.append(_require_reason_label(item, f"ineligible_reasons[{index}]"))
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
        normalized = locals()
        for field in fields(self):
            object.__setattr__(self, field.name, normalized[field.name])


def attempt_to_raw(attempt: Attempt) -> dict[str, object]:
    """Convert an Attempt to portable JSON types with stable key order."""
    raw = {field.name: getattr(attempt, field.name) for field in fields(attempt)}
    raw["dimensions"] = {k: float(attempt.dimensions[k]) for k in sorted(attempt.dimensions)}
    raw["evaluator_ids"] = list(attempt.evaluator_ids)
    raw["ineligible_reasons"] = list(attempt.ineligible_reasons)
    return raw


def attempt_from_raw(raw: Mapping[str, Any]) -> Attempt:
    """Rebuild a validated :class:`Attempt` from its portable representation."""
    if not isinstance(raw, Mapping):
        raise ValueError("raw attempt must be a mapping")
    if set(raw) != _RAW_ATTEMPT_KEYS:
        _exact_keys(raw, _RAW_ATTEMPT_KEYS, "raw attempt")
    return Attempt(**dict(raw))


def freeze_raw_attempt(raw: Mapping[str, Any] | Mapping[str, object]) -> Mapping[str, object]:
    """Deeply freeze one raw attempt into a MappingProxyType tree."""
    data = dict(raw)
    dims_src = data.get("dimensions", {})
    dims = (
        MappingProxyType({str(k): float(dims_src[k]) for k in sorted(dims_src.keys(), key=str)})
        if isinstance(dims_src, Mapping)
        else MappingProxyType({})
    )
    frozen: dict[str, object] = {}
    for key in RAW_ATTEMPT_KEY_ORDER:
        if key == "dimensions":
            frozen[key] = dims
        elif key in ("evaluator_ids", "ineligible_reasons"):
            frozen[key] = tuple(cast(Sequence[object], data.get(key, ())))
        else:
            frozen[key] = data[key]
    return MappingProxyType(frozen)


def attempt_canonical_json(attempt: Attempt) -> str:
    """Stable byte-identity serialization for exact-attempt deduplication."""
    return json.dumps(
        attempt_to_raw(attempt),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class AttemptLedger:
    """Comparison-scoped raw-attempt ledger (no derived model statistics)."""

    mode: str
    track: str
    contract_version: str
    contract_sha256: str
    generated_at: str
    runner_source_sha256: str
    seed_tree_sha256: str
    reference_manifest_sha256: str
    reference_tree_sha256: str
    prompt_sha256: str
    rubric_sha256: str
    schema_bundle_sha256: str
    dimension_profile: tuple[Mapping[str, object], ...]
    attempts: tuple[Attempt, ...]
    schema_version: str = _SCHEMA_VERSION

    @property
    def provenance(self) -> dict[str, str]:
        return {key: cast(str, getattr(self, key)) for key in _PROVENANCE_HASH_KEYS}

    def metadata_to_raw(self) -> dict[str, object]:
        """Serialize comparison metadata without materializing attempts."""
        raw = {
            key: getattr(self, key)
            for key in _LEDGER_KEYS
            if key not in {"dimension_profile", "attempts"}
        }
        raw["dimension_profile"] = profile_to_raw(self.dimension_profile)
        return raw

    def to_raw(self) -> dict[str, object]:
        """Serialize to the canonical persisted ledger payload."""
        raw_attempts = sorted(
            (attempt_to_raw(a) for a in self.attempts),
            key=raw_attempt_sort_key,
        )
        return {**self.metadata_to_raw(), "attempts": raw_attempts}


def profile_to_raw(profile: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        {"id": row["id"], "label": row["label"], "weight": float(cast(float, row["weight"]))}
        for row in profile
    ]


def _normalize_dimension_profile(raw_profile: Any, path: str) -> list[dict[str, object]]:
    """Validate/normalize a dimension profile for builders and loaders.

    Enforces a nonempty profile, unique safe ids, labels under display-name
    constraints, strictly positive finite weights, and weights summing to 1.0
    within tolerance. Returns rows sorted by id for deterministic ordering.
    """
    if not isinstance(raw_profile, Sequence) or isinstance(raw_profile, (str, bytes)):
        raise ValueError(f"{path}: expected nonempty array")
    if not raw_profile:
        raise ValueError(f"{path}: expected nonempty array")
    profile: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, row in enumerate(raw_profile):
        ppath = f"{path}[{index}]"
        if not isinstance(row, Mapping) or set(row) != _DIMENSION_PROFILE_KEYS:
            raise ValueError(f"{ppath}: invalid dimension metadata")
        dim_id = validate_identifier(row["id"], field=f"{ppath}.id")
        if dim_id in seen:
            raise ValueError(f"{ppath}: duplicate dimension id {dim_id!r}")
        seen.add(dim_id)
        label = require_display_name(row["label"], f"{ppath}.label")
        weight = _require_finite(row["weight"], f"{ppath}.weight")
        if weight <= 0.0:
            raise ValueError(f"{ppath}.weight: expected finite number > 0, got {weight!r}")
        profile.append({"id": dim_id, "label": label, "weight": weight})
    weight_sum = sum(float(cast(float | int, row["weight"])) for row in profile)
    if abs(weight_sum - 1.0) > _WEIGHT_SUM_TOLERANCE:
        raise ValueError(
            f"{path}: weights must sum to 1.0 (within {_WEIGHT_SUM_TOLERANCE}); got {weight_sum!r}"
        )
    profile.sort(key=lambda row: str(row["id"]))
    return profile


def _dedupe_attempts(
    attempts: Sequence[Attempt],
    *,
    path: str,
) -> tuple[Attempt, ...]:
    by_id: dict[tuple[str, str, int], tuple[str, Attempt]] = {}
    for index, attempt in enumerate(attempts):
        identity = (attempt.run_id, attempt.submission_id, attempt.repetition)
        canonical = attempt_canonical_json(attempt)
        existing = by_id.get(identity)
        if existing is not None and existing[0] != canonical:
            raise ValueError(
                f"{path}[{index}]: conflicting duplicate raw identity "
                f"run_id={identity[0]!r} submission_id={identity[1]!r} "
                f"repetition={identity[2]!r}"
            )
        if existing is None:
            by_id[identity] = (canonical, attempt)
    ordered = sorted(by_id.values(), key=lambda item: raw_attempt_sort_key(attempt_to_raw(item[1])))
    return tuple(item[1] for item in ordered)


def _parse_attempts_list(
    value: Any,
    *,
    track: str,
    contract_version: str,
    contract_sha256: str,
    path: str,
    model_id: str | None = None,
    harness: str | None = None,
) -> tuple[Attempt, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{path}: expected list of raw attempt objects")
    parsed: list[Attempt] = []
    for index, item in enumerate(value):
        apath = f"{path}[{index}]"
        if not isinstance(item, Mapping):
            raise ValueError(f"{apath}: expected object")
        try:
            attempt = attempt_from_raw(item)
        except ValueError as exc:
            raise ValueError(f"{apath}: {exc}") from exc
        if attempt.track != track:
            raise ValueError(
                f"{apath}.track: must match root track {track!r}, got {attempt.track!r}"
            )
        if attempt.contract_version != contract_version:
            raise ValueError(
                f"{apath}.contract_version: must match root identity "
                f"{contract_version!r}, got {attempt.contract_version!r}"
            )
        if attempt.contract_sha256 != contract_sha256:
            raise ValueError(
                f"{apath}.contract_sha256: must match root identity, got {attempt.contract_sha256!r}"
            )
        if model_id is not None and attempt.model_id != model_id:
            raise ValueError(
                f"{apath}.model_id: must match entry model_id {model_id!r}, got {attempt.model_id!r}"
            )
        if harness is not None and attempt.harness != harness:
            raise ValueError(
                f"{apath}.harness: must match entry harness {harness!r}, got {attempt.harness!r}"
            )
        parsed.append(attempt)
    return _dedupe_attempts(parsed, path=path)


def _require_generated_at(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"{field} must be a nonempty string")
    if not value or _has_control_chars(value):
        raise ValueError(f"{field} must be a nonempty string without controls")
    return value


def _parse_ledger_header(data: Mapping[str, Any], label: str) -> dict[str, Any]:
    mode = data["mode"]
    if mode not in _MODES:
        raise ValueError(f"{label}.mode: expected local or publication")
    track = data["track"]
    if track not in _TRACKS:
        raise ValueError(f"{label}.track: expected fe or be")
    contract_version = validate_identifier(
        data["contract_version"], field=f"{label}.contract_version"
    )
    contract_sha256 = _require_sha256(data["contract_sha256"], f"{label}.contract_sha256")
    generated_at = _require_generated_at(data["generated_at"], f"{label}.generated_at")
    provenance: dict[str, str] = {}
    for key in _PROVENANCE_HASH_KEYS:
        provenance[key] = _require_sha256(data[key], f"{label}.{key}")
    profile = _normalize_dimension_profile(data["dimension_profile"], f"{label}.dimension_profile")
    return {
        "mode": mode,
        "track": track,
        "contract_version": contract_version,
        "contract_sha256": contract_sha256,
        "generated_at": generated_at,
        **provenance,
        "dimension_profile": tuple(MappingProxyType(row) for row in profile),
    }


def _ledger_from_parts(
    header: Mapping[str, Any],
    attempts: Sequence[Attempt],
    *,
    label: str,
    schema_version: str,
) -> AttemptLedger:
    profile_ids = {str(row["id"]) for row in header["dimension_profile"]}
    for attempt in attempts:
        if attempt.evaluation_success and set(attempt.dimensions) != profile_ids:
            raise ValueError(f"{label}: raw attempt dimensions differ from dimension profile")
    return AttemptLedger(
        schema_version=schema_version,
        attempts=tuple(attempts),
        **dict(header),
    )


def load_attempt_ledger(path: Path) -> AttemptLedger:
    """Load a canonical ledger or legacy leaderboard JSON into :class:`AttemptLedger`."""
    path = Path(path)
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

    has_attempts = "attempts" in data
    has_entries = "entries" in data
    if has_attempts and has_entries:
        raise ValueError(f"{label}: cannot contain both attempts and entries")
    if has_attempts:
        _exact_keys(data, frozenset(_LEDGER_KEYS), label)
        if data.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError(f"{label}.schema_version: unsupported version")
        header = _parse_ledger_header(data, label)
        attempts = _parse_attempts_list(
            data["attempts"],
            track=header["track"],
            contract_version=header["contract_version"],
            contract_sha256=header["contract_sha256"],
            path=f"{label}.attempts",
        )
        return _ledger_from_parts(header, attempts, label=label, schema_version=_SCHEMA_VERSION)

    # Legacy schema 1.0 leaderboard with nested entries/raw_attempts.
    _exact_keys(data, _LEGACY_ROOT_KEYS, label)
    if data.get("schema_version") != _SCHEMA_VERSION_LEGACY:
        raise ValueError(f"{label}.schema_version: unsupported version")
    header = _parse_ledger_header(data, label)
    entries = data["entries"]
    if not isinstance(entries, list):
        raise ValueError(f"{label}.entries: expected list")
    collected: list[Attempt] = []
    for index, entry in enumerate(entries):
        epath = f"{label}:entries[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{epath}: expected object")
        if "raw_attempts" not in entry:
            raise ValueError(f"{epath}: missing keys ['raw_attempts']")
        model_id = validate_identifier(entry.get("model_id"), field=f"{epath}.model_id")
        harness = validate_identifier(entry.get("harness"), field=f"{epath}.harness")
        collected.extend(
            _parse_attempts_list(
                entry["raw_attempts"],
                track=header["track"],
                contract_version=header["contract_version"],
                contract_sha256=header["contract_sha256"],
                path=f"{epath}.raw_attempts",
                model_id=model_id,
                harness=harness,
            )
        )
    return _ledger_from_parts(
        header,
        _dedupe_attempts(collected, path=f"{label}.entries"),
        label=label,
        schema_version=_SCHEMA_VERSION_LEGACY,
    )


def _resolve_provenance(
    comparison_provenance: Mapping[str, str] | Mapping[str, Mapping[str, str]] | None,
) -> dict[str, dict[str, str]]:
    if comparison_provenance is None:
        fallback = hashlib.sha256(b"unspecified-local-provenance-v1").hexdigest()
        return {track: {key: fallback for key in _PROVENANCE_HASH_KEYS} for track in _TRACKS}
    if set(comparison_provenance) == set(_PROVENANCE_HASH_KEYS):
        flat = cast(Mapping[str, str], comparison_provenance)
        checked = {key: _require_sha256(flat[key], key) for key in _PROVENANCE_HASH_KEYS}
        return {track: dict(checked) for track in _TRACKS}
    nested = cast(Mapping[str, Mapping[str, str]], comparison_provenance)
    provenance_by_track: dict[str, dict[str, str]] = {}
    for track, values in nested.items():
        if track not in _TRACKS or set(values) != set(_PROVENANCE_HASH_KEYS):
            raise ValueError("comparison_provenance keys do not match the required identity")
        provenance_by_track[track] = {
            key: _require_sha256(values[key], f"{track}.{key}") for key in _PROVENANCE_HASH_KEYS
        }
    return provenance_by_track


def _resolve_dimension_profile(
    track: str,
    groups: Mapping[tuple[str, str], Sequence[Attempt]],
    dimension_profiles: Mapping[str, Sequence[Mapping[str, object]]] | None,
) -> list[dict[str, object]]:
    if dimension_profiles is None:
        inferred = sorted({key for group in groups.values() for a in group for key in a.dimensions})
        if not inferred:
            raise ValueError(
                f"cannot infer dimension profile for track {track!r}: "
                "no dimension keys present on attempts (all-failed or empty dimensions)"
            )
        weight = 1.0 / len(inferred)
        raw_inferred = [{"id": dim_id, "label": dim_id, "weight": weight} for dim_id in inferred]
        return _normalize_dimension_profile(raw_inferred, f"inferred dimension_profile[{track!r}]")
    if track not in dimension_profiles:
        raise ValueError(f"dimension_profiles missing track {track!r}")
    raw_profile = dimension_profiles[track]
    return _normalize_dimension_profile(raw_profile, f"dimension_profiles[{track!r}]")


def _group_attempts(
    attempts: Sequence[Attempt],
) -> dict[tuple[str, str, str], dict[tuple[str, str], list[Attempt]]]:
    if not isinstance(attempts, Sequence) or isinstance(attempts, (str, bytes)):
        raise ValueError("attempts must be a sequence of Attempt")
    sections: dict[tuple[str, str, str], dict[tuple[str, str], list[Attempt]]] = {}
    for index, item in enumerate(attempts):
        if not isinstance(item, Attempt):
            raise ValueError(f"attempts[{index}] must be Attempt, got {type(item).__name__}")
        section_key = (item.track, item.contract_version, item.contract_sha256)
        entry_key = (item.harness, item.model_id)
        sections.setdefault(section_key, {}).setdefault(entry_key, []).append(item)
    return sections


def build_attempt_ledgers(
    attempts: Sequence[Attempt],
    *,
    mode: Literal["local", "publication"],
    generated_at: str,
    comparison_provenance: Mapping[str, str] | Mapping[str, Mapping[str, str]] | None = None,
    dimension_profiles: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
) -> list[AttemptLedger]:
    """Group attempts into comparison-scoped ledgers without derived statistics."""
    if mode not in _MODES:
        raise ValueError(f"mode must be 'local' or 'publication', got {mode!r}")
    generated_at = _require_generated_at(generated_at, "generated_at")
    provenance_by_track = _resolve_provenance(comparison_provenance)
    sections = _group_attempts(attempts)
    ledgers: list[AttemptLedger] = []
    for section_key in sorted(sections.keys()):
        track, contract_version, contract_sha256 = section_key
        if track not in provenance_by_track:
            raise ValueError(f"comparison_provenance missing track {track!r}")
        groups = sections[section_key]
        profile = _resolve_dimension_profile(track, groups, dimension_profiles)
        section_attempts = _dedupe_attempts(
            [a for group in groups.values() for a in group],
            path="attempts",
        )
        prov = provenance_by_track[track]
        ledgers.append(
            AttemptLedger(
                mode=mode,
                track=track,
                contract_version=contract_version,
                contract_sha256=contract_sha256,
                generated_at=generated_at,
                **prov,
                dimension_profile=tuple(MappingProxyType(dict(row)) for row in profile),
                attempts=section_attempts,
            )
        )
    return ledgers


def _aggregate_group(
    attempts: Sequence[Attempt],
    *,
    mode: str,
) -> dict[str, object]:
    """Aggregate one (harness, model_id) group into a derived entry."""
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
        score = float(statistics.median(scores))
        score_mean = float(statistics.fmean(scores))
        score_stdev = float(statistics.pstdev(scores))
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
            key: float(statistics.median([float(a.dimensions[key]) for a in valid]))
            for key in sorted(consistent)
        }
        spreads = [float(a.judge_spread) for a in valid]  # type: ignore[arg-type]
        judge_spread = float(statistics.median(spreads))
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
        cost_per_attempt = float(statistics.median(impl_costs))
        implementation_cost_per_attempt = cost_per_attempt
        cost_mean = float(statistics.fmean(impl_costs))
        cost_stdev = float(statistics.pstdev(impl_costs))
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
        evaluation_cost_per_attempt = float(statistics.median(eval_costs))
        if len(eval_costs) != total and mode == "publication":
            if _REASON_EVAL_COST_INCOMPLETE not in reasons:
                reasons.append(_REASON_EVAL_COST_INCOMPLETE)
    token_values = [a.tokens for a in attempts]
    tokens = int(round(statistics.median(token_values)))
    tokens_mean = float(statistics.fmean(token_values))
    tokens_min, tokens_max = min(token_values), max(token_values)
    tokens_range = tokens_max - tokens_min
    duration_values = [a.duration_s for a in attempts]
    duration_s = float(statistics.median(duration_values))
    duration_mean_s = float(statistics.fmean(duration_values))
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
            len({eid.lower().strip().replace(" ", "-") for eid in a.evaluator_ids})
            < _MIN_PUBLICATION_EVALUATORS
            for a in valid
        ):
            reasons.append(_REASON_INSUFFICIENT_EVALS)
    stable = tuple(sorted(set(reasons)))
    eligible = valid_count > 0 and len(stable) == 0
    raw_attempts = sorted(
        (attempt_to_raw(a) for a in attempts),
        key=raw_attempt_sort_key,
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
    """Derive version-scoped model aggregates from raw attempts.

    Groups first by exact ``(track, contract_version, contract_sha256)``, then
    by ``(harness, model_id)``. Never combines FE/BE or distinct contract
    identities. Returned roots are in-memory report intermediates; they are not
    the persisted ledger shape.
    """
    roots: list[dict[str, object]] = []
    for ledger in build_attempt_ledgers(
        attempts,
        mode=mode,
        generated_at=generated_at,
        comparison_provenance=comparison_provenance,
        dimension_profiles=dimension_profiles,
    ):
        groups: dict[tuple[str, str], list[Attempt]] = {}
        for attempt in ledger.attempts:
            groups.setdefault((attempt.harness, attempt.model_id), []).append(attempt)
        root = {
            **ledger.metadata_to_raw(),
            "schema_version": _SCHEMA_VERSION_LEGACY,
            "entries": [_aggregate_group(groups[key], mode=mode) for key in sorted(groups)],
        }
        roots.append(root)
    return roots


def _leaderboard_basename(
    track: str,
    contract_version: str,
    contract_sha256: str,
) -> str:
    track_s = validate_identifier(track, field="track")
    version_s = validate_identifier(contract_version, field="contract_version")
    sha_s = _require_sha256(contract_sha256, "contract_sha256")
    return f"leaderboard_{track_s}_{version_s}_{sha_s}"


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(os.fspath(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
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
            with suppress(OSError):
                tmp_path.unlink()
        raise


def _write_plans(plans: Sequence[tuple[Path, Callable[[], str]]]) -> list[Path]:
    ordered = sorted(plans, key=lambda item: item[0].as_posix())
    collisions = [path for path, _ in ordered if os.path.lexists(path)]
    if collisions:
        raise ValueError(
            "refusing to overwrite existing files: "
            + ", ".join(sorted(str(path) for path in collisions))
        )
    for target, render in ordered:
        atomic_write_text(target, render())
    return [path for path, _ in ordered]


def _ledger_json(ledger: AttemptLedger) -> str:
    return json.dumps(ledger.to_raw(), sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def write_attempt_ledgers(
    output_dir: Path,
    ledgers: Sequence[AttemptLedger],
) -> list[Path]:
    """Atomically write canonical JSON attempt ledgers (no derived statistics)."""
    output_dir = _ensure_output_dir(output_dir)
    if not isinstance(ledgers, Sequence) or isinstance(ledgers, (str, bytes)):
        raise ValueError("ledgers must be a sequence of AttemptLedger")
    plans: list[tuple[Path, Callable[[], str]]] = []
    seen: set[str] = set()
    for index, ledger in enumerate(ledgers):
        if not isinstance(ledger, AttemptLedger):
            raise ValueError(f"ledgers[{index}]: expected AttemptLedger")
        base = _leaderboard_basename(ledger.track, ledger.contract_version, ledger.contract_sha256)
        if base in seen:
            raise ValueError(f"ledgers[{index}]: duplicate export basename {base!r}")
        seen.add(base)
        target = resolve_within(output_dir, f"{base}.json")
        plans.append((target, partial(_ledger_json, ledger)))
    return _write_plans(plans)


def write_leaderboards(
    output_dir: Path,
    leaderboards: Sequence[Mapping[str, object]],
) -> list[Path]:
    """Write legacy aggregate roots as deterministic JSON, CSV, and Markdown.

    This compatibility facade preserves the pre-ledger public API for callers
    that explicitly export all three views. Normal runner finalization uses
    :func:`write_attempt_ledgers` and therefore persists canonical JSON only.
    """
    output_dir = _ensure_output_dir(output_dir)
    if not isinstance(leaderboards, Sequence) or isinstance(leaderboards, (str, bytes)):
        raise ValueError("leaderboards must be a sequence of root mappings")

    plans: list[tuple[Path, Callable[[], str]]] = []
    seen_basenames: set[str] = set()
    for index, root in enumerate(leaderboards):
        if not isinstance(root, Mapping):
            raise ValueError(f"leaderboards[{index}]: expected mapping")
        _exact_keys(root, _LEGACY_ROOT_KEYS, f"leaderboards[{index}]")
        if root["schema_version"] != _SCHEMA_VERSION_LEGACY:
            raise ValueError(f"leaderboards[{index}].schema_version: unsupported version")
        track = root["track"]
        if not isinstance(track, str) or track not in _TRACKS:
            raise ValueError(f"leaderboards[{index}].track: expected 'fe' or 'be'")
        contract_version = root["contract_version"]
        contract_sha256 = root["contract_sha256"]
        if not isinstance(contract_version, str):
            raise ValueError(f"leaderboards[{index}].contract_version: expected string")
        if not isinstance(contract_sha256, str):
            raise ValueError(f"leaderboards[{index}].contract_sha256: expected string")
        entries = root["entries"]
        if not isinstance(entries, list):
            raise ValueError(f"leaderboards[{index}].entries: expected list")

        basename = _leaderboard_basename(track, contract_version, contract_sha256)
        if basename in seen_basenames:
            raise ValueError(f"leaderboards[{index}]: duplicate export basename {basename!r}")
        seen_basenames.add(basename)
        renderers = (
            ("json", lambda root=root: json.dumps(root, sort_keys=True, ensure_ascii=False) + "\n"),
            ("csv", lambda entries=entries: _render_csv(entries)),
            ("md", lambda root=root, entries=entries: _render_markdown(root, entries)),
        )
        for extension, render in renderers:
            plans.append((resolve_within(output_dir, f"{basename}.{extension}"), render))
    return _write_plans(plans)


def _serialize_tabular_cell(column: str, value: object) -> str:
    """Serialize one aggregate cell for CSV/Markdown (excludes raw_attempts)."""
    if column == "dimensions":
        if not isinstance(value, Mapping):
            return json.dumps(value, sort_keys=True, ensure_ascii=False)
        return json.dumps(dict(value), sort_keys=True, ensure_ascii=False)
    if column in ("ineligible_reasons", "run_ids"):
        if value is None:
            return "[]"
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            return json.dumps(value, ensure_ascii=False)
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
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _render_markdown(
    root: Mapping[str, object],
    entries: Sequence[Mapping[str, object]],
) -> str:
    lines: list[str] = [
        f"# Leaderboard ({root['track']} · {root['contract_version']})",
        "",
        f"- contract_sha256: `{root['contract_sha256']}`",
        f"- generated_at: `{root['generated_at']}`",
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


def _ensure_output_dir(output_dir: Path) -> Path:
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
    return output_dir.resolve()


def _derived_root_for_ledger(ledger: AttemptLedger) -> dict[str, object]:
    """Derive one aggregate root from a loaded ledger via :func:`aggregate_attempts`."""
    if not ledger.attempts:
        return {**ledger.metadata_to_raw(), "entries": []}
    roots = aggregate_attempts(
        ledger.attempts,
        mode=cast(Literal["local", "publication"], ledger.mode),
        generated_at=ledger.generated_at,
        comparison_provenance=ledger.provenance,
        dimension_profiles={ledger.track: profile_to_raw(ledger.dimension_profile)},
    )
    root = roots[0]
    # Preserve source ledger schema for tabular Markdown; aggregation math is unchanged.
    root["schema_version"] = ledger.schema_version
    return root


def write_tabular_views(input_path: Path, output_dir: Path) -> tuple[Path, Path]:
    """Project one canonical or legacy leaderboard JSON to CSV and Markdown.

    Loads via :func:`load_attempt_ledger`, derives model rows only through
    :func:`aggregate_attempts`, and writes deterministic UTF-8 ``.csv`` and
    ``.md`` files named from the comparison identity. Refuses overwrite and
    detects collisions before any write so failure cannot leave a partial pair.
    """
    ledger = load_attempt_ledger(input_path)
    root = _derived_root_for_ledger(ledger)
    entries = cast(list[Mapping[str, object]], root["entries"])
    output_dir = _ensure_output_dir(output_dir)
    base = _leaderboard_basename(ledger.track, ledger.contract_version, ledger.contract_sha256)
    csv_path = resolve_within(output_dir, f"{base}.csv")
    md_path = resolve_within(output_dir, f"{base}.md")
    written = _write_plans(
        [
            (csv_path, lambda: _render_csv(entries)),
            (md_path, lambda: _render_markdown(root, entries)),
        ]
    )
    return cast(tuple[Path, Path], tuple(written))
