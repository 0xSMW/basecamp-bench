from __future__ import annotations

import unittest

from basecamp_bench.naming import (
    judge_path_name,
    run_path_name,
    submission_path_name,
)


class NamingTests(unittest.TestCase):
    def test_single_model_run_name(self) -> None:
        self.assertEqual(
            run_path_name(
                "20260711t170807z-db8473",
                tracks=("be", "fe"),
                contestants=(("xai", "grok-4.5"),),
            ),
            (
                "2026-07-11T17-08-07Z--fe-be--xai-grok-4-5--"
                "20260711t170807z-db8473"
            ),
        )

    def test_multi_model_and_injected_run_names(self) -> None:
        self.assertEqual(
            run_path_name(
                "20260711t161255z-843ef2",
                tracks=("fe", "be"),
                contestants=(
                    ("openai", "gpt-5.6-sol"),
                    ("anthropic", "claude-fable-5"),
                    ("openrouter", "glm-5.2"),
                    ("xai", "grok-4.5"),
                ),
            ),
            (
                "2026-07-11T16-12-55Z--fe-be--anthropic-claude-fable-5_"
                "openai-gpt-5-6-sol_openrouter-glm-5-2_xai-grok-4-5--"
                "20260711t161255z-843ef2"
            ),
        )
        self.assertEqual(
            run_path_name(
                "abc123", tracks=("fe",), contestants=(("xai", "grok-4.5"),)
            ),
            "run--fe--xai-grok-4-5--abc123",
        )

    def test_short_shared_tail_does_not_alias_distinct_run_ids(self) -> None:
        first = run_path_name(
            "invalid-1-001", tracks=("fe",), contestants=(("xai", "grok-4.5"),)
        )
        second = run_path_name(
            "invalid-2-001", tracks=("fe",), contestants=(("xai", "grok-4.5"),)
        )
        self.assertNotEqual(first, second)

    def test_submission_names_remove_harness_model_overlap(self) -> None:
        cases = (
            (
                dict(
                    track="fe",
                    harness="claude",
                    provider="anthropic",
                    model="claude-fable-5",
                    repetition=1,
                    submission_id="id-0b1ef122",
                ),
                "fe-claude-anthropic-claude-fable-5-r1--0b1ef122",
            ),
            (
                dict(
                    track="be",
                    harness="codex",
                    provider="openai",
                    model="gpt-5.6-sol",
                    repetition=2,
                    submission_id="id-4d93e09d",
                ),
                "be-codex-openai-gpt-5-6-sol-r2--4d93e09d",
            ),
            (
                dict(
                    track="fe",
                    harness="pi-glm",
                    provider="openrouter",
                    model="glm-5.2",
                    repetition=1,
                    submission_id="id-ce950a6a",
                ),
                "fe-pi-glm-openrouter-glm-5-2-r1--ce950a6a",
            ),
        )
        for values, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(submission_path_name(**values), expected)

    def test_judge_is_readable(self) -> None:
        self.assertEqual(
            judge_path_name(
                harness="codex",
                provider="openai",
                model="gpt-5.6-sol",
                eval_attempt_id="id-4c5ff05c",
            ),
            "judge-codex-openai-gpt-5-6-sol--4c5ff05c",
        )

    def test_long_names_are_bounded_and_keep_suffix(self) -> None:
        value = submission_path_name(
            track="fe",
            harness="provider",
            provider="provider-family",
            model="x" * 300,
            repetition=1,
            submission_id="id-deadbeef",
        )
        self.assertLessEqual(len(value), 160)
        self.assertTrue(value.endswith("--deadbeef"))


if __name__ == "__main__":
    unittest.main()
