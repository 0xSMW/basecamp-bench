"""Deterministic HTML report generation from attempt ledgers (stdlib only).

Loads canonical or legacy leaderboard JSON, derives aggregates once via
:func:`aggregate_attempts`, classifies Pareto frontiers, and renders offline HTML.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

from basecamp_bench.leaderboard import (
    Attempt,
    AttemptLedger,
    aggregate_attempts,
    atomic_write_text,
    attempt_canonical_json,
    attempt_from_raw,
    attempt_to_raw,
    freeze_raw_attempt,
    load_attempt_ledger,
    profile_to_raw,
    require_display_name,
)
from basecamp_bench.report_rendering import render_report_html
from basecamp_bench.reporting_model import raw_attempt_sort_key as _raw_attempt_sort_key
from basecamp_bench.validation import is_finite_number

__all__ = [
    "ReportPoint",
    "expected_cost",
    "pareto_frontier",
    "load_leaderboards",
    "build_report_payload",
    "render_report_html",
    "rename_display_names",
    "write_report",
]

_PROVENANCE_HASH_KEYS = (
    "runner_source_sha256",
    "seed_tree_sha256",
    "reference_manifest_sha256",
    "reference_tree_sha256",
    "prompt_sha256",
    "rubric_sha256",
    "schema_bundle_sha256",
)


@dataclass(frozen=True, slots=True)
class ReportPoint:
    """One model entry on a version-scoped leaderboard (derived aggregates)."""

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
    runner_source_sha256_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"local", "publication"}:
            raise ValueError("mode must be 'local' or 'publication'")
        if not isinstance(self.dimensions, MappingProxyType):
            object.__setattr__(
                self,
                "dimensions",
                MappingProxyType({str(k): float(v) for k, v in dict(self.dimensions).items()}),
            )
        for name in ("ineligible_reasons", "run_ids", "generated_at_values", "source_run_ids"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not isinstance(self.raw_attempts, tuple) or any(
            not isinstance(item, MappingProxyType) for item in self.raw_attempts
        ):
            object.__setattr__(
                self, "raw_attempts", tuple(freeze_raw_attempt(item) for item in self.raw_attempts)
            )
        object.__setattr__(
            self, "runner_source_sha256_values", tuple(self.runner_source_sha256_values)
        )


def expected_cost(point: ReportPoint) -> float | None:
    """Return implementation_cost_per_attempt / success_rate (never evaluation cost)."""
    if any(
        reason in {"implementation_cost_unknown", "implementation_cost_incomplete"}
        for reason in point.ineligible_reasons
    ):
        return None
    cost, rate = point.implementation_cost_per_attempt, point.success_rate
    if isinstance(cost, bool) or isinstance(rate, bool):
        return None
    if not isinstance(cost, (int, float)) or not isinstance(rate, (int, float)):
        return None
    cost_f, rate_f = float(cost), float(rate)
    if not math.isfinite(cost_f) or not math.isfinite(rate_f):
        return None
    if cost_f < 0.0 or rate_f <= 0.0 or rate_f > 1.0:
        return None
    result = cost_f / rate_f
    return result if math.isfinite(result) else None


def _is_nonneg_finite(value: Any) -> bool:
    return is_finite_number(value) and float(value) >= 0.0


def _section_key(point: ReportPoint) -> tuple[str, ...]:
    return (
        point.track,
        point.contract_version,
        point.contract_sha256,
        point.mode,
        *(getattr(point, key) for key in _PROVENANCE_HASH_KEYS),
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


def _profile_json(profile: Sequence[Mapping[str, object]]) -> str:
    portable = profile_to_raw(profile)
    portable.sort(key=lambda row: str(row["id"]))
    return json.dumps(portable, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _compatibility_section(ledger: AttemptLedger) -> tuple[str, ...]:
    # Local reports may combine runner revisions; publication keeps runner hash.
    runner_compatibility = ledger.runner_source_sha256 if ledger.mode == "publication" else "local"
    return (
        ledger.track,
        ledger.contract_version,
        ledger.contract_sha256,
        ledger.mode,
        runner_compatibility,
        *(getattr(ledger, key) for key in _PROVENANCE_HASH_KEYS[1:]),
        _profile_json(ledger.dimension_profile),
    )


def _report_point_from_entry(
    entry: Mapping[str, object],
    *,
    track: str,
    contract_version: str,
    contract_sha256: str,
    provenance: Mapping[str, str],
    section_meta: Mapping[str, Any],
) -> ReportPoint:
    """Build a ReportPoint from trusted aggregate_attempts output."""

    point_fields = {field.name for field in dataclasses.fields(ReportPoint)}
    values: dict[str, Any] = {key: value for key, value in entry.items() if key in point_fields}
    success_rate = float(cast(float, entry["success_rate"]))
    eligible = bool(entry["eligible"])
    reasons = list(cast(Sequence[str], entry["ineligible_reasons"]))
    if success_rate == 0.0:
        eligible = False
        if "success_rate is zero" not in reasons:
            reasons.append("success_rate is zero")
    values.update(provenance)
    values.update(section_meta)
    values.update(
        track=track,
        contract_version=contract_version,
        contract_sha256=contract_sha256,
        success_rate=success_rate,
        eligible=eligible,
        ineligible_reasons=reasons,
    )
    return ReportPoint(**values)


def load_leaderboards(paths: Sequence[Path]) -> list[ReportPoint]:
    """Load ledgers/legacy leaderboards, dedupe, and recompute model aggregates."""
    collected: dict[tuple[str, ...], dict[str, Any]] = {}

    for raw_path in paths:
        path = Path(raw_path)
        label = os.fspath(path)
        ledger = load_attempt_ledger(path)
        profile_json = _profile_json(ledger.dimension_profile)
        section = _compatibility_section(ledger)
        bucket = collected.setdefault(
            section,
            {
                "schema_versions": set(),
                "timestamps": set(),
                "attempts": {},
                "model_identity": {},
                "provenance": {
                    "mode": ledger.mode,
                    **ledger.provenance,
                    "dimension_profile_json": profile_json,
                },
                "runner_source_hashes": set(),
                "profile": [dict(row) for row in ledger.dimension_profile],
            },
        )
        bucket["schema_versions"].add(ledger.schema_version)
        bucket["timestamps"].add(ledger.generated_at)
        bucket["runner_source_hashes"].add(ledger.runner_source_sha256)

        for attempt in ledger.attempts:
            identity = (attempt.harness, attempt.model_id)
            prior_name = bucket["model_identity"].setdefault(identity, attempt.display_name)
            if prior_name != attempt.display_name:
                raise ValueError(f"{label}: inconsistent display identity for {identity!r}")
            canonical = attempt_canonical_json(attempt)
            logical = (attempt.run_id, attempt.submission_id, attempt.repetition)
            existing = bucket["attempts"].get(logical)
            if existing is not None and existing[0] != canonical:
                raise ValueError(f"{label}: conflicting non-identical duplicate raw attempt")
            bucket["attempts"][logical] = (canonical, attempt)

    points: list[ReportPoint] = []
    for compat_key in sorted(collected):
        bucket = collected[compat_key]
        attempts = [item[1] for _, item in sorted(bucket["attempts"].items())]
        provenance = dict(bucket["provenance"])
        mode = cast(Literal["local", "publication"], provenance["mode"])
        runner_source_values = sorted(bucket["runner_source_hashes"])
        if len(runner_source_values) > 1:
            encoded_sources = json.dumps(
                runner_source_values,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
            provenance["runner_source_sha256"] = hashlib.sha256(encoded_sources).hexdigest()
        roots = aggregate_attempts(
            cast(Sequence[Attempt], attempts),
            mode=mode,
            generated_at=max(bucket["timestamps"]),
            comparison_provenance={key: provenance[key] for key in _PROVENANCE_HASH_KEYS},
            dimension_profiles={compat_key[0]: bucket["profile"]},
        )
        section_meta = {
            "schema_version": (
                next(iter(bucket["schema_versions"]))
                if len(bucket["schema_versions"]) == 1
                else None
            ),
            "generated_at_values": sorted(bucket["timestamps"]),
            "source_run_ids": sorted({attempt.run_id for attempt in attempts}),
            "runner_source_sha256_values": runner_source_values,
        }
        combined_entries = cast(list[dict[str, Any]], roots[0]["entries"])
        for entry in combined_entries:
            points.append(
                _report_point_from_entry(
                    entry,
                    track=compat_key[0],
                    contract_version=compat_key[1],
                    contract_sha256=compat_key[2],
                    provenance=provenance,
                    section_meta=section_meta,
                )
            )
    return points


def _classification(
    point: ReportPoint,
    frontier: set[tuple[str, str]],
) -> str:
    if not _frontier_eligible(point):
        return "ineligible"
    return "frontier" if _point_identity(point) in frontier else "dominated"


def _frontier_sort_key(point: ReportPoint) -> tuple[float, float, str, str]:
    cost = expected_cost(point)
    assert cost is not None
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


def _point_payload(
    point: ReportPoint,
    *,
    classification: str,
    dominator: str | None,
    marginal: float | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field in dataclasses.fields(point):
        if field.name == "mode":
            break
        if field.name not in {"track", "contract_version", "contract_sha256"}:
            payload[field.name] = getattr(point, field.name)
    payload["dimensions"] = {k: point.dimensions[k] for k in sorted(point.dimensions)}
    portable_raw = [
        attempt_to_raw(attempt_from_raw(dict(raw)))
        for raw in sorted(point.raw_attempts, key=_raw_attempt_sort_key)
    ]
    implementation_cost_complete = not any(
        reason in {"implementation_cost_unknown", "implementation_cost_incomplete"}
        for reason in point.ineligible_reasons
    )

    def implementation_cost(value: float) -> float | None:
        return value if implementation_cost_complete else None

    implementation_per_attempt = implementation_cost(point.implementation_cost_per_attempt)
    total_cost_per_attempt = (
        None
        if implementation_per_attempt is None
        else implementation_per_attempt + point.evaluation_cost_per_attempt
    )

    payload.update(
        {
            "point_id": _point_id(point),
            "cost_per_attempt": implementation_cost(point.cost_per_attempt),
            "cost_mean": implementation_cost(point.cost_mean),
            "implementation_cost_per_attempt": implementation_per_attempt,
            "evaluation_cost_per_attempt": point.evaluation_cost_per_attempt,
            "total_cost_per_attempt": total_cost_per_attempt,
            "cost_stdev": implementation_cost(point.cost_stdev),
            "cost_min": implementation_cost(point.cost_min),
            "cost_max": implementation_cost(point.cost_max),
            "cost_range": implementation_cost(point.cost_range),
            "ineligible_reasons": list(point.ineligible_reasons),
            "run_ids": list(point.run_ids),
            "raw_attempts": portable_raw,
            "expected_cost": expected_cost(point),
            "classification": classification,
            "dominator": dominator,
            "marginal_cost_per_quality": marginal,
        }
    )
    return payload


def build_report_payload(points: Sequence[ReportPoint]) -> dict[str, Any]:
    """Build a JSON-serializable, deterministic report payload.

    Groups points by full comparison identity. Every point appears, including
    ineligible ones. Provenance comes only from loaded ledger metadata (or null
    when absent).
    """
    sections_map: dict[tuple[str, ...], list[ReportPoint]] = {}
    for point in points:
        sections_map.setdefault(_section_key(point), []).append(point)

    sections: list[dict[str, Any]] = []
    for key in sorted(sections_map.keys()):
        track, contract_version, contract_sha256 = key[:3]
        group = list(sections_map[key])
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
            cls = _classification(point, frontier_ids)
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
                "runner_source_sha256_values": sorted(
                    {value for point in group for value in point.runner_source_sha256_values}
                ),
                **dict(zip(_PROVENANCE_HASH_KEYS[1:], key[5:11], strict=True)),
                "dimension_profile": json.loads(key[11]),
                "frontier": ordered_frontier_ids,
                "models": models,
            }
        )

    return {"sections": sections}


def rename_display_names(
    points: Sequence[ReportPoint], display_names: Mapping[str, str]
) -> list[ReportPoint]:
    """Return *points* with display names overridden by ``model_id``.

    Evidence bundles carry the display name that was configured when the run
    executed; renaming at report time corrects presentation without touching
    hash-pinned evidence. Every key must match at least one point so a typo
    fails loudly instead of silently keeping the stale label.
    """
    # Validate every override against Attempt display-name constraints before mutation.
    validated = {
        model_id: require_display_name(name, f"display name for {model_id!r}")
        for model_id, name in display_names.items()
    }
    unmatched = set(validated)
    renamed: list[ReportPoint] = []
    for point in points:
        new_name = validated.get(point.model_id)
        if new_name is None:
            renamed.append(point)
            continue
        unmatched.discard(point.model_id)
        raw_attempts = tuple(
            MappingProxyType({**dict(raw), "display_name": new_name}) for raw in point.raw_attempts
        )
        renamed.append(dataclasses.replace(point, display_name=new_name, raw_attempts=raw_attempts))
    if unmatched:
        known = ", ".join(sorted({p.model_id for p in points}))
        raise ValueError(
            f"rename targets not present in any leaderboard: "
            f"{', '.join(sorted(unmatched))} (known model ids: {known})"
        )
    return renamed


def write_report(
    paths: Sequence[Path],
    output: Path,
    *,
    display_names: Mapping[str, str] | None = None,
    commentary: Mapping[str, Any] | None = None,
) -> Path:
    """Load leaderboards, build payload, render HTML, atomically write *output*."""
    output = Path(output)
    points = load_leaderboards(paths)
    if display_names:
        points = rename_display_names(points, display_names)
    payload = build_report_payload(points)
    html_text = render_report_html(payload, commentary=commentary)

    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output, html_text)
    return output
