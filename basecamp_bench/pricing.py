"""Exact model pricing lookup and cost accounting.

Provides immutable rate records, exact (non-fuzzy) model ID lookup against
models.dev-shaped data and config overrides, cost arithmetic over disjoint
token buckets, and cached snapshot loading with offline/stale fallback and
atomic cache writes.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "PricingRates",
    "PricingLookup",
    "normalize_model_id",
    "find_exact_rates",
    "compute_cost",
    "load_pricing_snapshot",
    "PREFERRED_PROVIDERS",
]

PREFERRED_PROVIDERS: tuple[str, ...] = (
    "anthropic",
    "openai",
    "xai",
    "google",
    "mistral",
    "deepseek",
)

_DEFAULT_CURRENCY = "USD"
_SOURCE_OVERRIDE = "override"
_SOURCE_MODELS_DEV = "https://models.dev/api.json"
_TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class PricingRates:
    """Per-million-token USD rates with provenance for one model match."""

    input_usd_per_m: float
    output_usd_per_m: float
    cache_read_usd_per_m: float
    cache_write_usd_per_m: float
    currency: str
    source: str
    retrieved_at: str | None
    provider: str | None
    model_id: str
    match_kind: Literal["override", "exact"]


@dataclass(frozen=True)
class PricingLookup:
    """Result of an exact pricing lookup: rates, error, and staleness."""

    rates: PricingRates | None
    error: str | None
    stale: bool


def normalize_model_id(value: str) -> str:
    """Return a deterministic normalized model identifier.

    Normalization is lowercase, strip surrounding whitespace, and replace
    internal spaces with hyphens. Rejects non-strings (including bool) and
    values that normalize to empty.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise TypeError(f"model_id must be a str, got {type(value).__name__}")
    normalized = value.lower().strip().replace(" ", "-")
    if not normalized:
        raise ValueError("model_id must not be empty after normalization")
    return normalized


def find_exact_rates(
    model_id: str,
    pricing_data: Mapping[str, Any] | None,
    overrides: Mapping[str, Any] | None,
    retrieved_at: str | None = None,
) -> PricingLookup:
    """Look up per-million rates by exact normalized model ID only.

    Overrides win only on an exact normalized model ID. Pricing data is
    parsed as models.dev-shaped provider maps; never fuzzy-matched. When the
    same model ID appears under multiple providers, prefer
    :data:`PREFERRED_PROVIDERS` order, then lexicographic provider id for
    unlisted providers. Malformed rates produce an error lookup rather than
    fabricated values.
    """
    try:
        want = normalize_model_id(model_id)
    except (TypeError, ValueError) as exc:
        return PricingLookup(rates=None, error=str(exc), stale=False)

    if overrides is not None and not isinstance(overrides, Mapping):
        return PricingLookup(
            rates=None,
            error="overrides must be a mapping or None",
            stale=False,
        )

    if overrides:
        override_hit = _find_override(want, overrides, retrieved_at=retrieved_at)
        if override_hit is not None:
            return override_hit

    if pricing_data is None:
        return PricingLookup(
            rates=None,
            error=f"no exact pricing for model_id {want!r}",
            stale=False,
        )
    if not isinstance(pricing_data, Mapping):
        return PricingLookup(
            rates=None,
            error="pricing_data must be a mapping or None",
            stale=False,
        )

    return _find_exact_in_pricing_data(want, pricing_data, retrieved_at=retrieved_at)


def compute_cost(usage: Any, rates: PricingRates) -> float:
    """Compute USD cost from usage token counts and per-million rates.

    ``usage`` must expose ``input_tokens``, ``cached_input_tokens``,
    ``cache_write_tokens``, and ``output_tokens``. Token counts must be
    nonnegative integers (bool rejected). The four usage fields are disjoint
    buckets, matching the runner's normalized ``Usage`` contract.
    """
    if not isinstance(rates, PricingRates):
        raise TypeError(f"rates must be PricingRates, got {type(rates).__name__}")

    input_tokens = _require_nonneg_int(getattr(usage, "input_tokens", None), "input_tokens")
    cached_input_tokens = _require_nonneg_int(
        getattr(usage, "cached_input_tokens", None), "cached_input_tokens"
    )
    cache_write_tokens = _require_nonneg_int(
        getattr(usage, "cache_write_tokens", None), "cache_write_tokens"
    )
    output_tokens = _require_nonneg_int(getattr(usage, "output_tokens", None), "output_tokens")

    for name, value in (
        ("input_usd_per_m", rates.input_usd_per_m),
        ("output_usd_per_m", rates.output_usd_per_m),
        ("cache_read_usd_per_m", rates.cache_read_usd_per_m),
        ("cache_write_usd_per_m", rates.cache_write_usd_per_m),
    ):
        if not _is_nonneg_finite_number(value):
            raise ValueError(f"rates.{name} must be a finite nonnegative number")

    total = (
        input_tokens * rates.input_usd_per_m
        + cached_input_tokens * rates.cache_read_usd_per_m
        + cache_write_tokens * rates.cache_write_usd_per_m
        + output_tokens * rates.output_usd_per_m
    ) / _TOKENS_PER_MILLION
    if not math.isfinite(total):
        raise ValueError("computed cost is not finite")
    return float(total)


def load_pricing_snapshot(
    cache_path: Path,
    url: str,
    max_age_s: int,
    allow_network: bool = True,
    timeout_s: float = 20,
) -> tuple[dict | None, dict]:
    """Load models.dev-like pricing JSON from a local cache and/or the network.

    Prefer a fresh, valid cache file. When the cache is missing, stale, or
    malformed, optionally fetch ``url`` (urllib only) with ``timeout_s``.
    Successful network payloads are written atomically (same-directory temp
    file + :func:`os.replace`). A valid stale cache is used as a fallback when
    the network is disabled, fails, or returns unusable data. Never raises for
    expected offline/malformed conditions; errors are reported in provenance.

    Returns ``(data_or_None, provenance)`` where provenance always includes
    ``source``, ``url``, ``cache_path``, ``retrieved_at``, ``stale``, and
    ``error``.
    """
    if not isinstance(cache_path, Path):
        cache_path = Path(cache_path)
    if not isinstance(url, str) or not url:
        return None, _provenance(
            source=None,
            url=url if isinstance(url, str) else "",
            cache_path=cache_path,
            retrieved_at=None,
            stale=False,
            error="url must be a nonempty string",
        )
    if isinstance(max_age_s, bool) or not isinstance(max_age_s, int) or max_age_s < 0:
        return None, _provenance(
            source=None,
            url=url,
            cache_path=cache_path,
            retrieved_at=None,
            stale=False,
            error="max_age_s must be a nonnegative int",
        )
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        return None, _provenance(
            source=None,
            url=url,
            cache_path=cache_path,
            retrieved_at=None,
            stale=False,
            error="timeout_s must be a finite positive number",
        )
    timeout_f = float(timeout_s)
    if not math.isfinite(timeout_f) or timeout_f <= 0:
        return None, _provenance(
            source=None,
            url=url,
            cache_path=cache_path,
            retrieved_at=None,
            stale=False,
            error="timeout_s must be a finite positive number",
        )
    if not isinstance(allow_network, bool):
        return None, _provenance(
            source=None,
            url=url,
            cache_path=cache_path,
            retrieved_at=None,
            stale=False,
            error="allow_network must be a bool",
        )

    cache_payload, cache_meta = _read_cache_file(cache_path)
    age_s = cache_meta.get("age_s")
    cache_is_fresh = (
        cache_payload is not None and isinstance(age_s, (int, float)) and age_s <= max_age_s
    )

    if cache_is_fresh:
        return cache_payload, _provenance(
            source="cache",
            url=url,
            cache_path=cache_path,
            retrieved_at=cache_meta.get("retrieved_at"),
            stale=False,
            error=None,
            from_cache=True,
            from_network=False,
            cache_age_s=age_s,
        )

    network_error: str | None = None
    if allow_network:
        net_data, net_raw, network_error = _fetch_pricing_url(url, timeout_f)
        if net_data is not None and net_raw is not None:
            write_error = _atomic_write_text(cache_path, net_raw)
            return net_data, _provenance(
                source="network",
                url=url,
                cache_path=cache_path,
                retrieved_at=_utc_now(),
                stale=False,
                error=write_error,
                from_cache=False,
                from_network=True,
                cache_written=write_error is None,
            )

    # Offline / failure fallback: any still-valid cached object.
    if cache_payload is not None:
        return cache_payload, _provenance(
            source="cache",
            url=url,
            cache_path=cache_path,
            retrieved_at=cache_meta.get("retrieved_at"),
            stale=True,
            error=network_error,
            from_cache=True,
            from_network=False,
            cache_age_s=age_s,
            network_error=network_error,
        )

    parts: list[str] = []
    if cache_meta.get("error"):
        parts.append(str(cache_meta["error"]))
    if network_error:
        parts.append(network_error)
    if not allow_network and not parts:
        parts.append("network disabled and no usable cache")
    if not parts:
        parts.append("pricing unavailable")
    return None, _provenance(
        source=None,
        url=url,
        cache_path=cache_path,
        retrieved_at=None,
        stale=False,
        error="; ".join(parts),
        from_cache=False,
        from_network=bool(allow_network),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mtime_iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _provenance(
    *,
    source: str | None,
    url: str,
    cache_path: Path,
    retrieved_at: str | None,
    stale: bool,
    error: str | None,
    **extra: Any,
) -> dict[str, Any]:
    prov: dict[str, Any] = {
        "source": source,
        "url": url,
        "cache_path": cache_path.name,
        "retrieved_at": retrieved_at,
        "stale": bool(stale),
        "error": error,
    }
    for key, value in extra.items():
        prov[key] = value
    return prov


def _is_nonneg_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value):
        return False
    return float(value) >= 0.0


def _require_nonneg_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a nonnegative int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field} must be nonnegative, got {value}")
    return value


def _parse_rate_fields(
    cost: Mapping[str, Any],
) -> tuple[float, float, float, float] | str:
    """Return (input, output, cache_read, cache_write) or an error string."""
    if "input" not in cost or "output" not in cost:
        return "cost must include numeric input and output rates"
    input_rate = cost["input"]
    output_rate = cost["output"]
    if not _is_nonneg_finite_number(input_rate):
        return "input rate must be a finite nonnegative number"
    if not _is_nonneg_finite_number(output_rate):
        return "output rate must be a finite nonnegative number"

    if "cache_read" in cost:
        cache_read = cost["cache_read"]
        if not _is_nonneg_finite_number(cache_read):
            return "cache_read rate must be a finite nonnegative number"
    else:
        cache_read = input_rate

    if "cache_write" in cost:
        cache_write = cost["cache_write"]
        if not _is_nonneg_finite_number(cache_write):
            return "cache_write rate must be a finite nonnegative number"
    else:
        cache_write = input_rate

    return (
        float(input_rate),
        float(output_rate),
        float(cache_read),
        float(cache_write),
    )


def _provider_sort_key(provider_id: str) -> tuple[int, str]:
    try:
        rank = PREFERRED_PROVIDERS.index(provider_id)
    except ValueError:
        rank = len(PREFERRED_PROVIDERS)
    return (rank, provider_id)


def _find_override(
    want: str,
    overrides: Mapping[str, Any],
    *,
    retrieved_at: str | None,
) -> PricingLookup | None:
    """Return a PricingLookup if an override key normalizes to *want*.

    Returns None when no override key matches (caller continues to data).
    Returns an error lookup when a matching override is malformed.
    """
    matched_key: str | None = None
    matched_body: Any = None
    for key, body in overrides.items():
        if not isinstance(key, str):
            continue
        try:
            if normalize_model_id(key) == want:
                matched_key = key
                matched_body = body
                break
        except (TypeError, ValueError):
            continue

    if matched_key is None:
        return None

    if not isinstance(matched_body, Mapping):
        return PricingLookup(
            rates=None,
            error=f"override for {want!r} must be a mapping of rates",
            stale=False,
        )

    # Accept either flat rates or nested under "cost" for consistency.
    cost: Any = matched_body
    if "cost" in matched_body and isinstance(matched_body.get("cost"), Mapping):
        # Prefer nested cost only when top-level lacks input/output.
        if "input" not in matched_body or "output" not in matched_body:
            cost = matched_body["cost"]

    parsed = _parse_rate_fields(cost)
    if isinstance(parsed, str):
        return PricingLookup(
            rates=None,
            error=f"override for {want!r}: {parsed}",
            stale=False,
        )
    input_r, output_r, cache_read_r, cache_write_r = parsed
    currency = cost.get("currency", matched_body.get("currency", _DEFAULT_CURRENCY))
    if not isinstance(currency, str) or not currency:
        currency = _DEFAULT_CURRENCY

    rates = PricingRates(
        input_usd_per_m=input_r,
        output_usd_per_m=output_r,
        cache_read_usd_per_m=cache_read_r,
        cache_write_usd_per_m=cache_write_r,
        currency=currency,
        source=_SOURCE_OVERRIDE,
        retrieved_at=retrieved_at,
        provider=None,
        model_id=want,
        match_kind="override",
    )
    return PricingLookup(rates=rates, error=None, stale=False)


def _find_exact_in_pricing_data(
    want: str,
    pricing_data: Mapping[str, Any],
    *,
    retrieved_at: str | None,
) -> PricingLookup:
    # Collect exact matches: list of (sort_key, provider_id, mid, cost_map)
    candidates: list[tuple[tuple[int, str], str, str, Mapping[str, Any]]] = []
    invalid_exact: list[str] = []

    for provider_id, provider in pricing_data.items():
        if not isinstance(provider_id, str):
            continue
        if not isinstance(provider, Mapping):
            continue
        models = provider.get("models")
        if not isinstance(models, Mapping):
            continue
        for mid, body in models.items():
            if not isinstance(mid, str):
                continue
            try:
                nid = normalize_model_id(mid)
            except (TypeError, ValueError):
                continue
            if nid != want:
                continue
            if not isinstance(body, Mapping):
                invalid_exact.append(f"{provider_id}/{mid}: model body must be a mapping")
                continue
            cost = body.get("cost")
            if not isinstance(cost, Mapping):
                invalid_exact.append(f"{provider_id}/{mid}: missing cost mapping")
                continue
            parsed = _parse_rate_fields(cost)
            if isinstance(parsed, str):
                invalid_exact.append(f"{provider_id}/{mid}: {parsed}")
                continue
            candidates.append((_provider_sort_key(provider_id), provider_id, nid, cost))

    if not candidates:
        if invalid_exact:
            return PricingLookup(
                rates=None,
                error=(
                    f"exact match for {want!r} found but rates invalid: " + "; ".join(invalid_exact)
                ),
                stale=False,
            )
        return PricingLookup(
            rates=None,
            error=f"no exact pricing for model_id {want!r}",
            stale=False,
        )

    candidates.sort(key=lambda item: item[0])
    _sort_key, provider_id, nid, cost = candidates[0]
    parsed = _parse_rate_fields(cost)
    # Already validated above; re-parse for typed values.
    assert not isinstance(parsed, str)
    input_r, output_r, cache_read_r, cache_write_r = parsed
    currency = cost.get("currency", _DEFAULT_CURRENCY)
    if not isinstance(currency, str) or not currency:
        currency = _DEFAULT_CURRENCY

    rates = PricingRates(
        input_usd_per_m=input_r,
        output_usd_per_m=output_r,
        cache_read_usd_per_m=cache_read_r,
        cache_write_usd_per_m=cache_write_r,
        currency=currency,
        source=_SOURCE_MODELS_DEV,
        retrieved_at=retrieved_at,
        provider=provider_id,
        model_id=nid,
        match_kind="exact",
    )
    return PricingLookup(rates=rates, error=None, stale=False)


def _read_cache_file(
    cache_path: Path,
) -> tuple[dict | None, dict[str, Any]]:
    """Return (parsed_dict_or_None, meta).

    meta keys: error, age_s, retrieved_at, stale_by_age (placeholder).
    """
    meta: dict[str, Any] = {
        "error": None,
        "age_s": None,
        "retrieved_at": None,
        "stale_by_age": True,
    }
    try:
        if not cache_path.is_file():
            meta["error"] = "cache file not found"
            return None, meta
    except OSError as exc:
        meta["error"] = f"cache stat failed: {exc}"
        return None, meta

    try:
        mtime = cache_path.stat().st_mtime
    except OSError as exc:
        meta["error"] = f"cache stat failed: {exc}"
        return None, meta

    age_s = max(0.0, time.time() - mtime)
    meta["age_s"] = age_s
    meta["retrieved_at"] = _mtime_iso(mtime)
    meta["stale_by_age"] = True  # caller applies max_age_s

    try:
        text = cache_path.read_text(encoding="utf-8")
    except OSError as exc:
        meta["error"] = f"cache read failed: {exc}"
        return None, meta

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        meta["error"] = f"cache JSON malformed: {exc}"
        return None, meta

    if not isinstance(parsed, dict):
        meta["error"] = f"cache JSON root must be an object, got {type(parsed).__name__}"
        return None, meta

    meta["error"] = None
    return parsed, meta


def _fetch_pricing_url(url: str, timeout_s: float) -> tuple[dict | None, str | None, str | None]:
    """Return (data, raw_text, error)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "basecamp-bench/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        return None, None, f"HTTP error fetching pricing: {exc.code} {exc.reason}"
    except urllib.error.URLError as exc:
        return None, None, f"network error fetching pricing: {exc.reason}"
    except TimeoutError as exc:
        return None, None, f"timeout fetching pricing: {exc}"
    except OSError as exc:
        return None, None, f"network error fetching pricing: {exc}"

    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, None, f"pricing response is not valid UTF-8: {exc}"

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return None, None, f"pricing response JSON malformed: {exc}"

    if not isinstance(parsed, dict):
        return (
            None,
            None,
            f"pricing response root must be an object, got {type(parsed).__name__}",
        )
    return parsed, raw_text, None


def _atomic_write_text(path: Path, text: str) -> str | None:
    """Atomically write *text* to *path*. Return error string or None."""
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"cache directory create failed: {exc}"

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            # fd is closed by fdopen on success; on failure before fdopen
            # ownership may still be ours — best-effort close is via fdopen.
            raise
        os.replace(tmp_path, path)
        tmp_path = None
        return None
    except OSError as exc:
        return f"atomic cache write failed: {exc}"
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
