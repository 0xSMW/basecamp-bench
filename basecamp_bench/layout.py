"""Post-judging conversion from opaque working paths to readable run paths."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .manifest import write_manifest
from .naming import judge_path_name, run_path_name, submission_path_name
from .safety import atomic_write_json, sha256_file

__all__ = [
    "finalize_readable_layout",
    "recover_pending_layouts",
    "validate_planned_layout",
    "verify_readable_layout",
]

_JOURNAL = "layout-finalization.json"
_LOCK = ".layout-finalization.lock"


@contextmanager
def _layout_lock(run_root: Path) -> Iterator[None]:
    root = Path(run_root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("terminal layout run root must be a real directory")
    path = root / _LOCK
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError("terminal layout lock file is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def validate_planned_layout(
    *,
    run_root: Path,
    run_id: str,
    submissions: Sequence[tuple[str, str, str, str, str, int]],
    judges: Sequence[tuple[str, str, str, str, str]],
) -> Path:
    """Validate every terminal path before any paid provider process starts.

    Submission tuples contain canonical ID, track, harness, provider, model,
    and repetition. Judge tuples contain submission ID, evaluation-attempt ID,
    evaluator harness, provider, and model. The full canonical run ID remains
    in the terminal basename, so a pre-existing opaque run reservation also
    prevents cross-process terminal-name collisions.
    """
    if not submissions:
        raise ValueError("terminal layout requires at least one submission")
    submission_names: dict[str, str] = {}
    tracks: list[str] = []
    contestants: list[tuple[str, str]] = []
    for submission_id, track, harness, provider, model, repetition in submissions:
        if submission_id in submission_names:
            raise ValueError(f"duplicate terminal submission ID: {submission_id}")
        submission_names[submission_id] = submission_path_name(
            track=track,
            harness=harness,
            provider=provider,
            model=model,
            repetition=repetition,
            submission_id=submission_id,
        )
        tracks.append(track)
        contestants.append((provider, model))
    if len(set(submission_names.values())) != len(submission_names):
        raise ValueError("terminal submission paths collide")

    judge_paths: set[tuple[str, str]] = set()
    for submission_id, eval_attempt_id, harness, provider, model in judges:
        if submission_id not in submission_names:
            raise ValueError(f"terminal judge references unknown submission: {submission_id}")
        key = (
            submission_names[submission_id],
            judge_path_name(
                harness=harness,
                provider=provider,
                model=model,
                eval_attempt_id=eval_attempt_id,
            ),
        )
        if key in judge_paths:
            raise ValueError("terminal evaluator paths collide")
        judge_paths.add(key)

    final_dir = Path(run_root) / run_path_name(
        run_id,
        tracks=tracks,
        contestants=contestants,
    )
    if final_dir.exists() or final_dir.is_symlink():
        raise ValueError(f"terminal run path already exists: {final_dir.name}")
    return final_dir


def _submission_names(
    attempts: Sequence[Any], provider_by_harness: Mapping[str, str]
) -> dict[str, str]:
    names: dict[str, str] = {}
    for attempt in attempts:
        try:
            provider = provider_by_harness[attempt.harness]
        except KeyError as exc:
            raise ValueError(
                f"terminal layout cannot resolve contestant provider: {attempt.harness}"
            ) from exc
        names[attempt.submission_id] = submission_path_name(
            track=attempt.track,
            harness=attempt.harness,
            provider=provider,
            model=attempt.model_id,
            repetition=attempt.repetition,
            submission_id=attempt.submission_id,
        )
    if len(names) != len(attempts):
        raise ValueError("terminal layout requires unique submission IDs")
    return names


def _judge_names(
    jobs: Sequence[Mapping[str, Any]], evaluator_specs: Mapping[str, tuple[str, str, str]]
) -> dict[tuple[str, str], str]:
    names: dict[tuple[str, str], str] = {}
    for job in jobs:
        if job.get("kind") != "evaluate" or job.get("skipped") is True:
            continue
        submission_id = job.get("submission_id")
        eval_attempt_id = job.get("eval_attempt_id")
        evaluator_id = job.get("evaluator_id")
        if not all(
            isinstance(value, str) and value
            for value in (submission_id, eval_attempt_id, evaluator_id)
        ):
            raise ValueError("terminal layout found incomplete evaluator path provenance")
        assert isinstance(submission_id, str)
        assert isinstance(eval_attempt_id, str)
        assert isinstance(evaluator_id, str)
        try:
            harness, provider, model = evaluator_specs[evaluator_id]
        except KeyError as exc:
            raise ValueError(f"terminal layout cannot resolve evaluator: {evaluator_id}") from exc
        key = (submission_id, eval_attempt_id)
        value = judge_path_name(
            harness=harness,
            provider=provider,
            model=model,
            eval_attempt_id=eval_attempt_id,
        )
        if key in names and names[key] != value:
            raise ValueError("terminal layout found conflicting evaluator path provenance")
        names[key] = value
    return names


def _moves(
    run_dir: Path,
    submission_names: Mapping[str, str],
    judge_names: Mapping[tuple[str, str], str],
) -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []

    def optional(source: Path, target: Path) -> None:
        if source.exists() or source.is_symlink():
            moves.append((source, target))

    for submission_id, readable in sorted(submission_names.items()):
        for root in ("workspaces", "snapshots", "evaluations"):
            optional(run_dir / root / submission_id, run_dir / root / readable)
        optional(
            run_dir / "attempts" / f"{submission_id}.json",
            run_dir / "attempts" / f"{readable}.json",
        )
        optional(
            run_dir / "prompts" / f"implement-{submission_id}.md",
            run_dir / "prompts" / f"implement--{readable}.md",
        )
        optional(
            run_dir / "logs" / f"implement-{submission_id}.log",
            run_dir / "logs" / f"implement--{readable}.log",
        )
        optional(
            run_dir / "logs" / f"implement-{submission_id}.log.stderr",
            run_dir / "logs" / f"implement--{readable}.log.stderr",
        )
        optional(
            run_dir / "private" / f"implement-{submission_id}.last.md",
            run_dir / "private" / f"implement--{readable}.last.md",
        )
        pi_state = run_dir / "logs" / f".implement-{submission_id}.log.pi-state"
        optional(
            pi_state,
            run_dir / "logs" / f".implement--{readable}.log.pi-state",
        )
    # Evaluation parents move before their children. This makes every journal
    # step independently recoverable: after a crash, child paths are always
    # interpreted beneath the parent's readable destination.
    for (submission_id, eval_attempt_id), judge_name in sorted(judge_names.items()):
        readable_parent = run_dir / "evaluations" / submission_names[submission_id]
        opaque_child = run_dir / "evaluations" / submission_id / eval_attempt_id
        if opaque_child.exists() or opaque_child.is_symlink():
            moves.append((readable_parent / eval_attempt_id, readable_parent / judge_name))
        log_stem = f"evaluate-{submission_id}-{eval_attempt_id}"
        readable_log_stem = f"evaluate--{submission_id.rsplit('-', 1)[-1]}--{judge_name}"
        for suffix in (".log", ".log.stderr"):
            optional(
                run_dir / "logs" / f"{log_stem}{suffix}",
                run_dir / "logs" / f"{readable_log_stem}{suffix}",
            )
    return moves


def _validate_moves(run_dir: Path, moves: Sequence[tuple[Path, Path]]) -> None:
    destinations = [target for _, target in moves]
    if len(destinations) != len(set(destinations)):
        raise ValueError("terminal layout contains duplicate destination paths")
    for source, target in moves:
        if source.is_symlink() or target.is_symlink():
            raise ValueError("terminal layout refuses symlink paths")
        for parent in (source.parent, target.parent):
            current = parent
            while current != run_dir:
                if run_dir not in current.parents:
                    raise ValueError("terminal layout path escapes the run directory")
                if current.is_symlink():
                    raise ValueError("terminal layout refuses symlink parent paths")
                current = current.parent
        if target.exists():
            raise ValueError(f"terminal layout destination already exists: {target.name}")


def _apply_moves(moves: Sequence[tuple[Path, Path]]) -> None:
    for source, target in moves:
        source_exists = source.exists() or source.is_symlink()
        target_exists = target.exists() or target.is_symlink()
        if source_exists and target_exists:
            raise ValueError(f"terminal layout source and destination both exist: {target.name}")
        if source_exists:
            source.rename(target)
        elif not target_exists:
            raise ValueError(f"terminal layout source and destination are missing: {source.name}")


def _readable_artifacts(
    artifacts: Mapping[str, str],
    submission_names: Mapping[str, str],
    judge_names: Mapping[tuple[str, str], str],
) -> dict[str, str]:
    rewritten: dict[str, str] = {}
    for path, digest in artifacts.items():
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "snapshots" and parts[1] in submission_names:
            parts[1] = submission_names[parts[1]]
        elif len(parts) == 2 and parts[0] == "attempts" and parts[1].endswith(".json"):
            submission_id = parts[1][:-5]
            if submission_id in submission_names:
                parts[1] = f"{submission_names[submission_id]}.json"
        elif len(parts) >= 3 and parts[0] == "evaluations":
            submission_id, eval_attempt_id = parts[1], parts[2]
            key = (submission_id, eval_attempt_id)
            if submission_id in submission_names and key in judge_names:
                parts[1] = submission_names[submission_id]
                parts[2] = judge_names[key]
        rewritten_path = "/".join(parts)
        if rewritten_path in rewritten:
            raise ValueError(f"terminal layout artifact collision: {rewritten_path}")
        rewritten[rewritten_path] = digest
    return rewritten


def _validate_artifacts(run_dir: Path, artifacts: Mapping[str, str]) -> None:
    for relative, expected in artifacts.items():
        path = run_dir.joinpath(*relative.split("/"))
        current = path
        while current != run_dir:
            if current.is_symlink():
                raise ValueError(f"terminal layout artifact is a symlink: {relative}")
            current = current.parent
        if not path.is_file():
            raise ValueError(f"terminal layout artifact is missing: {relative}")
        if sha256_file(path) != expected:
            raise ValueError(f"terminal layout artifact hash mismatch: {relative}")


def _json_digest(value: Mapping[str, Any]) -> str:
    payload = (json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _journal_path(run_dir: Path) -> Path:
    return run_dir / "private" / _JOURNAL


def _relative_moves(run_dir: Path, moves: Sequence[tuple[Path, Path]]) -> list[list[str]]:
    return [
        [source.relative_to(run_dir).as_posix(), target.relative_to(run_dir).as_posix()]
        for source, target in moves
    ]


def _recover_layout(run_dir: Path, journal: Mapping[str, Any]) -> Path:
    final_name = journal.get("final_name")
    moves = journal.get("moves")
    layout = journal.get("layout")
    manifest = journal.get("manifest")
    if (
        not isinstance(final_name, str)
        or not isinstance(moves, list)
        or not isinstance(layout, dict)
        or not isinstance(manifest, dict)
    ):
        raise ValueError("terminal layout journal is malformed")
    root = run_dir
    absolute_moves: list[tuple[Path, Path]] = []
    for move in moves:
        if (
            not isinstance(move, list)
            or len(move) != 2
            or not all(isinstance(value, str) and value for value in move)
        ):
            raise ValueError("terminal layout journal move is malformed")
        source = root.joinpath(*move[0].split("/"))
        target = root.joinpath(*move[1].split("/"))
        if root not in source.parents or root not in target.parents:
            raise ValueError("terminal layout journal path escapes the run directory")
        absolute_moves.append((source, target))
    _apply_moves(absolute_moves)
    atomic_write_json(root / "layout.json", layout)
    write_manifest(root / "run-manifest.json", manifest)
    final_dir = root.parent / final_name
    if root.name != final_name:
        if final_dir.exists() or final_dir.is_symlink():
            raise ValueError(f"terminal run path already exists: {final_name}")
        root.rename(final_dir)
        root = final_dir
    journal_path = _journal_path(root)
    if journal_path.is_file() and not journal_path.is_symlink():
        journal_path.unlink()
    return root


def recover_pending_layouts(run_root: Path) -> list[Path]:
    """Roll forward any durable terminal-layout journals beneath ``run_root``."""
    root = Path(run_root)
    if not root.is_dir() or root.is_symlink():
        return []
    recovered: list[Path] = []
    with _layout_lock(root):
        for candidate in sorted(root.iterdir(), key=lambda path: path.name):
            journal_path = _journal_path(candidate)
            if not candidate.is_dir() or candidate.is_symlink() or not journal_path.is_file():
                continue
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"terminal layout journal is unreadable: {candidate.name}"
                ) from exc
            if not isinstance(journal, dict):
                raise ValueError(f"terminal layout journal is malformed: {candidate.name}")
            recovered.append(_recover_layout(candidate, journal))
    return recovered


def verify_readable_layout(run_dir: Path, manifest: Mapping[str, Any]) -> list[str]:
    """Return terminal path/identity errors for a completed benchmark run."""
    if manifest.get("status") not in {"complete", "ineligible"}:
        return []
    artifacts = manifest.get("artifacts")
    run = manifest.get("run")
    config = manifest.get("config")
    jobs = manifest.get("jobs")
    if not (
        isinstance(artifacts, dict)
        and isinstance(run, dict)
        and isinstance(config, dict)
        and isinstance(jobs, list)
    ):
        return []
    benchmark_roots = {str(path).split("/", 1)[0] for path in artifacts}
    if "layout.json" not in artifacts:
        return (
            ["complete benchmark run is missing layout.json"]
            if benchmark_roots & {"attempts", "snapshots", "evaluations"}
            else []
        )
    run_id = run.get("id")
    harnesses = config.get("harnesses")
    evaluators = config.get("evaluators")
    if (
        not isinstance(run_id, str)
        or not isinstance(harnesses, dict)
        or not isinstance(evaluators, list)
    ):
        return ["terminal layout cannot resolve manifest identities"]

    errors: list[str] = []
    submissions: dict[str, str] = {}
    tracks: set[str] = set()
    contestants: set[tuple[str, str]] = set()
    for job in jobs:
        if not isinstance(job, dict) or job.get("kind") != "implement":
            continue
        submission_id = job.get("submission_id")
        harness = job.get("harness")
        track = job.get("track")
        repetition = job.get("repetition")
        spec = harnesses.get(harness) if isinstance(harness, str) else None
        if not (
            isinstance(submission_id, str)
            and isinstance(harness, str)
            and isinstance(track, str)
            and isinstance(repetition, int)
            and not isinstance(repetition, bool)
            and repetition > 0
            and isinstance(spec, dict)
            and isinstance(spec.get("provider_family"), str)
            and isinstance(spec.get("model"), str)
        ):
            errors.append("terminal layout has incomplete implementation identity")
            continue
        provider, model = spec["provider_family"], spec["model"]
        submissions[submission_id] = submission_path_name(
            track=track,
            harness=harness,
            provider=provider,
            model=model,
            repetition=repetition,
            submission_id=submission_id,
        )
        tracks.add(track)
        contestants.add((provider, model))

    evaluator_by_id = {
        item.get("id"): item
        for item in evaluators
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    judge_names: dict[tuple[str, str], str] = {}
    for job in jobs:
        if not isinstance(job, dict) or job.get("kind") != "evaluate" or job.get("skipped") is True:
            continue
        submission_id = job.get("submission_id")
        eval_attempt_id = job.get("eval_attempt_id")
        evaluator = evaluator_by_id.get(job.get("evaluator_id"))
        if not (
            isinstance(submission_id, str)
            and isinstance(eval_attempt_id, str)
            and isinstance(evaluator, dict)
            and isinstance(evaluator.get("harness"), str)
            and isinstance(evaluator.get("provider_family"), str)
            and isinstance(evaluator.get("model"), str)
        ):
            errors.append("terminal layout has incomplete evaluator identity")
            continue
        judge_names[(submission_id, eval_attempt_id)] = judge_path_name(
            harness=evaluator["harness"],
            provider=evaluator["provider_family"],
            model=evaluator["model"],
            eval_attempt_id=eval_attempt_id,
        )

    if not submissions:
        errors.append("terminal layout has no implementation identities")
        return errors
    expected_run_name = run_path_name(
        run_id,
        tracks=tracks,
        contestants=contestants,
    )
    if Path(run_dir).name != expected_run_name:
        errors.append(
            f"run directory name does not match terminal identity: expected {expected_run_name}"
        )
    expected_layout = {
        "schema_version": "1.0",
        "run_id": run_id,
        "run_path": expected_run_name,
        "submissions": dict(sorted(submissions.items())),
        "evaluations": {
            f"{submission_id}:{eval_attempt_id}": name
            for (submission_id, eval_attempt_id), name in sorted(judge_names.items())
        },
    }
    try:
        actual_layout = json.loads((Path(run_dir) / "layout.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        errors.append("layout.json is unreadable")
    else:
        if actual_layout != expected_layout:
            errors.append("layout.json does not match canonical manifest identities")

    expected_attempts = {f"attempts/{name}.json" for name in submissions.values()}
    submission_by_name = {name: submission_id for submission_id, name in submissions.items()}
    for relative in artifacts:
        if not isinstance(relative, str):
            continue
        parts = relative.split("/")
        if parts[0] == "attempts" and relative not in expected_attempts:
            errors.append(f"attempt artifact path does not match terminal identity: {relative}")
        elif len(parts) >= 2 and parts[0] == "snapshots" and parts[1] not in submission_by_name:
            errors.append(f"snapshot artifact path does not match terminal identity: {relative}")
        elif len(parts) >= 3 and parts[0] == "evaluations":
            submission_id = submission_by_name.get(parts[1])
            valid_children = {
                name for (sid, _), name in judge_names.items() if sid == submission_id
            }
            if submission_id is None or parts[2] not in valid_children:
                errors.append(
                    f"evaluation artifact path does not match terminal identity: {relative}"
                )
    return sorted(set(errors))


def finalize_readable_layout(
    *,
    run_dir: Path,
    run_id: str,
    attempts: Sequence[Any],
    jobs: Sequence[Mapping[str, Any]],
    provider_by_harness: Mapping[str, str],
    evaluator_specs: Mapping[str, tuple[str, str, str]],
    artifacts: dict[str, str],
    manifest_factory: Callable[[dict[str, str]], dict[str, Any]],
) -> Path:
    """Durably roll a fully judged run forward to its readable terminal layout."""
    submission_names = _submission_names(attempts, provider_by_harness)
    judge_names = _judge_names(jobs, evaluator_specs)
    final_dir = run_dir.parent / run_path_name(
        run_id,
        tracks=(attempt.track for attempt in attempts),
        contestants=(
            (provider_by_harness[attempt.harness], attempt.model_id) for attempt in attempts
        ),
    )
    if final_dir.exists() or final_dir.is_symlink():
        raise ValueError(f"terminal run path already exists: {final_dir.name}")
    moves = _moves(run_dir, submission_names, judge_names)
    rewritten = _readable_artifacts(artifacts, submission_names, judge_names)
    _validate_artifacts(run_dir, artifacts)
    _validate_moves(run_dir, moves)
    layout_payload: dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "run_path": final_dir.name,
        "submissions": dict(sorted(submission_names.items())),
        "evaluations": {
            f"{submission_id}:{eval_attempt_id}": name
            for (submission_id, eval_attempt_id), name in sorted(judge_names.items())
        },
    }
    rewritten["layout.json"] = _json_digest(layout_payload)
    terminal_manifest = manifest_factory(rewritten)
    journal = {
        "schema_version": "1.0",
        "final_name": final_dir.name,
        "moves": _relative_moves(run_dir, moves),
        "layout": layout_payload,
        "manifest": terminal_manifest,
    }
    with _layout_lock(run_dir.parent):
        atomic_write_json(_journal_path(run_dir), journal)
        completed = _recover_layout(run_dir, journal)
    _validate_artifacts(completed, rewritten)
    artifacts.clear()
    artifacts.update(rewritten)
    return completed
