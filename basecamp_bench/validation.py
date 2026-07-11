"""Small, reusable validation predicates shared by file-format boundaries.

These helpers deliberately answer only structural questions. Callers retain
ownership of field-specific error messages and any domain constraints layered
on top of the primitive checks.
"""

from __future__ import annotations

import math
import re
from typing import TypeGuard

__all__ = ["is_finite_number", "is_sha256_hex"]

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def is_finite_number(value: object) -> TypeGuard[int | float]:
    """Return whether *value* is a finite real ``int`` or ``float``.

    Booleans are excluded even though ``bool`` subclasses ``int``. Extremely
    large integers that cannot be represented as floats are rejected cleanly.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def is_sha256_hex(value: object) -> TypeGuard[str]:
    """Return whether *value* is a lowercase 64-character SHA-256 digest."""
    return isinstance(value, str) and _SHA256_HEX_RE.fullmatch(value) is not None
