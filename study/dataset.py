"""Study dataset contracts; implementation follows in the GREEN phase."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import polars as pl


@dataclass(frozen=True)
class Trade:
    timestamp_ns: int
    yes_price_ticks: int
    trade_id: str


@dataclass(frozen=True)
class Market:
    ticker: str
    event_ticker: str
    series_ticker: str
    category: str
    result: str
    scheduled_close_ns: int | None
    actual_close_ns: int | None = None
    trades: tuple[Trade, ...] = ()


@dataclass(frozen=True)
class Quote:
    timestamp_ns: int
    yes_bid_ticks: int
    yes_ask_ticks: int
    flags: int = 1


@dataclass(frozen=True)
class PriceSelection:
    price_ticks: float | None
    timestamp_ns: int | None
    reason: str | None


@dataclass(frozen=True)
class Dataset:
    rows: pl.DataFrame
    exclusions: pl.DataFrame


def price_at(market: Market, horizon_ns: int, lookback_ns: int) -> PriceSelection:
    raise NotImplementedError


def book_price_at(
    market: Market,
    quotes: Sequence[Quote],
    horizon_ns: int,
    lookback_ns: int,
    max_spread_ticks: int,
) -> PriceSelection:
    raise NotImplementedError


def build_dataset(
    markets: Sequence[Market], horizons_ns: Sequence[int], lookback_ns: int
) -> Dataset:
    raise NotImplementedError


def write_dataset(dataset: Dataset, output: Path) -> None:
    raise NotImplementedError


def read_tob(path: Path, markets_path: Path) -> dict[str, tuple[Quote, ...]]:
    raise NotImplementedError
