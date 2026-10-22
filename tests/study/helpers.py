"""Turn deliberate RED stubs into explanatory assertion failures."""

from collections.abc import Awaitable, Callable

import pytest


def ready[**P, T](call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    try:
        return call(*args, **kwargs)
    except NotImplementedError:
        pytest.fail("Stage 10 behavior is not implemented")


async def ready_async[T](result: Awaitable[T]) -> T:
    try:
        return await result
    except NotImplementedError:
        pytest.fail("Stage 10 behavior is not implemented")
