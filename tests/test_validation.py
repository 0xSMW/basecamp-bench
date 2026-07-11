from __future__ import annotations

import math
import unittest

from basecamp_bench.validation import is_finite_number, is_sha256_hex


class PrimitiveValidationTests(unittest.TestCase):
    def test_finite_numbers_exclude_bool_nonfinite_and_unrepresentable_ints(self) -> None:
        self.assertTrue(is_finite_number(0))
        self.assertTrue(is_finite_number(1.25))
        for value in (True, math.nan, math.inf, "1", 10**10_000):
            with self.subTest(value=type(value).__name__):
                self.assertFalse(is_finite_number(value))

    def test_sha256_requires_exact_lowercase_hex(self) -> None:
        self.assertTrue(is_sha256_hex("a" * 64))
        for value in ("A" * 64, "a" * 63, "g" * 64, 1, None):
            with self.subTest(value=value):
                self.assertFalse(is_sha256_hex(value))


if __name__ == "__main__":
    unittest.main()
