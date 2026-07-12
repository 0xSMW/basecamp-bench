"""Reusable validation predicates for file-format boundaries."""

from __future__ import annotations

import math
import re
from typing import TypeGuard

__all__ = ["is_finite_number", "is_sha256_hex"]

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def is_finite_number(value: object) -> TypeGuard[int | float]:
    """Return whether *value* is a finite real ``int`` or ``float`` (bool excluded)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def is_sha256_hex(value: object) -> TypeGuard[str]:
    """Return whether *value* is a lowercase 64-character SHA-256 digest."""
    return isinstance(value, str) and _SHA256_HEX_RE.fullmatch(value) is not None
