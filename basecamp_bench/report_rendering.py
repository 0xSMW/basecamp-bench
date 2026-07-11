"""Pure deterministic rendering for self-contained benchmark HTML reports.

The renderer accepts an already validated payload and performs no filesystem,
leaderboard, or network access. Keeping this boundary pure makes visual output
reusable and independently testable.

The page is ordered for a human reader — verdict, scoreboard, chart,
dimension profile, comparison table — with audit material (provenance hashes,
methodology, per-attempt detail) collapsed or placed last. Machine consumers
read the embedded JSON payload, which carries every field unabridged.
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


def _escape(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _fmt_num(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _escape(value)
    if not math.isfinite(float(value)):
        return "—"
    return f"{float(value):.{digits}f}".rstrip("0").rstrip(".") if digits else str(value)


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


def _fmt_duration(value: Any) -> str:
    v = _finite(value)
    if v is None:
        return "—"
    total = int(round(v))
    if total >= 3600:
        return f"{total // 3600}h {total % 3600 // 60}m"
    if total >= 60:
        return f"{total // 60}m {total % 60}s"
    return f"{total}s"


# =============================================================================
# Model identity — one stable color per point, reused in every view
# =============================================================================


def _color_slug(point_id: str) -> str:
    return hashlib.sha256(point_id.encode("utf-8")).hexdigest()[:8]


def _color_class(point_id: str) -> str:
    return f"mc-{_color_slug(point_id)}"


def _model_hue_sat(point_id: str) -> tuple[int, int]:
    digest = hashlib.sha256(point_id.encode("utf-8")).hexdigest()
    hue = int(digest[:8], 16) % 360
    sat = 52 + (int(digest[8:10], 16) % 19)  # 52–70
    return hue, sat


def _identity_css(point_ids: Sequence[str]) -> str:
    """Per-model color classes, tuned separately for light and dark surfaces.

    Marks, chips, and bars use ``currentColor`` so a single class carries the
    identity everywhere. Identity is never color-alone: every colored mark is
    accompanied by the model's printed name.
    """
    light: list[str] = []
    dark: list[str] = []
    for pid in sorted(set(point_ids)):
        hue, sat = _model_hue_sat(pid)
        cls = _color_class(pid)
        light.append(f".{cls} {{ color: hsl({hue}, {sat}%, 37%); }}")
        dark.append(f".{cls} {{ color: hsl({hue}, {sat}%, 66%); }}")
    if not light:
        return ""
    return (
        "\n".join(light) + "\n@media (prefers-color-scheme: dark) {\n" + "\n".join(dark) + "\n}\n"
    )


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
# Verdict and scoreboard
# =============================================================================


def _model_name_html(m: Mapping[str, Any]) -> str:
    name = _escape(m.get("display_name") or m["model_id"])
    return f'<span class="hl-name {_color_class(str(m["point_id"]))}">{name}</span>'


def _ranked_for_verdict(models: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Scored models, eligible ones first — an ineligible point never 'leads'."""
    scored = [m for m in _sorted_models(models) if _finite(m.get("score")) is not None]
    eligible = [m for m in scored if m.get("eligible")]
    return eligible or scored


def _leader_point_id(models: Sequence[Mapping[str, Any]]) -> str | None:
    ranked = _ranked_for_verdict(models)
    return str(ranked[0]["point_id"]) if ranked else None


def _verdict_html(models: Sequence[Mapping[str, Any]]) -> str:
    """One computed sentence that answers the report's question."""
    scored = _ranked_for_verdict(models)
    if not scored:
        return '<p class="verdict muted">No scored results for this track.</p>'
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
    return f'<p class="verdict">{text}.</p>'


def _uniform_ineligibility(models: Sequence[Mapping[str, Any]]) -> str | None:
    """The shared reason string when every model is ineligible the same way.

    Exploratory local runs mark every point publication-ineligible; repeating
    that badge on each card is noise, so the section states it once instead.
    """
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


def _scoreboard_html(models: Sequence[Mapping[str, Any]], show_badges: bool = True) -> str:
    cards: list[str] = []
    ordered = _sorted_models(models)
    leader_id = _leader_point_id(models)
    for m in ordered:
        cls = _color_class(str(m["point_id"]))
        name = _escape(m.get("display_name") or m["model_id"])
        classification = str(m.get("classification") or "")
        badge = ""
        if classification == "frontier":
            badge = '<span class="badge badge-frontier">frontier</span>'
        elif classification and show_badges:
            badge = f'<span class="badge">{_escape(classification)}</span>'
        score = _fmt_score(m.get("score"))
        expected = _fmt_money(m.get("expected_cost"))
        tokens = _fmt_tokens(m.get("tokens"))
        duration = _fmt_duration(m.get("duration_s"))
        success = _fmt_percent(m.get("success_rate"))
        winner = " kpi-lead" if str(m["point_id"]) == leader_id else ""
        cards.append(
            f'<article class="kpi {cls}{winner}">'
            f'<div class="kpi-top"><span class="kpi-dot"></span>'
            f'<span class="kpi-name">{name}</span>{badge}</div>'
            f'<div class="kpi-score">{_escape(score)}</div>'
            f'<div class="kpi-meta">{_escape(expected)} per valid result</div>'
            f'<div class="kpi-meta muted">{_escape(duration)} · {_escape(tokens)} tokens '
            f"· {_escape(success)} success</div>"
            f"</article>"
        )
    return f'<div class="kpi-row">{"".join(cards)}</div>'


# =============================================================================
# Chart
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


def _chart_svg(section: Mapping[str, Any], section_index: int) -> str:
    models: list[dict[str, Any]] = list(section["models"])
    width, height = 960, 460
    margin_l, margin_r, margin_t, margin_b = 72, 36, 24, 64
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    plottable = [
        m
        for m in models
        if m.get("expected_cost") is not None
        and is_finite_number(m["expected_cost"])
        and is_finite_number(m["score"])
    ]

    track = section["track"]
    title = f"Cost vs quality for track {track}, contract {section['contract_version']}"
    aria = (
        f"Scatter chart of expected implementation cost versus score for {track}. "
        f"Expected implementation cost on the horizontal axis with cheaper "
        f"to the right. Score on the vertical axis. Includes error bars, "
        f"point labels, and a frontier polyline."
    )

    if not plottable:
        return (
            f'<svg class="chart" role="img" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" aria-label="{_escape(aria)}">'
            f"<title>{_escape(title)}</title>"
            f"<desc>{_escape(aria)}</desc>"
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            f'class="muted">No plottable points</text></svg>'
        )

    scores = [float(m["score"]) for m in plottable]
    costs = [float(m["expected_cost"]) for m in plottable]
    score_lo = min(scores)
    score_hi = max(scores)
    cost_lo = min(costs)
    cost_hi = max(costs)
    if score_hi == score_lo:
        score_lo -= 1.0
        score_hi += 1.0
    if cost_hi == cost_lo:
        cost_lo = max(0.0, cost_lo - 1.0)
        cost_hi = cost_hi + 1.0
    # Pad for error bars.
    score_pad = max((score_hi - score_lo) * 0.08, 0.05)
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
        f"<title>{_escape(title)}</title>",
        f"<desc>{_escape(aria)}</desc>",
        f'<rect x="0" y="0" width="{width}" height="{height}" class="chart-bg"/>',
    ]

    # Gridlines and tick labels. The x axis keeps the cheaper-right mapping,
    # so tick values are computed in cost space and placed through x_of.
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
        f'<text x="{margin_l + plot_w / 2}" y="{height - 12}" text-anchor="middle" '
        f'class="axis-label">Expected implementation cost (cheaper to the right)</text>',
        f'<text x="16" y="{margin_t + plot_h / 2}" text-anchor="middle" '
        f'class="axis-label" transform="rotate(-90 16 {margin_t + plot_h / 2})">'
        f"Score (quality)</text>",
        f'<text x="{margin_l}" y="{height - 32}" text-anchor="start" class="muted">'
        f"higher cost →</text>",
        f'<text x="{margin_l + plot_w}" y="{height - 32}" text-anchor="end" '
        f'class="muted">← lower cost (cheaper)</text>',
    ]

    # Frontier polyline in increasing score order.
    frontier_ids: list[str] = list(section.get("frontier") or [])
    by_id = {m["point_id"]: m for m in models}
    f_pts: list[tuple[float, float]] = []
    for mid in frontier_ids:
        m = by_id.get(mid)
        if not m or m.get("expected_cost") is None:
            continue
        f_pts.append((x_of(float(m["expected_cost"])), y_of(float(m["score"]))))
    if len(f_pts) >= 2:
        points_attr = " ".join(f"{x:.2f},{y:.2f}" for x, y in f_pts)
        parts.append(f'<polyline class="frontier-line" fill="none" points="{points_attr}"/>')
        mid_x = (f_pts[0][0] + f_pts[-1][0]) / 2
        mid_y = (f_pts[0][1] + f_pts[-1][1]) / 2
        parts.append(
            f'<text class="frontier-label" x="{mid_x + 10:.2f}" y="{mid_y - 10:.2f}">'
            f"Pareto frontier — best quality per dollar</text>"
        )

    for m in sorted(plottable, key=lambda item: item["point_id"]):
        mid = m["point_id"]
        score = float(m["score"])
        cost = float(m["expected_cost"])
        cx, cy = x_of(cost), y_of(score)
        color_cls = _color_class(str(mid))
        cls = m.get("classification", "ineligible")
        if cls == "frontier":
            marker = "frontier-point"
            r = 7
        elif cls == "dominated":
            marker = "dominated-point"
            r = 6
        else:
            marker = "ineligible-point"
            r = 5

        s_err = float(m.get("score_stdev") or 0.0)
        c_err = float(m.get("cost_stdev") or 0.0)
        # Vertical error bar (score).
        y1, y2 = y_of(score + s_err), y_of(score - s_err)
        parts.append(
            f'<line class="error-bar {color_cls}" x1="{cx:.2f}" y1="{y1:.2f}" '
            f'x2="{cx:.2f}" y2="{y2:.2f}" stroke="currentColor"/>'
        )
        # Horizontal error bar in data space (cost); cheaper-right mapping.
        x1, x2 = x_of(cost - c_err), x_of(cost + c_err)
        parts.append(
            f'<line class="error-bar {color_cls}" x1="{x1:.2f}" y1="{cy:.2f}" '
            f'x2="{x2:.2f}" y2="{cy:.2f}" stroke="currentColor"/>'
        )
        parts.append(
            f'<circle class="{marker} {color_cls}" cx="{cx:.2f}" cy="{cy:.2f}" r="{r}" '
            f'fill="currentColor" data-model="{_escape(mid)}">'
            f"<title>{_escape(m.get('display_name') or mid)}: "
            f"score={score:.4g}, expected_cost={cost:.4g}</title></circle>"
        )
        # Labels flip to the left of the marker near the right edge so cheap
        # models (plotted rightmost) never clip outside the canvas.
        if cx > margin_l + plot_w * 0.8:
            label_x, anchor = cx - 10, "end"
        else:
            label_x, anchor = cx + 10, "start"
        parts.append(
            f'<text class="point-label" x="{label_x:.2f}" y="{cy - 10:.2f}" '
            f'text-anchor="{anchor}">{_escape(m.get("display_name") or mid)}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


# =============================================================================
# Dimension profile — bars for reading, a table view for lookup
# =============================================================================


def _dimension_profile_map(
    profile: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, float]]:
    labels: dict[str, str] = {}
    weights: dict[str, float] = {}
    for row in profile:
        if not isinstance(row, Mapping) or row.get("id") is None:
            continue
        rid = str(row["id"])
        if row.get("label"):
            labels[rid] = str(row["label"])
        weight = _finite(row.get("weight"))
        if weight is not None:
            weights[rid] = weight
    return labels, weights


def _dimension_bars(
    models: Sequence[Mapping[str, Any]],
    profile: Sequence[Mapping[str, Any]],
) -> str:
    dim_keys: set[str] = set()
    for m in models:
        dims = m.get("dimensions") or {}
        if isinstance(dims, dict):
            dim_keys.update(str(k) for k in dims.keys())
    if not dim_keys:
        return '<p class="muted">No dimension scores.</p>'

    labels, weights = _dimension_profile_map(profile)
    # Heaviest dimensions first; alphabetical only as a deterministic tiebreak.
    keys = sorted(dim_keys, key=lambda k: (-(weights.get(k, 0.0)), k))
    ordered_models = _sorted_models(models)

    all_values = [
        _finite((m.get("dimensions") or {}).get(k))
        for m in models
        for k in dim_keys
        if isinstance(m.get("dimensions"), dict)
    ]
    observed = [v for v in all_values if v is not None]
    scale = max(10.0, max(observed) if observed else 10.0)

    blocks: list[str] = []
    for k in keys:
        weight = weights.get(k)
        weight_html = (
            f'<span class="dim-w">weight {_escape(_fmt_percent(weight))}</span>'
            if weight is not None
            else ""
        )
        rows: list[str] = []
        for m in ordered_models:
            dims = m.get("dimensions") or {}
            value = _finite(dims.get(k)) if isinstance(dims, dict) else None
            name = _escape(m.get("display_name") or m["model_id"])
            cls = _color_class(str(m["point_id"]))
            if value is None:
                rows.append(
                    f'<span class="dim-m">{name}</span>'
                    f'<span class="dim-track"></span>'
                    f'<span class="dim-v muted">—</span>'
                )
                continue
            pct = max(0.0, min(value / scale, 1.0)) * 100
            rows.append(
                f'<span class="dim-m">{name}</span>'
                f'<span class="dim-track"><span class="dim-fill {cls}" '
                f'style="width:{pct:.1f}%"></span></span>'
                f'<span class="dim-v">{_escape(_fmt_score(value))}</span>'
            )
        blocks.append(
            f'<div class="dim"><div class="dim-head">{_escape(labels.get(k, k))}'
            f"{weight_html}</div>"
            f'<div class="dim-grid">{"".join(rows)}</div></div>'
        )
    caption = (
        f'<p class="muted dim-note">Judge scores per weighted dimension, '
        f"0–{_fmt_score(scale)} scale.</p>"
    )
    return f'<div class="dims">{"".join(blocks)}</div>{caption}'


def _dimension_table(
    models: Sequence[Mapping[str, Any]],
    profile: Sequence[Mapping[str, Any]],
) -> str:
    """Accessible table view of the dimension bars, collapsed by default."""
    dim_keys: set[str] = set()
    for m in models:
        dims = m.get("dimensions") or {}
        if isinstance(dims, dict):
            dim_keys.update(str(k) for k in dims.keys())
    if not dim_keys:
        return ""

    labels, weights = _dimension_profile_map(profile)
    keys = sorted(dim_keys, key=lambda k: (-(weights.get(k, 0.0)), k))
    head_cells: list[str] = []
    for k in keys:
        label = _escape(labels.get(k, k))
        weight = weights.get(k)
        sub = (
            f'<span class="dim-weight">weight {_escape(_fmt_percent(weight))}</span>'
            if weight is not None
            else ""
        )
        head_cells.append(f'<th scope="col" class="num">{label}{sub}</th>')
    rows: list[str] = []
    for m in _sorted_models(models):
        dims = m.get("dimensions") or {}
        cells = "".join(f'<td class="num">{_escape(_fmt_score(dims.get(k)))}</td>' for k in keys)
        rows.append(
            f'<tr><th scope="row">{_escape(m.get("display_name") or m["model_id"])}'
            f"</th>{cells}</tr>"
        )
    return (
        '<details class="table-view"><summary>Dimension score table</summary>'
        '<div class="table-scroll"><table class="dim-table">'
        "<caption>Dimension scores</caption>"
        f'<thead><tr><th scope="col">Model</th>{"".join(head_cells)}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div></details>"
    )


# =============================================================================
# Comparison and attempts tables
# =============================================================================


def _classification_cell(m: Mapping[str, Any]) -> str:
    """Classification with ineligibility reasons folded in."""
    label = str(m.get("classification") or "—")
    reasons = [str(r) for r in (m.get("ineligible_reasons") or [])]
    if not m.get("eligible") and reasons:
        label = f"{label} — {', '.join(reasons)}"
    return label


def _models_table(models: Sequence[Mapping[str, Any]]) -> str:
    # Single-repetition sections collapse every spread statistic to the point
    # value, so stdev/min/max columns are shown only when a model actually has
    # repeats. Full statistics stay in the JSON payload.
    show_spread = any((m.get("repetitions") or 0) > 1 for m in models)

    headers: list[tuple[str, bool]] = [("Model", False), ("Score", True)]
    if show_spread:
        headers += [("Score stdev", True), ("Score min–max", True)]
    headers += [
        ("Expected implementation cost per valid result", True),
        ("Implementation cost per attempt", True),
    ]
    if show_spread:
        headers += [("Impl cost stdev", True), ("Impl cost min–max", True)]
    headers += [
        ("Evaluation overhead per attempt", True),
        ("Total cost per attempt", True),
        ("Success rate", True),
    ]
    if show_spread:
        headers.append(("Repetitions", True))
    headers += [
        ("Tokens", True),
        ("Duration", True),
        ("Classification", False),
    ]
    thead = "".join(
        '<th scope="col"{}>{}</th>'.format(' class="num"' if num else "", _escape(h))
        for h, num in headers
    )
    rows: list[str] = []
    leader_id = _leader_point_id(models)
    for m in _sorted_models(models):
        cls = _color_class(str(m["point_id"]))
        name = _escape(m.get("display_name") or m["model_id"])
        model_cell = f'<span class="chip {cls}"></span>{name}'
        cells: list[tuple[str, bool]] = [
            (model_cell, False),
            (_escape(_fmt_score(m.get("score"))), True),
        ]
        if show_spread:
            cells += [
                (_escape(_fmt_score(m.get("score_stdev"))), True),
                (
                    _escape(f"{_fmt_score(m.get('score_min'))}–{_fmt_score(m.get('score_max'))}"),
                    True,
                ),
            ]
        cells += [
            (_escape(_fmt_money(m.get("expected_cost"))), True),
            (_escape(_fmt_money(m.get("implementation_cost_per_attempt"))), True),
        ]
        if show_spread:
            cells += [
                (_escape(_fmt_money(m.get("cost_stdev"))), True),
                (
                    _escape(f"{_fmt_money(m.get('cost_min'))}–{_fmt_money(m.get('cost_max'))}"),
                    True,
                ),
            ]
        cells += [
            (_escape(_fmt_money(m.get("evaluation_cost_per_attempt"))), True),
            (_escape(_fmt_money(m.get("total_cost_per_attempt"))), True),
            (_escape(_fmt_percent(m.get("success_rate"))), True),
        ]
        if show_spread:
            cells.append((_escape(m.get("repetitions")), True))
        cells += [
            (_escape(_fmt_tokens(m.get("tokens"))), True),
            (_escape(_fmt_duration(m.get("duration_s"))), True),
            (_escape(_classification_cell(m)), False),
        ]
        row_cls = ' class="winner"' if str(m["point_id"]) == leader_id else ""
        rows.append(
            f"<tr{row_cls}>"
            + "".join("<td{}>{}</td>".format(' class="num"' if num else "", c) for c, num in cells)
            + "</tr>"
        )
    return (
        '<div class="table-scroll"><table class="raw-table">'
        "<caption>Aggregate model metrics for this contract section</caption>"
        f"<thead><tr>{thead}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _raw_attempts_table(models: Sequence[Mapping[str, Any]]) -> str:
    # Purely mechanical columns (run/submission ids, evaluator counts) stay in
    # the JSON payload; conditional columns render only when they carry signal.
    attempts: list[Mapping[str, Any]] = []
    for m in models:
        raw_value = m.get("raw_attempts") or []
        assert isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes))
        raw_list = list(raw_value)
        assert all(isinstance(raw, Mapping) for raw in raw_list)
        for raw in raw_list:
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

    headers: list[tuple[str, bool]] = [("Model", False)]
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
            (_escape(_fmt_duration(raw.get("duration_s"))), True),
        ]
        if show_reasons:
            reasons = ", ".join(raw.get("ineligible_reasons") or []) or "—"
            cells.append((_escape(reasons), False))
        rows.append(
            "<tr>"
            + "".join("<td{}>{}</td>".format(' class="num"' if num else "", c) for c, num in cells)
            + "</tr>"
        )
    if not rows:
        rows.append(f'<tr><td colspan="{len(headers)}" class="muted">No raw attempts.</td></tr>')
    table = (
        '<div class="table-scroll"><table class="attempts-table">'
        "<caption>Per-attempt raw results including failures "
        "(missing scores and costs shown as —)</caption>"
        f"<thead><tr>{thead}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )
    if has_problem:
        # Failures are part of the story — keep them visible.
        return f"<h3>Attempts</h3>{table}"
    count = len(attempts)
    noun = "attempt" if count == 1 else "attempts"
    return (
        f'<details class="table-view"><summary>Per-attempt detail '
        f"({count} {noun})</summary>{table}</details>"
    )


def _methodology_html() -> str:
    return """
<section class="methodology" id="methodology">
  <h2>Methodology and provenance</h2>
  <ul>
    <li><strong>Expected implementation cost per valid result</strong> is
      <code>implementation_cost_per_attempt / success_rate</code>
      (equal to <code>cost_per_attempt / success_rate</code> when aggregates
      are consistent). It estimates the expected implementation cost per valid
      submission. When success rate is zero, or inputs are non-finite or
      negative, expected cost is undefined and the point cannot join the
      Pareto frontier.</li>
    <li><strong>Evaluation overhead per attempt</strong>
      (<code>evaluation_cost_per_attempt</code>) is reported for transparency
      only. It never changes Pareto membership or expected implementation
      cost.</li>
    <li><strong>Total cost per attempt</strong> is observed implementation cost
      plus evaluator overhead. The frontier continues to use implementation
      cost so evaluator routing does not change contestant cost ranking.</li>
    <li><strong>End-to-end agent duration</strong> is implementation process
      time plus the critical-path evaluator process time. Parallel evaluators
      contribute their maximum duration rather than their sum; queueing,
      snapshot copying, aggregation, and report rendering are excluded.</li>
    <li><strong>Runner compatibility</strong>: local exploratory reports may
      combine matching benchmark evidence from different runner revisions and
      list every source hash. Publication reports keep runner revisions in
      separate comparison sections.</li>
    <li><strong>Success handling</strong>: a success rate of zero forces the
      entry ineligible even if the source marked it eligible. Ineligible and
      failed attempts remain visible in tables and denominators.</li>
    <li><strong>Pareto frontier</strong>: among eligible points with finite
      nonnegative score and expected implementation cost, point A dominates B
      when A has score ≥ B and expected cost ≤ B, with at least one strict
      inequality. Exact score/cost ties keep the lexicographically smaller
      <code>model_id</code> as the frontier representative. Among multiple
      dominators, prefer lowest expected cost, then highest score, then
      lexicographically smallest model id.</li>
    <li><strong>Marginal cost per quality</strong> is computed only between
      adjacent frontier points ordered by increasing score:
      Δexpected_cost / Δscore when Δscore &gt; 0.</li>
    <li><strong>Contracts and hashes</strong>: FE and BE tracks are never
      combined. Distinct <code>contract_version</code> /
      <code>contract_sha256</code> values form separate report sections.</li>
    <li><strong>Schema version and generated_at</strong> are carried from the
      source leaderboard files only; this report does not inject wall-clock
      time.</li>
    <li><strong>Harness</strong> identifies the agent/tooling configuration
      that produced the attempts.</li>
    <li><strong>Judge spread and error bars</strong>: score standard deviation
      and judge spread quantify observed variance; cost standard deviation is
      shown as horizontal error bars. These do not make nondeterministic
      models deterministic.</li>
    <li><strong>Colors</strong> are stable hues derived from
      SHA-256(<code>model_id</code>). Classification is also labeled in text.</li>
    <li>Only run IDs/hashes are shown; filesystem paths are never rendered.</li>
  </ul>
</section>
"""


def _provenance_details(section: Mapping[str, Any]) -> str:
    parts: list[str] = [
        '<details class="provenance-box"><summary>Provenance and hashes</summary>',
        '<dl class="provenance">',
    ]
    sha = section.get("contract_sha256", "")
    schema = section.get("schema_version")
    generated = section.get("generated_at")
    parts.append(f"<dt>contract_sha256</dt><dd><code>{_escape(sha)}</code></dd>")
    parts.append(
        f"<dt>schema_version</dt><dd>{_escape(schema if schema is not None else 'null')}</dd>"
    )
    parts.append(
        f"<dt>generated_at</dt><dd>{_escape(generated if generated is not None else 'null')}</dd>"
    )
    for name in (
        "mode",
        "runner_source_sha256",
        "seed_tree_sha256",
        "reference_manifest_sha256",
        "reference_tree_sha256",
        "prompt_sha256",
        "rubric_sha256",
        "schema_bundle_sha256",
    ):
        parts.append(f"<dt>{name}</dt><dd><code>{_escape(section.get(name, 'null'))}</code></dd>")
    runner_sources = section.get("runner_source_sha256_values") or []
    if len(runner_sources) > 1:
        parts.append(f"<dt>source runner hashes</dt><dd>{_escape(', '.join(runner_sources))}</dd>")
    parts.append(
        f"<dt>source timestamps</dt><dd>{_escape(', '.join(section.get('generated_at_values') or []) or '—')}</dd>"
    )
    parts.append(
        f"<dt>source run IDs</dt><dd>{_escape(', '.join(section.get('source_run_ids') or []) or '—')}</dd>"
    )
    parts.append(
        f"<dt>frontier</dt><dd>{_escape(', '.join(section.get('frontier') or []) or '—')}</dd>"
    )
    parts.append("</dl></details>")
    return "".join(parts)


def render_report_html(payload: Mapping[str, Any]) -> str:
    """Render a self-contained offline HTML5 report for *payload*."""
    sections = list(payload.get("sections") or [])
    all_point_ids: list[str] = [
        str(m["point_id"])
        for section in sections
        for m in (section.get("models") or [])
        if isinstance(m, Mapping) and m.get("point_id") is not None
    ]

    body_parts: list[str] = [
        "<header><h1>Basecamp Bench Report</h1>",
        '<p class="lede">Which agent builds it best, and at what cost. '
        "Cheaper sits to the right on every chart; methodology and provenance "
        "are at the end.</p></header>",
    ]

    for index, section in enumerate(sections):
        track = section.get("track", "")
        cv = section.get("contract_version", "")
        models = list(section.get("models") or [])
        profile = [
            row for row in (section.get("dimension_profile") or []) if isinstance(row, Mapping)
        ]
        sid = f"section-{index}-{track}-{cv}"

        body_parts.append(f'<section class="track-section" id="{_escape(sid)}">')
        body_parts.append(f'<p class="eyebrow">Track {_escape(track)} · contract {_escape(cv)}</p>')
        body_parts.append(_verdict_html(models))
        shared_ineligibility = _uniform_ineligibility(models)
        if shared_ineligibility is not None:
            reason = f" ({_escape(shared_ineligibility)})" if shared_ineligibility else ""
            body_parts.append(
                f'<p class="note muted">Exploratory run — every model is outside '
                f"publication eligibility{reason}.</p>"
            )
        body_parts.append(_scoreboard_html(models, show_badges=shared_ineligibility is None))

        body_parts.append("<h3>Cost vs quality</h3>")
        body_parts.append(_chart_svg(section, index))

        body_parts.append("<h3>Dimension scores</h3>")
        body_parts.append(_dimension_bars(models, profile))
        body_parts.append(_dimension_table(models, profile))

        body_parts.append("<h3>Model comparison</h3>")
        body_parts.append(_models_table(models))

        body_parts.append(_raw_attempts_table(models))
        body_parts.append(_provenance_details(section))
        body_parts.append("</section>")

    body_parts.append(_methodology_html())

    embedded = _embed_json(payload)
    identity_css = _identity_css(all_point_ids)
    css = """
:root { color-scheme: light dark; --bg:#f6f6f3; --fg:#17181a; --muted:#6a6d71;
  --card:#fff; --border:#e3e3de; --hairline:#ecece7; --frontier:#0b6e4f;
  --dom:#8a6d3b; --inel:#8a8a8a; --track:rgba(23,24,26,.08); }
@media (prefers-color-scheme: dark) {
  :root { --bg:#101113; --fg:#ecedee; --muted:#9ba0a6; --card:#17191c;
    --border:#2a2d31; --hairline:#222528; --frontier:#3dcea0; --dom:#e0b15c;
    --inel:#888; --track:rgba(236,237,238,.1); }
}
* { box-sizing: border-box; }
body { margin:0; font: 15px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
header, section { width: 100%; margin: 0;
  padding: 1.25rem clamp(1rem, 2vw, 2rem); }
header { border-bottom: 1px solid var(--border); padding-top: 1.5rem; }
h1 { font-size: 1.15rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
h2 { font-size: 1.2rem; margin: 0 0 .75rem; }
h3 { font-size: .72rem; margin: 2.25rem 0 .75rem; text-transform: uppercase;
  letter-spacing: .09em; color: var(--muted); font-weight: 650; }
.lede, .muted { color: var(--muted); }
.lede { margin: 0; font-size: .9rem; }
.eyebrow { font-size: .72rem; text-transform: uppercase; letter-spacing: .09em;
  color: var(--muted); font-weight: 650; margin: 0 0 .5rem; }
.track-section { background: var(--card); border: 1px solid var(--border);
  border-radius: 12px; margin: 1.25rem clamp(.75rem, 1.5vw, 1.5rem);
  width: auto; padding: 1.75rem clamp(1.25rem, 2vw, 2.25rem) 1.5rem; }
.verdict { font-size: clamp(1.25rem, 2.2vw, 1.6rem); font-weight: 650;
  line-height: 1.3; letter-spacing: -.015em; margin: 0 0 1.5rem; max-width: 62ch; }
.hl-name { color: currentColor; }
.verdict .hl-name { border-bottom: 3px solid currentColor; padding-bottom: 1px; }
.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: .75rem; margin: 0 0 .5rem; }
.kpi { border: 1px solid var(--border); border-radius: 10px; padding: .9rem 1rem;
  background: var(--bg); }
.kpi-lead { border-color: color-mix(in srgb, currentColor 45%, var(--border)); }
.kpi-top { display: flex; align-items: center; gap: .5rem; margin-bottom: .4rem; }
.kpi-dot { width: 10px; height: 10px; border-radius: 50%;
  background: currentColor; flex: none; }
.kpi-name { color: var(--fg); font-weight: 650; font-size: .9rem; min-width: 0; }
.badge { margin-left: auto; font-size: .66rem; text-transform: uppercase;
  letter-spacing: .07em; font-weight: 650; color: var(--muted);
  border: 1px solid var(--border); border-radius: 99px; padding: .1rem .5rem;
  white-space: nowrap; }
.badge-frontier { color: var(--frontier); border-color: var(--frontier); }
.kpi-score { color: var(--fg); font-size: 2.3rem; font-weight: 700;
  font-variant-numeric: tabular-nums; letter-spacing: -.02em; line-height: 1.1; }
.kpi-meta { color: var(--fg); font-size: .82rem; margin-top: .3rem;
  font-variant-numeric: tabular-nums; }
.kpi-meta.muted { color: var(--muted); margin-top: .1rem; }
.dims { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 1rem 2.5rem; }
.dim-head { font-size: .85rem; font-weight: 650; margin-bottom: .35rem;
  display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; }
.dim-w { color: var(--muted); font-weight: 400; font-size: .74rem;
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.dim-grid { display: grid; grid-template-columns: minmax(9rem, max-content) 1fr 2.5rem;
  gap: .3rem .6rem; align-items: center; }
.dim-m { font-size: .78rem; color: var(--muted); text-align: right;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.dim-track { display: block; height: 10px; background: var(--track);
  border-radius: 5px; overflow: hidden; }
.dim-fill { display: block; height: 100%; background: currentColor;
  border-radius: 0 4px 4px 0; }
.dim-v { font-size: .78rem; font-variant-numeric: tabular-nums; text-align: right; }
.dim-note { font-size: .78rem; margin: .75rem 0 0; }
.table-view { margin-top: 1rem; }
.table-view summary, .provenance-box summary { cursor: pointer;
  color: var(--muted); font-size: .82rem; font-weight: 600; }
.provenance-box { margin-top: 2rem; padding-top: 1rem;
  border-top: 1px solid var(--hairline); }
.provenance { display:grid; grid-template-columns: 14rem 1fr; gap:.25rem .75rem;
  margin: .75rem 0 0; font-size: 12.5px; }
.provenance dt { font-weight:600; }
.provenance dd { margin:0; word-break: break-all; color: var(--muted); }
.table-scroll { overflow-x: auto; margin: .5rem 0 1rem; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { border: 0; border-bottom: 1px solid var(--hairline);
  padding: .5rem .75rem; text-align: left; vertical-align: top; white-space: nowrap; }
thead th { font-size: .68rem; text-transform: uppercase; letter-spacing: .06em;
  color: var(--muted); border-bottom: 1.5px solid var(--border);
  white-space: normal; vertical-align: bottom; }
th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.winner td { background: color-mix(in srgb, var(--frontier) 7%, transparent); }
tr.winner td:first-child { box-shadow: inset 3px 0 0 var(--frontier); }
.chip { display: inline-block; width: 9px; height: 9px; border-radius: 50%;
  background: currentColor; margin-right: .5rem; vertical-align: baseline; }
.dim-weight { display: block; font-weight: 400; font-size: 10px;
  letter-spacing: 0; text-transform: none; color: var(--muted); }
.chart { width: 100%; max-width: 1200px; height: auto; background: transparent;
  margin-top: .25rem; }
.chart-bg { fill: transparent; }
.axis { stroke: var(--fg); stroke-width: 1.2; }
.axis-label { fill: var(--fg); font-size: 12.5px; }
.grid { stroke: var(--hairline); stroke-width: 1; }
.tick-label { fill: var(--muted); font-size: 11px; }
.frontier-line { stroke: var(--frontier); stroke-width: 2; stroke-dasharray: 4 2; }
.frontier-label { fill: var(--frontier); font-size: 11.5px; font-weight: 600; }
.frontier-point { stroke: var(--card); stroke-width: 1.5; }
.dominated-point { opacity: .8; stroke: var(--card); stroke-width: 1.5; }
.ineligible-point { opacity: .75; fill-opacity: .75; stroke: var(--card);
  stroke-width: 1.5; }
.note { font-size: .85rem; margin: -1rem 0 1.25rem; }
.error-bar { stroke-width: 1.25; opacity: .55; }
.point-label { font-size: 12.5px; font-weight: 600; fill: var(--fg); }
.methodology ul { padding-left: 1.2rem; max-width: 82ch; }
.methodology h2 { font-size: .9rem; }
.methodology { color: var(--muted); font-size: .85rem; }
table caption { caption-side: top; text-align: left; font-weight: 600;
  margin-bottom: .35rem; color: var(--muted); font-size: 12.5px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .92em; }
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
        f"{''.join(body_parts)}\n"
        f'<script type="application/json" id="report-payload">{embedded}</script>\n'
        "</body>\n"
        "</html>\n"
    )
    return doc
