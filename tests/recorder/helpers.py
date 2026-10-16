"""Test support that reports intentional RED stubs as assertion failures."""

from collections.abc import Awaitable, Callable

import pytest


def ready[**P, T](call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    try:
        return call(*args, **kwargs)
    except NotImplementedError:
        pytest.fail("Stage 3 behavior is not implemented")


async def ready_async[T](result: Awaitable[T]) -> T:
    try:
        return await result
    except NotImplementedError:
        pytest.fail("Stage 3 async behavior is not implemented")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


class FakeSigner:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []

    def headers(self, timestamp_ms: int, method: str, path: str) -> dict[str, str]:
        self.calls.append((timestamp_ms, method, path))
        return {
            "KALSHI-ACCESS-KEY": "test-id",
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "KALSHI-ACCESS-SIGNATURE": "test-signature",
        }
