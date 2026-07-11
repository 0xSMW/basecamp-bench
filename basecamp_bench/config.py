"""Strict, portable configuration for the benchmark runner."""

from __future__ import annotations

import math
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Any, Literal

from .adapters import registered_harnesses

Mode = Literal["local", "publication"]
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ROOT_KEYS = frozenset(
    {
        "mode",
        "run_root",
        "seed_root",
        "reference_root",
        "reference_manifest",
        "timeout_s",
        "full_access",
        "repetitions",
        "harnesses",
        "evaluators",
        "tracks",
        "pricing",
    }
)
_HARNESS_KEYS = frozenset(
    {"adapter", "model", "effort", "provider_family", "display_name", "binary", "enabled"}
)
_EVALUATOR_KEYS = frozenset({"id", "harness", "model", "effort", "provider_family", "enabled"})
_TRACK_KEYS = frozenset({"prompt", "rubric", "contract"})
_PRICE_KEYS = frozenset({"input", "output", "cache_read", "cache_write"})


@dataclass(frozen=True)
class HarnessSpec:
    id: str
    adapter: str
    model: str
    effort: str
    provider_family: str
    display_name: str
    binary: str | None
    enabled: bool


@dataclass(frozen=True)
class EvaluatorSpec:
    id: str
    harness: str
    model: str
    effort: str
    provider_family: str
    enabled: bool


@dataclass(frozen=True)
class TrackSpec:
    id: str
    prompt_file: Path
    rubric_file: Path
    contract_file: Path


@dataclass(frozen=True)
class BenchConfig:
    root: Path
    mode: Mode
    run_root: Path
    seed_root: Path
    reference_root: Path
    reference_manifest: Path
    timeout_s: int
    full_access: bool
    repetitions: int
    harnesses: Mapping[str, HarnessSpec]
    evaluators: tuple[EvaluatorSpec, ...]
    tracks: Mapping[str, TrackSpec]
    pricing_overrides: Mapping[str, Mapping[str, float]]


def _defaults(root: Path) -> dict[str, Any]:
    harnesses = {
        "codex": HarnessSpec(
            "codex", "codex", "gpt-5.6-sol", "high", "openai", "Codex", None, True
        ),
        "claude": HarnessSpec(
            "claude", "claude", "claude-fable-5", "high", "anthropic", "Claude", None, True
        ),
        "grok": HarnessSpec("grok", "grok", "grok-4.5", "high", "xai", "Grok", None, True),
    }
    evaluators = (EvaluatorSpec("eval-sol", "codex", "gpt-5.6-sol", "high", "openai", True),)
    tracks = {
        track: TrackSpec(
            track,
            root / "benchmarks" / track / "prompt.md",
            root / "benchmarks" / track / "eval.md",
            root / "benchmarks" / track / "contract.json",
        )
        for track in ("fe", "be")
    }
    return {
        "mode": "local",
        "run_root": root / "runs",
        "seed_root": root / "Repo",
        "reference_root": root / "Repo" / "reference",
        "reference_manifest": root / "benchmarks" / "reference-pack.json",
        "timeout_s": 14400,
        "full_access": False,
        "repetitions": 1,
        "harnesses": harnesses,
        "evaluators": evaluators,
        "tracks": tracks,
        "pricing": {},
    }


def _unknown(body: Mapping[str, Any], allowed: frozenset[str], field: str) -> None:
    extra = sorted(set(body) - allowed)
    if extra:
        raise ValueError(f"{field}: unknown key(s): {', '.join(extra)}")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a table")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite nonnegative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be a finite nonnegative number")
    return result


def _safe_id(value: Any, field: str) -> str:
    result = _string(value, field)
    if not _ID_RE.fullmatch(result):
        raise ValueError(f"{field} is not a safe lowercase identifier: {result!r}")
    return result


def _validate_root(value: Path) -> Path:
    raw = Path(value).expanduser().absolute()
    if raw.is_symlink() or not raw.is_dir():
        raise ValueError(f"root must be an existing non-symlink directory: {raw}")
    return raw.resolve()


def _relative(raw: Any, field: str) -> Path:
    value = _string(raw, field)
    path = Path(value)
    if path.is_absolute() or PureWindowsPath(value).is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must be a contained project-relative path")
    return path


def _contained(root: Path, relative: Path, field: str) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{field} contains a symlink: {current}")
    resolved = (root / relative).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field} escapes root") from exc
    return resolved


def _project_path(root: Path, raw: Any, field: str) -> Path:
    return _contained(root, _relative(raw, field), field)


def _input_dir(path: Path, field: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{field} must be an existing non-symlink directory: {path}")


def _input_file(path: Path, field: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{field} must be an existing non-symlink file: {path}")
    try:
        if path.stat().st_size == 0:
            raise ValueError(f"{field} must not be empty: {path}")
    except OSError as exc:
        raise ValueError(f"{field} cannot be read: {path}") from exc


def _config_file(path: Path, root: Path) -> Path:
    raw = Path(path).expanduser().absolute()
    if raw.is_symlink():
        raise ValueError(f"config contains a symlink: {raw}")
    canonical = raw.resolve(strict=False)
    try:
        rel = canonical.relative_to(root)
    except ValueError as exc:
        raise ValueError("config file must be contained beneath root") from exc
    checked = _contained(root, rel, "config")
    _input_file(checked, "config")
    return checked


def _harness(hid: str, body: Any, base: HarnessSpec | None) -> HarnessSpec:
    table = _mapping(body, f"harnesses.{hid}")
    _unknown(table, _HARNESS_KEYS, f"harnesses.{hid}")
    if base is None:
        missing = sorted(
            {"adapter", "model", "effort", "provider_family", "display_name"} - set(table)
        )
        if missing:
            raise ValueError(f"harnesses.{hid}: missing key(s): {', '.join(missing)}")

    def prior(name: str, fallback: Any = None) -> Any:
        return getattr(base, name) if base is not None else fallback

    binary = table.get("binary", prior("binary"))
    if binary is not None:
        binary = _string(binary, f"harnesses.{hid}.binary")
    return HarnessSpec(
        hid,
        _string(table.get("adapter", prior("adapter")), f"harnesses.{hid}.adapter"),
        _string(table.get("model", prior("model")), f"harnesses.{hid}.model"),
        _string(table.get("effort", prior("effort")), f"harnesses.{hid}.effort"),
        _string(
            table.get("provider_family", prior("provider_family")),
            f"harnesses.{hid}.provider_family",
        ),
        _string(table.get("display_name", prior("display_name")), f"harnesses.{hid}.display_name"),
        binary,
        _boolean(table.get("enabled", prior("enabled", True)), f"harnesses.{hid}.enabled"),
    )


def _evaluator(body: Any, index: int) -> EvaluatorSpec:
    field = f"evaluators[{index}]"
    table = _mapping(body, field)
    _unknown(table, _EVALUATOR_KEYS, field)
    missing = sorted(_EVALUATOR_KEYS - set(table))
    if missing:
        raise ValueError(f"{field}: missing key(s): {', '.join(missing)}")
    return EvaluatorSpec(
        _safe_id(table["id"], f"{field}.id"),
        _safe_id(table["harness"], f"{field}.harness"),
        _string(table["model"], f"{field}.model"),
        _string(table["effort"], f"{field}.effort"),
        _string(table["provider_family"], f"{field}.provider_family"),
        _boolean(table["enabled"], f"{field}.enabled"),
    )


def _track(root: Path, tid: str, body: Any, base: TrackSpec | None) -> TrackSpec:
    field = f"tracks.{tid}"
    table = _mapping(body, field)
    _unknown(table, _TRACK_KEYS, field)
    if base is None:
        missing = sorted(_TRACK_KEYS - set(table))
        if missing:
            raise ValueError(f"{field}: missing key(s): {', '.join(missing)}")

    def value(key: str, old: Path | None) -> Path:
        if key in table:
            return _project_path(root, table[key], f"{field}.{key}")
        assert old is not None
        return old

    return TrackSpec(
        tid,
        value("prompt", base.prompt_file if base else None),
        value("rubric", base.rubric_file if base else None),
        value("contract", base.contract_file if base else None),
    )


def _pricing(body: Any) -> dict[str, Mapping[str, float]]:
    table = _mapping(body, "pricing")
    result: dict[str, Mapping[str, float]] = {}
    for raw_id, raw_rates in table.items():
        mid = _safe_id(raw_id, "pricing model id")
        if mid in result:
            raise ValueError(f"duplicate pricing model id: {mid}")
        rates = _mapping(raw_rates, f"pricing.{mid}")
        _unknown(rates, _PRICE_KEYS, f"pricing.{mid}")
        missing = sorted({"input", "output"} - set(rates))
        if missing:
            raise ValueError(f"pricing.{mid}: missing key(s): {', '.join(missing)}")
        input_rate = _number(rates["input"], f"pricing.{mid}.input")
        normalized = {
            "input": input_rate,
            "output": _number(rates["output"], f"pricing.{mid}.output"),
            "cache_read": _number(rates.get("cache_read", input_rate), f"pricing.{mid}.cache_read"),
            "cache_write": _number(
                rates.get("cache_write", input_rate), f"pricing.{mid}.cache_write"
            ),
        }
        result[mid] = MappingProxyType(normalized)
    return result


def _read_toml(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("rb") as handle:
            body = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read config TOML: {exc}") from exc
    _unknown(body, _ROOT_KEYS, "config")
    return body


def _selection(values: Sequence[str], known: Mapping[str, Any], field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise ValueError(f"{field} must be a nonempty sequence")
    result = tuple(_safe_id(value, field) for value in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicate IDs")
    unknown = sorted(set(result) - set(known))
    if unknown:
        raise ValueError(f"{field} contains unknown ID(s): {', '.join(unknown)}")
    return result


def _validate(config: dict[str, Any]) -> None:
    harnesses: Mapping[str, HarnessSpec] = config["harnesses"]
    known_adapters = set(registered_harnesses())
    for spec in harnesses.values():
        if spec.adapter not in known_adapters:
            raise ValueError(f"harness {spec.id!r} uses unknown adapter {spec.adapter!r}")
    if not any(spec.enabled for spec in harnesses.values()):
        raise ValueError("at least one enabled implementation harness is required")
    evaluators: tuple[EvaluatorSpec, ...] = config["evaluators"]
    seen: set[str] = set()
    for evaluator in evaluators:
        if evaluator.id in seen:
            raise ValueError(f"duplicate evaluator id: {evaluator.id}")
        seen.add(evaluator.id)
        if evaluator.harness not in harnesses:
            raise ValueError(
                f"evaluator {evaluator.id!r} references unknown harness {evaluator.harness!r}"
            )
    if not any(evaluator.enabled for evaluator in evaluators):
        raise ValueError("at least one enabled evaluator is required")
    if not config["tracks"]:
        raise ValueError("at least one track is required")


def _validate_files(config: dict[str, Any], root: Path) -> None:
    def checked(path: Path, field: str) -> Path:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{field} escapes root") from exc
        return _contained(root, relative, field)

    run_root = checked(config["run_root"], "run_root")
    if run_root == root:
        raise ValueError("run_root may not equal root")
    _input_dir(checked(config["seed_root"], "seed_root"), "seed_root")
    _input_dir(checked(config["reference_root"], "reference_root"), "reference_root")
    _input_file(checked(config["reference_manifest"], "reference_manifest"), "reference_manifest")
    for tid, track in config["tracks"].items():
        _input_file(checked(track.prompt_file, f"tracks.{tid}.prompt"), f"tracks.{tid}.prompt")
        _input_file(checked(track.rubric_file, f"tracks.{tid}.rubric"), f"tracks.{tid}.rubric")
        _input_file(
            checked(track.contract_file, f"tracks.{tid}.contract"), f"tracks.{tid}.contract"
        )


def load_config(
    path: Path | None = None,
    *,
    root: Path | None = None,
    mode_override: str | None = None,
    selected_harnesses: Sequence[str] | None = None,
    selected_tracks: Sequence[str] | None = None,
    repetitions_override: int | None = None,
    timeout_override: int | None = None,
) -> BenchConfig:
    """Load defaults, strict TOML, then explicit overrides and selections."""
    if root is None:
        root = (
            Path(path).expanduser().absolute().parent
            if path is not None
            else Path(__file__).resolve().parent.parent
        )
    root = _validate_root(Path(root))
    data = _defaults(root)
    body: Mapping[str, Any] = {}
    if path is not None:
        body = _read_toml(_config_file(Path(path), root))

    if "mode" in body:
        data["mode"] = _string(body["mode"], "mode")
    if data["mode"] not in ("local", "publication"):
        raise ValueError("mode must be 'local' or 'publication'")
    for key in ("run_root", "seed_root", "reference_root", "reference_manifest"):
        if key in body:
            data[key] = _project_path(root, body[key], key)
    if "timeout_s" in body:
        data["timeout_s"] = _positive_int(body["timeout_s"], "timeout_s")
    if "repetitions" in body:
        data["repetitions"] = _positive_int(body["repetitions"], "repetitions")
    if "full_access" in body:
        data["full_access"] = _boolean(body["full_access"], "full_access")

    if "harnesses" in body:
        tables = _mapping(body["harnesses"], "harnesses")
        for raw_id, table in tables.items():
            hid = _safe_id(raw_id, "harness id")
            data["harnesses"][hid] = _harness(hid, table, data["harnesses"].get(hid))
    if "evaluators" in body:
        raw = body["evaluators"]
        if not isinstance(raw, list):
            raise ValueError("evaluators must be an array of tables")
        data["evaluators"] = tuple(_evaluator(item, index) for index, item in enumerate(raw))
    if "tracks" in body:
        tables = _mapping(body["tracks"], "tracks")
        for raw_id, table in tables.items():
            tid = _safe_id(raw_id, "track id")
            data["tracks"][tid] = _track(root, tid, table, data["tracks"].get(tid))
    if "pricing" in body:
        data["pricing"] = _pricing(body["pricing"])

    if mode_override is not None:
        data["mode"] = _string(mode_override, "mode_override")
        if data["mode"] not in ("local", "publication"):
            raise ValueError("mode_override must be 'local' or 'publication'")
    if repetitions_override is not None:
        data["repetitions"] = _positive_int(repetitions_override, "repetitions_override")
    if timeout_override is not None:
        data["timeout_s"] = _positive_int(timeout_override, "timeout_override")

    if selected_harnesses is not None:
        selected = set(_selection(selected_harnesses, data["harnesses"], "selected_harnesses"))
        for hid in selected:
            if not data["harnesses"][hid].enabled:
                raise ValueError(f"selected harness {hid!r} is disabled by configuration")
        data["harnesses"] = {
            hid: replace(spec, enabled=hid in selected) for hid, spec in data["harnesses"].items()
        }
    if selected_tracks is not None:
        selected_track_ids = _selection(selected_tracks, data["tracks"], "selected_tracks")
        data["tracks"] = {tid: data["tracks"][tid] for tid in selected_track_ids}

    _validate(data)
    _validate_files(data, root)
    pricing = MappingProxyType(dict(sorted(data["pricing"].items())))
    return BenchConfig(
        root,
        data["mode"],
        data["run_root"],
        data["seed_root"],
        data["reference_root"],
        data["reference_manifest"],
        data["timeout_s"],
        data["full_access"],
        data["repetitions"],
        MappingProxyType(dict(sorted(data["harnesses"].items()))),
        tuple(data["evaluators"]),
        MappingProxyType(dict(sorted(data["tracks"].items()))),
        pricing,
    )


def _public_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"public path escapes root: {path}") from exc


def config_to_public_dict(config: BenchConfig) -> dict[str, Any]:
    """Return deterministic JSON-compatible effective configuration."""
    harnesses = {
        hid: {
            "adapter": spec.adapter,
            "model": spec.model,
            "effort": spec.effort,
            "provider_family": spec.provider_family,
            "display_name": spec.display_name,
            "enabled": spec.enabled,
        }
        for hid, spec in sorted(config.harnesses.items())
    }
    evaluators = [
        {
            "id": spec.id,
            "harness": spec.harness,
            "model": spec.model,
            "effort": spec.effort,
            "provider_family": spec.provider_family,
            "enabled": spec.enabled,
        }
        for spec in sorted(config.evaluators, key=lambda item: item.id)
    ]
    tracks = {
        tid: {
            "prompt": _public_path(spec.prompt_file, config.root),
            "rubric": _public_path(spec.rubric_file, config.root),
            "contract": _public_path(spec.contract_file, config.root),
        }
        for tid, spec in sorted(config.tracks.items())
    }
    pricing = {
        mid: dict(sorted(rates.items())) for mid, rates in sorted(config.pricing_overrides.items())
    }
    return {
        "mode": config.mode,
        "run_root": _public_path(config.run_root, config.root),
        "seed_root": _public_path(config.seed_root, config.root),
        "reference_root": _public_path(config.reference_root, config.root),
        "reference_manifest": _public_path(config.reference_manifest, config.root),
        "timeout_s": config.timeout_s,
        "full_access": config.full_access,
        "repetitions": config.repetitions,
        "harnesses": harnesses,
        "evaluators": evaluators,
        "tracks": tracks,
        "pricing": pricing,
    }


__all__ = [
    "HarnessSpec",
    "EvaluatorSpec",
    "TrackSpec",
    "BenchConfig",
    "load_config",
    "config_to_public_dict",
]
