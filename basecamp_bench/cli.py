"""Command-line interface for Basecamp Bench."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import BenchConfig, config_to_public_dict, load_config
from .manifest import export_run, verify_run
from .reporting import write_report
from .runner import RunOptions, run_benchmark


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return result


def _add_config_args(parser: argparse.ArgumentParser, *, selections: bool = True) -> None:
    parser.add_argument("--root", type=_path, help="project root (default: current directory)")
    parser.add_argument("--config", type=_path, help="TOML config, relative to project root")
    parser.add_argument("--mode", choices=("local", "publication"))
    if selections:
        parser.add_argument("--harness", action="append", dest="harnesses", metavar="ID")
        parser.add_argument("--track", action="append", dest="tracks", metavar="ID")
    parser.add_argument("--repetitions", type=_positive_int)
    parser.add_argument("--timeout", type=_positive_int, metavar="SECONDS")


def _add_safety_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-unsafe-host-execution",
        action="store_true",
        help="acknowledge configuration that grants full host access",
    )
    parser.add_argument(
        "--confirmed-isolated-environment",
        "--isolated-environment",
        action="store_true",
        dest="confirmed_isolated_environment",
        help="confirm execution is inside a disposable VM/container",
    )
    parser.add_argument(
        "--offline-pricing",
        action="store_true",
        help="forbid network pricing retrieval and use cache/overrides only",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="basecamp-bench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run implementation and evaluation agents")
    _add_config_args(run)
    _add_safety_args(run)

    reevaluate = sub.add_parser("reevaluate", help="re-evaluate immutable prior submissions")
    reevaluate.add_argument("run_dir", type=_path)
    _add_config_args(reevaluate)
    _add_safety_args(reevaluate)

    report = sub.add_parser("report", help="regenerate one offline HTML report")
    report.add_argument(
        "inputs", nargs="+", type=_path, help="leaderboard JSON files or directories"
    )
    report.add_argument("-o", "--output", required=True, type=_path)

    verify = sub.add_parser("verify-run", help="verify a run manifest and artifacts")
    verify.add_argument("run_dir", type=_path)

    export = sub.add_parser("export-run", help="verify and export a portable run ZIP")
    export.add_argument("run_dir", type=_path)
    export.add_argument("output_zip", type=_path)

    show = sub.add_parser("show-config", help="print deterministic effective public config JSON")
    _add_config_args(show)
    return parser


def _root(args: argparse.Namespace) -> Path:
    raw = args.root if args.root is not None else Path.cwd()
    return Path(raw).expanduser().absolute()


def _config_path(args: argparse.Namespace, root: Path) -> Path | None:
    explicit = args.config
    if explicit is not None:
        path = Path(explicit).expanduser()
        return path if path.is_absolute() else root / path
    candidate = root / "bench.toml"
    return candidate if candidate.exists() else None


def _selected(values: list[str] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    result: list[str] = []
    for value in values:
        result.extend(part.strip() for part in value.split(",") if part.strip())
    if not result:
        raise ValueError("selection flags must include at least one ID")
    return tuple(result)


def _load(args: argparse.Namespace) -> BenchConfig:
    root = _root(args)
    return load_config(
        _config_path(args, root),
        root=root,
        mode_override=args.mode,
        selected_harnesses=_selected(getattr(args, "harnesses", None)),
        selected_tracks=_selected(getattr(args, "tracks", None)),
        repetitions_override=args.repetitions,
        timeout_override=args.timeout,
    )


def _options(args: argparse.Namespace) -> RunOptions:
    return RunOptions(
        allow_unsafe_host_execution=args.allow_unsafe_host_execution,
        confirmed_isolated_environment=args.confirmed_isolated_environment,
        allow_network_pricing=not args.offline_pricing,
    )


def _is_leaderboard_json(path: Path) -> bool:
    return path.name.startswith("leaderboard_") and path.suffix.lower() == ".json"


def _discover_leaderboards(inputs: Sequence[Path]) -> list[Path]:
    discovered: list[Path] = []
    seen_inputs: set[Path] = set()
    seen_files: set[Path] = set()
    for supplied in inputs:
        path = Path(supplied).expanduser().absolute()
        resolved = path.resolve(strict=False)
        if resolved in seen_inputs:
            raise ValueError(f"duplicate report input: {supplied}")
        seen_inputs.add(resolved)
        if path.is_symlink():
            raise ValueError(f"report input must not be a symlink: {supplied}")
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = sorted(
                (item for item in path.rglob("leaderboard_*.json") if item.is_file()),
                key=lambda item: item.as_posix(),
            )
        else:
            raise ValueError(f"report input does not exist: {supplied}")
        for candidate in candidates:
            if not _is_leaderboard_json(candidate):
                if path.is_file():
                    raise ValueError(f"not a leaderboard JSON file: {supplied}")
                continue
            if candidate.is_symlink():
                raise ValueError(f"leaderboard JSON must not be a symlink: {candidate}")
            canonical = candidate.resolve()
            if canonical in seen_files:
                raise ValueError(f"duplicate leaderboard JSON: {candidate}")
            seen_files.add(canonical)
            discovered.append(canonical)
    if not discovered:
        raise ValueError("no leaderboard JSON files found")
    return sorted(discovered, key=lambda item: item.as_posix())


def _run(args: argparse.Namespace) -> Path:
    return run_benchmark(_load(args), options=_options(args))


def _reevaluate(args: argparse.Namespace) -> Path:
    from . import runner

    function = getattr(runner, "reevaluate_run", None)
    if function is None:
        raise ValueError("this runner does not provide reevaluate_run")
    return function(_load(args), Path(args.run_dir), options=_options(args))


def _show_config(args: argparse.Namespace) -> None:
    payload = config_to_public_dict(_load(args))
    sys.stdout.write(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            print(os.fspath(_run(args)))
        elif args.command == "reevaluate":
            print(os.fspath(_reevaluate(args)))
        elif args.command == "report":
            paths = _discover_leaderboards(args.inputs)
            print(os.fspath(write_report(paths, args.output)))
        elif args.command == "verify-run":
            errors = verify_run(args.run_dir)
            if errors:
                for error in errors:
                    print(f"error: {error}", file=sys.stderr)
                return 1
            print(f"verified: {args.run_dir}")
        elif args.command == "export-run":
            print(os.fspath(export_run(args.run_dir, args.output_zip)))
        elif args.command == "show-config":
            _show_config(args)
        else:  # pragma: no cover - argparse enforces subcommands
            parser.error(f"unknown command: {args.command}")
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
