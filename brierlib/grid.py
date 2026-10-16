"""Exact per-market price grids built from fixed-point range bands."""

from collections.abc import Sequence
from dataclasses import dataclass

from brierlib.fixed_point import parse_price


@dataclass(frozen=True)
class PriceRange:
    start: str
    end: str
    step: str


class PriceGrid:
    def __init__(self, ranges: Sequence[PriceRange]) -> None:
        if not ranges:
            raise ValueError("price ranges must not be empty")

        prices: list[int] = []
        lookup: list[int | None] = [None] * 10001
        previous_end = 0
        for band in ranges:
            start = parse_price(band.start)
            end = parse_price(band.end)
            step = parse_price(band.step)
            if start != previous_end:
                raise ValueError("price ranges must be sorted, contiguous, and start at zero")
            if start >= end or step <= 0 or (end - start) % step:
                raise ValueError("each band needs positive width and a positive step dividing it")
            for price in range(start, end + 1, step):
                if prices and price == prices[-1]:
                    continue
                lookup[price] = len(prices)
                prices.append(price)
            previous_end = end
        if previous_end != 10000:
            raise ValueError("price ranges must cover the full interval from zero to one dollar")
        self._prices = tuple(prices)
        self._index_by_tick = tuple(lookup)

    def __len__(self) -> int:
        return len(self._prices)

    def index_of(self, price: int) -> int | None:
        if price < 0 or price > 10000:
            return None
        return self._index_by_tick[price]

    def price_of(self, index: int) -> int | None:
        if index < 0 or index >= len(self._prices):
            return None
        return self._prices[index]

    def is_valid(self, price: int) -> bool:
        return self.index_of(price) is not None
