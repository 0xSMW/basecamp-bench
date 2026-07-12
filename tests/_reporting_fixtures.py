"""Shared builders for the reporting test modules."""

from __future__ import annotations

from basecamp_bench.reporting import ReportPoint

_DEFAULT_SHA = "b" * 64


def _raw_attempt(
    *,
    run_id: str = "run-1",
    submission_id: str = "sub-1",
    repetition: int = 1,
    track: str = "fe",
    contract_version: str = "1.0",
    contract_sha256: str | None = None,
    harness: str = "h1",
    model_id: str = "m1",
    display_name: str | None = None,
    implementation_success: bool = True,
    evaluation_success: bool = True,
    score: float | None = 5.0,
    dimensions: dict[str, float] | None = None,
    judge_spread: float | None = 0.05,
    implementation_cost_usd: float | None = 1.0,
    evaluation_cost_usd: float | None = 0.1,
    tokens: int = 100,
    duration_s: float = 12.5,
    evaluator_ids: list[str] | None = None,
    ineligible_reasons: list[str] | None = None,
    **overrides: object,
) -> dict:
    if evaluation_success:
        sc = 5.0 if score is None else float(score)
        dims = (
            dimensions if dimensions is not None else {"quality": sc, "craft": max(0.0, sc - 0.5)}
        )
        spread = 0.05 if judge_spread is None else judge_spread
    else:
        dims = {} if dimensions is None else dimensions
        spread = judge_spread
        sc = score
    base: dict = {
        "run_id": run_id,
        "submission_id": submission_id,
        "repetition": repetition,
        "track": track,
        "contract_version": contract_version,
        "contract_sha256": contract_sha256 or _DEFAULT_SHA,
        "harness": harness,
        "model_id": model_id,
        "display_name": display_name or model_id,
        "implementation_success": implementation_success,
        "evaluation_success": evaluation_success,
        "score": sc,
        "dimensions": dims,
        "judge_spread": spread,
        "implementation_cost_usd": implementation_cost_usd,
        "evaluation_cost_usd": evaluation_cost_usd,
        "tokens": tokens,
        "duration_s": duration_s,
        "evaluator_ids": list(evaluator_ids) if evaluator_ids is not None else ["j1"],
        "ineligible_reasons": list(ineligible_reasons) if ineligible_reasons is not None else [],
    }
    base.update(overrides)
    return base


def _failed_raw_attempt(**overrides: object) -> dict:
    defaults: dict = {
        "implementation_success": False,
        "evaluation_success": False,
        "score": None,
        "dimensions": {},
        "judge_spread": None,
        "implementation_cost_usd": None,
        "evaluation_cost_usd": None,
        "ineligible_reasons": ["failed"],
    }
    defaults.update(overrides)
    return _raw_attempt(**defaults)  # type: ignore[arg-type]


def _point(
    *,
    model_id: str = "model-a",
    display_name: str | None = None,
    track: str = "fe",
    contract_version: str = "1.0",
    contract_sha256: str = "a" * 64,
    harness: str = "harness-x",
    score: float = 5.0,
    score_mean: float | None = None,
    score_stdev: float = 0.1,
    score_min: float | None = None,
    score_max: float | None = None,
    score_range: float = 0.0,
    judge_spread: float = 0.2,
    cost_per_attempt: float = 1.0,
    cost_mean: float | None = None,
    cost_stdev: float = 0.05,
    cost_min: float | None = None,
    cost_max: float | None = None,
    cost_range: float = 0.0,
    success_rate: float = 1.0,
    repetitions: int = 3,
    dimensions: dict[str, float] | None = None,
    tokens: int = 1000,
    tokens_mean: float | None = None,
    tokens_min: int | None = None,
    tokens_max: int | None = None,
    tokens_range: int = 0,
    duration_s: float = 10.0,
    duration_mean_s: float | None = None,
    duration_min_s: float | None = None,
    duration_max_s: float | None = None,
    duration_range_s: float = 0.0,
    eligible: bool = True,
    ineligible_reasons: tuple[str, ...] = (),
    run_ids: tuple[str, ...] = ("run-1",),
    implementation_cost_per_attempt: float | None = None,
    evaluation_cost_per_attempt: float = 0.1,
    raw_attempts: tuple | None = None,
    mode: str = "publication",
) -> ReportPoint:
    impl = (
        cost_per_attempt
        if implementation_cost_per_attempt is None
        else implementation_cost_per_attempt
    )
    return ReportPoint(
        track=track,
        contract_version=contract_version,
        contract_sha256=contract_sha256,
        model_id=model_id,
        display_name=display_name or model_id,
        harness=harness,
        score=score,
        score_mean=score if score_mean is None else score_mean,
        score_stdev=score_stdev,
        score_min=score if score_min is None else score_min,
        score_max=score if score_max is None else score_max,
        score_range=score_range,
        judge_spread=judge_spread,
        cost_per_attempt=cost_per_attempt,
        cost_mean=cost_per_attempt if cost_mean is None else cost_mean,
        cost_stdev=cost_stdev,
        cost_min=cost_per_attempt if cost_min is None else cost_min,
        cost_max=cost_per_attempt if cost_max is None else cost_max,
        cost_range=cost_range,
        success_rate=success_rate,
        repetitions=repetitions,
        dimensions=dimensions if dimensions is not None else {"dim_a": 5.0},
        tokens=tokens,
        tokens_mean=float(tokens) if tokens_mean is None else tokens_mean,
        tokens_min=tokens if tokens_min is None else tokens_min,
        tokens_max=tokens if tokens_max is None else tokens_max,
        tokens_range=tokens_range,
        duration_s=duration_s,
        duration_mean_s=duration_s if duration_mean_s is None else duration_mean_s,
        duration_min_s=duration_s if duration_min_s is None else duration_min_s,
        duration_max_s=duration_s if duration_max_s is None else duration_max_s,
        duration_range_s=duration_range_s,
        eligible=eligible,
        ineligible_reasons=ineligible_reasons,
        run_ids=run_ids,
        implementation_cost_per_attempt=impl,
        evaluation_cost_per_attempt=evaluation_cost_per_attempt,
        raw_attempts=raw_attempts if raw_attempts is not None else (),
        mode=mode,
    )


def _entry(
    model_id: str,
    *,
    score: float = 5.0,
    cost_per_attempt: float = 1.0,
    success_rate: float = 1.0,
    eligible: bool = True,
    ineligible_reasons: list[str] | None = None,
    display_name: str | None = None,
    harness: str = "h1",
    evaluation_cost_per_attempt: float = 0.1,
    track: str = "fe",
    contract_version: str = "1.0",
    contract_sha256: str | None = None,
    raw_attempts: list[dict] | None = None,
    **overrides: object,
) -> dict:
    sha = contract_sha256 or _DEFAULT_SHA
    if raw_attempts is None:
        if success_rate == 0.0:
            raw_attempts = [
                _failed_raw_attempt(
                    run_id=f"{model_id}-run-1",
                    submission_id=f"{model_id}-sub-1",
                    model_id=model_id,
                    display_name=display_name or model_id,
                    harness=harness,
                    track=track,
                    contract_version=contract_version,
                    contract_sha256=sha,
                    implementation_cost_usd=cost_per_attempt,
                    evaluation_cost_usd=evaluation_cost_per_attempt,
                )
            ]
        else:
            raw_attempts = [
                _raw_attempt(
                    run_id=f"{model_id}-run-1",
                    submission_id=f"{model_id}-sub-1",
                    model_id=model_id,
                    display_name=display_name or model_id,
                    harness=harness,
                    track=track,
                    contract_version=contract_version,
                    contract_sha256=sha,
                    score=score,
                    dimensions={"quality": score, "craft": max(0.0, score - 0.5)},
                    implementation_cost_usd=cost_per_attempt,
                    evaluation_cost_usd=evaluation_cost_per_attempt,
                )
            ]
    base: dict = {
        "model_id": model_id,
        "display_name": display_name or model_id,
        "harness": harness,
        "score": score,
        "score_mean": score,
        "score_stdev": 0.1,
        "score_min": max(0.0, score - 0.1),
        "score_max": min(10.0, score + 0.1),
        "score_range": min(10.0, score + 0.1) - max(0.0, score - 0.1),
        "judge_spread": 0.05,
        "cost_per_attempt": cost_per_attempt,
        "cost_mean": cost_per_attempt,
        "cost_stdev": 0.02,
        "cost_min": max(0.0, cost_per_attempt - 0.02),
        "cost_max": cost_per_attempt + 0.02,
        "cost_range": cost_per_attempt + 0.02 - max(0.0, cost_per_attempt - 0.02),
        "success_rate": success_rate,
        "repetitions": 3,
        "dimensions": {"quality": score, "craft": max(0.0, score - 0.5)},
        "tokens": 100,
        "tokens_mean": 100.0,
        "tokens_min": 100,
        "tokens_max": 100,
        "tokens_range": 0,
        "duration_s": 12.5,
        "duration_mean_s": 12.5,
        "duration_min_s": 12.5,
        "duration_max_s": 12.5,
        "duration_range_s": 0.0,
        "eligible": eligible,
        "ineligible_reasons": ineligible_reasons if ineligible_reasons is not None else [],
        "run_ids": [f"{model_id}-run-1"],
        "implementation_cost_per_attempt": cost_per_attempt,
        "evaluation_cost_per_attempt": evaluation_cost_per_attempt,
        "raw_attempts": raw_attempts,
    }
    base.update(overrides)
    return base


def _sync_entry_identity(
    entry: dict,
    *,
    track: str,
    contract_version: str,
    contract_sha256: str,
) -> dict:
    """Align raw-attempt identity fields with the leaderboard root and entry."""
    e = dict(entry)
    if "raw_attempts" in e:
        raws: list[dict] = []
        for raw in e["raw_attempts"] or []:
            r = dict(raw)
            r["track"] = track
            r["contract_version"] = contract_version
            r["contract_sha256"] = contract_sha256
            r["model_id"] = e["model_id"]
            r["harness"] = e["harness"]
            raws.append(r)
        e["raw_attempts"] = raws
    return e


def _leaderboard(
    entries: list[dict],
    *,
    track: str = "fe",
    contract_version: str = "1.0",
    contract_sha256: str | None = None,
    schema_version: str = "1.0",
    generated_at: str = "2026-01-01T00:00:00Z",
    sync_identity: bool = True,
) -> dict:
    sha = contract_sha256 or _DEFAULT_SHA
    fixed = (
        [
            _sync_entry_identity(
                e, track=track, contract_version=contract_version, contract_sha256=sha
            )
            for e in entries
        ]
        if sync_identity
        else list(entries)
    )
    dimension_ids = sorted(
        {
            key
            for entry in fixed
            for raw in entry.get("raw_attempts", [])
            for key in raw.get("dimensions", {})
        }
    ) or ["quality"]
    weight = 1.0 / len(dimension_ids)
    return {
        "schema_version": schema_version,
        "mode": "publication",
        "track": track,
        "contract_version": contract_version,
        "contract_sha256": sha,
        "generated_at": generated_at,
        "runner_source_sha256": "1" * 64,
        "seed_tree_sha256": "2" * 64,
        "reference_manifest_sha256": "3" * 64,
        "reference_tree_sha256": "4" * 64,
        "prompt_sha256": "5" * 64,
        "rubric_sha256": "6" * 64,
        "schema_bundle_sha256": "7" * 64,
        "dimension_profile": [
            {"id": dim_id, "label": dim_id.title(), "weight": weight} for dim_id in dimension_ids
        ],
        "entries": fixed,
    }
