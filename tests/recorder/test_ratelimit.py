import asyncio
import math
import random
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from recorder.ratelimit import Backoff, TokenBucket
from tests.recorder.helpers import FakeClock, ready, ready_async


async def test_exact_token_schedule() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=2, capacity=2, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        await ready_async(bucket.acquire(cost=1))
    # Two initial tokens are free; each of the remaining three needs 1/2 second.
    assert clock.sleeps == [0.5, 0.5, 0.5]
    assert clock.now == 1.5


async def test_idle_refill_is_capped_and_cost_is_weighted() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=2, capacity=4, clock=clock, sleep=clock.sleep)
    await ready_async(bucket.acquire(cost=4))
    clock.now += 10
    await ready_async(bucket.acquire(cost=4))
    await ready_async(bucket.acquire(cost=2))
    # Idle time cannot bank more than capacity=4; the next two tokens take 2/2=1 sec.
    assert clock.sleeps == [1.0]


async def test_default_budget_is_conservative() -> None:
    clock = FakeClock()
    bucket = TokenBucket(clock=clock, sleep=clock.sleep)
    await ready_async(bucket.acquire())
    await ready_async(bucket.acquire())
    # Default cost 10, refill 50 tokens/sec, capacity 10 -> one request per .2 sec.
    assert clock.sleeps == [0.2]


@pytest.mark.parametrize(
    "rate, capacity", [(0, 1), (-1, 1), (1, 0), (math.inf, 1), (math.nan, 1), (1, math.inf)]
)
def test_invalid_bucket_parameters(rate: float, capacity: float) -> None:
    with pytest.raises(ValueError):
        ready(TokenBucket, rate=rate, capacity=capacity)


@pytest.mark.parametrize("cost", [0, -1, 3, math.inf, math.nan])
async def test_invalid_or_unfulfillable_cost(cost: float) -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=1, capacity=2, clock=clock, sleep=clock.sleep)
    with pytest.raises(ValueError):
        await ready_async(bucket.acquire(cost))
    assert clock.sleeps == []


@given(rate=st.integers(1, 8), capacity=st.integers(1, 8), count=st.integers(1, 25))
def test_token_budget_property(rate: int, capacity: int, count: int) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        bucket = TokenBucket(rate=rate, capacity=capacity, clock=clock, sleep=clock.sleep)
        for acquired in range(1, count + 1):
            await ready_async(bucket.acquire(1))
            assert acquired <= capacity + rate * clock.now + 1e-9
        # With no external idle time, the exact waiting time is excess tokens / refill rate.
        assert clock.now == pytest.approx(max(0, count - capacity) / rate)

    asyncio.run(scenario())


def test_backoff_exact_and_capped() -> None:
    backoff = Backoff(base=0.5, cap=2, jitter=0)
    # .5 * 2**attempt, capped at 2 -> [.5, 1, 2, 2, 2].
    assert [ready(backoff.delay, i) for i in range(5)] == [0.5, 1, 2, 2, 2]
    assert ready(backoff.delay, 1000000) == 2


def test_retry_after_is_minimum_even_above_cap() -> None:
    backoff = Backoff(base=0.5, cap=2, jitter=0)
    assert ready(backoff.delay, 0, "7") == 7
    assert ready(backoff.delay, 2, "1") == 2
    assert ready(backoff.delay, 0, "invalid") == 0.5
    assert ready(backoff.delay, 0, "-5") == 0.5


def test_seeded_jitter_bounds_and_reproducibility() -> None:
    first = Backoff(base=1, cap=8, jitter=0.25, rng=random.Random(719))
    second = Backoff(base=1, cap=8, jitter=0.25, rng=random.Random(719))
    draws = [ready(first.delay, i) for i in range(6)]
    assert draws == [ready(second.delay, i) for i in range(6)]
    # Additive jitter lies in [0, .25 * base_delay], with the final result capped.
    for attempt, delay in enumerate(draws):
        base = min(8, 2**attempt)
        assert base <= delay <= min(8, base * 1.25)
    assert any(delay > base for delay, base in zip(draws[:3], [1, 2, 4], strict=True))


@pytest.mark.parametrize(
    "kwargs", [{"base": 0}, {"cap": 0}, {"jitter": -0.1}, {"base": math.inf}, {"cap": math.nan}]
)
def test_invalid_backoff_parameters(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        ready(
            Backoff,
            base=kwargs.get("base", 0.5),
            cap=kwargs.get("cap", 30),
            jitter=kwargs.get("jitter", 0.25),
        )


def test_retry_after_http_date_uses_injected_wall_clock() -> None:
    now = datetime(2015, 10, 21, 7, 27, 53, tzinfo=UTC).timestamp()
    backoff = Backoff(base=0.5, cap=2, jitter=0, wall_clock=lambda: now)
    # 07:28:00 - 07:27:53 = 7 seconds; an expired date leaves exponential backoff in force.
    assert ready(backoff.delay, 0, "Wed, 21 Oct 2015 07:28:00 GMT") == 7
    assert ready(backoff.delay, 0, "Wed, 21 Oct 2015 07:27:00 GMT") == 0.5
