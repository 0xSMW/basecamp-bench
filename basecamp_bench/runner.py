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
    aggregate_judges,
    contract_sha256,
    load_contract,
    validate_judge_result,
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
from basecamp_bench.leaderboard import Attempt, aggregate_attempts, write_leaderboards
from basecamp_bench.manifest import build_manifest, hash_inputs, verify_run, write_manifest
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
_AMBIENT_IGNORE = (".DS_Store", "**/.DS_Store")
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
    """Execute a complete benchmark and return its newly created run directory.

    Implementation tasks run concurrently. Evaluators for a successful,
    immutable snapshot may overlap other implementations, while one shared
    semaphore enforces ``options.max_parallel_agents`` across both pools.
    Checkpoints are written throughout the run; interruption or an unexpected
    worker failure records a failed manifest when possible and is re-raised.
    """
    ctx = _open_run(config, options, id_factory, pricing_data, now)
    jobs: list[dict[str, Any]] = []
    attempts: list[Attempt] = []
    arts: dict[str, str] = {}
    checkpoint = _checkpoint_writer(config, ctx, jobs, arts)
    cancel_event = threading.Event()
    agent_slots = threading.BoundedSemaphore(options.max_parallel_agents)
    restore_signal = _install_termination_cancellation(cancel_event)
    try:
        checkpoint(status="running")
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
        _emit_progress(
            options,
            "run.planned",
            implementations=len(tasks),
            evaluators=sum(len(task.evaluator_attempts) for task in tasks),
        )
        evaluator_count = sum(len(task.evaluator_attempts) for task in tasks)
        with (
            executor_pool(
                workers=worker_count(evaluator_count, options.max_parallel_agents),
                thread_name_prefix="basecamp-bench-evaluate",
            ) as evaluator_executor,
            executor_pool(
                workers=worker_count(len(tasks), options.max_parallel_agents),
                thread_name_prefix="basecamp-bench-implement",
            ) as implementation_executor,
        ):
            results = collect_indexed(
                tasks,
                executor=implementation_executor,
                cancel_event=cancel_event,
                submit=lambda executor, task: executor.submit(
                    _run_repetition,
                    config,
                    options,
                    ctx.run_dir,
                    ctx.run_id,
                    task.harness,
                    task.track,
                    task.repetition,
                    task.submission_id,
                    ctx.contracts,
                    ctx.contract_hashes,
                    task.evaluator_attempts,
                    ctx.pricing_payload,
                    ctx.pricing_retrieved_at,
                    ctx.pricing_ok,
                    ctx.pricing_reasons,
                    ctx.ineligible,
                    checkpoint,
                    cancel_event,
                    agent_slots,
                    evaluator_executor,
                ),
            )
        for result in results:
            attempt, j, a = result
            attempts.append(attempt)
            checkpoint(j, a)
        return _finalize_run(config, ctx, jobs, attempts, arts, now, options)
    except (Exception, KeyboardInterrupt) as exc:
        _emit_progress(options, "run.failed", error=type(exc).__name__)
        _fail_run_without_masking(config, ctx.run_dir, checkpoint, exc)
        raise
    finally:
        restore_signal()


def reevaluate_run(
    config: BenchConfig,
    prior_run_dir: Path,
    *,
    options: RunOptions = RunOptions(),
    id_factory: Callable[[], str] | None = None,
    pricing_data: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> Path:
    """Re-evaluate verified snapshots and return a separate new run directory.

    The prior run and every declared snapshot are verified before reuse. The
    copied snapshots are evaluated with the current contracts and evaluator
    configuration; the source run remains unchanged. Evaluator failures use
    the same checkpoint-and-reraise behavior as a normal benchmark run.
    """
    prior = _require_prior_run_dir(config, prior_run_dir)
    errs = verify_run(prior)
    if errs:
        raise ValueError(f"prior run verification failed: {errs[0]}")
    prior_man = _load_prior_manifest(prior)
    reusable = _prior_reusable_submissions(prior, prior_man)
    if not reusable:
        raise ValueError("prior run has no reusable verified snapshots")
    ctx = _open_run(config, options, id_factory, pricing_data, now)
    jobs: list[dict[str, Any]] = []
    attempts: list[Attempt] = []
    arts: dict[str, str] = {}
    checkpoint = _checkpoint_writer(config, ctx, jobs, arts)
    cancel_event = threading.Event()
    agent_slots = threading.BoundedSemaphore(options.max_parallel_agents)
    restore_signal = _install_termination_cancellation(cancel_event)
    try:
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
        checkpoint(status="running")
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
        _emit_progress(
            options,
            "reevaluate.planned",
            submissions=len(planned),
            evaluators=sum(len(evaluator_attempts) for _, evaluator_attempts in planned),
        )
        evaluator_count = sum(len(attempts) for _, attempts in planned)
        with (
            executor_pool(
                workers=worker_count(evaluator_count, options.max_parallel_agents),
                thread_name_prefix="basecamp-bench-evaluate",
            ) as evaluator_executor,
            executor_pool(
                workers=worker_count(len(planned), options.max_parallel_agents),
                thread_name_prefix="basecamp-bench-reevaluate",
            ) as reevaluation_executor,
        ):
            results = collect_indexed(
                planned,
                executor=reevaluation_executor,
                cancel_event=cancel_event,
                submit=lambda executor, planned_item: executor.submit(
                    _reeval_submission,
                    config,
                    options,
                    ctx,
                    prior_man["run_id"],
                    planned_item[0],
                    planned_item[1],
                    checkpoint,
                    cancel_event,
                    agent_slots,
                    evaluator_executor,
                ),
            )
        for (item, _), result in zip(planned, results, strict=True):
            attempt, j, a = result
            attempts.append(attempt)
            checkpoint(j, a)
        checkpoint()
        return _finalize_run(config, ctx, jobs, attempts, arts, now, options)
    except (Exception, KeyboardInterrupt) as exc:
        _emit_progress(options, "run.failed", error=type(exc).__name__)
        _fail_run_without_masking(config, ctx.run_dir, checkpoint, exc)
        raise
    finally:
        restore_signal()


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
    roots = aggregate_attempts(
        attempts,
        mode=config.mode,
        generated_at=_utc_now(now),
        comparison_provenance=ctx.comparison_provenance,
        dimension_profiles={
            tid: [
                {"id": dim.id, "label": dim.label, "weight": dim.weight}
                for dim in contract.dimensions
            ]
            for tid, contract in ctx.contracts.items()
        },
    )
    written = write_leaderboards(run_dir / "leaderboards", roots)
    _emit_progress(options, "aggregate.finished", leaderboards=len(roots))
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
    _write_manifest(
        config,
        run_dir,
        ctx.run_id,
        ctx.started,
        final_status,
        _public_pricing(ctx.pricing_prov, ctx.pricing_ok, ctx.pricing_reasons),
        job_records,
        artifact_hashes,
        dict(ctx.input_hashes),
        list(ctx.tooling),
        finished=_utc_now(now),
    )
    _emit_progress(options, "run.finished", run_id=ctx.run_id, status=final_status)
    return run_dir


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
    validate_identifier(resolved.name, field="prior_run_id")
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
    if run_id != prior.name:
        raise ValueError("prior run id does not match directory name")
    prior_status = data.get("status")
    if prior_status not in {"complete", "ineligible", "failed"}:
        raise ValueError(f"prior run status is not reusable: {data.get('status')!r}")
    jobs, artifacts, inputs = data.get("jobs"), data.get("artifacts"), data.get("inputs")
    if (
        not isinstance(jobs, list)
        or not isinstance(artifacts, dict)
        or not isinstance(inputs, dict)
    ):
        raise ValueError("prior run-manifest.json jobs/artifacts shape is invalid")
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
    sids = sorted(
        {
            rel.split("/")[1]
            for rel in artifacts
            if isinstance(rel, str) and rel.startswith("snapshots/") and rel.count("/") >= 2
        }
    )
    out: list[dict[str, Any]] = []
    for sid in sids:
        validate_identifier(sid, field="submission_id")
        rel = f"attempts/{sid}.json"
        digest = artifacts.get(rel)
        if not isinstance(digest, str):
            raise ValueError(f"prior attempt artifact is undeclared: {rel}")
        path = prior / "attempts" / f"{sid}.json"
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise ValueError(f"prior attempt is missing or hash-mismatched: {rel}")
        try:
            attempt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"prior attempt JSON is unreadable: {exc}") from exc
        if not isinstance(attempt, dict):
            raise ValueError(f"prior attempt JSON must be an object: {rel}")
        if attempt.get("submission_id") != sid or attempt.get("run_id") != prior_run_id:
            raise ValueError("prior attempt identity mismatch")
        if attempt.get("implementation_success") is not True:
            raise ValueError(f"prior attempt is not implementation-successful: {sid}")
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
        prefix, declared = f"snapshots/{sid}/", {}
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
        snap = prior / "snapshots" / sid
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


def _eval_pass(
    config: BenchConfig,
    options: RunOptions,
    run_dir: Path,
    sid: str,
    snapshot: Path,
    track: Any,
    contract: EvaluationContract,
    contract_hash: str,
    evaluator_attempts: Sequence[tuple[Any, str]],
    pricing_data: Mapping[str, Any] | None,
    pricing_retrieved_at: str | None,
    repetition: int,
    checkpoint: Callable[..., None],
    cancel_event: threading.Event,
    agent_slots: threading.BoundedSemaphore,
    evaluator_executor: ThreadPoolExecutor,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], dict[str, str], list[str], float, bool, int, float
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
    valid_results: list[dict[str, Any]] = []
    valid_eval_ids: list[str] = []
    eval_cost_total, eval_cost_known, eval_tokens, eval_duration = 0.0, True, 0, 0.0
    results: list[dict[str, Any] | None] = [None] * len(evaluator_attempts)
    futures: dict[Future[Any], tuple[int, Any, str]] = {}
    first_error: BaseException | None = None
    for index, (evaluator, eval_attempt_id) in enumerate(evaluator_attempts):
        futures[
            evaluator_executor.submit(
                _run_evaluator,
                config,
                options,
                run_dir,
                sid,
                snapshot,
                track,
                contract,
                contract_hash,
                evaluator,
                eval_attempt_id,
                pricing_data,
                pricing_retrieved_at,
                repetition,
                cancel_event,
                agent_slots,
            )
        ] = (index, evaluator, eval_attempt_id)
    for future in as_completed(futures):
        index, evaluator, eval_attempt_id = futures[future]
        try:
            ev = future.result()
        except BaseException as exc:
            cancel_event.set()
            failure = _raised_job_record(
                config=config,
                run_dir=run_dir,
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
            checkpoint((failure,), artifacts)
            if first_error is None:
                first_error = exc
            continue
        results[index] = ev
        checkpoint((ev["job_record"],), ev["artifacts"])
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


def _score_results(
    valid_results: Sequence[dict[str, Any]],
    contract: EvaluationContract,
) -> tuple[float | None, dict[str, float], float | None]:
    if not valid_results:
        return None, {}, None
    agg = aggregate_judges(list(valid_results), contract)
    score = float(agg["overall"])
    dimensions = {dim_id: float(stats["median"]) for dim_id, stats in agg["dimensions"].items()}
    overalls = [float(j["overall"]) for j in agg["judges"]]
    spread = 0.0 if len(overalls) <= 1 else float(statistics.pstdev(overalls))
    return score, dimensions, spread


def _reeval_submission(
    config: BenchConfig,
    options: RunOptions,
    ctx: _RunContext,
    prior_run_id: str,
    item: Mapping[str, Any],
    evaluator_attempts: Sequence[tuple[Any, str]],
    checkpoint: Callable[..., None],
    cancel_event: threading.Event,
    agent_slots: threading.BoundedSemaphore,
    evaluator_executor: ThreadPoolExecutor,
) -> tuple[Attempt, list[dict[str, Any]], dict[str, str]]:
    """Copy one prior snapshot, evaluate it, and record its replacement attempt.

    The copied tree must exactly match the prior manifest. Historical
    implementation usage and cost remain attached as provenance, while only
    the new evaluator calls contribute newly incurred evaluation work.
    """

    jobs: list[dict[str, Any]] = []
    artifacts: dict[str, str] = {}
    run_dir, run_id = ctx.run_dir, ctx.run_id
    contracts, contract_hashes = ctx.contracts, ctx.contract_hashes
    sid, prior_att, impl_job = item["submission_id"], item["attempt"], item["impl_job"]
    track_id = prior_att["track"]
    if track_id not in config.tracks or track_id not in contracts:
        raise ValueError(f"prior track is missing from current config: {track_id}")
    track, contract, contract_hash = (
        config.tracks[track_id],
        contracts[track_id],
        contract_hashes[track_id],
    )
    impl_tokens = _usage_token_total(impl_job.get("usage"))
    impl_duration = float(impl_job["duration_s"])
    impl_cost = float(impl_job["cost_usd"]) if impl_job.get("cost_usd") is not None else None
    new_snap = run_dir / "snapshots" / sid
    snap_manifest = atomic_snapshot(item["snapshot_path"], new_snap)
    if snap_manifest != item["declared"]:
        raise ValueError(f"copied snapshot does not match prior declared hashes: {sid}")
    for rel, digest in snap_manifest.items():
        artifacts[f"snapshots/{sid}/{rel}"] = digest
    jobs.append(
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
        }
    )
    checkpoint(jobs, artifacts)
    ej, valid_results, ea, valid_eval_ids, eval_cost, eval_known, eval_tokens, eval_dur = (
        _eval_pass(
            config,
            options,
            run_dir,
            sid,
            new_snap,
            track,
            contract,
            contract_hash,
            evaluator_attempts,
            ctx.pricing_payload,
            ctx.pricing_retrieved_at,
            int(prior_att["repetition"]),
            checkpoint,
            cancel_event,
            agent_slots,
            evaluator_executor,
        )
    )
    jobs.extend(ej)
    artifacts.update(ea)
    min_evals = _MIN_PUB_EVALS if config.mode == "publication" else _MIN_LOCAL_EVALS
    evaluation_success = len(valid_results) >= min_evals
    score, dimensions, judge_spread = (
        _score_results(valid_results, contract) if evaluation_success else (None, {}, None)
    )
    reasons: list[str] = list(ctx.ineligible)
    if not evaluation_success:
        reasons.append("insufficient_valid_evaluators")
    attempt = Attempt(
        run_id=run_id,
        submission_id=sid,
        repetition=int(prior_att["repetition"]),
        track=track_id,
        contract_version=contract.contract_version,
        contract_sha256=contract_hash,
        harness=prior_att["harness"],
        model_id=validate_identifier(prior_att["model_id"], field="model_id"),
        display_name=prior_att["display_name"],
        implementation_success=True,
        evaluation_success=evaluation_success,
        score=score,
        dimensions=dimensions,
        judge_spread=judge_spread,
        implementation_cost_usd=impl_cost,
        evaluation_cost_usd=eval_cost if eval_known else None,
        tokens=impl_tokens + eval_tokens,
        duration_s=impl_duration + eval_dur,
        evaluator_ids=tuple(valid_eval_ids),
        ineligible_reasons=tuple(reasons),
    )
    attempt_path = run_dir / "attempts" / f"{sid}.json"
    atomic_write_json(attempt_path, _attempt_public(attempt))
    artifacts[f"attempts/{sid}.json"] = sha256_file(attempt_path)
    checkpoint(jobs, artifacts)
    return attempt, jobs, artifacts


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
            probe_key = str(Path(resolved).resolve())
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
) -> None:
    write_manifest(
        run_dir / "run-manifest.json",
        build_manifest(
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
            repo=config.root,
        ),
    )


def _attempt_public(attempt: Attempt) -> dict[str, Any]:
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
    config: BenchConfig,
    options: RunOptions,
    run_dir: Path,
    run_id: str,
    harness: Any,
    track: Any,
    repetition: int,
    submission_id: str,
    contracts: Mapping[str, EvaluationContract],
    contract_hashes: Mapping[str, str],
    evaluator_attempts: Sequence[tuple[Any, str]],
    pricing_data: Mapping[str, Any] | None,
    pricing_retrieved_at: str | None,
    pricing_ok: bool,
    pricing_reasons: Sequence[str],
    global_ineligible_reasons: Sequence[str],
    checkpoint: Callable[..., None],
    cancel_event: threading.Event,
    agent_slots: threading.BoundedSemaphore,
    evaluator_executor: ThreadPoolExecutor,
) -> tuple[Attempt, list[dict[str, Any]], dict[str, str]]:
    """Implement, snapshot, evaluate, and persist one benchmark repetition.

    A successful implementation is snapshotted before evaluators receive it.
    Implementation process failures become an ineligible attempt; unexpected
    Python exceptions are checkpointed and propagated. The return value is the
    attempt plus all job records and public artifact hashes produced here.
    """

    jobs: list[dict[str, Any]] = []
    artifacts: dict[str, str] = {}
    _emit_progress(
        options,
        "build.started",
        harness=harness.id,
        model=harness.model,
        track=track.id,
        repetition=repetition,
        submission_id=submission_id,
    )
    workspace = create_unique_directory(run_dir / "workspaces" / submission_id)
    materialize_seed(config, workspace / "tree")
    workdir = workspace / "tree"
    prompt_path = run_dir / "prompts" / f"implement-{submission_id}.md"
    prompt_path.write_bytes(implementation_prompt_bytes(track.prompt_file))
    impl_job = AgentJob(
        kind="implement",
        harness=harness.adapter,
        model=ModelSpec(model=harness.model, effort=harness.effort),
        workdir=workdir,
        prompt_path=prompt_path,
        log_path=run_dir / "logs" / f"implement-{submission_id}.log",
        last_message_path=run_dir / "private" / f"implement-{submission_id}.last.md",
        evidence_dirs=(),
        sandbox_mode=_sandbox(config),
    )
    try:
        with agent_slots:
            impl = execute_agent(
                config,
                impl_job,
                pricing_data=pricing_data,
                pricing_retrieved_at=pricing_retrieved_at,
                options=options,
                cancel_event=cancel_event,
            )
    except (Exception, KeyboardInterrupt) as exc:
        jobs.append(
            _raised_job_record(
                config=config,
                run_dir=run_dir,
                job_id=f"implement-{submission_id}",
                kind="implement",
                harness_id=harness.id,
                track=track.id,
                repetition=repetition,
                submission_id=submission_id,
                exc=exc,
            )
        )
        checkpoint(jobs, artifacts)
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
    checkpoint(jobs, artifacts)
    impl_ok = _process_ok(impl.process) and impl.error is None
    snapshot_path: Path | None = None
    valid_results: list[dict[str, Any]] = []
    valid_eval_ids: list[str] = []
    eval_cost_total, eval_cost_known, eval_tokens, eval_duration = 0.0, True, 0, 0.0
    if impl_ok:
        snapshot_path = run_dir / "snapshots" / submission_id
        for rel, digest in atomic_snapshot(
            workdir,
            snapshot_path,
            ignore_patterns=_AMBIENT_IGNORE,
        ).items():
            artifacts[f"snapshots/{submission_id}/{rel}"] = digest
        checkpoint(jobs, artifacts)
        _emit_progress(
            options,
            "build.finished",
            harness=harness.id,
            model=harness.model,
            track=track.id,
            repetition=repetition,
            submission_id=submission_id,
            status="succeeded",
            duration_s=round(float(impl.process.duration_s), 3),
        )
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
            config,
            options,
            run_dir,
            submission_id,
            snapshot_path,
            track,
            contracts[track.id],
            contract_hashes[track.id],
            evaluator_attempts,
            pricing_data,
            pricing_retrieved_at,
            repetition,
            checkpoint,
            cancel_event,
            agent_slots,
            evaluator_executor,
        )
        jobs.extend(ej)
        artifacts.update(ea)
    else:
        _emit_progress(
            options,
            "build.finished",
            harness=harness.id,
            model=harness.model,
            track=track.id,
            repetition=repetition,
            submission_id=submission_id,
            status="failed",
            duration_s=round(float(impl.process.duration_s), 3),
        )
    min_evals = _MIN_PUB_EVALS if config.mode == "publication" else _MIN_LOCAL_EVALS
    evaluation_success = impl_ok and len(valid_results) >= min_evals
    reasons: list[str] = list(global_ineligible_reasons)
    if not impl_ok:
        reasons.append("implementation_failed")
        if impl.process.timed_out:
            reasons.append("implementation_timeout")
    elif not evaluation_success:
        reasons.append("insufficient_valid_evaluators")
    score, dimensions, judge_spread = (
        _score_results(valid_results, contracts[track.id])
        if evaluation_success
        else (None, {}, None)
    )
    attempt = Attempt(
        run_id=run_id,
        submission_id=submission_id,
        repetition=repetition,
        track=track.id,
        contract_version=contracts[track.id].contract_version,
        contract_sha256=contract_hashes[track.id],
        harness=harness.id,
        model_id=_model_identifier(harness.model),
        display_name=harness.display_name,
        implementation_success=impl_ok,
        evaluation_success=evaluation_success,
        score=score,
        dimensions=dimensions,
        judge_spread=judge_spread,
        implementation_cost_usd=impl.cost_usd,
        evaluation_cost_usd=None if not impl_ok else (eval_cost_total if eval_cost_known else None),
        tokens=_tokens(impl.usage) + eval_tokens,
        duration_s=float(impl.process.duration_s) + eval_duration,
        evaluator_ids=tuple(valid_eval_ids),
        ineligible_reasons=tuple(reasons),
    )
    attempt_path = run_dir / "attempts" / f"{submission_id}.json"
    atomic_write_json(attempt_path, _attempt_public(attempt))
    artifacts[f"attempts/{submission_id}.json"] = sha256_file(attempt_path)
    checkpoint(jobs, artifacts)
    return attempt, jobs, artifacts


def _model_identifier(model: str) -> str:
    return validate_identifier(normalize_model_id(model), field="model_id")


def _run_evaluator(
    config: BenchConfig,
    options: RunOptions,
    run_dir: Path,
    submission_id: str,
    snapshot_path: Path,
    track: Any,
    contract: EvaluationContract,
    contract_hash: str,
    evaluator: Any,
    eval_attempt_id: str,
    pricing_data: Mapping[str, Any] | None,
    pricing_retrieved_at: str | None,
    repetition: int,
    cancel_event: threading.Event,
    agent_slots: threading.BoundedSemaphore,
) -> dict[str, Any]:
    """Evaluate one immutable submission copy and return a normalized outcome.

    Seed and submission trees are copied into a disposable evaluation area and
    hashed before execution. A result is valid only when the process succeeds,
    both evidence trees remain unchanged, and the report and contract-bound
    JSON result are present and valid. Invalid outputs remain recorded with
    explicit reasons and artifact hashes for auditability.
    """

    _emit_progress(
        options,
        "evaluate.started",
        evaluator=evaluator.id,
        model=evaluator.model,
        track=track.id,
        repetition=repetition,
        submission_id=submission_id,
        eval_attempt_id=eval_attempt_id,
    )
    eval_parent = run_dir / "evaluations" / submission_id
    eval_parent.mkdir(parents=True, exist_ok=True)
    eval_root = create_unique_directory(eval_parent / eval_attempt_id)
    seed_dir, submission_dir, output_dir = (
        eval_root / "seed",
        eval_root / "submission",
        eval_root / "output",
    )
    output_dir.mkdir()
    materialize_seed(config, seed_dir)
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
    href = config.harnesses[evaluator.harness]
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
    with agent_slots:
        execution = execute_agent(
            config,
            job,
            pricing_data=pricing_data,
            pricing_retrieved_at=pricing_retrieved_at,
            options=options,
            cancel_event=cancel_event,
        )
    artifacts: dict[str, str] = {}
    invalid: list[str] = []
    valid_result: dict[str, Any] | None = None
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
            errs = validate_judge_result(
                result_data,
                contract,
                expected_track=track.id,
                expected_submission_id=submission_id,
                expected_contract_sha256=contract_hash,
                expected_judge_id=evaluator.id,
            )
            if errs:
                invalid.append("invalid_judge_result")
            elif not invalid:
                valid_result = result_data
    rel = f"evaluations/{submission_id}/{eval_attempt_id}"
    if report_path.is_file():
        artifacts[f"{rel}/output/report.md"] = sha256_file(report_path)
    if result_path.is_file():
        artifacts[f"{rel}/output/result.json"] = sha256_file(result_path)
    _emit_progress(
        options,
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
