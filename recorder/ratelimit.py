"""Token-budget throttling and bounded exponential backoff."""

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC
from email.utils import parsedate_to_datetime


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
        if not math.isfinite(rate) or rate <= 0 or not math.isfinite(capacity) or capacity <= 0:
            raise ValueError("Token rate and capacity must be finite and positive")
        self._rate = rate
        self._capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._tokens = capacity
        self._last = clock()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 10.0) -> None:
        if not math.isfinite(cost) or cost <= 0 or cost > self._capacity:
            raise ValueError("Request cost must be finite, positive, and no larger than capacity")
        async with self._lock:
            now = self._clock()
            self._tokens = min(
                self._capacity, self._tokens + max(0.0, now - self._last) * self._rate
            )
            self._last = now
            if self._tokens < cost:
                deadline = now + (cost - self._tokens) / self._rate
                # Compare against the deadline rather than repeatedly rounding a tiny token deficit.
                while now < deadline:
                    await self._sleep(deadline - now)
                    now = self._clock()
                self._tokens = min(
                    self._capacity, self._tokens + max(0.0, now - self._last) * self._rate
                )
                self._last = now
            self._tokens = max(0.0, self._tokens - cost)


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
        if not math.isfinite(base) or base <= 0 or not math.isfinite(cap) or cap <= 0:
            raise ValueError("Backoff base and cap must be finite and positive")
        if not math.isfinite(jitter) or jitter < 0:
            raise ValueError("Backoff jitter must be finite and nonnegative")
        self._base = base
        self._cap = cap
        self._jitter = jitter
        self._rng = rng if rng is not None else random.Random()
        self._wall_clock = wall_clock

    def delay(self, attempt: int, retry_after: str | None = None) -> float:
        """Retry-After seconds or an HTTP-date sets a minimum wait, even above the cap."""
        if attempt < 0:
            raise ValueError("Backoff attempt must be nonnegative")
        if self._base >= self._cap or attempt >= math.ceil(
            math.log2(self._cap) - math.log2(self._base)
        ):
            exponential = self._cap
        else:
            exponential = min(self._cap, math.ldexp(self._base, attempt))
        delay = min(self._cap, exponential + self._rng.random() * self._jitter * exponential)
        if retry_after is None:
            return delay
        value = retry_after.strip()
        try:
            if value.isascii() and value.isdecimal():
                minimum = float(value)
            else:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                minimum = max(0.0, date.timestamp() - self._wall_clock())
        except (ValueError, TypeError, OverflowError):
            return delay
        if not math.isfinite(minimum):
            raise ValueError("Retry-After is outside the supported time range")
        return max(delay, minimum)
