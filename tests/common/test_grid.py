"""The shared price-range fixture defines exact grid behavior."""

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from brierlib.fixed_point import format_price, parse_price
from brierlib.grid import PriceGrid, PriceRange

CASES = json.loads((Path(__file__).resolve().parents[2] / "fixtures/price_grids.json").read_text())


def _ranges(case: dict[str, Any]) -> list[PriceRange]:
    return [PriceRange(**band) for band in case["ranges"]]


def _grid(case: dict[str, Any]) -> PriceGrid:
    try:
        return PriceGrid(_ranges(case))
    except NotImplementedError:
        pytest.fail("price grid construction is not implemented")


def _reject(case: dict[str, Any]) -> None:
    try:
        PriceGrid(_ranges(case))
    except ValueError:
        return
    except NotImplementedError:
        pytest.fail("price grid validation is not implemented")
    pytest.fail(f"malformed grid accepted: {case['name']}")


@pytest.mark.parametrize("case", CASES["valid"], ids=lambda case: case["name"])
def test_grid_size_and_boundaries(case: dict[str, Any]) -> None:
    grid = _grid(case)
    # Tapered: 0-.10 gives 101, .10-.90 adds 80, .90-1 adds 100 = 281.
    assert len(grid) == case["count"]
    for price, index in case["indices"].items():
        ticks = parse_price(price)
        assert grid.index_of(ticks) == index
        assert grid.price_of(index) == ticks
    assert grid.price_of(len(grid)) is None
    assert grid.index_of(-1) is None
    assert grid.index_of(10001) is None


@pytest.mark.parametrize("case", CASES["valid"], ids=lambda case: case["name"])
def test_grid_membership_and_order(case: dict[str, Any]) -> None:
    grid = _grid(case)
    # All 101 whole-cent prices are valid on each documented grid.
    for cents in range(101):
        assert grid.is_valid(cents * 100)
    for price in case["off_grid"]:
        ticks = parse_price(price)
        assert not grid.is_valid(ticks)
        assert grid.index_of(ticks) is None
    prices = [grid.price_of(i) for i in range(len(grid))]
    present = [price for price in prices if price is not None]
    assert len(present) == len(prices)
    assert present == sorted(set(present))
    assert present[0] == 0 and present[-1] == 10000
    for index, price in enumerate(prices):
        assert price is not None
        assert grid.index_of(price) == index


@pytest.mark.parametrize("case", CASES["invalid"], ids=lambda case: case["name"])
def test_rejects_malformed_bands(case: dict[str, Any]) -> None:
    _reject(case)


@given(st.sampled_from([1, 2, 4, 5, 10, 20, 25, 50, 100, 125, 200, 250, 500, 1000]))
def test_generated_divisor_grids_are_strictly_increasing(step: int) -> None:
    # Every listed step divides 10000 ticks exactly, so [0, 10000] is a valid band.
    grid = _grid({"ranges": [{"start": "0.0000", "end": "1.0000", "step": format_price(step)}]})
    prices = [grid.price_of(i) for i in range(len(grid))]
    assert len(prices) == 10000 // step + 1
    assert all(
        a is not None and b is not None and a < b for a, b in zip(prices, prices[1:], strict=False)
    )
