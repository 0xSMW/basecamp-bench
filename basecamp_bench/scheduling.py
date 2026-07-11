"""Reusable concurrency mechanics for benchmark orchestration.

The runner owns benchmark semantics. This module owns only executor lifecycle,
indexed result collection, and coordinated cancellation after worker failures.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import TypeVar, cast

__all__ = ["collect_indexed", "executor_pool", "worker_count"]

_Item = TypeVar("_Item")
_Result = TypeVar("_Result")
_MISSING = object()


def worker_count(item_count: int, limit: int) -> int:
    """Return a valid executor size bounded by planned work and *limit*."""
    if item_count < 0:
        raise ValueError("item_count must be nonnegative")
    if limit < 1:
        raise ValueError("limit must be positive")
    return max(1, min(item_count, limit))


@contextmanager
def executor_pool(*, workers: int, thread_name_prefix: str) -> Iterator[ThreadPoolExecutor]:
    """Yield an executor and cancel queued work during deterministic shutdown."""
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=thread_name_prefix)
    try:
        yield executor
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def collect_indexed(
    items: Sequence[_Item],
    *,
    executor: ThreadPoolExecutor,
    submit: Callable[[ThreadPoolExecutor, _Item], Future[_Result]],
    cancel_event: threading.Event,
) -> list[_Result]:
    """Execute *items* concurrently and return results in input order.

    A worker exception sets the shared cancellation event, cancels queued work,
    and is re-raised. Missing results are treated as an internal scheduler error.
    """
    results: list[_Result | object] = [_MISSING] * len(items)
    futures: dict[Future[_Result], int] = {}
    try:
        for index, item in enumerate(items):
            futures[submit(executor, item)] = index
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    except BaseException:
        cancel_event.set()
        for future in futures:
            future.cancel()
        raise
    if any(result is _MISSING for result in results):
        raise RuntimeError("concurrent scheduler returned an incomplete result set")
    return cast(list[_Result], results)
