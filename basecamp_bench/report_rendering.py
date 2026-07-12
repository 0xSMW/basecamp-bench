"""Pure deterministic rendering for self-contained benchmark HTML reports.

The renderer accepts an already validated payload and performs no filesystem,
leaderboard, or network access. Keeping this boundary pure makes visual output
reusable and independently testable.

The page is an editorial briefing: masthead, results pivot across tracks,
charts, per-model cards, then methodology. Optional *commentary* (a checked-in
JSON document) adds human-written briefing text, per-model analysis, chart
captions, and methodology prose; without it every section renders from data
alone. Full evidence stays in the embedded JSON payload (provenance hashes,
classifications, source IDs, raw attempts).

Layout rule, learned the hard way: constrain page width ONCE, on ``.wrap``.
Nothing inside gets its own max-width — nested caps read as broken right
padding. Chart marks and swatches share each model's identity color; a swatch
is a chart legend key, not decoration.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from basecamp_bench.reporting_model import raw_attempt_sort_key
from basecamp_bench.validation import is_finite_number

__all__ = ["render_report_html"]


# =============================================================================
# Formatting
# =============================================================================


def _escape(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _finite_or(value: Any, fallback: float) -> float:
    parsed = _finite(value)
    return fallback if parsed is None else parsed


def _fmt_money(value: Any) -> str:
    v = _finite(value)
    if v is None:
        return "—"
    if v != 0 and abs(v) < 0.1:
        return f"${v:.3f}"
    return f"${v:,.2f}"


def _fmt_score(value: Any) -> str:
    v = _finite(value)
    if v is None:
        return "—"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _fmt_percent(value: Any) -> str:
    v = _finite(value)
    if v is None:
        return "—"
    return f"{v * 100:.0f}%"


def _fmt_tokens(value: Any) -> str:
    v = _finite(value)
    if v is None:
        return "—"
    n = float(v)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:,.0f}"


def _fmt_clock(value: Any) -> str:
    v = _finite(value)
    if v is None:
        return "—"
    total = int(round(v))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


# =============================================================================
# Model identity — one stable color per point, reused in every view
# =============================================================================

# Curated editorial palette for models without an explicit color override.
# Hash-assigned by point id so a model keeps its color across reports.
_PALETTE: tuple[str, ...] = (
    "#d97757",
    "#8a63d2",
    "#2f3640",
    "#10a37f",
    "#74c6ad",
    "#3b6ea5",
    "#b48b2f",
    "#a5527a",
    "#5b8c5a",
    "#8c5b45",
)


def _color_class(point_id: str) -> str:
    return f"mc-{hashlib.sha256(point_id.encode('utf-8')).hexdigest()[:8]}"


def _identity_css(points: Mapping[str, str], colors: Mapping[str, str] | None) -> str:
    """CSS classes carrying each model's identity color via ``color``.

    *points* maps point_id -> model_id. Explicit *colors* (keyed by model_id)
    win; the curated palette is the deterministic fallback. Identity is never
    color-alone: every colored mark sits beside the model's printed name.
    """
    rules: list[str] = []
    for pid in sorted(points):
        model_id = points[pid]
        override = (colors or {}).get(model_id)
        if override is not None:
            color = str(override)
        else:
            index = int(hashlib.sha256(pid.encode("utf-8")).hexdigest()[:8], 16)
            color = _PALETTE[index % len(_PALETTE)]
        rules.append(f".{_color_class(pid)} {{ color: {_escape(color)}; }}")
    return "\n".join(rules) + ("\n" if rules else "")


def _sorted_models(models: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Score-descending order used by every human-facing view."""

    def key(m: Mapping[str, Any]) -> tuple[int, float, str]:
        score = _finite(m.get("score"))
        if score is None:
            return (1, 0.0, str(m.get("point_id")))
        return (0, -score, str(m.get("point_id")))

    return sorted(models, key=key)


def _embed_json(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # Prevent </script> breakout and HTML parsing of raw < characters.
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


# =============================================================================
# Commentary (optional, human-written, checked in beside the evidence)
# =============================================================================


def _commentary_models(commentary: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not commentary:
        return {}
    models = commentary.get("models")
    return models if isinstance(models, Mapping) else {}


def validate_commentary(commentary: Mapping[str, Any], known_model_ids: set[str]) -> None:
    """Fail loudly when commentary targets a model the data does not contain."""
    for key in ("models", "colors"):
        section = commentary.get(key)
        if not isinstance(section, Mapping):
            continue
        unknown = sorted(set(map(str, section)) - known_model_ids)
        if unknown:
            known = ", ".join(sorted(known_model_ids))
            raise ValueError(
                f"commentary {key} reference unknown model ids: "
                f"{', '.join(unknown)} (known: {known})"
            )


# =============================================================================
# Cross-track pivot
# =============================================================================


def _section_label(section: Mapping[str, Any], multi_version: bool) -> str:
    track = str(section.get("track", "")).upper()
    if multi_version:
        return f"{track} {section.get('contract_version', '')}".strip()
    return track


def _ordered_sections(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """FE before BE: the reader meets the product surface first."""
    sections = [s for s in (payload.get("sections") or []) if isinstance(s, Mapping)]
    return sorted(
        sections,
        key=lambda s: (
            str(s.get("track", "")) != "fe",
            str(s.get("track", "")),
            str(s.get("contract_version", "")),
        ),
    )


def _pivot_rows(
    sections: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """One row per point across sections, joined on point_id."""
    rows: dict[str, dict[str, Any]] = {}
    for index, section in enumerate(sections):
        for m in section.get("models") or []:
            if not isinstance(m, Mapping) or m.get("point_id") is None:
                continue
            pid = str(m["point_id"])
            row = rows.setdefault(
                pid,
                {
                    "point_id": pid,
                    "model_id": str(m.get("model_id", "")),
                    "display_name": str(m.get("display_name") or m.get("model_id")),
                    "cells": {},
                },
            )
            row["cells"][index] = m
    ordered = sorted(
        rows.values(),
        key=lambda r: (
            -max(
                (_finite_or(c.get("score"), -math.inf) for c in r["cells"].values()),
                default=-math.inf,
            ),
            r["point_id"],
        ),
    )
    return ordered


def _row_total_cost(row: Mapping[str, Any]) -> float | None:
    costs = [_finite(c.get("total_cost_per_attempt")) for c in row["cells"].values()]
    known = [c for c in costs if c is not None]
    return sum(known) if known else None


def _row_total_duration(row: Mapping[str, Any]) -> float | None:
    values = [_finite(c.get("duration_s")) for c in row["cells"].values()]
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _row_total_tokens(row: Mapping[str, Any]) -> float | None:
    values = [_finite(c.get("tokens")) for c in row["cells"].values()]
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _results_table(sections: Sequence[Mapping[str, Any]], labels: Sequence[str]) -> str:
    rows = _pivot_rows(sections)
    if not rows:
        return '<p class="muted">No scored results.</p>'
    show_spread = any(
        (c.get("repetitions") or 0) > 1 for row in rows for c in row["cells"].values()
    )

    heads = ['<th scope="col">Model</th>']
    for label in labels:
        heads.append(f'<th scope="col" class="num">{_escape(label)} score</th>')
        heads.append(f'<th scope="col" class="num">{_escape(label)} time</th>')
        heads.append(f'<th scope="col" class="num">{_escape(label)} cost</th>')
    heads.append('<th scope="col" class="num">Total cost</th>')

    # Best value per numeric column: highest score, lowest time/cost.
    best: dict[tuple[int, str], float] = {}
    for row in rows:
        for index in range(len(labels)):
            cell = row["cells"].get(index)
            if not cell:
                continue
            score = _finite(cell.get("score"))
            if score is not None:
                best[(index, "score")] = max(best.get((index, "score"), -math.inf), score)
            for field in ("duration_s", "total_cost_per_attempt"):
                v = _finite(cell.get(field))
                if v is not None:
                    best[(index, field)] = min(best.get((index, field), math.inf), v)
    totals = [t for t in (_row_total_cost(r) for r in rows) if t is not None]
    best_total = min(totals) if totals else None

    body: list[str] = []
    for row in rows:
        cls = _color_class(row["point_id"])
        cells = [f'<td><span class="swatch {cls}"></span>{_escape(row["display_name"])}</td>']
        for index in range(len(labels)):
            cell = row["cells"].get(index)
            if not cell:
                cells.extend('<td class="num">—</td>' for _ in range(3))
                continue
            score = _finite(cell.get("score"))
            score_text = _fmt_score(score)
            if show_spread and _finite_or(cell.get("score_stdev"), 0.0) > 0:
                score_text += f" ±{_fmt_score(cell.get('score_stdev'))}"
            success = _finite(cell.get("success_rate"))
            if success is not None and success < 1.0:
                score_text += f" ({_fmt_percent(success)} success)"
            win = " winner" if score is not None and score == best.get((index, "score")) else ""
            cells.append(f'<td class="num{win}">{_escape(score_text)}</td>')
            duration = _finite(cell.get("duration_s"))
            win = (
                " winner"
                if duration is not None and duration == best.get((index, "duration_s"))
                else ""
            )
            cells.append(f'<td class="num{win}">{_escape(_fmt_clock(duration))}</td>')
            cost = _finite(cell.get("total_cost_per_attempt"))
            win = (
                " winner"
                if cost is not None and cost == best.get((index, "total_cost_per_attempt"))
                else ""
            )
            cells.append(f'<td class="num{win}">{_escape(_fmt_money(cost))}</td>')
        total = _row_total_cost(row)
        win = " winner" if total is not None and total == best_total else ""
        cells.append(f'<td class="num{win}">{_escape(_fmt_money(total))}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")

    return (
        '<div class="table-scroll"><table class="raw-table" id="results-table">'
        "<caption>Scores, agent wall-clock time, and cost per track. "
        "Cost includes evaluator overhead. Best value per column in bold."
        "</caption>"
        f"<thead><tr>{''.join(heads)}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


# =============================================================================
# Verdicts
# =============================================================================


def _model_name_html(m: Mapping[str, Any]) -> str:
    name = _escape(m.get("display_name") or m["model_id"])
    return f'<span class="hl-name {_color_class(str(m["point_id"]))}">{name}</span>'


def _ranked_for_verdict(models: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Scored models, eligible ones first — an ineligible point never 'leads'."""
    scored = [m for m in _sorted_models(models) if _finite(m.get("score")) is not None]
    eligible = [m for m in scored if m.get("eligible")]
    return eligible or scored


def _verdict_html(section: Mapping[str, Any], label: str) -> str:
    """One computed sentence per track answering the report's question."""
    models = [m for m in (section.get("models") or []) if isinstance(m, Mapping)]
    scored = _ranked_for_verdict(models)
    if not scored:
        return ""
    leader = scored[0]
    leader_score = _finite(leader.get("score")) or 0.0
    leader_cost = _finite(leader.get("expected_cost"))

    text = f"{_model_name_html(leader)} leads at {_escape(_fmt_score(leader_score))}"
    if leader_cost is not None:
        text += f" for {_escape(_fmt_money(leader_cost))} per valid result"

    value_pick: Mapping[str, Any] | None = None
    if leader_cost is not None and leader_cost > 0 and leader_score > 0:
        rivals = [
            m
            for m in scored[1:]
            if _finite_or(m.get("expected_cost"), math.inf) < leader_cost
            and (_finite(m.get("score")) or 0.0) > 0
        ]
        if rivals:
            value_pick = min(
                rivals,
                key=lambda m: (
                    _finite_or(m.get("expected_cost"), math.inf),
                    str(m["point_id"]),
                ),
            )
    if value_pick is not None:
        assert leader_cost is not None
        v_score = _finite(value_pick.get("score")) or 0.0
        v_cost = _finite(value_pick.get("expected_cost")) or 0.0
        quality_pct = f"{v_score / leader_score * 100:.0f}%"
        cost_pct = f"{v_cost / leader_cost * 100:.0f}%"
        text += (
            f" — {_model_name_html(value_pick)} delivers {_escape(quality_pct)} of the "
            f"quality at {_escape(cost_pct)} of the cost"
        )
    return f'<p class="verdict"><span class="verdict-track">{_escape(label)}</span> {text}.</p>'


def _uniform_ineligibility(models: Sequence[Mapping[str, Any]]) -> str | None:
    """Shared reason string when every model is ineligible the same way."""
    if not models:
        return None
    signatures: set[tuple[str, ...]] = set()
    for m in models:
        if m.get("eligible") or str(m.get("classification") or "") != "ineligible":
            return None
        signatures.add(tuple(str(r) for r in (m.get("ineligible_reasons") or [])))
    if len(signatures) != 1:
        return None
    return ", ".join(next(iter(signatures)))


def _eligibility_footnote(models: Sequence[Mapping[str, Any]], label: str) -> str:
    """Name ineligible models when eligibility actually varies.

    A uniform state (all eligible, or all ineligible the same way) carries no
    per-model signal; classification stays in the JSON payload.
    """
    if _uniform_ineligibility(models) is not None:
        return ""
    entries: list[str] = []
    for m in _sorted_models(models):
        if m.get("eligible"):
            continue
        name = str(m.get("display_name") or m["model_id"])
        reasons = ", ".join(str(r) for r in (m.get("ineligible_reasons") or []))
        entries.append(f"{name} ({reasons})" if reasons else name)
    if not entries:
        return ""
    return (
        f'<p class="caption">Not frontier-eligible on {_escape(label)}: '
        f"{_escape('; '.join(entries))}.</p>"
    )


# =============================================================================
# Charts (inline SVG, deterministic, self-contained)
# =============================================================================


def _axis_ticks(lo: float, hi: float, target: int = 6) -> list[float]:
    """Deterministic 1/2/2.5/5-stepped tick positions covering [lo, hi]."""
    span = hi - lo
    if span <= 0 or not math.isfinite(span):
        return [lo]
    raw = span / max(target, 1)
    magnitude = 10.0 ** math.floor(math.log10(raw))
    step = magnitude * 10
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if span / (magnitude * mult) <= target:
            step = magnitude * mult
            break
    first = math.ceil(lo / step) * step
    ticks: list[float] = []
    t = first
    while t <= hi + step * 1e-9:
        ticks.append(round(t, 10))
        t += step
    return ticks


def _fmt_tick(value: float, money: bool) -> str:
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"${text}" if money else text


def _marker_svg(shape: str, cx: float, cy: float, r: float, extra: str) -> str:
    if shape == "triangle":
        points = (
            f"{cx:.2f},{cy - r:.2f} {cx - r * 0.9:.2f},{cy + r * 0.7:.2f} "
            f"{cx + r * 0.9:.2f},{cy + r * 0.7:.2f}"
        )
        return f'<polygon points="{points}" {extra}/>'
    return f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r}" {extra}/>'


_TRACK_SHAPES: tuple[str, ...] = ("circle", "triangle", "square")


def _scatter_svg(sections: Sequence[Mapping[str, Any]], labels: Sequence[str]) -> str:
    """All tracks on one canvas; marker shape encodes the track."""
    width, height = 880, 460
    margin_l, margin_r, margin_t, margin_b = 56, 24, 20, 64
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    points: list[tuple[int, Mapping[str, Any]]] = []
    for index, section in enumerate(sections):
        for m in section.get("models") or []:
            if (
                isinstance(m, Mapping)
                and m.get("expected_cost") is not None
                and is_finite_number(m["expected_cost"])
                and is_finite_number(m.get("score"))
            ):
                points.append((index, m))

    shape_note = ", ".join(
        f"{_TRACK_SHAPES[min(i, len(_TRACK_SHAPES) - 1)]} = {label}"
        for i, label in enumerate(labels)
    )
    aria = (
        "Scatter chart of expected implementation cost versus score across "
        f"tracks ({shape_note}). Expected implementation cost on the "
        "horizontal axis with cheaper to the right. Score on the vertical "
        "axis. Frontier polylines mark the best quality per dollar."
    )
    if not points:
        return (
            f'<svg class="chart" role="img" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" aria-label="{_escape(aria)}">'
            f"<title>Quality versus cost</title><desc>{_escape(aria)}</desc>"
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            f'class="muted">No plottable points</text></svg>'
        )

    scores = [float(m["score"]) for _, m in points]
    costs = [float(m["expected_cost"]) for _, m in points]
    score_lo, score_hi = min(scores), max(scores)
    cost_lo, cost_hi = min(costs), max(costs)
    if score_hi == score_lo:
        score_lo -= 1.0
        score_hi += 1.0
    if cost_hi == cost_lo:
        cost_lo = max(0.0, cost_lo - 1.0)
        cost_hi += 1.0
    score_pad = max((score_hi - score_lo) * 0.1, 0.05)
    cost_pad = max((cost_hi - cost_lo) * 0.08, 0.05)
    score_lo -= score_pad
    score_hi += score_pad
    cost_lo = max(0.0, cost_lo - cost_pad)
    cost_hi += cost_pad

    def x_of(cost: float) -> float:
        # Cheaper (lower expected cost) maps to the right.
        t = (cost - cost_lo) / (cost_hi - cost_lo)
        return margin_l + (1.0 - t) * plot_w

    def y_of(score: float) -> float:
        t = (score - score_lo) / (score_hi - score_lo)
        return margin_t + (1.0 - t) * plot_h

    parts: list[str] = [
        f'<svg class="chart" role="img" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" aria-label="{_escape(aria)}">',
        "<title>Quality versus cost</title>",
        f"<desc>{_escape(aria)}</desc>",
    ]
    for tick in _axis_ticks(cost_lo, cost_hi):
        tx = x_of(tick)
        parts.append(
            f'<line class="grid" x1="{tx:.2f}" y1="{margin_t}" '
            f'x2="{tx:.2f}" y2="{margin_t + plot_h}"/>'
        )
        parts.append(
            f'<text class="tick-label" x="{tx:.2f}" y="{margin_t + plot_h + 16}" '
            f'text-anchor="middle">{_escape(_fmt_tick(tick, money=True))}</text>'
        )
    for tick in _axis_ticks(score_lo, score_hi):
        ty = y_of(tick)
        parts.append(
            f'<line class="grid" x1="{margin_l}" y1="{ty:.2f}" '
            f'x2="{margin_l + plot_w}" y2="{ty:.2f}"/>'
        )
        parts.append(
            f'<text class="tick-label" x="{margin_l - 8}" y="{ty + 4:.2f}" '
            f'text-anchor="end">{_escape(_fmt_tick(tick, money=False))}</text>'
        )
    parts += [
        f'<line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" '
        f'y2="{margin_t + plot_h}" class="axis"/>',
        f'<line x1="{margin_l}" y1="{margin_t + plot_h}" '
        f'x2="{margin_l + plot_w}" y2="{margin_t + plot_h}" class="axis"/>',
        f'<text x="{margin_l + plot_w / 2}" y="{height - 10}" text-anchor="middle" '
        f'class="axis-label">Expected implementation cost (cheaper to the right)</text>',
        f'<text x="{margin_l}" y="{height - 30}" text-anchor="start" '
        f'class="axis-note">higher cost →</text>',
        f'<text x="{margin_l + plot_w}" y="{height - 30}" text-anchor="end" '
        f'class="axis-note">← lower cost (cheaper)</text>',
    ]

    for index, section in enumerate(sections):
        frontier_ids = list(section.get("frontier") or [])
        by_id = {
            str(m.get("point_id")): m for m in section.get("models") or [] if isinstance(m, Mapping)
        }
        f_pts: list[str] = []
        for mid in frontier_ids:
            m = by_id.get(str(mid))
            if not m or m.get("expected_cost") is None:
                continue
            f_pts.append(f"{x_of(float(m['expected_cost'])):.2f},{y_of(float(m['score'])):.2f}")
        if len(f_pts) >= 2:
            parts.append(
                f'<polyline class="frontier-line" fill="none" points="{" ".join(f_pts)}"/>'
            )

    for index, m in sorted(points, key=lambda item: (item[0], str(item[1]["point_id"]))):
        shape = _TRACK_SHAPES[min(index, len(_TRACK_SHAPES) - 1)]
        score = float(m["score"])
        cost = float(m["expected_cost"])
        cx, cy = x_of(cost), y_of(score)
        color_cls = _color_class(str(m["point_id"]))
        name = _escape(m.get("display_name") or m["point_id"])
        title = (
            f"<title>{name} ({_escape(labels[index])}): "
            f"score={score:.4g}, expected_cost={cost:.4g}</title>"
        )
        extra = f'class="mark {color_cls}" fill="currentColor"'
        mark = _marker_svg(shape, cx, cy, 7, extra)
        parts.append(mark[:-2] + f">{title}</" + mark[1 : mark.index(" ")] + ">")
        if cx > margin_l + plot_w * 0.82:
            label_x, anchor = cx - 11, "end"
        else:
            label_x, anchor = cx + 11, "start"
        parts.append(
            f'<text class="point-label" x="{label_x:.2f}" y="{cy + 4:.2f}" '
            f'text-anchor="{anchor}">{name}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def _dimension_keys(
    section: Mapping[str, Any],
) -> tuple[list[str], dict[str, str], dict[str, float]]:
    labels: dict[str, str] = {}
    weights: dict[str, float] = {}
    for row in section.get("dimension_profile") or []:
        if not isinstance(row, Mapping) or row.get("id") is None:
            continue
        rid = str(row["id"])
        if row.get("label"):
            labels[rid] = str(row["label"])
        w = _finite(row.get("weight"))
        if w is not None:
            weights[rid] = w
    keys: set[str] = set()
    for m in section.get("models") or []:
        dims = m.get("dimensions") if isinstance(m, Mapping) else None
        if isinstance(dims, Mapping):
            keys.update(str(k) for k in dims)
    ordered = sorted(keys, key=lambda k: (-(weights.get(k, 0.0)), k))
    return ordered, labels, weights


def _dimension_bars_svg(section: Mapping[str, Any], label: str) -> str:
    """Grouped horizontal bars: rows = dimensions, bars = models."""
    keys, labels, weights = _dimension_keys(section)
    models = _sorted_models([m for m in (section.get("models") or []) if isinstance(m, Mapping)])
    if not keys or not models:
        return '<p class="muted">No dimension scores.</p>'

    bar_h, bar_gap, group_gap, header_h = 14, 3, 18, 20
    margin_l, margin_r, margin_t, margin_b = 8, 44, 6, 8
    width = 880
    plot_w = width - margin_l - margin_r
    group_h = header_h + len(models) * (bar_h + bar_gap) + group_gap
    height = margin_t + len(keys) * group_h + margin_b

    observed = [
        v
        for m in models
        for v in [_finite((m.get("dimensions") or {}).get(k)) for k in keys]
        if v is not None
    ]
    scale = max(10.0, max(observed) if observed else 10.0)

    aria = (
        f"Grouped bar chart of {label} dimension scores, 0 to "
        f"{_fmt_score(scale)}, one group per weighted dimension with one bar "
        "per model."
    )
    parts = [
        f'<svg class="chart" role="img" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" aria-label="{_escape(aria)}">',
        f"<title>{_escape(label)} dimensions</title>",
        f"<desc>{_escape(aria)}</desc>",
    ]
    y = float(margin_t)
    for k in keys:
        head = labels.get(k, k)
        weight = weights.get(k)
        weight_text = f" · weight {_fmt_percent(weight)}" if weight is not None else ""
        parts.append(
            f'<text class="bar-head" x="{margin_l}" y="{y + 13:.2f}">'
            f'{_escape(head)}<tspan class="bar-weight">{_escape(weight_text)}'
            f"</tspan></text>"
        )
        y += header_h
        for m in models:
            value = _finite((m.get("dimensions") or {}).get(k))
            cls = _color_class(str(m["point_id"]))
            name = _escape(m.get("display_name") or m["model_id"])
            if value is not None:
                w = max(0.0, min(value / scale, 1.0)) * plot_w
                parts.append(
                    f'<rect class="bar {cls}" x="{margin_l}" y="{y:.2f}" '
                    f'width="{w:.2f}" height="{bar_h}" rx="2" fill="currentColor">'
                    f"<title>{name}: {_escape(_fmt_score(value))}</title></rect>"
                )
                parts.append(
                    f'<text class="bar-value" x="{margin_l + w + 6:.2f}" '
                    f'y="{y + bar_h - 3:.2f}">{_escape(_fmt_score(value))}'
                    f'<tspan class="bar-name"> {name}</tspan></text>'
                )
            else:
                parts.append(
                    f'<text class="bar-value muted" x="{margin_l}" '
                    f'y="{y + bar_h - 3:.2f}">— {name}</text>'
                )
            y += bar_h + bar_gap
        y += group_gap
    parts.append("</svg>")
    return "\n".join(parts)


def _gap_chart_svg(sections: Sequence[Mapping[str, Any]], labels: Sequence[str]) -> str:
    """Score gap between the first two tracks per model, joined on point_id."""
    if len(sections) < 2:
        return ""
    rows = _pivot_rows(sections[:2])
    gaps: list[tuple[str, str, float]] = []
    for row in rows:
        a = row["cells"].get(0)
        b = row["cells"].get(1)
        if not a or not b:
            continue
        sa, sb = _finite(a.get("score")), _finite(b.get("score"))
        if sa is None or sb is None:
            continue
        gaps.append((row["point_id"], row["display_name"], sb - sa))
    if not gaps:
        return ""

    row_h, name_w = 30, 130
    margin_t, margin_b = 10, 30
    width = 880
    height = margin_t + len(gaps) * row_h + margin_b
    max_abs = max(0.5, max(abs(g) for _, _, g in gaps))
    zero_x = name_w + (width - name_w - 60) / 2
    unit = (width - name_w - 60) / 2 / max_abs

    aria = (
        f"Bar chart of per-model score gap, {labels[1]} minus {labels[0]}. "
        "Bars right of the zero line mean the model scored higher on "
        f"{labels[1]}."
    )
    parts = [
        f'<svg class="chart" role="img" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" aria-label="{_escape(aria)}">',
        f"<title>{_escape(labels[0])} versus {_escape(labels[1])} gap</title>",
        f"<desc>{_escape(aria)}</desc>",
        f'<line class="axis" x1="{zero_x:.2f}" y1="{margin_t}" '
        f'x2="{zero_x:.2f}" y2="{height - margin_b}"/>',
        f'<text class="tick-label" x="{zero_x:.2f}" y="{height - 12}" '
        f'text-anchor="middle">0</text>',
    ]
    y = float(margin_t)
    for pid, name, gap in gaps:
        cls = _color_class(pid)
        bar_w = abs(gap) * unit
        x = zero_x if gap >= 0 else zero_x - bar_w
        parts.append(
            f'<text class="bar-value" x="{name_w - 8}" y="{y + row_h / 2 + 4:.2f}" '
            f'text-anchor="end">{_escape(name)}</text>'
        )
        parts.append(
            f'<rect class="bar {cls}" x="{x:.2f}" y="{y + 6:.2f}" '
            f'width="{bar_w:.2f}" height="{row_h - 12}" rx="2" fill="currentColor">'
            f"<title>{_escape(name)}: {gap:+.2f}</title></rect>"
        )
        tx = x + bar_w + 6 if gap >= 0 else x - 6
        anchor = "start" if gap >= 0 else "end"
        parts.append(
            f'<text class="bar-value" x="{tx:.2f}" y="{y + row_h / 2 + 4:.2f}" '
            f'text-anchor="{anchor}">{gap:+.2f}</text>'
        )
        y += row_h
    parts.append("</svg>")
    return "\n".join(parts)


def _dimension_table(section: Mapping[str, Any], label: str) -> str:
    """Tabular fallback for the dimension chart."""
    keys, labels, weights = _dimension_keys(section)
    models = _sorted_models([m for m in (section.get("models") or []) if isinstance(m, Mapping)])
    if not keys or not models:
        return ""
    head_cells = []
    for k in keys:
        head = _escape(labels.get(k, k))
        weight = weights.get(k)
        sub = (
            f'<span class="dim-weight">weight {_escape(_fmt_percent(weight))}</span>'
            if weight is not None
            else ""
        )
        head_cells.append(f'<th scope="col" class="num">{head}{sub}</th>')
    rows = []
    for m in models:
        dims = m.get("dimensions") or {}
        cells = "".join(f'<td class="num">{_escape(_fmt_score(dims.get(k)))}</td>' for k in keys)
        rows.append(
            f'<tr><th scope="row">{_escape(m.get("display_name") or m["model_id"])}'
            f"</th>{cells}</tr>"
        )
    return (
        f'<details class="table-view"><summary>{_escape(label)} dimension table '
        f"(tabular fallback)</summary>"
        '<div class="table-scroll"><table class="dim-table">'
        f"<caption>{_escape(label)} dimension scores and weights</caption>"
        f'<thead><tr><th scope="col">Model</th>{"".join(head_cells)}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div></details>"
    )


def _caption(commentary: Mapping[str, Any] | None, key: str, computed: str) -> str:
    captions = (commentary or {}).get("captions")
    text = None
    if isinstance(captions, Mapping):
        text = captions.get(key)
    return f'<p class="caption">{_escape(text if text else computed)}</p>'


def _computed_dim_caption(section: Mapping[str, Any]) -> str:
    keys, labels, _ = _dimension_keys(section)
    values: list[tuple[float, str]] = []
    for m in section.get("models") or []:
        dims = m.get("dimensions") if isinstance(m, Mapping) else None
        if not isinstance(dims, Mapping):
            continue
        for k in keys:
            v = _finite(dims.get(k))
            if v is not None:
                values.append((v, labels.get(k, k)))
    if not values:
        return "Judge scores per weighted dimension."
    lo = min(values)
    hi = max(values)
    return (
        f"Scores span {_fmt_score(lo[0])} ({lo[1]}) to {_fmt_score(hi[0])} "
        f"({hi[1]}), 0–10 scale, heaviest weights first."
    )


def _computed_gap_caption(sections: Sequence[Mapping[str, Any]], labels: Sequence[str]) -> str:
    rows = _pivot_rows(sections[:2])
    gaps = []
    for row in rows:
        a, b = row["cells"].get(0), row["cells"].get(1)
        if a and b:
            sa, sb = _finite(a.get("score")), _finite(b.get("score"))
            if sa is not None and sb is not None:
                gaps.append(sb - sa)
    if not gaps:
        return ""
    if min(gaps) >= 0:
        return (
            f"Every model scores higher on {labels[1]}, gaps {min(gaps):+.1f} to {max(gaps):+.1f}."
        )
    return f"Score gaps range {min(gaps):+.1f} to {max(gaps):+.1f} ({labels[1]} minus {labels[0]})."


# =============================================================================
# Model cards
# =============================================================================

_BANDS: tuple[tuple[float, str, str], ...] = (
    (8.0, "score-strong", "Strong"),
    (7.0, "score-good", "Good"),
    (6.0, "score-mixed", "Mixed"),
    (5.0, "score-weak", "Weak"),
    (-math.inf, "score-poor", "Poor"),
)


def _band_class(value: float | None) -> str:
    if value is None:
        return "score-poor"
    for floor, cls, _ in _BANDS:
        if value >= floor:
            return cls
    return "score-poor"


def _score_legend() -> str:
    entries = [
        ("score-strong", "8.0–10 Strong"),
        ("score-good", "7.0–7.9 Good"),
        ("score-mixed", "6.0–6.9 Mixed"),
        ("score-weak", "5.0–5.9 Weak"),
        ("score-poor", "<5.0 Poor"),
    ]
    badges = "".join(
        f'<span class="score-badge {cls}">{_escape(text)}</span>' for cls, text in entries
    )
    return f'<div class="score-legend">{badges}</div>'


def _computed_headline(row: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    """A data-derived descriptor when no commentary headline exists."""
    scores = [_finite(c.get("score")) for c in row["cells"].values()]
    known = [s for s in scores if s is not None]
    best_avg = max(
        (
            sum(v for v in (map(lambda c: _finite_or(c.get("score"), 0.0), r["cells"].values())))
            / max(len(r["cells"]), 1)
            for r in rows
        ),
        default=0.0,
    )
    avg = sum(known) / len(known) if known else 0.0
    total = _row_total_cost(row)
    cheapest = min(
        (t for t in (_row_total_cost(r) for r in rows) if t is not None),
        default=None,
    )
    if known and avg == best_avg:
        return "highest scores overall"
    if total is not None and cheapest is not None and total == cheapest:
        return "lowest total cost"
    return f"{_fmt_score(avg)} average across tracks" if known else "no scored results"


def _commentary_list(items: Any) -> str:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return ""
    parts = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        title = str(item.get("title") or "")
        body = str(item.get("body") or "")
        title_html = f"<b>{_escape(title)}</b> " if title else ""
        parts.append(f"<li>{title_html}{_escape(body)}</li>")
    return f"<ul>{''.join(parts)}</ul>" if parts else ""


def _model_card(
    row: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    sections: Sequence[Mapping[str, Any]],
    labels: Sequence[str],
    commentary: Mapping[str, Any] | None,
) -> str:
    model_id = row["model_id"]
    pid = row["point_id"]
    cls = _color_class(pid)
    name = _escape(row["display_name"])
    entry = _commentary_models(commentary).get(model_id)
    entry = entry if isinstance(entry, Mapping) else {}
    headline = str(entry.get("headline") or _computed_headline(row, rows))

    stats: list[str] = []
    for index, label in enumerate(labels):
        cell = row["cells"].get(index)
        if cell:
            stats.append(f"{label} {_fmt_score(cell.get('score'))}")
    total = _row_total_cost(row)
    if total is not None:
        stats.append(f"{_fmt_money(total)} total")
    duration = _row_total_duration(row)
    if duration is not None:
        stats.append(f"{_fmt_clock(duration)} total")
    tokens = _row_total_tokens(row)
    if tokens is not None:
        stats.append(f"{_fmt_tokens(tokens)} tokens")

    parts = [
        # Identity color rides only on the swatch: the card text stays ink.
        f'<div class="model" id="model-{_escape(model_id)}">',
        f'<h3><span class="swatch {cls}"></span>{name} — {_escape(headline)}</h3>',
        f'<p class="scores">{_escape(" · ".join(stats))}</p>',
    ]
    for key, title in (
        ("shines", "Where it shines"),
        ("okay", "Where it's okay"),
        ("underperforms", "Where it underperforms"),
    ):
        rendered = _commentary_list(entry.get(key))
        if rendered:
            parts.append(f"<h4>{_escape(title)}</h4>{rendered}")
    failure = entry.get("failure_modes")
    if failure:
        parts.append(f"<h4>Failure modes</h4><p>{_escape(str(failure))}</p>")

    tile_tracks: list[str] = []
    for index, label in enumerate(labels):
        cell = row["cells"].get(index)
        if not cell:
            continue
        section = sections[index]
        keys, dim_labels, _ = _dimension_keys(section)
        dims = cell.get("dimensions") or {}
        tiles: list[str] = []
        for k in keys:
            value = _finite(dims.get(k)) if isinstance(dims, Mapping) else None
            if value is None:
                continue
            full = dim_labels.get(k, k)
            short = full.split()[0] if full else k
            tiles.append(
                f'<span class="tile {_band_class(value)}" '
                f'data-tip="{_escape(full)}" tabindex="0">'
                f'<span class="tile-box">{_escape(_fmt_score(value))}</span>'
                f'<span class="tile-label">{_escape(short)}</span></span>'
            )
        if tiles:
            tile_tracks.append(
                f'<div class="dims-track">{_escape(label)}</div>'
                f'<div class="dims-row">{"".join(tiles)}</div>'
            )
    if tile_tracks:
        parts.append(
            '<details class="dims"><summary>Dimension scores</summary>'
            + "".join(tile_tracks)
            + "</details>"
        )
    parts.append("</div>")
    return "".join(parts)


# =============================================================================
# Attempts (rendered only when it adds information beyond the pivot)
# =============================================================================


def _raw_attempts_table(sections: Sequence[Mapping[str, Any]]) -> str:
    attempts: list[Mapping[str, Any]] = []
    for section in sections:
        for m in section.get("models") or []:
            raw_value = m.get("raw_attempts") or []
            assert isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes))
            for raw in raw_value:
                assert isinstance(raw, Mapping)
                attempts.append(raw)
    attempts.sort(key=raw_attempt_sort_key)

    show_repetition = any((raw.get("repetition") or 0) > 1 for raw in attempts)
    show_reasons = any(raw.get("ineligible_reasons") for raw in attempts)
    has_problem = any(
        not raw.get("implementation_success")
        or not raw.get("evaluation_success")
        or raw.get("ineligible_reasons")
        for raw in attempts
    )
    # A clean single-repetition run repeats the results table row for row, so
    # per-attempt detail renders only when it adds information. The full
    # per-attempt record always remains in the JSON payload.
    if not attempts or (not has_problem and not show_repetition):
        return ""

    headers: list[tuple[str, bool]] = [("Model", False), ("Track", False)]
    if show_repetition:
        headers.append(("Repetition", True))
    headers += [
        ("Implementation success", False),
        ("Evaluation success", False),
        ("Score", True),
        ("Implementation cost", True),
        ("Evaluation cost", True),
        ("Tokens", True),
        ("Duration", True),
    ]
    if show_reasons:
        headers.append(("Reasons", False))
    thead = "".join(
        '<th scope="col"{}>{}</th>'.format(' class="num"' if num else "", _escape(h))
        for h, num in headers
    )
    rows: list[str] = []
    for raw in attempts:
        cells: list[tuple[str, bool]] = [
            (_escape(raw.get("display_name") or raw.get("model_id") or "—"), False),
            (_escape(str(raw.get("track") or "—").upper()), False),
        ]
        if show_repetition:
            cells.append((_escape(raw.get("repetition")), True))
        cells += [
            (_escape("yes" if raw.get("implementation_success") else "no"), False),
            (_escape("yes" if raw.get("evaluation_success") else "no"), False),
            (_escape(_fmt_score(raw.get("score"))), True),
            (_escape(_fmt_money(raw.get("implementation_cost_usd"))), True),
            (_escape(_fmt_money(raw.get("evaluation_cost_usd"))), True),
            (_escape(_fmt_tokens(raw.get("tokens"))), True),
            (_escape(_fmt_clock(raw.get("duration_s"))), True),
        ]
        if show_reasons:
            reasons = ", ".join(raw.get("ineligible_reasons") or []) or "—"
            cells.append((_escape(reasons), False))
        rows.append(
            "<tr>"
            + "".join("<td{}>{}</td>".format(' class="num"' if num else "", c) for c, num in cells)
            + "</tr>"
        )
    table = (
        '<div class="table-scroll"><table class="attempts-table">'
        "<caption>Per-attempt raw results including failures "
        "(missing scores and costs shown as —)</caption>"
        f"<thead><tr>{thead}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )
    if has_problem:
        # Failures are part of the story — keep them visible.
        return f"<h2>Attempts</h2>{table}"
    count = len(attempts)
    noun = "attempt" if count == 1 else "attempts"
    return (
        f'<details class="table-view"><summary>Per-attempt detail '
        f"({count} {noun})</summary>{table}</details>"
    )


# =============================================================================
# Assembly
# =============================================================================


def _report_date(sections: Sequence[Mapping[str, Any]]) -> str:
    stamps = sorted(
        str(s.get("generated_at")) for s in sections if s.get("generated_at") is not None
    )
    return stamps[-1] if stamps else ""


def render_report_html(
    payload: Mapping[str, Any],
    commentary: Mapping[str, Any] | None = None,
) -> str:
    """Render a self-contained offline HTML5 report for *payload*."""
    sections = _ordered_sections(payload)
    track_counts: dict[str, int] = {}
    for s in sections:
        track = str(s.get("track", ""))
        track_counts[track] = track_counts.get(track, 0) + 1
    labels = [_section_label(s, track_counts[str(s.get("track", ""))] > 1) for s in sections]
    rows = _pivot_rows(sections)
    known_model_ids = {row["model_id"] for row in rows}
    if commentary:
        validate_commentary(commentary, known_model_ids)
    point_to_model = {row["point_id"]: row["model_id"] for row in rows}
    colors = (commentary or {}).get("colors")
    colors = colors if isinstance(colors, Mapping) else None

    date = _report_date(sections)
    contract_versions = " · ".join(
        sorted({str(s.get("contract_version", "")) for s in sections if s.get("contract_version")})
    )

    briefing_paras = (commentary or {}).get("briefing")
    commentary_paras = (commentary or {}).get("commentary")
    methodology_blocks = (commentary or {}).get("methodology")

    toc: list[str] = []
    if isinstance(briefing_paras, Sequence) and briefing_paras:
        toc.append('<a href="#briefing">Briefing</a>')
    toc.append('<a href="#results">Results</a>')
    toc.append('<a href="#charts">Charts</a>')
    toc.append('<a href="#models">Models</a>')
    if isinstance(commentary_paras, Sequence) and commentary_paras:
        toc.append('<a href="#commentary">Commentary</a>')
    toc.append('<a href="#methodology">Methodology</a>')

    body: list[str] = ['<div class="wrap">']
    body.append('<nav class="toc" aria-label="Sections">' + "".join(toc) + "</nav>")
    subtitle_bits = [
        b for b in (f"eval {contract_versions}" if contract_versions else "", date) if b
    ]
    body.append(
        '<header><div class="masthead"><h1>Basecamp Bench</h1>'
        f'<span class="date">{_escape(" · ".join(subtitle_bits))}</span></div>'
        '<p class="sub">Which agent builds it best, at what cost.</p></header>'
    )

    if isinstance(briefing_paras, Sequence) and briefing_paras:
        body.append('<section id="briefing">')
        for para in briefing_paras:
            body.append(f"<p>{_escape(str(para))}</p>")
        body.append("</section>")

    body.append('<section id="results"><h2>Results</h2>')
    for section, label in zip(sections, labels):
        body.append(_verdict_html(section, label))
    body.append(_results_table(sections, labels))
    for section, label in zip(sections, labels):
        models = [m for m in (section.get("models") or []) if isinstance(m, Mapping)]
        body.append(_eligibility_footnote(models, label))
    body.append("</section>")

    body.append('<section id="charts"><h2>Charts</h2>')
    shape_note = ", ".join(
        f"{_TRACK_SHAPES[min(i, len(_TRACK_SHAPES) - 1)]} = {label}"
        for i, label in enumerate(labels)
    )
    body.append('<div class="chart-card"><h3>Quality versus cost</h3>')
    body.append(
        _caption(
            commentary,
            "Quality versus cost",
            f"Each model appears once per track: {shape_note}. Cheaper to the right.",
        )
    )
    body.append(_scatter_svg(sections, labels))
    body.append("</div>")
    for index, (section, label) in enumerate(zip(sections, labels)):
        body.append(f'<div class="chart-card"><h3>{_escape(label)} dimensions</h3>')
        body.append(_caption(commentary, f"{label} dimensions", _computed_dim_caption(section)))
        body.append(_dimension_bars_svg(section, label))
        body.append(_dimension_table(section, label))
        body.append("</div>")
    if len(sections) >= 2:
        gap = _gap_chart_svg(sections, labels)
        if gap:
            body.append(
                f'<div class="chart-card"><h3>{_escape(labels[0])} versus '
                f"{_escape(labels[1])} gap</h3>"
            )
            body.append(
                _caption(
                    commentary,
                    f"{labels[0]} versus {labels[1]} gap",
                    _computed_gap_caption(sections, labels),
                )
            )
            body.append(gap)
            body.append("</div>")
    body.append("</section>")

    body.append('<section id="models"><h2>Model deep dives</h2>')
    body.append(_score_legend())
    for row in rows:
        body.append(_model_card(row, rows, sections, labels, commentary))
    body.append("</section>")

    body.append(_raw_attempts_table(sections))

    if isinstance(commentary_paras, Sequence) and commentary_paras:
        body.append('<section id="commentary"><h2>Commentary</h2>')
        for para in commentary_paras:
            body.append(f"<p>{_escape(str(para))}</p>")
        body.append("</section>")

    body.append('<section id="methodology"><h2>Methodology</h2>')
    if isinstance(methodology_blocks, Sequence):
        for block in methodology_blocks:
            if not isinstance(block, Mapping):
                continue
            body.append(f"<h4>{_escape(str(block.get('title') or ''))}</h4>")
            for para in block.get("paragraphs") or []:
                body.append(f"<p>{_escape(str(para))}</p>")
    body.append(
        "<h4>Definitions</h4>"
        "<p>Expected implementation cost per valid result is "
        "<code>implementation_cost_per_attempt / success_rate</code>; it sets "
        "the scatter's cost axis and the Pareto frontier. Table costs include "
        "evaluator overhead, which never changes frontier membership. Duration "
        "is implementation time plus critical-path evaluator time. Scoring "
        "rules, eligibility, dominance, and claim boundaries: see the "
        "repository methodology document. Provenance hashes, classifications, "
        "source IDs, judge spread, and raw attempts are in the embedded JSON "
        "payload.</p>"
    )
    body.append("</section>")

    body.append(
        f'<div class="foot">basecamp-bench{" run of " + _escape(date) if date else ""}</div>'
    )
    body.append("</div>")

    embedded = _embed_json(payload)
    identity_css = _identity_css(point_to_model, colors)
    css = """
:root {
  color-scheme: light;
  --ink: #1a1a1a; --muted: #6b6b6b; --faint: #9a9a9a; --line: #e4e0da;
  --bg: #faf9f7; --card: #ffffff; --accent: #c15f3c;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI",
  Roboto, Helvetica, Arial, sans-serif; color: var(--ink); background: var(--bg);
  line-height: 1.55; font-size: 15.5px; -webkit-font-smoothing: antialiased; }
.wrap { max-width: 940px; margin: 0 auto; padding: 40px 28px 96px; }
.toc { display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 13px;
  margin-bottom: 28px; }
.toc a { color: var(--muted); text-decoration: none; }
.toc a:hover { color: var(--ink); text-decoration: underline; }
header { border-bottom: 1px solid var(--line); padding-bottom: 20px;
  margin-bottom: 36px; }
.masthead { display: flex; align-items: baseline; justify-content: space-between;
  gap: 16px; }
.masthead .date { color: var(--muted); font-size: 14.5px; white-space: nowrap;
  font-variant-numeric: tabular-nums; }
h1 { font-size: 28px; letter-spacing: -0.4px; line-height: 1.2; }
.sub { color: var(--muted); margin-top: 8px; font-size: 14.5px; }
h2 { font-size: 20px; margin: 52px 0 14px; letter-spacing: -0.2px;
  border-bottom: 1px solid var(--line); padding-bottom: 8px; }
h3 { font-size: 16px; margin: 26px 0 8px; }
h4 { font-size: 13px; margin: 18px 0 6px; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--muted); font-weight: 600; }
p { margin: 0 0 14px; }
ul { margin: 0 0 16px 20px; }
li { margin-bottom: 8px; }
li b { display: block; font-weight: 600; margin-bottom: 2px; }
.muted { color: var(--muted); }
.verdict { text-wrap: pretty; }
.verdict-track { font-weight: 600; color: var(--muted); font-size: 13px;
  text-transform: uppercase; letter-spacing: 0.06em; margin-right: 4px; }
.hl-name { color: currentColor; font-weight: 600;
  border-bottom: 2px solid currentColor; padding-bottom: 1px; }
.table-scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin: 18px 0 8px;
  font-size: 14px; }
th { text-align: left; font-weight: 600; border-bottom: 2px solid var(--ink);
  padding: 8px 10px; white-space: nowrap; }
th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
td { border-bottom: 1px solid var(--line); padding: 8px 10px;
  white-space: nowrap; }
td.winner { font-weight: 600; }
table caption { caption-side: bottom; text-align: left; color: var(--muted);
  font-size: 13px; padding-top: 8px; }
.caption { color: var(--muted); font-size: 13px; margin: 8px 0 0; }
.swatch { display: inline-block; width: 12px; height: 12px; border-radius: 2px;
  background: currentColor; margin-right: 8px; vertical-align: baseline; flex: none; }
.score-badge { display: inline-block; padding: 1px 7px;
  border: 1px solid transparent; border-radius: 999px; font-weight: 600;
  line-height: 1.35; white-space: nowrap; font-variant-numeric: tabular-nums; }
.score-strong { color: #175c38; background: #e1f2e8; border-color: #b8ddc7; }
.score-good { color: #52611e; background: #eef2dc; border-color: #d4dda9; }
.score-mixed { color: #785400; background: #fff0c7; border-color: #efd58b; }
.score-weak { color: #864100; background: #fde3c8; border-color: #edbd8b; }
.score-poor { color: #882626; background: #f8dddd; border-color: #e9b7b7; }
.score-legend { display: flex; flex-wrap: wrap; gap: 7px 12px;
  align-items: center; margin: 8px 0 22px; color: var(--muted); font-size: 12px; }
.score-legend .score-badge { font-size: 11px; }
.chart-card { background: var(--card); border: 1px solid var(--line);
  border-radius: 8px; padding: 24px; margin: 22px 0 30px; }
.chart-card h3 { margin-top: 0; }
.chart { width: 100%; height: auto; margin-top: 12px; }
.chart .muted { fill: var(--muted); }
.axis { stroke: var(--ink); stroke-width: 1.2; }
.axis-label { fill: var(--ink); font-size: 12.5px; }
.axis-note { fill: var(--muted); font-size: 11.5px; }
.grid { stroke: var(--line); stroke-width: 1; }
.tick-label { fill: var(--muted); font-size: 11px; }
.frontier-line { stroke: var(--accent); stroke-width: 1.5;
  stroke-dasharray: 4 3; opacity: 0.85; }
.mark { stroke: var(--card); stroke-width: 1.5; }
.point-label { font-size: 12px; font-weight: 600; fill: var(--ink); }
.bar-head { font-size: 12.5px; font-weight: 600; fill: var(--ink); }
.bar-weight { fill: var(--muted); font-weight: 400; font-size: 11px; }
.bar-value { font-size: 11px; fill: var(--ink);
  font-variant-numeric: tabular-nums; }
.bar-name { fill: var(--muted); }
.model { background: var(--card); border: 1px solid var(--line);
  border-radius: 8px; padding: 24px; margin: 24px 0; }
.model h3 { margin-top: 0; display: flex; align-items: center; gap: 9px; }
.model p:last-child { margin-bottom: 0; }
.model .scores { color: var(--muted); font-size: 13.5px; margin-bottom: 14px;
  font-variant-numeric: tabular-nums; }
.dims { margin-top: 16px; }
.dims summary, .table-view summary { cursor: pointer; font-size: 13px;
  text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);
  font-weight: 600; }
.table-view { margin-top: 10px; }
.dims-track { font-size: 11px; letter-spacing: 0.08em; color: var(--faint);
  font-weight: 600; margin: 14px 0 6px; }
.dims-row { display: flex; flex-wrap: wrap; gap: 10px; }
.tile { display: flex; flex-direction: column; align-items: center; gap: 1px;
  position: relative; width: 64px; padding: 8px 4px 7px; border-radius: 8px;
  border: 0; }
.tile-box { font-weight: 600; font-size: 14px;
  font-variant-numeric: tabular-nums; }
.tile-label { font-size: 10.5px; text-transform: uppercase;
  letter-spacing: 0.04em; opacity: 0.75; max-width: 100%; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.tile:hover::after, .tile:focus-visible::after { content: attr(data-tip);
  position: absolute; bottom: calc(100% + 7px); left: 50%;
  transform: translateX(-50%); background: var(--ink); color: var(--bg);
  padding: 5px 9px; border-radius: 6px; font-size: 12px; line-height: 1.3;
  white-space: nowrap; z-index: 10; pointer-events: none; }
.dim-weight { display: block; font-weight: 400; font-size: 10px;
  letter-spacing: 0; text-transform: none; color: var(--muted); }
.foot { margin-top: 64px; padding-top: 16px; border-top: 1px solid var(--line);
  color: var(--faint); font-size: 13px; text-align: center; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.92em; }
"""

    doc = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
        "<title>Basecamp Bench Report</title>\n"
        f"<style>\n{css}\n{identity_css}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{''.join(body)}\n"
        f'<script type="application/json" id="report-payload">{embedded}</script>\n'
        "</body>\n"
        "</html>\n"
    )
    return doc
