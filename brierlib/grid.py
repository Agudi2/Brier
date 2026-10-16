"""Per-market price grids (Stage 2 RED stubs)."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PriceRange:
    start: str
    end: str
    step: str


class PriceGrid:
    def __init__(self, ranges: Sequence[PriceRange]) -> None:
        raise NotImplementedError("Stage 2 GREEN: construct grid")

    def __len__(self) -> int:
        raise NotImplementedError("Stage 2 GREEN: grid size")

    def index_of(self, price: int) -> int | None:
        raise NotImplementedError("Stage 2 GREEN: index_of")

    def price_of(self, index: int) -> int | None:
        raise NotImplementedError("Stage 2 GREEN: price_of")

    def is_valid(self, price: int) -> bool:
        raise NotImplementedError("Stage 2 GREEN: is_valid")
