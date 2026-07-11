"""Deterministic human-readable filesystem names for canonical benchmark IDs."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from .safety import validate_identifier

__all__ = ["judge_path_name", "run_path_name", "submission_path_name"]

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_RUN_ID = re.compile(
    r"^(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})t"
    r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})z-(?P<suffix>[a-z0-9]+)$"
)
_OPAQUE_ID = re.compile(r"^id-(?P<suffix>[a-z0-9]{8})$")
_MAX_SEGMENT = 160


def _slug(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    result = _NON_ALNUM.sub("-", value.strip().lower()).strip("-")
    if not result:
        raise ValueError(f"{field} has no filesystem-safe characters")
    return result


def _suffix(identifier: str, length: int) -> str:
    validate_identifier(identifier, field="canonical_id")
    opaque = _OPAQUE_ID.fullmatch(identifier)
    if opaque and length == 8:
        return opaque["suffix"]
    if identifier.isalnum() and len(identifier) <= length:
        return identifier.lower()
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:length]


def _bounded(prefix: str, suffix: str, *, limit: int = _MAX_SEGMENT) -> str:
    ending = f"--{suffix}"
    available = limit - len(ending)
    if available < 1:
        raise ValueError("filesystem name bound is too small for its unique suffix")
    trimmed = prefix[:available].rstrip("-") or "run"
    return f"{trimmed}{ending}"


def _tracks(values: Iterable[str]) -> str:
    selected = {_slug(value, field="track") for value in values}
    if not selected:
        raise ValueError("run path requires at least one track")
    ordered = sorted(selected, key=lambda value: ({"fe": 0, "be": 1}.get(value, 2), value))
    return "-".join(ordered)


def _contestant(provider: str, model: str) -> str:
    return f"{_slug(provider, field='provider')}-{_slug(model.rsplit('/', 1)[-1], field='model')}"


def run_path_name(
    run_id: str,
    *,
    tracks: Iterable[str],
    contestants: Iterable[tuple[str, str]],
) -> str:
    """Return a readable terminal run basename with provider/model identities."""
    validate_identifier(run_id, field="run_id")
    selected = sorted({_contestant(provider, model) for provider, model in contestants})
    if not selected:
        raise ValueError("run path requires at least one contestant")
    contestant_part = "_".join(selected)
    match = _RUN_ID.fullmatch(run_id)
    if match:
        stamp = (
            f"{match['year']}-{match['month']}-{match['day']}T"
            f"{match['hour']}-{match['minute']}-{match['second']}Z"
        )
    else:
        stamp = "run"
    return _bounded(
        f"{stamp}--{_tracks(tracks)}--{contestant_part}",
        _slug(run_id, field="run_id"),
    )


def submission_path_name(
    *,
    track: str,
    harness: str,
    provider: str,
    model: str,
    repetition: int,
    submission_id: str,
) -> str:
    """Return a readable post-judging contestant path with a unique suffix."""
    if isinstance(repetition, bool) or not isinstance(repetition, int) or repetition < 1:
        raise ValueError("repetition must be a positive integer")
    prefix = (
        f"{_slug(track, field='track')}-"
        f"{_slug(harness, field='harness')}-"
        f"{_contestant(provider, model)}-r{repetition}"
    )
    return _bounded(prefix, _suffix(submission_id, 8))


def judge_path_name(
    *,
    harness: str,
    provider: str,
    model: str,
    eval_attempt_id: str,
) -> str:
    """Return a readable post-judging evaluator path with a unique suffix."""
    prefix = f"judge-{_slug(harness, field='evaluator_harness')}-{_contestant(provider, model)}"
    return _bounded(prefix, _suffix(eval_attempt_id, 8))
