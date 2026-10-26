"""Stage 13 assertion adapter and explicitly constructed observations."""

from collections.abc import Callable
from datetime import date, timedelta

import pytest

from study.imbalance import BookEvent, BookQuote, Sample

DAY_NS = 86_400_000_000_000
BASE_DAY = date(2026, 10, 1)
BASE_NS = (BASE_DAY - date(1970, 1, 1)).days * DAY_NS


def ready[**P, T](call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    try:
        return call(*args, **kwargs)
    except NotImplementedError:
        pytest.fail("Stage 13 behavior is not implemented")


def event(
    offset_ns: int,
    bid: int = 4000,
    ask: int = 6000,
    bid_qty: int = 100,
    ask_qty: int = 100,
    *,
    market: str = "A",
) -> BookEvent:
    return BookEvent(market, BASE_NS + offset_ns, BookQuote(bid, ask, bid_qty, ask_qty))


def sample(
    day: int,
    signal: float,
    target: int,
    *,
    market: str = "A",
    ofi: int | None = 0,
    offset_ns: int = 0,
) -> Sample:
    return Sample(
        market,
        BASE_NS + day * DAY_NS + offset_ns,
        BASE_DAY + timedelta(days=day),
        signal,
        ofi,
        target,
    )
