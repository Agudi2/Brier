"""Stage 11 assertion adapter and independent percentile arithmetic."""

import math
from collections.abc import Callable, Sequence

import pytest


def ready[**P, T](call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    try:
        return call(*args, **kwargs)
    except NotImplementedError:
        pytest.fail("Stage 11 behavior is not implemented")


def percentile(values: Sequence[float], q: float) -> float:
    """Linear order-statistic interpolation, independent of production code."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[math.ceil(position)] * fraction
