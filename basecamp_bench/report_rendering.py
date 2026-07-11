"""Pure deterministic rendering for self-contained benchmark HTML reports.

The renderer accepts an already validated payload and performs no filesystem,
leaderboard, or network access. Keeping this boundary pure makes visual output
reusable and independently testable.
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


def _model_color(model_id: str) -> str:
    """Stable contrast-conscious HSL from SHA-256 of model_id."""
    digest = hashlib.sha256(model_id.encode("utf-8")).hexdigest()
    hue = int(digest[:8], 16) % 360
    # Mid saturation/lightness for readable dark and light backgrounds.
    sat = 55 + (int(digest[8:10], 16) % 21)  # 55–75
    light = 42 + (int(digest[10:12], 16) % 17)  # 42–58
    return f"hsl({hue}, {sat}%, {light}%)"


def _embed_json(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # Prevent </script> breakout and HTML parsing of raw < characters.
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _chart_svg(section: Mapping[str, Any], section_index: int) -> str:
    models: list[dict[str, Any]] = list(section["models"])
    width, height = 720, 420
    margin_l, margin_r, margin_t, margin_b = 64, 28, 28, 56
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
        f'<line x1="{margin_l}" y1="{margin_t}" x2="{margin_l}" '
        f'y2="{margin_t + plot_h}" class="axis"/>',
        f'<line x1="{margin_l}" y1="{margin_t + plot_h}" '
        f'x2="{margin_l + plot_w}" y2="{margin_t + plot_h}" class="axis"/>',
        f'<text x="{margin_l + plot_w / 2}" y="{height - 12}" text-anchor="middle" '
        f'class="axis-label">Expected implementation cost (cheaper to the right)</text>',
        f'<text x="16" y="{margin_t + plot_h / 2}" text-anchor="middle" '
        f'class="axis-label" transform="rotate(-90 16 {margin_t + plot_h / 2})">'
        f"Score (quality)</text>",
        f'<text x="{margin_l}" y="{height - 30}" text-anchor="start" class="muted">'
        f"higher cost →</text>",
        f'<text x="{margin_l + plot_w}" y="{height - 30}" text-anchor="end" '
        f'class="muted">← lower cost (cheaper)</text>',
    ]

    # Frontier polyline in increasing score order.
    frontier_ids: list[str] = list(section.get("frontier") or [])
    by_id = {m["point_id"]: m for m in models}
    f_pts: list[str] = []
    for mid in frontier_ids:
        m = by_id.get(mid)
        if not m or m.get("expected_cost") is None:
            continue
        f_pts.append(f"{x_of(float(m['expected_cost'])):.2f},{y_of(float(m['score'])):.2f}")
    if len(f_pts) >= 2:
        parts.append(f'<polyline class="frontier-line" fill="none" points="{" ".join(f_pts)}"/>')
    elif len(f_pts) == 1:
        # Single frontier point still marked via point styling.
        pass

    for m in sorted(plottable, key=lambda item: item["point_id"]):
        mid = m["point_id"]
        score = float(m["score"])
        cost = float(m["expected_cost"])
        cx, cy = x_of(cost), y_of(score)
        color = _model_color(mid)
        cls = m.get("classification", "ineligible")
        if cls == "frontier":
            marker = "frontier-point"
            r = 6
        elif cls == "dominated":
            marker = "dominated-point"
            r = 5
        else:
            marker = "ineligible-point"
            r = 4

        s_err = float(m.get("score_stdev") or 0.0)
        c_err = float(m.get("cost_stdev") or 0.0)
        # Vertical error bar (score).
        y1, y2 = y_of(score + s_err), y_of(score - s_err)
        parts.append(
            f'<line class="error-bar" x1="{cx:.2f}" y1="{y1:.2f}" '
            f'x2="{cx:.2f}" y2="{y2:.2f}" stroke="{_escape(color)}"/>'
        )
        # Horizontal error bar in data space (cost); cheaper-right mapping.
        x1, x2 = x_of(cost - c_err), x_of(cost + c_err)
        parts.append(
            f'<line class="error-bar" x1="{x1:.2f}" y1="{cy:.2f}" '
            f'x2="{x2:.2f}" y2="{cy:.2f}" stroke="{_escape(color)}"/>'
        )
        parts.append(
            f'<circle class="{marker}" cx="{cx:.2f}" cy="{cy:.2f}" r="{r}" '
            f'fill="{_escape(color)}" data-model="{_escape(mid)}">'
            f"<title>{_escape(m.get('display_name') or mid)}: "
            f"score={score:.4g}, expected_cost={cost:.4g}</title></circle>"
        )
        parts.append(
            f'<text class="point-label" x="{cx + 8:.2f}" y="{cy - 8:.2f}">'
            f"{_escape(m.get('display_name') or mid)}</text>"
        )

    parts.append("</svg>")
    return "\n".join(parts)


def _dimension_table(models: Sequence[Mapping[str, Any]]) -> str:
    dim_keys: set[str] = set()
    for m in models:
        dims = m.get("dimensions") or {}
        if isinstance(dims, dict):
            dim_keys.update(str(k) for k in dims.keys())
    keys = sorted(dim_keys)
    if not keys:
        return '<p class="muted">No dimension scores.</p>'

    head = "".join(f'<th scope="col">{_escape(k)}</th>' for k in keys)
    rows: list[str] = []
    for m in models:
        dims = m.get("dimensions") or {}
        cells = "".join(f"<td>{_escape(_fmt_num(dims.get(k)))}</td>" for k in keys)
        rows.append(
            f'<tr><th scope="row">{_escape(m.get("display_name") or m["model_id"])}'
            f"</th>{cells}</tr>"
        )
    return (
        '<table class="dim-table">'
        "<caption>Per-model dimension scores</caption>"
        f'<thead><tr><th scope="col">Model</th>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _models_table(models: Sequence[Mapping[str, Any]]) -> str:
    headers = [
        "Model",
        "Score median",
        "Score mean",
        "Score stdev",
        "Score min",
        "Score max",
        "Score range",
        "Expected implementation cost per valid result",
        "Implementation cost median per attempt",
        "Implementation cost mean",
        "Implementation cost stdev",
        "Implementation cost min",
        "Implementation cost max",
        "Implementation cost range",
        "Evaluation overhead per attempt",
        "Success rate",
        "Repetitions",
        "Judge spread",
        "Tokens median",
        "Tokens mean",
        "Tokens min",
        "Tokens max",
        "Tokens range",
        "Duration median (s)",
        "Duration mean (s)",
        "Duration min (s)",
        "Duration max (s)",
        "Duration range (s)",
        "Classification",
        "Dominator",
        "Marginal cost/quality",
        "Eligible",
        "Ineligible reasons",
        "Run IDs",
        "Harness",
    ]
    thead = "".join(f'<th scope="col">{_escape(h)}</th>' for h in headers)
    rows: list[str] = []
    for m in models:
        reasons = ", ".join(m.get("ineligible_reasons") or []) or "—"
        run_ids = ", ".join(m.get("run_ids") or []) or "—"
        cells = [
            _escape(m.get("display_name") or m["model_id"]),
            _escape(_fmt_num(m.get("score"))),
            _escape(_fmt_num(m.get("score_mean"))),
            _escape(_fmt_num(m.get("score_stdev"))),
            _escape(_fmt_num(m.get("score_min"))),
            _escape(_fmt_num(m.get("score_max"))),
            _escape(_fmt_num(m.get("score_range"))),
            _escape(_fmt_num(m.get("expected_cost"))),
            _escape(_fmt_num(m.get("implementation_cost_per_attempt"))),
            _escape(_fmt_num(m.get("cost_mean"))),
            _escape(_fmt_num(m.get("cost_stdev"))),
            _escape(_fmt_num(m.get("cost_min"))),
            _escape(_fmt_num(m.get("cost_max"))),
            _escape(_fmt_num(m.get("cost_range"))),
            _escape(_fmt_num(m.get("evaluation_cost_per_attempt"))),
            _escape(_fmt_num(m.get("success_rate"))),
            _escape(m.get("repetitions")),
            _escape(_fmt_num(m.get("judge_spread"))),
            _escape(m.get("tokens")),
            _escape(_fmt_num(m.get("tokens_mean"))),
            _escape(m.get("tokens_min")),
            _escape(m.get("tokens_max")),
            _escape(m.get("tokens_range")),
            _escape(_fmt_num(m.get("duration_s"))),
            _escape(_fmt_num(m.get("duration_mean_s"))),
            _escape(_fmt_num(m.get("duration_min_s"))),
            _escape(_fmt_num(m.get("duration_max_s"))),
            _escape(_fmt_num(m.get("duration_range_s"))),
            _escape(m.get("classification")),
            _escape(m.get("dominator") if m.get("dominator") is not None else "—"),
            _escape(_fmt_num(m.get("marginal_cost_per_quality"))),
            _escape("yes" if m.get("eligible") else "no"),
            _escape(reasons),
            _escape(run_ids),
            _escape(m.get("harness")),
        ]
        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    return (
        '<table class="raw-table">'
        "<caption>Aggregate model metrics for this contract section</caption>"
        f"<thead><tr>{thead}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _raw_attempts_table(models: Sequence[Mapping[str, Any]]) -> str:
    headers = [
        "Run ID",
        "Submission ID",
        "Repetition",
        "Model",
        "Harness",
        "Implementation success",
        "Evaluation success",
        "Score",
        "Implementation cost",
        "Evaluation cost",
        "Evaluator count",
        "Tokens",
        "Duration",
        "Reason labels",
    ]
    thead = "".join(f'<th scope="col">{_escape(h)}</th>' for h in headers)
    rows: list[str] = []
    for m in models:
        raw_value = m.get("raw_attempts") or []
        assert isinstance(raw_value, Sequence) and not isinstance(raw_value, (str, bytes))
        raw_list = list(raw_value)
        assert all(isinstance(raw, Mapping) for raw in raw_list)
        raw_list.sort(key=raw_attempt_sort_key)
        for raw in raw_list:
            reasons = ", ".join(raw.get("ineligible_reasons") or []) or "—"
            evals = raw.get("evaluator_ids") or []
            cells = [
                _escape(raw.get("run_id")),
                _escape(raw.get("submission_id")),
                _escape(raw.get("repetition")),
                _escape(raw.get("display_name") or raw.get("model_id") or m.get("model_id")),
                _escape(raw.get("harness")),
                _escape("yes" if raw.get("implementation_success") else "no"),
                _escape("yes" if raw.get("evaluation_success") else "no"),
                _escape(_fmt_num(raw.get("score"))),
                _escape(_fmt_num(raw.get("implementation_cost_usd"))),
                _escape(_fmt_num(raw.get("evaluation_cost_usd"))),
                _escape(len(evals) if isinstance(evals, (list, tuple)) else 0),
                _escape(raw.get("tokens")),
                _escape(_fmt_num(raw.get("duration_s"))),
                _escape(reasons),
            ]
            rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    if not rows:
        rows.append('<tr><td colspan="14" class="muted">No raw attempts.</td></tr>')
    return (
        '<table class="attempts-table">'
        "<caption>Per-attempt raw results including failures "
        "(missing scores and costs shown as —)</caption>"
        f"<thead><tr>{thead}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
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
    <li><strong>Colors</strong> are stable HSL values derived from
      SHA-256(<code>model_id</code>). Classification is also labeled in text.</li>
    <li>Only run IDs/hashes are shown; filesystem paths are never rendered.</li>
  </ul>
</section>
"""


def render_report_html(payload: Mapping[str, Any]) -> str:
    """Render a self-contained offline HTML5 report for *payload*."""
    sections = list(payload.get("sections") or [])
    body_parts: list[str] = [
        "<header><h1>Basecamp Bench Report</h1>",
        '<p class="lede">Deterministic cost-vs-quality report. '
        "FE and BE tracks and contract revisions are shown in separate "
        "sections. Expected implementation cost uses implementation cost "
        "per attempt divided by success rate; cheaper models appear to the "
        "right on charts. Evaluation overhead is shown separately and does "
        "not affect the Pareto frontier.</p></header>",
        _methodology_html(),
    ]

    for index, section in enumerate(sections):
        track = section.get("track", "")
        cv = section.get("contract_version", "")
        sha = section.get("contract_sha256", "")
        schema = section.get("schema_version")
        generated = section.get("generated_at")
        models = list(section.get("models") or [])
        sid = f"section-{index}-{track}-{cv}"

        body_parts.append(f'<section class="track-section" id="{_escape(sid)}">')
        body_parts.append(f"<h2>Track {_escape(track)} · contract {_escape(cv)}</h2>")
        body_parts.append('<dl class="provenance">')
        body_parts.append(f"<dt>contract_sha256</dt><dd><code>{_escape(sha)}</code></dd>")
        body_parts.append(
            f"<dt>schema_version</dt><dd>{_escape(schema if schema is not None else 'null')}</dd>"
        )
        body_parts.append(
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
            body_parts.append(
                f"<dt>{name}</dt><dd><code>{_escape(section.get(name, 'null'))}</code></dd>"
            )
        body_parts.append(
            f"<dt>source timestamps</dt><dd>{_escape(', '.join(section.get('generated_at_values') or []) or '—')}</dd>"
        )
        body_parts.append(
            f"<dt>source run IDs</dt><dd>{_escape(', '.join(section.get('source_run_ids') or []) or '—')}</dd>"
        )
        body_parts.append(
            f"<dt>frontier</dt><dd>{_escape(', '.join(section.get('frontier') or []) or '—')}</dd>"
        )
        body_parts.append("</dl>")

        body_parts.append("<h3>Cost vs quality</h3>")
        body_parts.append(_chart_svg(section, index))

        body_parts.append("<h3>Dimension profile</h3>")
        profile = section.get("dimension_profile") or []
        body_parts.append(
            "<table><thead><tr><th>ID</th><th>Label</th><th>Weight</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td><code>{_escape(row.get('id'))}</code></td><td>{_escape(row.get('label'))}</td><td>{_fmt_num(row.get('weight'))}</td></tr>"
                for row in profile
                if isinstance(row, Mapping)
            )
            + "</tbody></table>"
        )
        body_parts.append(_dimension_table(models))

        body_parts.append("<h3>Aggregate model table</h3>")
        body_parts.append(_models_table(models))

        body_parts.append("<h3>Raw attempt table</h3>")
        body_parts.append(_raw_attempts_table(models))
        body_parts.append("</section>")

    embedded = _embed_json(payload)
    css = """
:root { color-scheme: light dark; --bg:#f7f7f5; --fg:#1a1a1a; --muted:#5c5c5c;
  --card:#fff; --border:#d0d0cc; --frontier:#0b6e4f; --dom:#8a6d3b; --inel:#8a8a8a; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141414; --fg:#f0f0f0; --muted:#a0a0a0; --card:#1e1e1e;
    --border:#333; --frontier:#3dcea0; --dom:#e0b15c; --inel:#888; }
}
* { box-sizing: border-box; }
body { margin:0; font: 15px/1.45 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
header, section { max-width: 1080px; margin: 0 auto; padding: 1.25rem 1rem; }
header { border-bottom: 1px solid var(--border); }
h1 { font-size: 1.6rem; margin: 0 0 .5rem; }
h2 { font-size: 1.25rem; margin-top: 1.5rem; }
h3 { font-size: 1.05rem; margin-top: 1.25rem; }
.lede, .muted { color: var(--muted); }
.track-section { background: var(--card); border: 1px solid var(--border);
  border-radius: 8px; margin: 1rem auto; }
.provenance { display:grid; grid-template-columns: 12rem 1fr; gap:.25rem .75rem; }
.provenance dt { font-weight:600; }
.provenance dd { margin:0; word-break: break-all; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; font-size: 13px; }
th, td { border: 1px solid var(--border); padding: .35rem .5rem; text-align: left; vertical-align: top; }
th { background: rgba(127,127,127,.12); }
.chart { width: 100%; max-width: 720px; height: auto; background: transparent; }
.chart-bg { fill: transparent; }
.axis { stroke: var(--fg); stroke-width: 1.2; }
.axis-label { fill: var(--fg); font-size: 12px; }
.frontier-line { stroke: var(--frontier); stroke-width: 2; stroke-dasharray: 4 2; }
.frontier-point { stroke: #000; stroke-width: 1.2; }
.dominated-point { opacity: .75; stroke: var(--dom); stroke-width: 1; stroke-dasharray: 2 2; }
.ineligible-point { opacity: .45; stroke: var(--inel); stroke-width: 1; fill-opacity: .5; }
.error-bar { stroke-width: 1; opacity: .7; }
.point-label { font-size: 11px; fill: var(--fg); }
.methodology ul { padding-left: 1.2rem; }
table caption { caption-side: top; text-align: left; font-weight: 600; margin-bottom: .35rem; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .92em; }
"""

    doc = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8"/>\n'
        "<title>Basecamp Bench Report</title>\n"
        f"<style>\n{css}\n</style>\n"
        "</head>\n"
        "<body>\n"
        f"{''.join(body_parts)}\n"
        f'<script type="application/json" id="report-payload">{embedded}</script>\n'
        "</body>\n"
        "</html>\n"
    )
    return doc
