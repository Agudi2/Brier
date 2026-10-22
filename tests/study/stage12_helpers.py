"""Convert Stage 12 RED stubs to clear assertion failures."""

from collections.abc import Callable

import pytest


def ready[**P, T](call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    try:
        return call(*args, **kwargs)
    except NotImplementedError:
        pytest.fail("Stage 12 behavior is not implemented")
