"""Hand-calculated windows and adversarial future-data invariants."""

import json
import struct
from dataclasses import replace
from pathlib import Path

import polars as pl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from study.dataset import (
    Market,
    PriceSelection,
    Quote,
    Trade,
    book_price_at,
    build_dataset,
    price_at,
    read_tob,
    write_dataset,
)
from tests.study.helpers import ready

BASE = Market("M", "E", "S", "Economics", "yes", 1000, 600)


def market(*trades: tuple[int, int]) -> Market:
    return replace(BASE, trades=tuple(Trade(t, p, str(i)) for i, (t, p) in enumerate(trades)))


def test_scheduled_anchor_and_last_trade_not_input_order() -> None:
    # Scheduled 1000 - h100 = 900; window (700,900]. Actual-close anchor 500 is wrong.
    m = market((900, 6100), (499, 9900), (800, 4200), (901, 10000), (700, 100))
    assert ready(price_at, m, 100, 200) == PriceSelection(6100, 900, None)
    assert ready(price_at, replace(m, actual_close_ns=999), 100, 200) == PriceSelection(
        6100, 900, None
    )


@pytest.mark.parametrize(
    ("trades", "expected"),
    [
        ((), PriceSelection(None, None, "no_trade_in_window")),
        (((700, 3000), (901, 7000)), PriceSelection(None, None, "no_trade_in_window")),
        (((701, 0),), PriceSelection(0, 701, None)),
        (((900, 10000),), PriceSelection(10000, 900, None)),
    ],
)
def test_open_lower_closed_upper_boundary(
    trades: tuple[tuple[int, int], ...], expected: PriceSelection
) -> None:
    assert ready(price_at, market(*trades), 100, 200) == expected


def test_missing_schedule_never_falls_back_to_actual_close() -> None:
    assert ready(price_at, replace(market((500, 9999)), scheduled_close_ns=None), 100, 200) == (
        PriceSelection(None, None, "missing_scheduled_close")
    )


def test_equal_timestamp_tie_is_trade_id_order_and_exact_duplicates_are_harmless() -> None:
    # Trade IDs provide a reproducible tie-break, not an inferred exchange ordering.
    a, z = Trade(900, 2000, "a"), Trade(900, 8000, "z")
    for trades in ((a, z), (z, a), (z, a, z)):
        assert ready(price_at, replace(BASE, trades=trades), 100, 200) == PriceSelection(
            8000, 900, None
        )


@pytest.mark.parametrize("h,w", [(-1, 200), (100, 0), (100, -1)])
def test_invalid_windows_raise(h: int, w: int) -> None:
    with pytest.raises(ValueError):
        ready(price_at, BASE, h, w)


@pytest.mark.parametrize("price", [-1, 10001])
def test_bad_trade_price_rejected(price: int) -> None:
    with pytest.raises(ValueError):
        ready(price_at, market((850, price)), 100, 200)


def test_conflicting_trade_id_rejected() -> None:
    with pytest.raises(ValueError):
        ready(price_at, replace(BASE, trades=(Trade(850, 20, "x"), Trade(850, 30, "x"))), 100, 200)


@given(st.lists(st.tuples(st.integers(901, 10**12), st.integers(0, 10000)), max_size=50))
def test_appending_future_trades_cannot_change_price(future: list[tuple[int, int]]) -> None:
    baseline = market((701, 2100), (899, 6300))
    appended = tuple(Trade(t, p, f"future-{i}") for i, (t, p) in enumerate(future))
    assert ready(price_at, replace(baseline, trades=baseline.trades + appended), 100, 200) == (
        PriceSelection(6300, 899, None)
    )


@given(st.integers(0, 10000), st.integers(701, 900))
def test_single_eligible_trade_is_preserved(price: int, timestamp: int) -> None:
    assert ready(price_at, market((700, 9999), (timestamp, price), (901, 1)), 100, 200) == (
        PriceSelection(price, timestamp, None)
    )


@pytest.mark.parametrize(
    ("quote", "spread", "expected"),
    [
        # (4000+4101)/2 = 4050.5 ticks; half ticks must not be truncated.
        (Quote(900, 4000, 4101), 101, PriceSelection(4050.5, 900, None)),
        (Quote(900, 4000, 4101), 100, PriceSelection(None, None, "wide_spread")),
        (Quote(900, -1, 4100), 200, PriceSelection(None, None, "missing_book_side")),
        (Quote(900, 4000, -1), 200, PriceSelection(None, None, "missing_book_side")),
        (Quote(900, 4100, 4000, 3), 200, PriceSelection(None, None, "crossed_book")),
        (Quote(900, 4000, 4000, 5), 0, PriceSelection(4000, 900, None)),
        (Quote(900, 0, 10000), 10000, PriceSelection(5000, 900, None)),
        (Quote(900, -1, -1, 0), 100, PriceSelection(None, None, "invalid_book")),
    ],
)
def test_book_mid_and_exclusions(quote: Quote, spread: int, expected: PriceSelection) -> None:
    assert ready(book_price_at, BASE, [quote], 100, 200, spread) == expected


def test_latest_invalid_book_must_not_resurrect_older_valid_quote() -> None:
    quotes = [Quote(800, 4000, 4100), Quote(890, -1, -1, 0), Quote(901, 7000, 7100)]
    assert ready(book_price_at, BASE, quotes, 100, 200, 100) == PriceSelection(
        None, None, "invalid_book"
    )


def test_book_window_and_missing_schedule() -> None:
    assert ready(book_price_at, BASE, [Quote(700, 1, 2)], 100, 200, 100) == PriceSelection(
        None, None, "no_quote_in_window"
    )
    assert ready(book_price_at, replace(BASE, scheduled_close_ns=None), [], 100, 200, 100) == (
        PriceSelection(None, None, "missing_scheduled_close")
    )


@pytest.mark.parametrize("h,w,s", [(-1, 1, 1), (0, 0, 1), (0, 1, -1)])
def test_bad_book_options_rejected(h: int, w: int, s: int) -> None:
    with pytest.raises(ValueError):
        ready(book_price_at, BASE, [], h, w, s)


@given(st.lists(st.integers(901, 10**12), max_size=30))
def test_future_book_updates_do_not_change_selection(times: list[int]) -> None:
    quotes = [Quote(850, 2000, 2200)] + [Quote(t, -1, -1, 0) for t in times]
    assert ready(book_price_at, BASE, quotes, 100, 200, 200) == PriceSelection(2100, 850, None)


def test_rows_outcomes_yes_perspective_and_exclusion_counts() -> None:
    markets = [
        market((850, 3000)),
        replace(market((850, 8000)), ticker="N", result="no"),
        replace(BASE, ticker="V", result="void"),
        replace(BASE, ticker="O", result="scalar"),
        replace(BASE, ticker="U", scheduled_close_ns=None),
        replace(BASE, ticker="EMPTY"),
    ]
    result = ready(build_dataset, markets, [100, 200], 200)
    # h100 uses (700,900], h200 uses (600,800]: only first two at h100 qualify.
    assert result.rows.select("ticker", "horizon_ns", "price_ticks", "p", "y").rows() == [
        ("M", 100, 3000.0, 0.3, 1),
        ("N", 100, 8000.0, 0.8, 0),  # No outcome does NOT complement YES price.
    ]
    assert result.rows.select("event_ticker", "series_ticker", "category").rows() == [
        ("E", "S", "Economics"),
        ("E", "S", "Economics"),
    ]
    assert result.rows["scheduled_close_ns"].to_list() == [1000, 1000]
    assert result.rows["price_timestamp_ns"].to_list() == [850, 850]
    # 6 markets * 2 horizons = 12; 2 accepted + 4 void/other + 2 missing + 4 empty.
    assert dict(result.exclusions.select("reason", "count").rows()) == {
        "unsupported_outcome": 4,
        "missing_scheduled_close": 2,
        "no_trade_in_window": 4,
    }
    assert result.rows.height + result.exclusions["count"].sum() == 12


def test_dataset_rejects_duplicate_keys_and_invalid_options() -> None:
    for markets, horizons, lookback in [
        ([BASE, BASE], [100], 200),
        ([BASE], [100, 100], 200),
        ([BASE], [-1], 200),
        ([BASE], [100], 0),
    ]:
        with pytest.raises(ValueError):
            ready(build_dataset, markets, horizons, lookback)


def test_empty_dataset_has_stable_typed_schema() -> None:
    result = ready(build_dataset, [], [100], 200)
    assert result.rows.schema == {
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
    assert result.exclusions.schema == {"reason": pl.String, "count": pl.UInt64}
    assert result.rows.is_empty() and result.exclusions.is_empty()


def test_partitioned_parquet_round_trip_and_rerun(tmp_path: Path) -> None:
    result = ready(build_dataset, [market((750, 2000), (850, 3000))], [100, 200], 200)
    output = tmp_path / "dataset"
    ready(write_dataset, result, output)
    for horizon, expected in ((100, 3000.0), (200, 2000.0)):
        files = list((output / "rows" / f"horizon_ns={horizon}").glob("*.parquet"))
        assert files
        rows = pl.read_parquet(files, hive_partitioning=False)
        assert rows["price_ticks"].to_list() == [expected]
        assert rows["horizon_ns"].to_list() == [horizon]
    assert pl.read_parquet(output / "exclusions.parquet").equals(result.exclusions)
    # Rewriting a smaller dataset must not retain stale horizon partitions.
    smaller = ready(build_dataset, [market((850, 3000))], [100], 200)
    ready(write_dataset, smaller, output)
    assert not list((output / "rows" / "horizon_ns=200").glob("*.parquet"))
    actual = pl.read_parquet(list((output / "rows").rglob("*.parquet")), hive_partitioning=False)
    assert actual.equals(smaller.rows)


def test_tob_binary_adapter_preserves_invalidations_and_yes_asks(tmp_path: Path) -> None:
    # Wire format: u64 recv, u32 ID, i32 bid/ask, i64 sizes, u32 flags = 40 bytes.
    binary, mapping = tmp_path / "tob.bin", tmp_path / "markets.json"
    mapping.write_text(json.dumps(["M", "N"]))
    binary.write_bytes(
        struct.pack("<QIiiqqI", 850, 0, 4000, 4101, 100, 200, 1)
        + struct.pack("<QIiiqqI", 890, 0, -1, -1, 0, 0, 0)
        + struct.pack("<QIiiqqI", 900, 1, 7000, 7100, 300, 400, 1)
    )
    assert ready(read_tob, binary, mapping) == {
        "M": (Quote(850, 4000, 4101), Quote(890, -1, -1, 0)),
        "N": (Quote(900, 7000, 7100),),
    }


@pytest.mark.parametrize("payload", [b"x", struct.pack("<QIiiqqI", 1, 7, 1, 2, 1, 1, 1)])
def test_tob_truncation_or_unmapped_market_rejected(tmp_path: Path, payload: bytes) -> None:
    binary, mapping = tmp_path / "tob.bin", tmp_path / "markets.json"
    binary.write_bytes(payload)
    mapping.write_text('["M"]')
    with pytest.raises(ValueError):
        ready(read_tob, binary, mapping)
