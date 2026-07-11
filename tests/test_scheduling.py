from __future__ import annotations

import threading
import unittest

from basecamp_bench.scheduling import collect_indexed, executor_pool, worker_count


class SchedulingTests(unittest.TestCase):
    def test_worker_count_is_always_positive_and_bounded(self) -> None:
        self.assertEqual(worker_count(0, 8), 1)
        self.assertEqual(worker_count(3, 8), 3)
        self.assertEqual(worker_count(10, 4), 4)
        with self.assertRaisesRegex(ValueError, "item_count"):
            worker_count(-1, 4)
        with self.assertRaisesRegex(ValueError, "limit"):
            worker_count(1, 0)

    def test_collect_indexed_preserves_input_order(self) -> None:
        cancel = threading.Event()
        with executor_pool(workers=2, thread_name_prefix="test-order") as executor:
            results = collect_indexed(
                [3, 1, 2],
                executor=executor,
                cancel_event=cancel,
                submit=lambda pool, value: pool.submit(lambda: value * 2),
            )
        self.assertEqual(results, [6, 2, 4])
        self.assertFalse(cancel.is_set())

    def test_collect_indexed_allows_none_as_a_worker_result(self) -> None:
        cancel = threading.Event()
        with executor_pool(workers=1, thread_name_prefix="test-none") as executor:
            results = collect_indexed(
                [1],
                executor=executor,
                cancel_event=cancel,
                submit=lambda pool, _value: pool.submit(lambda: None),
            )
        self.assertEqual(results, [None])

    def test_collect_indexed_sets_cancellation_on_worker_failure(self) -> None:
        cancel = threading.Event()

        def fail() -> int:
            raise RuntimeError("worker failed")

        with executor_pool(workers=1, thread_name_prefix="test-failure") as executor:
            with self.assertRaisesRegex(RuntimeError, "worker failed"):
                collect_indexed(
                    [1],
                    executor=executor,
                    cancel_event=cancel,
                    submit=lambda pool, _value: pool.submit(fail),
                )
        self.assertTrue(cancel.is_set())


if __name__ == "__main__":
    unittest.main()
