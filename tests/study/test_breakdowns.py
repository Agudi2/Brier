"""Hand-calculated grouping, event tests, and Holm correction gates."""

import math

import polars as pl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from study.breakdowns import (
    Breakdown,
    breakdowns,
    holm_adjust,
    liquidity_bucket,
    parse_horizon,
    spread_bucket,
)
from tests.study.stage12_helpers import ready


@pytest.mark.parametrize(
    ("text", "nanoseconds"),
    [
        ("24h", 86_400_000_000_000),
        ("90m", 5_400_000_000_000),
        ("2d", 172_800_000_000_000),
        ("1s", 1_000_000_000),
    ],
)
def test_horizon_units(text: str, nanoseconds: int) -> None:
    assert ready(parse_horizon, text) == nanoseconds


@pytest.mark.parametrize(
    "text",
    ["", "0h", "-1h", "1.5h", "24H", "24", "1w", " 1h", "1h ", "1h30m", "9223372036854775808d"],
)
def test_horizon_invalid_or_int64_overflow(text: str) -> None:
    with pytest.raises(ValueError):
        ready(parse_horizon, text)


@pytest.mark.parametrize(
    ("ticks", "bucket"),
    [
        (None, "missing"),
        (0, "locked"),
        (1, "1-100"),
        (100, "1-100"),
        (101, "101-500"),
        (500, "101-500"),
        (501, ">500"),
    ],
)
def test_spread_buckets_are_in_integer_ticks(ticks: int | None, bucket: str) -> None:
    assert ready(spread_bucket, ticks) == bucket


@pytest.mark.parametrize(
    ("quantity", "bucket"),
    [(None, "missing"), (0, "zero"), (1, "1-9999"), (9999, "1-9999"), (10000, ">=10000")],
)
def test_liquidity_buckets_use_hundredths(quantity: int | None, bucket: str) -> None:
    assert ready(liquidity_bucket, quantity) == bucket


@pytest.mark.parametrize("fn", [spread_bucket, liquidity_bucket])
def test_negative_book_measures_are_invalid(fn: object) -> None:
    assert callable(fn)
    with pytest.raises(ValueError):
        ready(fn, -1)


def test_holm_step_down_and_original_order() -> None:
    # Sorted .01,.03,.04,1 => products .04,.09,.08,1; cumulative maxima .04,.09,.09,1.
    assert ready(holm_adjust, [0.03, 0.01, 0.04, 1.0]) == pytest.approx((0.09, 0.04, 0.09, 1.0))
    # Tied .01,.01,.04 => .03,.02,.04 -> monotone .03,.03,.04.
    assert ready(holm_adjust, [0.01, 0.01, 0.04]) == pytest.approx((0.03, 0.03, 0.04))
    assert ready(holm_adjust, []) == ()


@pytest.mark.parametrize("bad", [-0.01, 1.01, math.nan, math.inf])
def test_holm_rejects_invalid_probabilities(bad: float) -> None:
    with pytest.raises(ValueError):
        ready(holm_adjust, [0.1, bad])


@given(st.lists(st.floats(0, 1, allow_nan=False, allow_infinity=False), max_size=20))
def test_holm_never_decreases_raw_p_and_is_permutation_equivariant(p: list[float]) -> None:
    adjusted = ready(holm_adjust, p)
    assert len(adjusted) == len(p)
    assert all(raw <= new <= 1 for raw, new in zip(p, adjusted, strict=True))
    assert ready(holm_adjust, p[::-1]) == pytest.approx(adjusted[::-1])


def group_rows() -> pl.DataFrame:
    # A: six independent events, p=.2, y=1 -> gap=.8, Brier=.64.
    # B: six independent events, p=.5, alternating outcomes -> gap=0, Brier=.25.
    # C: two rows with a large gap; min_n=5 must withhold inference.
    categories = ["A"] * 6 + ["B"] * 6 + ["C"] * 2
    outcomes = [1] * 6 + [0, 1] * 3 + [1] * 2
    prices = [0.2] * 6 + [0.5] * 6 + [0.1] * 2
    return pl.DataFrame(
        {
            "ticker": [f"PRIVATE-{i}" for i in range(14)],
            "event_ticker": [f"EVENT-{i}" for i in range(14)],
            "category": categories,
            "horizon_ns": [86_400_000_000_000] * 14,
            "p": prices,
            "y": outcomes,
            "spread_ticks": [0] * 6 + [100] * 6 + [None] * 2,
            "liquidity_qty": [10000] * 6 + [9999] * 6 + [None] * 2,
        }
    )


def _find(items: tuple[Breakdown, ...], dimension: str, group: str) -> Breakdown:
    return next(item for item in items if item.dimension == dimension and item.group == group)


def test_category_results_min_n_and_familywise_holm_gate() -> None:
    rows = group_rows()
    result = ready(breakdowns, rows, dimensions=("category",), min_n=5, n_resamples=127, seed=7)
    a, b, c = (_find(result, "category", group) for group in ("A", "B", "C"))
    assert (a.n, a.status) == (6, "result")
    assert a.brier == pytest.approx(0.64) and a.gap == pytest.approx(0.8)
    assert a.ci_low == pytest.approx(0.8) and a.ci_high == pytest.approx(0.8)
    # Six same-sign event sums: only all+ and all- among 2^6 signs are as extreme.
    assert a.p_value == pytest.approx(2 / 64)
    assert a.p_adjusted == pytest.approx(2 * (2 / 64))
    assert not a.breaks_down  # Raw .03125 < .05, Holm-adjusted .0625 > .05.
    assert b.n == 6 and b.status == "result" and b.gap == pytest.approx(0)
    assert b.p_value == b.p_adjusted == 1.0
    assert not b.breaks_down
    assert c.n == 2 and c.status == "insufficient n"
    assert c.brier is None and c.gap is None and c.p_value is None
    assert c.p_adjusted is None and not c.breaks_down


def test_single_eligible_group_can_be_labeled_after_adjustment() -> None:
    a_only = group_rows().filter(pl.col("category") == "A")
    result = ready(breakdowns, a_only, dimensions=("category",), min_n=5, n_resamples=127, seed=7)
    a = _find(result, "category", "A")
    assert a.p_value == a.p_adjusted == pytest.approx(2 / 64)
    assert a.breaks_down


def test_all_dimensions_and_missing_measure_are_explicit() -> None:
    rows = group_rows().drop("liquidity_qty")
    result = ready(breakdowns, rows, min_n=5, n_resamples=63, seed=8)
    assert {item.dimension for item in result} == {
        "category",
        "horizon_ns",
        "spread_bucket",
        "liquidity_bucket",
    }
    locked = _find(result, "spread_bucket", "locked")
    assert locked.n == 6 and locked.gap == pytest.approx(0.8)
    assert _find(result, "spread_bucket", "1-100").n == 6
    missing = _find(result, "liquidity_bucket", "missing")
    assert missing.n == 14 and missing.status == "missing data"
    assert missing.brier is None and missing.p_value is None and not missing.breaks_down
    horizon = _find(result, "horizon_ns", "86400000000000")
    assert horizon.n == 14
    eligible = [item for item in result if item.status == "result"]
    assert all(item.p_value is not None for item in eligible)
    ranked = sorted(enumerate(eligible), key=lambda pair: pair[1].p_value or 0)
    adjusted: dict[int, float] = {}
    running = 0.0
    for rank, (original, item) in enumerate(ranked):
        assert item.p_value is not None
        running = max(running, (len(eligible) - rank) * item.p_value)
        adjusted[original] = min(1.0, running)
    for position, item in enumerate(eligible):
        assert item.p_adjusted == pytest.approx(adjusted[position])
    assert all(
        not item.breaks_down or (item.p_adjusted is not None and item.p_adjusted <= 0.05)
        for item in result
    )


def test_one_event_with_many_rows_is_not_published_as_significant() -> None:
    rows = (
        group_rows()
        .filter(pl.col("category") == "A")
        .with_columns(pl.lit("ONE-EVENT").alias("event_ticker"))
    )
    result = ready(breakdowns, rows, dimensions=("category",), min_n=5)
    a = _find(result, "category", "A")
    assert a.n == 6 and a.status == "insufficient events"
    assert a.brier is None and a.gap is None and a.p_value is None
    assert not a.breaks_down


@pytest.mark.parametrize("min_n,alpha", [(0, 0.05), (-1, 0.05), (1, 0.0), (1, 1.0), (1, math.nan)])
def test_invalid_breakdown_options(min_n: int, alpha: float) -> None:
    with pytest.raises(ValueError):
        ready(breakdowns, group_rows(), min_n=min_n, alpha=alpha)


def test_unknown_breakdown_dimension_rejected() -> None:
    with pytest.raises(ValueError):
        ready(breakdowns, group_rows(), dimensions=("ticker",), min_n=5)
