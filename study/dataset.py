"""Build leakage-safe, YES-perspective study rows from settled market history."""

import json
import shutil
import struct
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
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


_ROW_SCHEMA = {
    "ticker": pl.String,
    "event_ticker": pl.String,
    "series_ticker": pl.String,
    "category": pl.String,
    "scheduled_close_ns": pl.Int64,
    "horizon_ns": pl.Int64,
    "price_timestamp_ns": pl.Int64,
    "price_ticks": pl.Float64,
    "p": pl.Float64,
    "y": pl.Int8,
}
_EXCLUSION_SCHEMA = {"reason": pl.String, "count": pl.UInt64}
_TOB_RECORD = struct.Struct("<QIiiqqI")


def _window(market: Market, horizon_ns: int, lookback_ns: int) -> tuple[int, int] | None:
    if (
        not isinstance(horizon_ns, int)
        or isinstance(horizon_ns, bool)
        or horizon_ns < 0
        or not isinstance(lookback_ns, int)
        or isinstance(lookback_ns, bool)
        or lookback_ns <= 0
    ):
        raise ValueError("horizon must be nonnegative and lookback positive")
    if market.scheduled_close_ns is None:
        return None
    upper = market.scheduled_close_ns - horizon_ns
    return upper - lookback_ns, upper


def _valid_tick(ticks: int) -> bool:
    return isinstance(ticks, int) and not isinstance(ticks, bool) and 0 <= ticks <= 10000


def price_at(market: Market, horizon_ns: int, lookback_ns: int) -> PriceSelection:
    window = _window(market, horizon_ns, lookback_ns)
    if window is None:
        return PriceSelection(None, None, "missing_scheduled_close")
    lower, upper = window
    by_id: dict[str, Trade] = {}
    best: Trade | None = None
    for trade in market.trades:
        if not _valid_tick(trade.yes_price_ticks):
            raise ValueError("trade YES price is outside [0, 10000] ticks")
        prior = by_id.setdefault(trade.trade_id, trade)
        if prior != trade:
            raise ValueError(f"conflicting trade ID: {trade.trade_id}")
        if lower < trade.timestamp_ns <= upper and (
            best is None
            or (trade.timestamp_ns, trade.trade_id) > (best.timestamp_ns, best.trade_id)
        ):
            best = trade
    if best is None:
        return PriceSelection(None, None, "no_trade_in_window")
    return PriceSelection(float(best.yes_price_ticks), best.timestamp_ns, None)


def book_price_at(
    market: Market,
    quotes: Sequence[Quote],
    horizon_ns: int,
    lookback_ns: int,
    max_spread_ticks: int,
) -> PriceSelection:
    if (
        not isinstance(max_spread_ticks, int)
        or isinstance(max_spread_ticks, bool)
        or max_spread_ticks < 0
    ):
        raise ValueError("max spread must be nonnegative")
    window = _window(market, horizon_ns, lookback_ns)
    if window is None:
        return PriceSelection(None, None, "missing_scheduled_close")
    lower, upper = window
    latest: Quote | None = None
    for quote in quotes:
        if lower < quote.timestamp_ns <= upper and (
            latest is None or quote.timestamp_ns >= latest.timestamp_ns
        ):
            latest = quote
    if latest is None:
        return PriceSelection(None, None, "no_quote_in_window")
    if latest.flags & 1 == 0:
        return PriceSelection(None, None, "invalid_book")
    if latest.yes_bid_ticks == -1 or latest.yes_ask_ticks == -1:
        return PriceSelection(None, None, "missing_book_side")
    if not _valid_tick(latest.yes_bid_ticks) or not _valid_tick(latest.yes_ask_ticks):
        raise ValueError("book price is outside [0, 10000] ticks")
    if latest.yes_bid_ticks > latest.yes_ask_ticks or latest.flags & 2:
        return PriceSelection(None, None, "crossed_book")
    if latest.yes_ask_ticks - latest.yes_bid_ticks > max_spread_ticks:
        return PriceSelection(None, None, "wide_spread")
    return PriceSelection(
        (latest.yes_bid_ticks + latest.yes_ask_ticks) / 2, latest.timestamp_ns, None
    )


def build_dataset(
    markets: Sequence[Market], horizons_ns: Sequence[int], lookback_ns: int
) -> Dataset:
    return _build_dataset(markets, horizons_ns, lookback_ns, price_at)


def build_book_dataset(
    markets: Sequence[Market],
    quotes_by_ticker: Mapping[str, Sequence[Quote]],
    horizons_ns: Sequence[int],
    lookback_ns: int,
    max_spread_ticks: int,
) -> Dataset:
    """Build the same YES-perspective rows from replay's valid top-of-book quotes."""
    if (
        not isinstance(max_spread_ticks, int)
        or isinstance(max_spread_ticks, bool)
        or max_spread_ticks < 0
    ):
        raise ValueError("max spread must be nonnegative")

    def select(market: Market, horizon: int, lookback: int) -> PriceSelection:
        return book_price_at(
            market, quotes_by_ticker.get(market.ticker, ()), horizon, lookback, max_spread_ticks
        )

    return _build_dataset(markets, horizons_ns, lookback_ns, select)


def _build_dataset(
    markets: Sequence[Market],
    horizons_ns: Sequence[int],
    lookback_ns: int,
    select: Callable[[Market, int, int], PriceSelection],
) -> Dataset:
    if len({market.ticker for market in markets}) != len(markets):
        raise ValueError("duplicate market ticker")
    if len(set(horizons_ns)) != len(horizons_ns):
        raise ValueError("duplicate horizon")
    # Validate options even if there are no candidate markets or horizons.
    if (
        not isinstance(lookback_ns, int)
        or isinstance(lookback_ns, bool)
        or lookback_ns <= 0
        or any(not isinstance(h, int) or isinstance(h, bool) or h < 0 for h in horizons_ns)
    ):
        raise ValueError("horizons must be nonnegative and lookback positive")
    rows: list[dict[str, str | int | float]] = []
    exclusions: Counter[str] = Counter()
    for market in sorted(markets, key=lambda m: m.ticker):
        for horizon in sorted(horizons_ns):
            if market.result not in ("yes", "no"):
                exclusions["unsupported_outcome"] += 1
                continue
            selected = select(market, horizon, lookback_ns)
            if selected.reason is not None:
                exclusions[selected.reason] += 1
                continue
            assert selected.price_ticks is not None and selected.timestamp_ns is not None
            assert market.scheduled_close_ns is not None
            rows.append(
                {
                    "ticker": market.ticker,
                    "event_ticker": market.event_ticker,
                    "series_ticker": market.series_ticker,
                    "category": market.category,
                    "scheduled_close_ns": market.scheduled_close_ns,
                    "horizon_ns": horizon,
                    "price_timestamp_ns": selected.timestamp_ns,
                    "price_ticks": selected.price_ticks,
                    "p": selected.price_ticks / 10000,
                    "y": 1 if market.result == "yes" else 0,
                }
            )
    exclusion_rows = [
        {"reason": reason, "count": count} for reason, count in sorted(exclusions.items())
    ]
    return Dataset(
        pl.DataFrame(rows, schema=_ROW_SCHEMA),
        pl.DataFrame(exclusion_rows, schema=_EXCLUSION_SCHEMA),
    )


def write_dataset(dataset: Dataset, output: Path) -> None:
    """Stage a complete Parquet replacement before changing the published directory."""
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    backup = output.parent / f".{output.name}-previous"
    if backup.exists():
        raise FileExistsError(backup)
    try:
        for horizon in dataset.rows["horizon_ns"].unique().to_list():
            partition = staging / "rows" / f"horizon_ns={horizon}"
            partition.mkdir(parents=True)
            dataset.rows.filter(pl.col("horizon_ns") == horizon).write_parquet(
                partition / "part-0.parquet"
            )
        dataset.exclusions.write_parquet(staging / "exclusions.parquet")
        if output.exists():
            output.rename(backup)
        try:
            staging.rename(output)
        except Exception:
            if backup.exists():
                backup.rename(output)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def read_tob(path: Path, markets_path: Path) -> dict[str, tuple[Quote, ...]]:
    tickers = json.loads(markets_path.read_text())
    if not isinstance(tickers, list) or any(not isinstance(x, str) for x in tickers):
        raise ValueError("markets.json must be an array of tickers")
    if len(set(tickers)) != len(tickers):
        raise ValueError("duplicate market ticker in markets.json")
    quotes: dict[str, list[Quote]] = {}
    with path.open("rb") as source:
        while record := source.read(_TOB_RECORD.size):
            if len(record) != _TOB_RECORD.size:
                raise ValueError("truncated TOB record")
            timestamp, market_id, bid, ask, _bid_qty, _ask_qty, flags = _TOB_RECORD.unpack(record)
            if market_id >= len(tickers):
                raise ValueError("TOB record has unmapped market ID")
            quotes.setdefault(tickers[market_id], []).append(Quote(timestamp, bid, ask, flags))
    return {ticker: tuple(rows) for ticker, rows in quotes.items()}
