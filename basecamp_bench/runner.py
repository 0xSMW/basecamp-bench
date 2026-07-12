"""Benchmark core execution: materialize, run agents, evaluate, aggregate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import statistics
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from basecamp_bench import __version__
from basecamp_bench.adapters import AgentJob, Harness, ModelSpec, Usage, get_harness
from basecamp_bench.config import BenchConfig, config_to_public_dict
from basecamp_bench.contracts import (
    EvaluationContract,
    ValidatedJudgeScores,
    aggregate_judges,
    contract_sha256,
    load_contract,
    normalize_validated_judge_result,
)
from basecamp_bench.execution import (
    AgentExecution,
    execute_agent,
)
from basecamp_bench.execution import (
    process_ok as _process_ok,
)
from basecamp_bench.execution import (
    read_text as _read_text,
)
from basecamp_bench.execution import (
    safe_error as _safe_error,
)
from basecamp_bench.execution import (
    secret_env_values as _secret_env_values,
)
from basecamp_bench.layout import (
    finalize_readable_layout,
    recover_pending_layouts,
    validate_planned_layout,
)
from basecamp_bench.leaderboard import (
    Attempt,
    aggregate_attempts,
    attempt_to_raw,
    build_attempt_ledgers,
    write_attempt_ledgers,
)
from basecamp_bench.locks import acquire_lane_locks
from basecamp_bench.manifest import (
    build_manifest,
    git_provenance,
    hash_inputs,
    write_manifest,
)
from basecamp_bench.naming import run_path_name, submission_path_name
from basecamp_bench.pricing import find_exact_rates, load_pricing_snapshot, normalize_model_id
from basecamp_bench.processes import ProcessResult, run_managed
from basecamp_bench.prompts import build_evaluator_prompt, implementation_prompt_bytes
from basecamp_bench.reference_pack import load_reference_pack
from basecamp_bench.reporting import write_report
from basecamp_bench.safety import (
    atomic_snapshot,
    atomic_write_json,
    create_unique_directory,
    portable_path,
    redact_text,
    sha256_file,
    tree_manifest,
    validate_identifier,
    verify_tree_manifest,
)
from basecamp_bench.scheduling import collect_indexed, executor_pool, worker_count
from basecamp_bench.validation import is_finite_number

__all__ = [
    "RunOptions",
    "AgentExecution",
    "new_run_id",
    "materialize_seed",
    "execute_agent",
    "run_benchmark",
    "reevaluate_run",
]

_SEED_IGNORE = (
    "reference",
    "reference/**",
    ".git",
    ".git/**",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "credentials*",
    "secrets*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "auth.json",
    "service-account.json",
    ".DS_Store",
    "**/.DS_Store",
    "__pycache__",
    "__pycache__/**",
    "*.pyc",
)
_AMBIENT_IGNORE = (
    ".DS_Store",
    "**/.DS_Store",
    "__pycache__",
    "**/__pycache__",
    "*.pyc",
    "**/*.pyc",
    ".pytest_cache",
    "**/.pytest_cache",
    ".mypy_cache",
    "**/.mypy_cache",
    ".ruff_cache",
    "**/.ruff_cache",
    "node-compile-cache",
    "**/node-compile-cache",
    "node_modules",
    "**/node_modules",
    ".venv",
    "**/.venv",
)
_PRICING_URL = "https://models.dev/api.json"
_PRICING_MAX_AGE_S = 7 * 24 * 3600
_MIN_PUB_REPS, _MIN_PUB_EVALS, _MIN_LOCAL_EVALS = 3, 2, 1
_VERSION_TIMEOUT_S = 10.0
_VERSION_MAX_BYTES = 16 * 1024
_NO_SEED_LIMITATION = "Agent CLI exposes no deterministic seed control."


@dataclass(frozen=True)
class RunOptions:
    """Runtime controls that apply to every agent process in a run.

    ``max_parallel_agents`` is a global cap shared by implementation and
    evaluator work. When supplied, ``progress`` receives an event name and a
    read-only field mapping; callback failures are deliberately ignored so UI
    or logging code cannot fail a paid benchmark job.
    """

    allow_unsafe_host_execution: bool = False
    confirmed_isolated_environment: bool = False
    allow_network_pricing: bool = True
    max_log_bytes: int = 50_000_000
    max_parallel_agents: int = 32
    progress: Callable[[str, Mapping[str, object]], None] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.max_parallel_agents, bool) or not isinstance(
            self.max_parallel_agents, int
        ):
            raise ValueError("max_parallel_agents must be a positive integer")
        if self.max_parallel_agents < 1:
            raise ValueError("max_parallel_agents must be a positive integer")


@dataclass(frozen=True)
class _RunTask:
    harness: Any
    track: Any
    repetition: int
    submission_id: str
    evaluator_attempts: tuple[tuple[Any, str], ...]


@dataclass(slots=True)
class _RunContext:
    """Typed state shared across one run's lifecycle and checkpoint writes.

    Mutable collections are intentional: reevaluation adds lineage hashes and
    the runner accumulates eligibility reasons before finalization.
    """

    run_dir: Path
    run_id: str
    make_id: Callable[[], str]
    started: str
    runner_git: dict[str, Any]
    pricing_payload: Mapping[str, Any] | None
    pricing_prov: dict[str, Any]
    pricing_retrieved_at: str | None
    pricing_ok: bool
    pricing_reasons: list[str]
    ineligible: list[str]
    contracts: dict[str, EvaluationContract]
    contract_hashes: dict[str, str]
    input_hashes: dict[str, str]
    comparison_provenance: dict[str, dict[str, str]]
    tooling: list[dict[str, Any]]


@dataclass(frozen=True)
class _PreparedSubmission:
    """Fresh or reused implementation provenance for shared evaluation."""

    submission_id: str
    track_id: str
    harness_id: str
    model_id: str
    display_name: str
    repetition: int
    snapshot_path: Path | None
    implementation_success: bool
    jobs: tuple[dict[str, Any], ...]
    artifacts: Mapping[str, str]
    implementation_cost_usd: float | None
    implementation_tokens: int
    implementation_duration_s: float
    extra_ineligible_reasons: tuple[str, ...]


@dataclass(frozen=True)
class _ExecutionContext:
    """Shared controls for concurrent submission and evaluator workers."""

    config: BenchConfig
    options: RunOptions
    run: _RunContext
    checkpoint: Callable[..., None]
    cancel_event: threading.Event
    agent_slots: threading.BoundedSemaphore
    evaluator_executor: ThreadPoolExecutor


def _emit_progress(options: RunOptions, event: str, **fields: object) -> None:
    """Emit best-effort structured progress without affecting benchmark results."""
    if options.progress is None:
        return
    try:
        options.progress(event, fields)
    except Exception:  # progress rendering must never fail a paid benchmark job
        pass


def _allocate_planned_id(make_id: Callable[[], str], field: str, allocated: set[str]) -> str:
    value = validate_identifier(make_id(), field=field)
    if value in allocated:
        raise ValueError(f"duplicate planned identifier: {value}")
    allocated.add(value)
    return value


def _install_termination_cancellation(cancel_event: threading.Event) -> Callable[[], None]:
    """Convert main-thread SIGTERM into coordinated agent cancellation."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGTERM"):
        return lambda: None
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(_signum: int, _frame: object) -> None:
        cancel_event.set()
        raise KeyboardInterrupt("received SIGTERM")

    signal.signal(signal.SIGTERM, terminate)

    def restore() -> None:
        signal.signal(signal.SIGTERM, previous)

    return restore


def new_run_id(now: datetime | None = None, nonce: str | None = None) -> str:
    """Safe lowercase run id; *now*/*nonce* enable determinism."""
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)
    stamp = now.strftime("%Y%m%dT%H%M%SZ").lower()
    token = (nonce if nonce is not None else secrets.token_hex(3)).lower()
    token = token[: max(1, 64 - len(stamp) - 1)]
    return validate_identifier(f"{stamp}-{token}", field="run_id")


def materialize_seed(config: BenchConfig, destination: Path) -> dict:
    """Copy seed (excluding reference/secrets/.git) and overlay reference_root."""
    dest = Path(destination)
    if os.path.lexists(dest):
        raise ValueError(f"materialize destination already exists: {dest}")
    if not dest.parent.is_dir():
        raise ValueError(f"materialize parent does not exist: {dest.parent}")
    atomic_snapshot(config.seed_root, dest, ignore_patterns=_SEED_IGNORE)
    ref_dest = dest / "reference"
    if os.path.lexists(ref_dest):
        raise ValueError(f"seed materialization left a reference path: {ref_dest}")
    # Reference-pack validation intentionally tolerates undeclared Finder
    # metadata; materialization must exclude the same ambient files so they do
    # not affect submissions or portable baseline artifacts.
    atomic_snapshot(
        config.reference_root,
        ref_dest,
        ignore_patterns=_AMBIENT_IGNORE,
    )
    return tree_manifest(dest)


def run_benchmark(
    config: BenchConfig,
    *,
    options: RunOptions = RunOptions(),
    id_factory: Callable[[], str] | None = None,
    pricing_data: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> Path:
    """Execute one benchmark after atomically reserving every paid work lane.

    A lane is one enabled implementation harness and track pair. OS-backed
    locks reject overlapping processes before run creation, pricing access, or
    provider invocation, while disjoint benchmark processes may run together.
    """
    recover_pending_layouts(config.run_root)
    lanes = (
        (harness.id, track.id)
        for harness in config.harnesses.values()
        if harness.enabled
        for track in config.tracks.values()
    )
    with acquire_lane_locks(config.root, lanes):
        return _run_benchmark_once(
            config,
            options=options,
            id_factory=id_factory,
            pricing_data=pricing_data,
            now=now,
        )


def _execute_plan(
    config: BenchConfig,
    options: RunOptions,
    ctx: _RunContext,
    now: datetime | None,
    *,
    prepare: Callable[[], Sequence[Any]],
    evaluator_attempts: Callable[[Any], Sequence[tuple[Any, str]]],
    submission_row: Callable[[Any], tuple[str, str, str, str, str, int]],
    progress_event: str,
    progress_count: str,
    work_pool_prefix: str,
    worker: Callable[
        [_ExecutionContext, Any], tuple[Attempt, list[dict[str, Any]], dict[str, str]]
    ],
) -> Path:
    """Execute either submission source through one checkpointed lifecycle."""
    jobs: list[dict[str, Any]] = []
    attempts: list[Attempt] = []
    artifacts: dict[str, str] = {}
    checkpoint = _checkpoint_writer(config, ctx, jobs, artifacts)
    cancel_event = threading.Event()
    agent_slots = threading.BoundedSemaphore(options.max_parallel_agents)
    restore_signal = _install_termination_cancellation(cancel_event)
    try:
        items = list(prepare())
        checkpoint(status="running")
        validate_planned_layout(
            run_root=config.run_root,
            run_id=ctx.run_id,
            submissions=[submission_row(item) for item in items],
            judges=[
                (
                    submission_row(item)[0],
                    attempt_id,
                    evaluator.harness,
                    evaluator.provider_family,
                    evaluator.model,
                )
                for item in items
                for evaluator, attempt_id in evaluator_attempts(item)
            ],
        )
        evaluator_count = sum(len(evaluator_attempts(item)) for item in items)
        _emit_progress(
            options,
            progress_event,
            **{progress_count: len(items), "evaluators": evaluator_count},
        )
        with (
            executor_pool(
                workers=worker_count(evaluator_count, options.max_parallel_agents),
                thread_name_prefix="basecamp-bench-evaluate",
            ) as evaluator_executor,
            executor_pool(
                workers=worker_count(len(items), options.max_parallel_agents),
                thread_name_prefix=work_pool_prefix,
            ) as work_executor,
        ):
            execution = _ExecutionContext(
                config, options, ctx, checkpoint, cancel_event, agent_slots, evaluator_executor
            )
            results = collect_indexed(
                items,
                executor=work_executor,
                cancel_event=cancel_event,
                submit=lambda executor, item: executor.submit(worker, execution, item),
            )
        for attempt, new_jobs, new_artifacts in results:
            attempts.append(attempt)
            checkpoint(new_jobs, new_artifacts)
        checkpoint()
        return _finalize_run(config, ctx, jobs, attempts, artifacts, now, options)
    except (Exception, KeyboardInterrupt) as exc:
        _emit_progress(options, "run.failed", error=type(exc).__name__)
        _fail_run_without_masking(config, ctx.run_dir, checkpoint, exc)
        raise
    finally:
        restore_signal()


def _run_benchmark_once(
    config: BenchConfig,
    *,
    options: RunOptions = RunOptions(),
    id_factory: Callable[[], str] | None = None,
    pricing_data: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> Path:
    """Execute a reserved benchmark and return its newly created run directory.

    Implementation tasks run concurrently. Evaluators for a successful,
    immutable snapshot may overlap other implementations, while one shared
    semaphore enforces ``options.max_parallel_agents`` across both pools.
    Checkpoints are written throughout the run; interruption or an unexpected
    worker failure records a failed manifest when possible and is re-raised.
    """
    ctx = _open_run(config, options, id_factory, pricing_data, now)

    def prepare() -> list[_RunTask]:
        harnesses = [
            config.harnesses[h] for h in sorted(config.harnesses) if config.harnesses[h].enabled
        ]
        evaluators = [e for e in config.evaluators if e.enabled]
        tracks = [config.tracks[t] for t in sorted(config.tracks)]
        tasks: list[_RunTask] = []
        allocated_ids = {ctx.run_id}
        for harness in harnesses:
            for track in tracks:
                for rep in range(1, config.repetitions + 1):
                    submission_id = _allocate_planned_id(
                        ctx.make_id, "submission_id", allocated_ids
                    )
                    evaluator_attempts = tuple(
                        (
                            evaluator,
                            _allocate_planned_id(ctx.make_id, "eval_attempt_id", allocated_ids),
                        )
                        for evaluator in evaluators
                    )
                    tasks.append(
                        _RunTask(
                            harness=harness,
                            track=track,
                            repetition=rep,
                            submission_id=submission_id,
                            evaluator_attempts=evaluator_attempts,
                        )
                    )
        return tasks

    return _execute_plan(
        config,
        options,
        ctx,
        now,
        prepare=prepare,
        evaluator_attempts=lambda task: task.evaluator_attempts,
        submission_row=lambda task: (
            task.submission_id,
            task.track.id,
            task.harness.id,
            task.harness.provider_family,
            task.harness.model,
            task.repetition,
        ),
        progress_event="run.planned",
        progress_count="implementations",
        work_pool_prefix="basecamp-bench-implement",
        worker=_run_repetition,
    )


def reevaluate_run(
    config: BenchConfig,
    prior_run_dir: Path,
    *,
    options: RunOptions = RunOptions(),
    id_factory: Callable[[], str] | None = None,
    pricing_data: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    submission_ids: Sequence[str] | None = None,
) -> Path:
    """Re-evaluate verified snapshots and return a separate new run directory.

    The prior run and every declared snapshot are verified before reuse. The
    selected copied snapshots are evaluated with the current contracts and
    evaluator configuration; the source run remains unchanged. When
    ``submission_ids`` is absent, every reusable snapshot is selected.
    Evaluator failures use the same checkpoint-and-reraise behavior as a
    normal benchmark run.
    """
    requested_prior = Path(prior_run_dir)
    recovered = recover_pending_layouts(config.run_root)
    if not requested_prior.exists():
        for candidate in recovered:
            try:
                layout = json.loads((candidate / "layout.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(layout, dict) and layout.get("run_id") == requested_prior.name:
                requested_prior = candidate
                break
    prior = _require_prior_run_dir(config, requested_prior)
    from basecamp_bench.manifest import verify_run  # publication/reeval boundary

    errs = verify_run(prior)
    if errs:
        raise ValueError(f"prior run verification failed: {errs[0]}")
    prior_man = _load_prior_manifest(prior)
    reusable = _prior_reusable_submissions(prior, prior_man)
    if not reusable:
        raise ValueError("prior run has no reusable verified snapshots")
    reusable = _select_reusable_submissions(reusable, submission_ids)
    lanes = {(str(item["attempt"]["harness"]), str(item["attempt"]["track"])) for item in reusable}
    with acquire_lane_locks(config.root, lanes):
        return _reevaluate_reserved(
            config,
            prior,
            prior_man,
            reusable,
            options=options,
            id_factory=id_factory,
            pricing_data=pricing_data,
            now=now,
        )


def _reevaluate_reserved(
    config: BenchConfig,
    prior: Path,
    prior_man: Mapping[str, Any],
    reusable: Sequence[dict[str, Any]],
    *,
    options: RunOptions,
    id_factory: Callable[[], str] | None,
    pricing_data: Mapping[str, Any] | None,
    now: datetime | None,
) -> Path:
    """Evaluate prevalidated snapshots while their paid-work lanes are reserved."""
    ctx = _open_run(config, options, id_factory, pricing_data, now)

    def prepare() -> list[tuple[dict[str, Any], tuple[tuple[Any, str], ...]]]:
        _require_reevaluation_inputs_match(
            prior_man,
            ctx.input_hashes,
            tracks={item["attempt"]["track"] for item in reusable},
            prior_attempts=[item["attempt"] for item in reusable],
            publication=config.mode == "publication",
        )
        if ctx.run_id == prior_man["run_id"]:
            raise ValueError("re-evaluation run_id collides with prior run_id")
        for item in reusable:
            ctx.input_hashes[f"reuse_snapshot_tree:{item['submission_id']}"] = item["tree_hash"]
        ctx.input_hashes[f"prior_run:{prior_man['run_id']}"] = sha256_file(
            prior / "run-manifest.json"
        )
        evaluators = [e for e in config.evaluators if e.enabled]
        allocated_ids = {ctx.run_id, *(str(item["submission_id"]) for item in reusable)}
        planned = [
            (
                item,
                tuple(
                    (
                        evaluator,
                        _allocate_planned_id(ctx.make_id, "eval_attempt_id", allocated_ids),
                    )
                    for evaluator in evaluators
                ),
            )
            for item in reusable
        ]
        return planned

    def run_verified(
        execution: _ExecutionContext,
        planned: tuple[dict[str, Any], tuple[tuple[Any, str], ...]],
    ) -> tuple[Attempt, list[dict[str, Any]], dict[str, str]]:
        return _run_verified_submission(execution, str(prior_man["run_id"]), *planned)

    return _execute_plan(
        config,
        options,
        ctx,
        now,
        prepare=prepare,
        evaluator_attempts=lambda planned: planned[1],
        submission_row=lambda planned: (
            str(planned[0]["submission_id"]),
            str(planned[0]["attempt"]["track"]),
            str(planned[0]["attempt"]["harness"]),
            config.harnesses[str(planned[0]["attempt"]["harness"])].provider_family,
            str(planned[0]["attempt"]["model_id"]),
            int(planned[0]["attempt"]["repetition"]),
        ),
        progress_event="reevaluate.planned",
        progress_count="submissions",
        work_pool_prefix="basecamp-bench-reevaluate",
        worker=run_verified,
    )


def _open_run(
    config: BenchConfig,
    options: RunOptions,
    id_factory: Callable[[], str] | None,
    pricing_data: Mapping[str, Any] | None,
    now: datetime | None,
) -> _RunContext:
    """Create and validate the initial run state used by later phases.

    This allocates the run directory, records tooling, applies startup gates,
    resolves pricing and contracts, hashes inputs, and writes the initial
    ``planned`` checkpoint. The typed context remains authoritative for the
    rest of the run while exposing the few collections that evolve in place.
    """

    started = _utc_now(now)
    if id_factory is None:
        run_id = new_run_id(now=now)
        make_id: Callable[[], str] = lambda: validate_identifier(  # noqa: E731
            f"id-{secrets.token_hex(4)}",
            field="id",
        )
    else:
        make_id = id_factory
        run_id = validate_identifier(make_id(), field="run_id")
    runner_git = git_provenance(config.root)
    config.run_root.mkdir(parents=True, exist_ok=True)
    run_dir = create_unique_directory(config.run_root / run_id)
    for name in (
        "workspaces",
        "snapshots",
        "evaluations",
        "attempts",
        "leaderboards",
        "prompts",
        "logs",
        "private",
    ):
        (run_dir / name).mkdir()
    tooling, tooling_limitations = _collect_tooling(config, run_dir)
    pack = load_reference_pack(
        config.reference_manifest,
        config.reference_root,
        publication=config.mode == "publication",
    )
    gate_errors = _startup_gates(config, options)
    hard = [e for e in gate_errors if e.startswith("publication:")]
    if hard:
        _write_manifest(
            config,
            run_dir,
            run_id,
            started,
            "ineligible",
            {"error": "; ".join(hard)},
            [],
            {},
            {},
            tooling,
            finished=_utc_now(now),
            runner_git=runner_git,
        )
        raise ValueError("; ".join(hard))
    unsafe = [e for e in gate_errors if e.startswith("unsafe:")]
    if unsafe:
        raise ValueError("; ".join(unsafe))
    pricing_payload, pricing_prov = _load_pricing(config, options, pricing_data)
    retrieved_at = pricing_prov.get("retrieved_at")
    pricing_retrieved_at = retrieved_at if isinstance(retrieved_at, str) else None
    ineligible: list[str] = list(gate_errors)
    if config.mode == "publication":
        ineligible.extend(tooling_limitations)
    pricing_ok, pricing_reasons = _pricing_coverage(
        config,
        pricing_payload,
        pricing_retrieved_at,
        pricing_prov,
    )
    if not pricing_ok:
        ineligible.extend(pricing_reasons)
    contracts: dict[str, EvaluationContract] = {}
    contract_hashes: dict[str, str] = {}
    for tid, track in config.tracks.items():
        contracts[tid] = load_contract(track.contract_file)
        contract_hashes[tid] = contract_sha256(track.contract_file)
    input_hashes = _input_hashes(config, pack)
    if pricing_payload is not None:
        input_hashes["pricing_snapshot"] = _pricing_digest(pricing_payload)
    ctx = _RunContext(
        run_dir=run_dir,
        run_id=run_id,
        make_id=make_id,
        started=started,
        runner_git=runner_git,
        pricing_payload=pricing_payload,
        pricing_prov=pricing_prov,
        pricing_retrieved_at=pricing_retrieved_at,
        pricing_ok=pricing_ok,
        pricing_reasons=pricing_reasons,
        ineligible=ineligible,
        contracts=contracts,
        contract_hashes=contract_hashes,
        input_hashes=input_hashes,
        comparison_provenance=_comparison_provenance(config, input_hashes),
        tooling=tooling,
    )
    _checkpoint_run(config, ctx, "planned", [], {}, finished=None)
    return ctx


def _finalize_run(
    config: BenchConfig,
    ctx: _RunContext,
    job_records: list[dict[str, Any]],
    attempts: list[Attempt],
    artifact_hashes: dict[str, str],
    now: datetime | None,
    options: RunOptions,
) -> Path:
    """Write aggregate artifacts and the terminal manifest, then return the run path."""

    run_dir: Path = ctx.run_dir
    _emit_progress(options, "aggregate.started", attempts=len(attempts))
    generated_at = _utc_now(now)
    dimension_profiles = {
        tid: [
            {"id": dim.id, "label": dim.label, "weight": dim.weight} for dim in contract.dimensions
        ]
        for tid, contract in ctx.contracts.items()
    }
    ledgers = build_attempt_ledgers(
        attempts,
        mode=config.mode,
        generated_at=generated_at,
        comparison_provenance=ctx.comparison_provenance,
        dimension_profiles=dimension_profiles,
    )
    written = write_attempt_ledgers(run_dir / "leaderboards", ledgers)
    _emit_progress(options, "aggregate.finished", leaderboards=len(ledgers))
    lb_json = [p for p in written if p.suffix == ".json"]
    for path in written:
        rel = portable_path(path, run_dir)
        if rel != "<external>":
            artifact_hashes[rel] = sha256_file(path)
    report_path = run_dir / "report.html"
    _emit_progress(options, "report.started", output="report.html")
    write_report(lb_json, report_path)
    artifact_hashes["report.html"] = sha256_file(report_path)
    _emit_progress(options, "report.finished", output="report.html")
    for attempt in attempts:
        ap = run_dir / "attempts" / f"{attempt.submission_id}.json"
        if ap.is_file():
            artifact_hashes[f"attempts/{attempt.submission_id}.json"] = sha256_file(ap)
    # Publication final status uses the same in-memory aggregate eligibility as
    # pre-ledger finalization: any derived entry with eligible=False forces
    # status ineligible. Local mode never consults entry eligibility.
    roots = (
        aggregate_attempts(
            attempts,
            mode=config.mode,
            generated_at=generated_at,
            comparison_provenance=ctx.comparison_provenance,
            dimension_profiles=dimension_profiles,
        )
        if config.mode == "publication"
        else ()
    )
    final_status = (
        "ineligible"
        if config.mode == "publication"
        and any(
            not e.get("eligible")
            for root in roots
            for e in cast(Sequence[Mapping[str, Any]], root.get("entries", []))
        )
        else _final_status(config, ctx.ineligible, attempts)
    )
    final_run_dir = finalize_readable_layout(
        run_dir=run_dir,
        run_id=ctx.run_id,
        attempts=attempts,
        jobs=job_records,
        provider_by_harness={spec.id: spec.provider_family for spec in config.harnesses.values()},
        evaluator_specs={
            evaluator.id: (
                evaluator.harness,
                evaluator.provider_family,
                evaluator.model,
            )
            for evaluator in config.evaluators
            if evaluator.enabled
        },
        artifacts=artifact_hashes,
        manifest_factory=lambda rewritten: _manifest_payload(
            config,
            ctx.run_id,
            ctx.started,
            final_status,
            _public_pricing(ctx.pricing_prov, ctx.pricing_ok, ctx.pricing_reasons),
            job_records,
            rewritten,
            dict(ctx.input_hashes),
            list(ctx.tooling),
            finished=_utc_now(now),
            runner_git=ctx.runner_git,
        ),
    )
    _emit_progress(options, "run.finished", run_id=ctx.run_id, status=final_status)
    return final_run_dir


def _require_prior_run_dir(config: BenchConfig, prior_run_dir: Path) -> Path:
    prior = Path(prior_run_dir)
    if prior.is_symlink():
        raise ValueError("prior run directory must not be a symlink")
    if not prior.exists() or not prior.is_dir():
        raise ValueError("prior run directory does not exist or is not a directory")
    try:
        run_root = config.run_root.resolve(strict=True)
        resolved = prior.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"prior run path is not resolvable: {exc}") from exc
    if resolved == run_root or resolved.parent != run_root:
        raise ValueError("prior run directory must be a direct child of config.run_root")
    if prior.name != resolved.name:
        raise ValueError("prior run directory basename is unsafe")
    return resolved


def _load_prior_manifest(prior: Path) -> dict[str, Any]:
    try:
        data = json.loads((prior / "run-manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"prior run-manifest.json is unreadable: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("run"), dict):
        raise ValueError("prior run-manifest.json shape is invalid")
    run_id = data["run"].get("id")
    if not isinstance(run_id, str):
        raise ValueError("prior run id is missing")
    validate_identifier(run_id, field="prior_run_id")
    config = data.get("config")
    if not isinstance(config, dict):
        raise ValueError("prior run config is missing")
    harnesses = config.get("harnesses")
    jobs, artifacts, inputs = data.get("jobs"), data.get("artifacts"), data.get("inputs")
    if not isinstance(config.get("tracks"), dict) or not isinstance(harnesses, dict):
        raise ValueError("prior run config path identity is invalid")
    if (
        not isinstance(jobs, list)
        or not isinstance(artifacts, dict)
        or not isinstance(inputs, dict)
    ):
        raise ValueError("prior run-manifest.json jobs/artifacts shape is invalid")
    implementation_jobs = [
        job for job in jobs if isinstance(job, dict) and job.get("kind") == "implement"
    ]
    used_harnesses = {job.get("harness") for job in implementation_jobs}
    used_tracks = {job.get("track") for job in implementation_jobs}
    if not implementation_jobs or any(
        not isinstance(value, str) or not value for value in used_harnesses | used_tracks
    ):
        raise ValueError("prior run implementation path identity is invalid")
    contestants: list[tuple[str, str]] = []
    provider_by_harness: dict[str, str] = {}
    for harness_id in sorted(cast(set[str], used_harnesses)):
        spec = harnesses.get(harness_id)
        if not isinstance(spec, dict):
            raise ValueError(f"prior run harness config is missing: {harness_id}")
        provider, model = spec.get("provider_family"), spec.get("model")
        if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
            raise ValueError("prior run config contestant identity is invalid")
        contestants.append((provider, model))
        provider_by_harness[harness_id] = provider
    expected_name = run_path_name(
        run_id,
        tracks=cast(set[str], used_tracks),
        contestants=contestants,
    )
    if prior.name != expected_name:
        raise ValueError("prior run directory name does not match manifest identity")
    prior_status = data.get("status")
    if prior_status not in {"complete", "ineligible", "failed"}:
        raise ValueError(f"prior run status is not reusable: {data.get('status')!r}")
    prior_mode = data["run"].get("mode")
    if prior_mode not in {"local", "publication"}:
        raise ValueError("prior run mode is invalid")
    return {
        "run_id": run_id,
        "mode": prior_mode,
        "status": prior_status,
        "jobs": jobs,
        "artifacts": artifacts,
        "inputs": inputs,
        "provider_by_harness": provider_by_harness,
    }


def _require_reevaluation_inputs_match(
    prior_manifest: Mapping[str, Any],
    current_inputs: Mapping[str, str],
    *,
    tracks: set[str],
    prior_attempts: Sequence[Mapping[str, Any]],
    publication: bool,
) -> None:
    if publication and prior_manifest.get("mode") != "publication":
        raise ValueError("publication re-evaluation requires a publication prior run")
    if publication and prior_manifest.get("status") != "complete":
        raise ValueError("publication re-evaluation requires a completed eligible prior run")
    if publication:
        for attempt in prior_attempts:
            reasons = attempt.get("ineligible_reasons")
            if not isinstance(reasons, list) or reasons:
                raise ValueError(
                    "publication re-evaluation requires prior attempts without ineligibility reasons"
                )
    prior_inputs = prior_manifest["inputs"]
    for key in (
        "seed_root",
        "reference_manifest",
        "reference_root",
        "reference_pack.manifest_sha256",
        "reference_pack.tree_sha256",
    ):
        prior_value = prior_inputs.get(key)
        current_value = current_inputs.get(key)
        if not isinstance(prior_value, str) or prior_value != current_value:
            raise ValueError(f"re-evaluation input mismatch: {key}")
    for track in sorted(tracks):
        key = f"prompt:{track}"
        prior_value = prior_inputs.get(key)
        current_value = current_inputs.get(key)
        if not isinstance(prior_value, str) or prior_value != current_value:
            raise ValueError(f"re-evaluation input mismatch: {key}")


def _usage_token_total(usage: Any) -> int:
    if not isinstance(usage, dict):
        raise ValueError("prior implementation usage provenance is missing")
    total = 0
    for key in ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens"):
        value = usage.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid implementation usage field {key!r}")
        total += value
    return total


def _tree_hash(file_hashes: Mapping[str, str]) -> str:
    payload = json.dumps(
        {k: file_hashes[k] for k in sorted(file_hashes)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _prior_reusable_submissions(prior: Path, prior_man: Mapping[str, Any]) -> list[dict[str, Any]]:
    artifacts, jobs, prior_run_id = prior_man["artifacts"], prior_man["jobs"], prior_man["run_id"]
    attempt_dir = prior / "attempts"
    if attempt_dir.is_symlink() or not attempt_dir.is_dir():
        raise ValueError("prior attempts path must be a real directory")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(attempt_dir.glob("*.json"), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"prior attempt must be a regular file: {path.name}")
        try:
            attempt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"prior attempt JSON is unreadable: {exc}") from exc
        if not isinstance(attempt, dict):
            raise ValueError(f"prior attempt JSON must be an object: {path.name}")
        sid = validate_identifier(attempt.get("submission_id"), field="submission_id")
        if sid in seen:
            raise ValueError(f"duplicate prior submission ID: {sid}")
        seen.add(sid)
        if attempt.get("run_id") != prior_run_id:
            raise ValueError("prior attempt identity mismatch")
        for field in (
            "track",
            "harness",
            "model_id",
            "display_name",
            "contract_version",
            "contract_sha256",
        ):
            if not isinstance(attempt.get(field), str) or not attempt[field]:
                raise ValueError(f"prior attempt missing {field}")
        if (
            not isinstance(attempt.get("repetition"), int)
            or isinstance(attempt.get("repetition"), bool)
            or attempt["repetition"] < 1
        ):
            raise ValueError("prior attempt repetition is invalid")
        readable = submission_path_name(
            track=attempt["track"],
            harness=attempt["harness"],
            provider=prior_man["provider_by_harness"][attempt["harness"]],
            model=attempt["model_id"],
            repetition=attempt["repetition"],
            submission_id=sid,
        )
        if path.name == f"{readable}.json":
            stored_name = readable
        elif path.name == f"{sid}.json":
            stored_name = sid
        else:
            raise ValueError(f"prior attempt filename does not match identity: {path.name}")
        rel = f"attempts/{stored_name}.json"
        digest = artifacts.get(rel)
        if not isinstance(digest, str):
            raise ValueError(f"prior attempt artifact is undeclared: {rel}")
        if sha256_file(path) != digest:
            raise ValueError(f"prior attempt is hash-mismatched: {rel}")
        if attempt.get("implementation_success") is not True:
            continue
        prefix, declared = f"snapshots/{stored_name}/", {}
        for a_rel, a_dig in artifacts.items():
            if not isinstance(a_rel, str) or not a_rel.startswith(prefix):
                continue
            if not isinstance(a_dig, str) or len(a_dig) != 64:
                raise ValueError(f"invalid snapshot artifact digest: {a_rel}")
            file_rel = a_rel[len(prefix) :]
            if (
                not file_rel
                or "\\" in file_rel
                or file_rel.startswith("/")
                or ".." in file_rel.split("/")
            ):
                raise ValueError(f"unsafe snapshot artifact path: {a_rel}")
            declared[file_rel] = a_dig
        if not declared:
            raise ValueError(f"empty or undeclared snapshot for submission {sid}")
        snap = prior / "snapshots" / stored_name
        if snap.is_symlink() or not snap.is_dir():
            raise ValueError(f"prior snapshot is not a real directory: {sid}")
        try:
            actual = tree_manifest(snap)
        except ValueError as exc:
            raise ValueError(f"prior snapshot tree is unsafe: {exc}") from exc
        if set(actual) != set(declared):
            extra, missing = (
                sorted(set(actual) - set(declared)),
                sorted(set(declared) - set(actual)),
            )
            raise ValueError(
                f"undeclared snapshot file for {sid}: {extra[0]}"
                if extra
                else f"missing declared snapshot file for {sid}: {missing[0]}"
            )
        for file_rel, dig in declared.items():
            if actual[file_rel] != dig:
                raise ValueError(f"snapshot hash mismatch for {sid}: {file_rel}")
        matches = [
            j
            for j in jobs
            if isinstance(j, dict)
            and j.get("kind") == "implement"
            and j.get("submission_id") == sid
        ]
        if len(matches) != 1:
            raise ValueError(f"prior implementation job provenance missing for {sid}")
        impl = matches[0]
        if (
            impl.get("track") != attempt["track"]
            or impl.get("harness") != attempt["harness"]
            or impl.get("repetition") != attempt["repetition"]
            or impl.get("returncode") != 0
            or impl.get("timed_out")
            or impl.get("interrupted")
            or impl.get("error")
            or not is_finite_number(impl.get("duration_s"))
            or float(impl["duration_s"]) < 0.0
        ):
            raise ValueError("prior implementation job provenance is incoherent")
        job_cost, att_cost = impl.get("cost_usd"), attempt.get("implementation_cost_usd")
        if job_cost is not None and (not is_finite_number(job_cost) or float(job_cost) < 0.0):
            raise ValueError("prior implementation job cost is invalid")
        if (att_cost is None) != (job_cost is None):
            raise ValueError("incoherent implementation cost provenance")
        if att_cost is not None:
            assert job_cost is not None
            if float(att_cost) != float(job_cost):
                raise ValueError("incoherent implementation cost provenance")
        _usage_token_total(impl.get("usage"))
        out.append(
            {
                "submission_id": sid,
                "attempt": attempt,
                "snapshot_path": snap,
                "declared": declared,
                "tree_hash": _tree_hash(declared),
                "impl_job": impl,
            }
        )
    return out


def _select_reusable_submissions(
    reusable: Sequence[dict[str, Any]], submission_ids: Sequence[str] | None
) -> list[dict[str, Any]]:
    """Return requested reusable submissions in explicit selection order."""
    if submission_ids is None:
        return list(reusable)
    selected = [validate_identifier(value, field="submission_id") for value in submission_ids]
    if not selected:
        raise ValueError("submission selection must not be empty")
    if len(selected) != len(set(selected)):
        raise ValueError("submission selection contains duplicate IDs")
    available = {str(item["submission_id"]): item for item in reusable}
    missing = [value for value in selected if value not in available]
    if missing:
        raise ValueError(f"selected submission is not reusable: {missing[0]}")
    return [available[value] for value in selected]


def _eval_pass(
    execution: _ExecutionContext,
    sid: str,
    snapshot: Path,
    track: Any,
    contract: EvaluationContract,
    contract_hash: str,
    evaluator_attempts: Sequence[tuple[Any, str]],
    repetition: int,
) -> tuple[
    list[dict[str, Any]],
    list[ValidatedJudgeScores],
    dict[str, str],
    list[str],
    float,
    bool,
    int,
    float,
]:
    """Run all evaluators for one snapshot and normalize their outcomes.

    Results are collected concurrently and returned in configured evaluator
    order. The tuple contains job records, valid judge results, artifact
    hashes, valid evaluator model IDs, known cost total, whether every cost was
    known, token total, and wall-clock evaluator duration. Any raised worker
    error is checkpointed, triggers shared cancellation, and is re-raised after
    already-completed sibling outcomes have been recorded.
    """

    jobs: list[dict[str, Any]] = []
    artifacts: dict[str, str] = {}
    valid_results: list[ValidatedJudgeScores] = []
    valid_eval_ids: list[str] = []
    eval_cost_total, eval_cost_known, eval_tokens, eval_duration = 0.0, True, 0, 0.0

    results: list[dict[str, Any] | None] = [None] * len(evaluator_attempts)
    futures: dict[Future[Any], tuple[int, Any, str]] = {}
    first_error: BaseException | None = None
    for index, (evaluator, eval_attempt_id) in enumerate(evaluator_attempts):
        futures[
            execution.evaluator_executor.submit(
                _run_evaluator,
                execution,
                sid,
                snapshot,
                track,
                contract,
                contract_hash,
                evaluator,
                eval_attempt_id,
                repetition,
            )
        ] = (index, evaluator, eval_attempt_id)
    for future in as_completed(futures):
        index, evaluator, eval_attempt_id = futures[future]
        try:
            ev = future.result()
        except BaseException as exc:
            execution.cancel_event.set()
            failure = _raised_job_record(
                config=execution.config,
                run_dir=execution.run.run_dir,
                job_id=f"evaluate-{eval_attempt_id}",
                kind="evaluate",
                harness_id=evaluator.harness,
                track=track.id,
                repetition=repetition,
                submission_id=sid,
                exc=exc,
                extra={
                    "evaluator_id": evaluator.id,
                    "eval_attempt_id": eval_attempt_id,
                    "valid": False,
                    "invalid_reasons": ["evaluator_exception"],
                },
            )
            jobs.append(failure)
            execution.checkpoint((failure,), artifacts)
            if first_error is None:
                first_error = exc
            continue
        results[index] = ev
        execution.checkpoint((ev["job_record"],), ev["artifacts"])
    if first_error is not None:
        raise first_error
    for (evaluator, _), ev in zip(evaluator_attempts, results, strict=True):
        if ev is None:
            raise RuntimeError("evaluator scheduler returned an incomplete result set")
        jobs.append(ev["job_record"])
        artifacts.update(ev["artifacts"])
        eval_tokens += ev["tokens"]
        eval_duration = max(eval_duration, ev["duration_s"])
        if ev["cost_usd"] is None:
            eval_cost_known = False
        else:
            eval_cost_total += float(ev["cost_usd"])
        if ev["valid_result"] is not None:
            valid_results.append(ev["valid_result"])
            valid_eval_ids.append(_model_identifier(evaluator.model))
    return (
        jobs,
        valid_results,
        artifacts,
        valid_eval_ids,
        eval_cost_total,
        eval_cost_known,
        eval_tokens,
        eval_duration,
    )


def _evaluate_and_persist_attempt(
    execution: _ExecutionContext,
    prepared: _PreparedSubmission,
    evaluator_attempts: Sequence[tuple[Any, str]],
) -> tuple[Attempt, list[dict[str, Any]], dict[str, str]]:
    """Evaluate one prepared submission, write its Attempt, and checkpoint.

    Shared by build and verified-snapshot sources so scoring, eligibility,
    attempt codec, and evaluator policy remain single.
    """

    jobs = list(prepared.jobs)
    artifacts = dict(prepared.artifacts)
    track_id = prepared.track_id
    track = execution.config.tracks[track_id]
    contract = execution.run.contracts[track_id]
    contract_hash = execution.run.contract_hashes[track_id]
    valid_results: list[ValidatedJudgeScores] = []
    valid_eval_ids: list[str] = []
    eval_cost_total, eval_cost_known, eval_tokens, eval_duration = 0.0, True, 0, 0.0
    if prepared.implementation_success:
        if prepared.snapshot_path is None:
            raise RuntimeError("successful implementation missing snapshot path")
        (
            ej,
            valid_results,
            ea,
            valid_eval_ids,
            eval_cost_total,
            eval_cost_known,
            eval_tokens,
            eval_duration,
        ) = _eval_pass(
            execution,
            prepared.submission_id,
            prepared.snapshot_path,
            track,
            contract,
            contract_hash,
            evaluator_attempts,
            prepared.repetition,
        )
        jobs.extend(ej)
        artifacts.update(ea)
    min_evals = _MIN_PUB_EVALS if execution.config.mode == "publication" else _MIN_LOCAL_EVALS
    evaluation_success = prepared.implementation_success and len(valid_results) >= min_evals
    reasons = [*execution.run.ineligible, *prepared.extra_ineligible_reasons]
    if prepared.implementation_success and not evaluation_success:
        reasons.append("insufficient_valid_evaluators")
    if evaluation_success:
        agg = aggregate_judges(list(valid_results), contract)
        score = float(agg["overall"])
        dimensions = {d: float(s["median"]) for d, s in agg["dimensions"].items()}
        overalls = [float(j["overall"]) for j in agg["judges"]]
        judge_spread = 0.0 if len(overalls) <= 1 else float(statistics.pstdev(overalls))
    else:
        score, dimensions, judge_spread = None, {}, None
    eval_cost = (
        (eval_cost_total if eval_cost_known else None) if prepared.implementation_success else None
    )
    attempt = Attempt(
        run_id=execution.run.run_id,
        submission_id=prepared.submission_id,
        repetition=prepared.repetition,
        track=track_id,
        contract_version=contract.contract_version,
        contract_sha256=contract_hash,
        harness=prepared.harness_id,
        model_id=prepared.model_id,
        display_name=prepared.display_name,
        implementation_success=prepared.implementation_success,
        evaluation_success=evaluation_success,
        score=score,
        dimensions=dimensions,
        judge_spread=judge_spread,
        implementation_cost_usd=prepared.implementation_cost_usd,
        evaluation_cost_usd=eval_cost,
        tokens=prepared.implementation_tokens + eval_tokens,
        duration_s=prepared.implementation_duration_s + eval_duration,
        evaluator_ids=tuple(valid_eval_ids),
        ineligible_reasons=tuple(reasons),
    )
    attempt_path = execution.run.run_dir / "attempts" / f"{prepared.submission_id}.json"
    atomic_write_json(attempt_path, attempt_to_raw(attempt))
    artifacts[f"attempts/{prepared.submission_id}.json"] = sha256_file(attempt_path)
    execution.checkpoint(jobs, artifacts)
    return attempt, jobs, artifacts


def _run_verified_submission(
    execution: _ExecutionContext,
    prior_run_id: str,
    item: Mapping[str, Any],
    evaluator_attempts: Sequence[tuple[Any, str]],
) -> tuple[Attempt, list[dict[str, Any]], dict[str, str]]:
    """Copy one prior snapshot (preparation only), then share evaluation/persistence."""

    sid, prior_att, impl_job = item["submission_id"], item["attempt"], item["impl_job"]
    track_id = prior_att["track"]
    if track_id not in execution.config.tracks or track_id not in execution.run.contracts:
        raise ValueError(f"prior track is missing from current config: {track_id}")
    contract = execution.run.contracts[track_id]
    contract_hash = execution.run.contract_hashes[track_id]
    impl_tokens = _usage_token_total(impl_job.get("usage"))
    impl_duration = float(impl_job["duration_s"])
    impl_cost = float(impl_job["cost_usd"]) if impl_job.get("cost_usd") is not None else None
    new_snap = execution.run.run_dir / "snapshots" / sid
    snap_manifest = atomic_snapshot(item["snapshot_path"], new_snap)
    if snap_manifest != item["declared"]:
        raise ValueError(f"copied snapshot does not match prior declared hashes: {sid}")
    artifacts = {f"snapshots/{sid}/{rel}": digest for rel, digest in snap_manifest.items()}
    jobs: tuple[dict[str, Any], ...] = (
        {
            "id": f"implement-{sid}",
            "kind": "implement",
            "harness": prior_att["harness"],
            "track": track_id,
            "repetition": int(prior_att["repetition"]),
            "submission_id": sid,
            "command_preview": (
                f"reuse prior_run={prior_run_id} snapshot_tree_sha256={item['tree_hash']} "
                f"prior_contract_version={prior_att['contract_version']} "
                f"prior_contract_sha256={prior_att['contract_sha256']} "
                f"current_contract_version={contract.contract_version} "
                f"current_contract_sha256={contract_hash}"
            ),
            "returncode": 0,
            "duration_s": impl_duration,
            "timed_out": False,
            "interrupted": False,
            "error": None,
            "cost_usd": impl_cost,
            "reported_cost_usd": impl_job.get("reported_cost_usd"),
            "usage": impl_job.get("usage"),
        },
    )
    execution.checkpoint(jobs, artifacts)
    prepared = _PreparedSubmission(
        submission_id=sid,
        track_id=track_id,
        harness_id=prior_att["harness"],
        model_id=validate_identifier(prior_att["model_id"], field="model_id"),
        display_name=prior_att["display_name"],
        repetition=int(prior_att["repetition"]),
        snapshot_path=new_snap,
        implementation_success=True,
        jobs=jobs,
        artifacts=artifacts,
        implementation_cost_usd=impl_cost,
        implementation_tokens=impl_tokens,
        implementation_duration_s=impl_duration,
        extra_ineligible_reasons=(),
    )
    return _evaluate_and_persist_attempt(
        execution,
        prepared,
        evaluator_attempts,
    )


def _utc_now(now: datetime | None = None) -> str:
    if now is None:
        dt = datetime.now(UTC)
    elif now.tzinfo is None:
        dt = now.replace(tzinfo=UTC)
    else:
        dt = now.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sandbox(config: BenchConfig) -> str:
    return "danger-full-access" if config.full_access else "workspace-write"


def _startup_gates(config: BenchConfig, options: RunOptions) -> list[str]:
    errors: list[str] = []
    if config.full_access and not (
        options.allow_unsafe_host_execution or options.confirmed_isolated_environment
    ):
        errors.append(
            "unsafe: full_access requires allow_unsafe_host_execution "
            "or confirmed_isolated_environment"
        )
    if config.mode == "publication":
        if config.repetitions < _MIN_PUB_REPS:
            errors.append(
                f"publication: repetitions must be >= {_MIN_PUB_REPS}, got {config.repetitions}"
            )
        if not options.confirmed_isolated_environment:
            errors.append("publication: confirmed_isolated_environment must be true")
    return errors


def _load_pricing(
    config: BenchConfig,
    options: RunOptions,
    injected: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if injected is not None:
        return dict(injected), {
            "source": "injected",
            "url": None,
            "cache_path": None,
            "retrieved_at": _utc_now(),
            "stale": False,
            "error": None,
        }
    return load_pricing_snapshot(
        config.root / ".pricing-cache.json",
        _PRICING_URL,
        _PRICING_MAX_AGE_S,
        allow_network=options.allow_network_pricing,
    )


def _pricing_coverage(
    config: BenchConfig,
    pricing_data: Mapping[str, Any] | None,
    retrieved_at: str | None,
    provenance: Mapping[str, Any],
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if provenance.get("stale"):
        reasons.append("pricing_stale")
    if provenance.get("error") and pricing_data is None:
        reasons.append("pricing_unavailable")
    models = {h.model for h in config.harnesses.values() if h.enabled}
    models.update(ev.model for ev in config.evaluators if ev.enabled)
    for model in sorted(models):
        lookup = find_exact_rates(
            model,
            pricing_data,
            config.pricing_overrides,
            retrieved_at=retrieved_at,
        )
        if lookup.rates is None:
            reasons.append(f"pricing_missing:{normalize_model_id(model)}")
        elif lookup.stale:
            reasons.append(f"pricing_stale:{normalize_model_id(model)}")
    return not reasons, reasons


def _input_hashes(config: BenchConfig, pack: Any) -> dict[str, str]:
    paths: dict[str, Path] = {
        "seed_root": config.seed_root,
        "reference_manifest": config.reference_manifest,
        "reference_root": config.reference_root,
    }
    for tid, track in config.tracks.items():
        paths[f"prompt:{tid}"] = track.prompt_file
        paths[f"rubric:{tid}"] = track.rubric_file
        paths[f"contract:{tid}"] = track.contract_file
    schemas = config.root / "schemas"
    if schemas.is_dir():
        for path in sorted(schemas.glob("*.json")):
            paths[f"schema:{path.name}"] = path
    hashed = hash_inputs(paths)
    hashed["reference_pack.manifest_sha256"] = pack.manifest_sha256
    hashed["reference_pack.tree_sha256"] = pack.tree_sha256
    return hashed


def _checked_in_bundle_hash(paths: Sequence[Path]) -> str:
    """Hash a named source bundle without admitting caches or generated files."""
    rows: dict[str, str] = {}
    for path in sorted(paths, key=lambda item: item.as_posix()):
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"comparison source is not a regular file: {path}")
        rows[path.relative_to(path.parents[1]).as_posix()] = sha256_file(path)
    if not rows:
        raise ValueError("comparison source bundle is empty")
    return _tree_hash(rows)


def _comparison_provenance(
    config: BenchConfig, input_hashes: Mapping[str, str]
) -> dict[str, dict[str, str]]:
    package_root = Path(__file__).resolve().parent
    source_files = [path for path in package_root.rglob("*.py") if "__pycache__" not in path.parts]
    schema_files = sorted((package_root.parent / "schemas").glob("*.json"))
    runner_source = _checked_in_bundle_hash(source_files)
    schema_bundle = _checked_in_bundle_hash(schema_files)
    common = {
        "runner_source_sha256": runner_source,
        "seed_tree_sha256": input_hashes["seed_root"],
        "reference_manifest_sha256": input_hashes["reference_pack.manifest_sha256"],
        "reference_tree_sha256": input_hashes["reference_pack.tree_sha256"],
        "schema_bundle_sha256": schema_bundle,
    }
    return {
        tid: {
            **common,
            "prompt_sha256": input_hashes[f"prompt:{tid}"],
            "rubric_sha256": input_hashes[f"rubric:{tid}"],
        }
        for tid in sorted(config.tracks)
    }


def _sanitize_version_text(
    value: str,
    *,
    config: BenchConfig,
    run_dir: Path,
    adapter: Harness,
) -> str:
    text = redact_text(
        value,
        roots=(config.root, run_dir),
        secret_values=_secret_env_values(adapter),
    )
    text = re.sub(r"(?<![A-Za-z0-9])/[A-Za-z0-9._~+@%:/=-]+", "<path>", text)
    text = re.sub(r"\b[A-Za-z]:[\\/][^\s]+", "<path>", text)
    return " ".join(text.split())[:512]


def _probe_tool_version(
    config: BenchConfig,
    run_dir: Path,
    adapter: Harness,
    probe_id: int,
) -> tuple[str | None, str | None, str | None]:
    """Return resolved executable identity internally, version, and safe error."""
    try:
        resolved = adapter.resolve_binary()
        command = adapter.version_command()
    except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
        error = _sanitize_version_text(
            str(exc) or type(exc).__name__, config=config, run_dir=run_dir, adapter=adapter
        )
        return None, None, error or "version command unavailable"
    stdout = run_dir / "private" / f"tool-version-{probe_id}.stdout"
    stderr = run_dir / "private" / f"tool-version-{probe_id}.stderr"
    result = run_managed(
        command,
        cwd=None,
        env=adapter.prepare_env(),
        stdout_path=stdout,
        stderr_path=stderr,
        timeout_s=_VERSION_TIMEOUT_S,
        max_stream_bytes=_VERSION_MAX_BYTES,
    )
    raw = _read_text(stdout).strip() or _read_text(stderr).strip()
    if result.timed_out:
        error = "version command timed out"
    elif result.error:
        error = result.error
    elif result.returncode != 0:
        error = f"version command exited with status {result.returncode}"
    elif result.stdout_truncated or result.stderr_truncated:
        error = "version command output exceeded limit"
    elif not raw:
        error = "version command produced no output"
    else:
        version = _sanitize_version_text(raw, config=config, run_dir=run_dir, adapter=adapter)
        return resolved, version or None, None if version else "version command produced no output"
    safe = _sanitize_version_text(error, config=config, run_dir=run_dir, adapter=adapter)
    return resolved, None, safe or "version unavailable"


def _collect_tooling(
    config: BenchConfig,
    run_dir: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    roles: list[tuple[str, str, str | None, Any, Any]] = []
    for hid in sorted(config.harnesses):
        spec = config.harnesses[hid]
        if spec.enabled:
            roles.append(("implementation", spec.id, None, spec, spec))
    for evaluator in sorted((e for e in config.evaluators if e.enabled), key=lambda e: e.id):
        href = config.harnesses[evaluator.harness]
        roles.append(("evaluator", href.id, evaluator.id, href, evaluator))

    probe_cache: dict[str, tuple[str | None, str | None]] = {}
    unresolved_cache: dict[tuple[str, str | None], tuple[str | None, str | None]] = {}
    records: list[dict[str, Any]] = []
    limitations: list[str] = []
    for role, config_id, evaluator_id, href, model_spec in roles:
        adapter = get_harness(href.adapter, binary=href.binary)
        unresolved_key = (href.adapter, href.binary)
        try:
            resolved = adapter.resolve_binary()
        except (FileNotFoundError, OSError, ValueError) as exc:
            if unresolved_key not in unresolved_cache:
                error = _sanitize_version_text(
                    str(exc) or type(exc).__name__, config=config, run_dir=run_dir, adapter=adapter
                )
                unresolved_cache[unresolved_key] = (None, error or "executable unavailable")
            version, version_error = unresolved_cache[unresolved_key]
        else:
            # Command dispatchers such as Volta expose multiple tool names as
            # symlinks to one shim.  The invoked path selects the real tool, so
            # following the symlink would incorrectly share version output
            # across otherwise distinct executables.
            probe_key = os.path.abspath(resolved)
            if probe_key not in probe_cache:
                _, version, version_error = _probe_tool_version(
                    config, run_dir, adapter, len(probe_cache) + 1
                )
                probe_cache[probe_key] = (version, version_error)
            version, version_error = probe_cache[probe_key]
        if version is None:
            label = evaluator_id or config_id
            limitations.append(f"tool_version_unavailable:{role}:{label}")
        records.append(
            {
                "role": role,
                "config_id": config_id,
                "evaluator_id": evaluator_id,
                "adapter": href.adapter,
                "model_id": model_spec.model,
                "provider_family": model_spec.provider_family,
                "effort": model_spec.effort,
                "executable_version": version,
                "version_error": version_error,
                "adapter_version": __version__,
                "runner_version": __version__,
                "deterministic_seed": {"supported": False, "limitation": _NO_SEED_LIMITATION},
            }
        )
    return records, sorted(limitations)


def _public_pricing(
    provenance: Mapping[str, Any],
    pricing_ok: bool,
    reasons: Sequence[str],
) -> dict[str, Any]:
    return {
        "source": provenance.get("source"),
        "retrieved_at": provenance.get("retrieved_at"),
        "stale": bool(provenance.get("stale")),
        "error": provenance.get("error"),
        "complete": pricing_ok,
        "limitations": list(reasons),
        "url": provenance.get("url"),
        "cache_path": provenance.get("cache_path"),
    }


def _pricing_digest(pricing_data: Mapping[str, Any]) -> str:
    payload = json.dumps(
        pricing_data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _final_status(
    config: BenchConfig,
    ineligible: Sequence[str],
    attempts: Sequence[Attempt],
) -> str:
    if config.mode != "publication":
        return "complete"
    if ineligible or not attempts:
        return "ineligible"
    if any(not a.evaluation_success or a.ineligible_reasons for a in attempts):
        return "ineligible"
    return "complete"


def _checkpoint_run(
    config: BenchConfig,
    ctx: _RunContext,
    status: str,
    jobs: Sequence[Mapping[str, Any]],
    artifacts: Mapping[str, str],
    *,
    finished: str | None,
) -> None:
    _write_manifest(
        config,
        ctx.run_dir,
        ctx.run_id,
        ctx.started,
        status,
        _public_pricing(ctx.pricing_prov, ctx.pricing_ok, ctx.pricing_reasons),
        [dict(job) for job in jobs],
        dict(artifacts),
        dict(ctx.input_hashes),
        [dict(item) for item in ctx.tooling],
        finished=finished,
        runner_git=ctx.runner_git,
    )


def _checkpoint_writer(
    config: BenchConfig,
    ctx: _RunContext,
    jobs: list[dict[str, Any]],
    artifacts: dict[str, str],
) -> Callable[..., None]:
    """Return the serialized, idempotent manifest checkpoint writer.

    Calls may arrive from concurrent workers. New jobs are deduplicated by ID,
    all jobs are deterministically sorted, artifacts are merged, and a complete
    manifest snapshot is atomically rewritten while the lock is held.
    """

    seen = {job["id"] for job in jobs}
    lock = threading.RLock()

    def checkpoint(
        new_jobs: Sequence[Mapping[str, Any]] = (),
        new_artifacts: Mapping[str, str] | None = None,
        *,
        status: str = "running",
        finished: str | None = None,
    ) -> None:
        with lock:
            for record in new_jobs:
                job_id = record.get("id")
                if not isinstance(job_id, str):
                    raise ValueError("checkpoint job is missing an id")
                if job_id not in seen:
                    jobs.append(dict(record))
                    seen.add(job_id)
            jobs.sort(
                key=lambda record: (
                    str(record.get("submission_id") or ""),
                    0 if record.get("kind") == "implement" else 1,
                    str(record.get("evaluator_id") or ""),
                    str(record["id"]),
                )
            )
            if new_artifacts:
                artifacts.update(new_artifacts)
            _checkpoint_run(config, ctx, status, jobs, artifacts, finished=finished)

    return checkpoint


def _record_private_failure(config: BenchConfig, run_dir: Path, exc: BaseException) -> None:
    kind = "interrupted" if isinstance(exc, KeyboardInterrupt) else "exception"
    message = _safe_error(
        str(exc) or type(exc).__name__,
        roots=(config.root, config.run_root, run_dir),
        secret_values=(),
    )
    atomic_write_json(
        run_dir / "private" / "failure.json",
        {"kind": kind, "message": message or type(exc).__name__},
    )


def _fail_run_without_masking(
    config: BenchConfig,
    run_dir: Path,
    checkpoint: Callable[..., None],
    original: BaseException,
) -> None:
    try:
        _record_private_failure(config, run_dir, original)
    except Exception:  # the original execution failure remains authoritative
        pass
    try:
        checkpoint(status="failed", finished=_utc_now())
    except Exception:  # never replace the agent/interruption exception
        pass


def _write_manifest(
    config: BenchConfig,
    run_dir: Path,
    run_id: str,
    started: str,
    status: str,
    pricing: Mapping[str, Any],
    jobs: list[dict[str, Any]],
    artifacts: dict[str, str],
    inputs: dict[str, str],
    tooling: list[dict[str, Any]],
    finished: str | None = None,
    runner_git: Mapping[str, Any] | None = None,
) -> None:
    write_manifest(
        run_dir / "run-manifest.json",
        _manifest_payload(
            config,
            run_id,
            started,
            status,
            pricing,
            jobs,
            artifacts,
            inputs,
            tooling,
            finished=finished,
            runner_git=runner_git,
        ),
    )


def _manifest_payload(
    config: BenchConfig,
    run_id: str,
    started: str,
    status: str,
    pricing: Mapping[str, Any],
    jobs: list[dict[str, Any]],
    artifacts: dict[str, str],
    inputs: dict[str, str],
    tooling: list[dict[str, Any]],
    *,
    finished: str | None = None,
    runner_git: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return build_manifest(
        runner_version=__version__,
        run_id=run_id,
        mode=config.mode,
        config=config_to_public_dict(config),
        inputs=inputs,
        pricing=dict(pricing),
        jobs=jobs,
        artifacts=artifacts,
        tooling=tooling,
        status=status,
        started_at=started,
        finished_at=finished,
        runner_git=runner_git,
    )


def _job_record(
    *,
    job_id: str,
    kind: str,
    harness_id: str,
    track: str,
    repetition: int,
    submission_id: str | None,
    execution: AgentExecution,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    proc, usage = execution.process, None
    if execution.usage is not None:
        u = execution.usage
        usage = {
            "input_tokens": u.input_tokens,
            "cached_input_tokens": u.cached_input_tokens,
            "cache_write_tokens": u.cache_write_tokens,
            "output_tokens": u.output_tokens,
        }
    record: dict[str, Any] = {
        "id": job_id,
        "kind": kind,
        "harness": harness_id,
        "track": track,
        "repetition": repetition,
        "submission_id": submission_id,
        "command_preview": execution.command_preview,
        "returncode": proc.returncode,
        "duration_s": proc.duration_s,
        "timed_out": proc.timed_out,
        "interrupted": proc.interrupted,
        "error": execution.error,
        "cost_usd": execution.cost_usd,
        "reported_cost_usd": execution.reported_cost_usd,
        "usage": usage,
    }
    if extra:
        record.update(dict(extra))
    return record


def _raised_job_record(
    *,
    config: BenchConfig,
    run_dir: Path,
    job_id: str,
    kind: str,
    harness_id: str,
    track: str,
    repetition: int,
    submission_id: str | None,
    exc: BaseException,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    message = _safe_error(
        str(exc) or type(exc).__name__,
        roots=(config.root, config.run_root, run_dir),
        secret_values=(),
    )
    interrupted = isinstance(exc, KeyboardInterrupt)
    process = ProcessResult.not_started(
        message or type(exc).__name__,
        interrupted=interrupted,
    )
    return _job_record(
        job_id=job_id,
        kind=kind,
        harness_id=harness_id,
        track=track,
        repetition=repetition,
        submission_id=submission_id,
        execution=AgentExecution(
            process=process,
            usage=None,
            cost_usd=None,
            reported_cost_usd=None,
            last_message=None,
            command_preview="<execution-raised>",
            error=message or type(exc).__name__,
        ),
        extra=extra,
    )


def _tokens(usage: Usage | None) -> int:
    return 0 if usage is None else usage.total()


def _run_repetition(
    execution: _ExecutionContext,
    task: _RunTask,
) -> tuple[Attempt, list[dict[str, Any]], dict[str, str]]:
    """Implement and snapshot one repetition, then share evaluation/persistence.

    A successful implementation is snapshotted before evaluators receive it.
    Implementation process failures become an ineligible attempt; unexpected
    Python exceptions are checkpointed and propagated. The return value is the
    attempt plus all job records and public artifact hashes produced here.
    """

    harness, track = task.harness, task.track
    repetition, submission_id = task.repetition, task.submission_id
    jobs: list[dict[str, Any]] = []
    artifacts: dict[str, str] = {}
    progress_fields = {
        "harness": harness.id,
        "model": harness.model,
        "track": track.id,
        "repetition": repetition,
        "submission_id": submission_id,
    }
    _emit_progress(execution.options, "build.started", **progress_fields)
    workspace = create_unique_directory(execution.run.run_dir / "workspaces" / submission_id)
    materialize_seed(execution.config, workspace / "tree")
    workdir = workspace / "tree"
    prompt_path = execution.run.run_dir / "prompts" / f"implement-{submission_id}.md"
    prompt_path.write_bytes(implementation_prompt_bytes(track.prompt_file))
    try:
        with execution.agent_slots:
            impl = execute_agent(
                execution.config,
                AgentJob(
                    kind="implement",
                    harness=harness.adapter,
                    model=ModelSpec(model=harness.model, effort=harness.effort),
                    workdir=workdir,
                    prompt_path=prompt_path,
                    log_path=(execution.run.run_dir / "logs" / f"implement-{submission_id}.log"),
                    last_message_path=(
                        execution.run.run_dir / "private" / f"implement-{submission_id}.last.md"
                    ),
                    evidence_dirs=(),
                    sandbox_mode=_sandbox(execution.config),
                ),
                pricing_data=execution.run.pricing_payload,
                pricing_retrieved_at=execution.run.pricing_retrieved_at,
                options=execution.options,
                cancel_event=execution.cancel_event,
            )
    except (Exception, KeyboardInterrupt) as exc:
        jobs.append(
            _raised_job_record(
                config=execution.config,
                run_dir=execution.run.run_dir,
                job_id=f"implement-{submission_id}",
                kind="implement",
                harness_id=harness.id,
                track=track.id,
                repetition=repetition,
                submission_id=submission_id,
                exc=exc,
            )
        )
        execution.checkpoint(jobs, artifacts)
        raise
    jobs.append(
        _job_record(
            job_id=f"implement-{submission_id}",
            kind="implement",
            harness_id=harness.id,
            track=track.id,
            repetition=repetition,
            submission_id=submission_id,
            execution=impl,
        )
    )
    execution.checkpoint(jobs, artifacts)
    impl_ok = _process_ok(impl.process) and impl.error is None
    snapshot_path: Path | None = None
    if impl_ok:
        snapshot_path = execution.run.run_dir / "snapshots" / submission_id
        for rel, digest in atomic_snapshot(
            workdir,
            snapshot_path,
            ignore_patterns=_AMBIENT_IGNORE,
        ).items():
            artifacts[f"snapshots/{submission_id}/{rel}"] = digest
        execution.checkpoint(jobs, artifacts)
    _emit_progress(
        execution.options,
        "build.finished",
        **progress_fields,
        status="succeeded" if impl_ok else "failed",
        duration_s=round(float(impl.process.duration_s), 3),
    )
    extra: list[str] = []
    if not impl_ok:
        extra.append("implementation_failed")
        if impl.process.timed_out:
            extra.append("implementation_timeout")
    prepared = _PreparedSubmission(
        submission_id=submission_id,
        track_id=track.id,
        harness_id=harness.id,
        model_id=_model_identifier(harness.model),
        display_name=harness.display_name,
        repetition=repetition,
        snapshot_path=snapshot_path,
        implementation_success=impl_ok,
        jobs=tuple(jobs),
        artifacts=artifacts,
        implementation_cost_usd=impl.cost_usd,
        implementation_tokens=_tokens(impl.usage),
        implementation_duration_s=float(impl.process.duration_s),
        extra_ineligible_reasons=tuple(extra),
    )
    return _evaluate_and_persist_attempt(
        execution,
        prepared,
        task.evaluator_attempts,
    )


def _model_identifier(model: str) -> str:
    return validate_identifier(normalize_model_id(model), field="model_id")


def _run_evaluator(
    execution_context: _ExecutionContext,
    submission_id: str,
    snapshot_path: Path,
    track: Any,
    contract: EvaluationContract,
    contract_hash: str,
    evaluator: Any,
    eval_attempt_id: str,
    repetition: int,
) -> dict[str, Any]:
    """Evaluate one immutable submission copy and return a normalized outcome.

    Seed and submission trees are copied into a disposable evaluation area and
    hashed before execution. A result is valid only when the process succeeds,
    both evidence trees remain unchanged, and the report and contract-bound
    JSON result are present and valid. Invalid outputs remain recorded with
    explicit reasons and artifact hashes for auditability.
    """

    _emit_progress(
        execution_context.options,
        "evaluate.started",
        evaluator=evaluator.id,
        model=evaluator.model,
        track=track.id,
        repetition=repetition,
        submission_id=submission_id,
        eval_attempt_id=eval_attempt_id,
    )
    run_dir = execution_context.run.run_dir
    eval_parent = run_dir / "evaluations" / submission_id
    eval_parent.mkdir(parents=True, exist_ok=True)
    eval_root = create_unique_directory(eval_parent / eval_attempt_id)
    seed_dir, submission_dir, output_dir = (
        eval_root / "seed",
        eval_root / "submission",
        eval_root / "output",
    )
    output_dir.mkdir()
    materialize_seed(execution_context.config, seed_dir)
    atomic_snapshot(snapshot_path, submission_dir)
    pre_seed = tree_manifest(seed_dir, ignore=_AMBIENT_IGNORE)
    pre_sub = tree_manifest(submission_dir, ignore=_AMBIENT_IGNORE)
    report_path, result_path = output_dir / "report.md", output_dir / "result.json"
    prompt_text = build_evaluator_prompt(
        track=track.id,
        submission_id=submission_id,
        evaluator_id=evaluator.id,
        contract_sha256=contract_hash,
        contract=json.loads(track.contract_file.read_text(encoding="utf-8")),
        rubric=track.rubric_file.read_text(encoding="utf-8"),
        seed_dir=seed_dir,
        submission_dir=submission_dir,
        report_path=report_path,
        result_path=result_path,
    )
    prompt_path = eval_root / "prompt.md"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    href = execution_context.config.harnesses[evaluator.harness]
    job = AgentJob(
        kind="evaluate",
        harness=href.adapter,
        model=ModelSpec(model=evaluator.model, effort=evaluator.effort),
        workdir=output_dir,
        prompt_path=prompt_path,
        log_path=run_dir / "logs" / f"evaluate-{submission_id}-{eval_attempt_id}.log",
        last_message_path=eval_root / "last.md",
        evidence_dirs=(seed_dir, submission_dir),
        sandbox_mode="workspace-write",
    )
    with execution_context.agent_slots:
        execution = execute_agent(
            execution_context.config,
            job,
            pricing_data=execution_context.run.pricing_payload,
            pricing_retrieved_at=execution_context.run.pricing_retrieved_at,
            options=execution_context.options,
            cancel_event=execution_context.cancel_event,
        )
    artifacts: dict[str, str] = {}
    invalid: list[str] = []
    valid_result: ValidatedJudgeScores | None = None
    if not _process_ok(execution.process) or execution.error is not None:
        invalid.append("evaluator_execution_failed")
    if verify_tree_manifest(seed_dir, pre_seed, ignore=_AMBIENT_IGNORE):
        invalid.append("seed_mutated")
    if verify_tree_manifest(submission_dir, pre_sub, ignore=_AMBIENT_IGNORE):
        invalid.append("submission_mutated")
    if not report_path.is_file() or report_path.stat().st_size == 0:
        invalid.append("missing_report")
    if not result_path.is_file() or result_path.stat().st_size == 0:
        invalid.append("missing_result")
    else:
        try:
            result_data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            invalid.append("malformed_result_json")
            result_data = None
        if result_data is not None:
            try:
                normalized = normalize_validated_judge_result(
                    result_data,
                    contract,
                    expected_track=track.id,
                    expected_submission_id=submission_id,
                    expected_contract_sha256=contract_hash,
                    expected_judge_id=evaluator.id,
                )
            except ValueError:
                invalid.append("invalid_judge_result")
            else:
                if not invalid:
                    valid_result = normalized
    rel = f"evaluations/{submission_id}/{eval_attempt_id}"
    if report_path.is_file():
        artifacts[f"{rel}/output/report.md"] = sha256_file(report_path)
    if result_path.is_file():
        artifacts[f"{rel}/output/result.json"] = sha256_file(result_path)
    _emit_progress(
        execution_context.options,
        "evaluate.finished",
        evaluator=evaluator.id,
        model=evaluator.model,
        track=track.id,
        repetition=repetition,
        submission_id=submission_id,
        eval_attempt_id=eval_attempt_id,
        status="succeeded" if valid_result is not None else "failed",
        duration_s=round(float(execution.process.duration_s), 3),
    )
    return {
        "job_record": _job_record(
            job_id=f"evaluate-{eval_attempt_id}",
            kind="evaluate",
            harness_id=evaluator.harness,
            track=track.id,
            repetition=repetition,
            submission_id=submission_id,
            execution=execution,
            extra={
                "evaluator_id": evaluator.id,
                "eval_attempt_id": eval_attempt_id,
                "valid": valid_result is not None,
                "invalid_reasons": invalid,
            },
        ),
        "artifacts": artifacts,
        "valid_result": valid_result,
        "cost_usd": execution.cost_usd,
        "tokens": _tokens(execution.usage),
        "duration_s": float(execution.process.duration_s),
    }
