"""Breakdown and multiple-testing interfaces; deliberate RED stubs."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import polars as pl

BreakdownStatus = Literal["result", "insufficient n", "insufficient events", "missing data"]


@dataclass(frozen=True)
class Breakdown:
    dimension: str
    group: str
    n: int
    brier: float | None
    gap: float | None
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    p_adjusted: float | None
    status: BreakdownStatus
    breaks_down: bool


def parse_horizon(value: str) -> int:
    """Parse a positive duration into integer nanoseconds."""
    raise NotImplementedError


def spread_bucket(spread_ticks: int | None) -> str:
    raise NotImplementedError


def liquidity_bucket(quantity: int | None) -> str:
    raise NotImplementedError


def holm_adjust(p_values: Sequence[float]) -> tuple[float, ...]:
    raise NotImplementedError


def breakdowns(
    rows: pl.DataFrame,
    *,
    dimensions: Sequence[str] = ("category", "horizon_ns", "spread_bucket", "liquidity_bucket"),
    min_n: int = 30,
    alpha: float = 0.05,
    n_resamples: int = 2000,
    seed: int = 0,
) -> tuple[Breakdown, ...]:
    raise NotImplementedError
