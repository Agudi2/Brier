"""Token-budget throttling and bounded backoff (Stage 3 RED stubs)."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    def __init__(
        self,
        rate: float = 50.0,
        capacity: float = 10.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Initially full; rate/capacity/cost are measured in API tokens."""

    async def acquire(self, cost: float = 10.0) -> None:
        raise NotImplementedError("Stage 3 GREEN: token bucket acquire")


class Backoff:
    def __init__(
        self,
        base: float = 0.5,
        cap: float = 30.0,
        jitter: float = 0.25,
        *,
        rng: random.Random | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """Attempt zero uses base; additive jitter is a fraction, capped with the delay."""

    def delay(self, attempt: int, retry_after: str | None = None) -> float:
        """Retry-After seconds or an HTTP-date sets a minimum wait, even above the cap."""
        raise NotImplementedError("Stage 3 GREEN: backoff delay")
