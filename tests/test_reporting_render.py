"""Tests for report HTML rendering and report writing."""

from __future__ import annotations

import unittest

from basecamp_bench.reporting import (
    build_report_payload,
    load_leaderboards,
    render_report_html,
    write_report,
)
from tests._reporting_fixtures import (
    _entry,
    _leaderboard,
    _point,
)
from tests._support import TempDirTestCase


class RenderReportHtmlTests(unittest.TestCase):
    def _sample_payload(self) -> dict:
        points = [
            _point(
                model_id="alpha",
                display_name="Alpha",
                score=8.0,
                cost_per_attempt=2.0,
                score_stdev=0.3,
                cost_stdev=0.1,
                dimensions={"quality": 8.0, "craft": 7.0},
            ),
            _point(
                model_id="beta",
                display_name="Beta",
                score=5.0,
                cost_per_attempt=1.0,
                dimensions={"quality": 5.0, "craft": 5.5},
            ),
            _point(
                model_id="gamma",
                display_name="Gamma <script>alert(1)</script>",
                score=9.0,
                cost_per_attempt=0.5,
                eligible=False,
                ineligible_reasons=('evil "attr" & stuff',),
                run_ids=("run<script>",),
            ),
            _point(
                model_id="be-model",
                track="be",
                contract_version="9.9",
                contract_sha256="f" * 64,
                score=6.0,
                cost_per_attempt=1.5,
            ),
        ]
        return build_report_payload(points)

    def test_xss_escaping(self) -> None:
        payload = self._sample_payload()
        html_out = render_report_html(payload)
        self.assertNotIn("<script>alert(1)</script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)
        self.assertIn("evil &quot;attr&quot;", html_out)
        # Embedded JSON must not allow script breakout via literal </script>
        self.assertNotIn("</script>alert", html_out)
        self.assertIn("\\u003c", html_out)

    def test_no_network_or_external_resources(self) -> None:
        html_out = render_report_html(self._sample_payload())
        lowered = html_out.lower()
        for needle in (
            "http://",
            "https://",
            "cdn.",
            "<iframe",
            "fetch(",
            "xmlhttprequest",
            "websocket",
        ):
            self.assertNotIn(needle, lowered)

    def test_zero_cost_model_can_be_the_value_pick(self) -> None:
        points = [
            _point(
                model_id="leader",
                display_name="Leader",
                score=8.0,
                cost_per_attempt=2.0,
            ),
            _point(
                model_id="free",
                display_name="Free",
                score=6.0,
                cost_per_attempt=0.0,
            ),
        ]
        html_out = render_report_html(build_report_payload(points))
        # The zero-cost model must be named as the value pick in a verdict line
        # alongside the leader; the exact prose is presentation detail.
        verdicts = [seg for seg in html_out.split("</p>") if 'class="verdict"' in seg]
        self.assertTrue(any("Leader" in v and "Free" in v for v in verdicts))

    def test_chart_accessibility(self) -> None:
        html_out = render_report_html(self._sample_payload())
        self.assertIn('role="img"', html_out)
        self.assertIn("aria-label=", html_out)
        self.assertIn("<title>", html_out)
        self.assertIn("<desc>", html_out)

    def test_pivot_ordering_payload_and_no_external_assets(self) -> None:
        html_out = render_report_html(self._sample_payload())
        # The pivot joins tracks: FE columns precede BE columns.
        self.assertIn("FE score", html_out)
        self.assertIn("BE score", html_out)
        self.assertLess(html_out.index("FE score"), html_out.index("BE score"))
        # The full payload is embedded for programmatic consumers.
        self.assertIn("report-payload", html_out)
        # This fixture has repetitions=3 with nonzero stdev: score shows ±.
        self.assertIn("±", html_out)
        # Self-contained document: no external scripts or stylesheets.
        self.assertNotIn("<script src=", html_out.lower())
        self.assertNotIn('rel="stylesheet"', html_out.lower())

    def test_spread_marker_hidden_for_single_repetition(self) -> None:
        points = [
            _point(
                model_id="solo",
                score=7.0,
                cost_per_attempt=2.0,
                repetitions=1,
                score_stdev=0.0,
            ),
        ]
        html_out = render_report_html(build_report_payload(points))
        self.assertNotIn("±", html_out)
        self.assertIn("results-table", html_out)

    def test_commentary_renders_and_validates(self) -> None:
        commentary = {
            "briefing": ["What this benchmark measures."],
            "commentary": ["Closing thoughts prose."],
            "captions": {"Quality versus cost": "Hand-written caption."},
            "colors": {"alpha": "#123456"},
            "models": {
                "alpha": {
                    "headline": "custom headline",
                    "shines": [{"title": "Speed", "body": "Very fast."}],
                    "failure_modes": "Falls over on <edge> cases.",
                }
            },
            "methodology": [{"title": "Prompts", "paragraphs": ["One prompt per track."]}],
        }
        html_out = render_report_html(self._sample_payload(), commentary=commentary)
        self.assertIn("What this benchmark measures.", html_out)
        self.assertIn("Closing thoughts prose.", html_out)
        self.assertIn("Hand-written caption.", html_out)
        self.assertIn("custom headline", html_out)
        self.assertIn("Where it shines", html_out)
        self.assertIn("Very fast.", html_out)
        self.assertIn("Falls over on &lt;edge&gt; cases.", html_out)
        self.assertIn("One prompt per track.", html_out)
        self.assertIn("color: #123456", html_out)
        self.assertIn('href="#briefing"', html_out)
        self.assertIn('href="#commentary"', html_out)

    def test_commentary_unknown_model_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown model ids"):
            render_report_html(
                self._sample_payload(),
                commentary={"models": {"nope": {"headline": "x"}}},
            )

    def test_no_filesystem_paths_rendered(self) -> None:
        points = [
            _point(
                model_id="m",
                run_ids=("abc123",),
            )
        ]
        html_out = render_report_html(build_report_payload(points))
        self.assertIn("abc123", html_out)
        self.assertNotIn("/Users/", html_out)
        self.assertNotIn("C:\\", html_out)


class WriteReportTests(TempDirTestCase):
    def test_atomic_write_and_return_path(self) -> None:
        lb = self.write_json(
            "lb.json",
            _leaderboard(
                [
                    _entry("m1", score=5.0, cost_per_attempt=1.0),
                    _entry("m2", score=7.0, cost_per_attempt=3.0),
                ],
                track="fe",
                generated_at="2026-02-02T12:00:00Z",
            ),
        )
        out = self.root / "nested" / "report.html"
        result = write_report([lb], out)
        self.assertEqual(result, out)
        self.assertTrue(out.is_file())
        text = out.read_text(encoding="utf-8")
        self.assertIn("m1", text)
        self.assertIn("m2", text)
        self.assertIn("2026-02-02T12:00:00Z", text)
        self.assertTrue(text.startswith("<!DOCTYPE html>"))
        # No leftover temps
        leftovers = list(out.parent.glob(".report.html.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_display_name_renames_apply_everywhere(self) -> None:
        lb = self.write_json(
            "lb.json",
            _leaderboard(
                [
                    _entry("claude-fable-5", display_name="Claude", score=8.0),
                    _entry("gpt-5.6-sol", display_name="Codex", score=7.0),
                ]
            ),
        )
        out = self.root / "renamed.html"
        write_report(
            [lb],
            out,
            display_names={"claude-fable-5": "Fable 5", "gpt-5.6-sol": "GPT-5.6 Sol"},
        )
        text = out.read_text(encoding="utf-8")
        self.assertIn("Fable 5", text)
        self.assertIn("GPT-5.6 Sol", text)
        # The stale names must be gone from rendered HTML and the JSON payload.
        self.assertNotIn(">Claude<", text)
        self.assertNotIn(">Codex<", text)
        self.assertNotIn('"display_name":"Claude"', text)
        self.assertNotIn('"display_name":"Codex"', text)

    def test_display_name_rename_unknown_model_fails(self) -> None:
        lb = self.write_json("lb.json", _leaderboard([_entry("m1")]))
        out = self.root / "r.html"
        with self.assertRaisesRegex(ValueError, "not present"):
            write_report([lb], out, display_names={"nope": "Nope"})
        self.assertFalse(out.exists())

    def test_display_name_rename_validates_before_mutation(self) -> None:
        from basecamp_bench.reporting import rename_display_names

        lb = self.write_json("lb.json", _leaderboard([_entry("m1", display_name="Original")]))
        points = load_leaderboards([lb])
        original = points[0].display_name
        overlong = "x" * 257
        cases = [
            ("", "nonempty"),
            ("   ", "nonempty"),
            ("name\x00null", "control"),
            ("/Users/secret/path", "path"),
            ("C:\\Windows\\system32", "path"),
            ("file:///etc/passwd", "path"),
            ("python -m evil --flag", "path"),
            (overlong, "exceeds"),
        ]
        for name, needle in cases:
            with self.subTest(name=name[:40], needle=needle):
                with self.assertRaises(ValueError) as ctx:
                    rename_display_names(points, {"m1": name})
                self.assertRegex(str(ctx.exception).lower(), needle)
                self.assertEqual(points[0].display_name, original)
        renamed = rename_display_names(points, {"m1": "Café Model ✨"})
        self.assertEqual(renamed[0].display_name, "Café Model ✨")
        self.assertEqual(renamed[0].raw_attempts[0]["display_name"], "Café Model ✨")

    def test_write_report_deterministic_bytes(self) -> None:
        lb = self.write_json(
            "lb.json",
            _leaderboard(
                [
                    _entry("z-model", score=4.0),
                    _entry("a-model", score=6.0, cost_per_attempt=2.0),
                ]
            ),
        )
        out1 = self.root / "r1.html"
        out2 = self.root / "r2.html"
        write_report([lb], out1)
        write_report([lb], out2)
        self.assertEqual(out1.read_bytes(), out2.read_bytes())

    def test_write_report_cleanup_on_failure(self) -> None:
        # Invalid leaderboard should not leave the output or orphan tmp in a
        # successful state; parent may be created.
        bad = self.root / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        out = self.root / "out" / "report.html"
        with self.assertRaises(ValueError):
            write_report([bad], out)
        self.assertFalse(out.exists())
