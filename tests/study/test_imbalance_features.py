"""Hand-calculated depth, CKS flow, and forward event-time label contracts."""

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from study.imbalance import BookEvent, BookQuote, build_samples, imbalance, order_flow
from tests.study.imbalance_helpers import BASE_DAY, BASE_NS, DAY_NS, event, ready


@pytest.mark.parametrize(
    "bid,ask,expected",
    [(300, 100, 0.5), (100, 300, -0.5), (155, 155, 0.0), (100, 0, 1.0), (0, 100, -1.0)],
)
def test_imbalance_from_integer_hundredths(bid: int, ask: int, expected: float) -> None:
    # (300-100)/(300+100)=1/2; swapping sides negates the ratio.
    assert ready(imbalance, bid, ask) == expected


def test_empty_depth_has_no_imbalance() -> None:
    assert ready(imbalance, 0, 0) is None


def test_subtract_quantities_before_float_conversion_and_avoid_int64_sum_overflow() -> None:
    # These integers differ by one, but casting each to float first loses that difference.
    assert ready(imbalance, 2**53 + 1, 2**53) == pytest.approx(1 / (2**54 + 1), abs=1e-30)
    # Both inputs fit signed int64, while their sum does not.
    assert ready(imbalance, 2**63 - 1, 2**63 - 2) == pytest.approx(1 / (2**64 - 3), abs=1e-32)


@pytest.mark.parametrize("bad", [-1, 1.5, True, 2**63])
def test_invalid_quantity_rejected(bad: int) -> None:
    with pytest.raises(ValueError):
        ready(imbalance, bad, 100)
    with pytest.raises(ValueError):
        ready(imbalance, 100, bad)


@given(st.integers(0, 10**9), st.integers(0, 10**9), st.integers(1, 100))
def test_imbalance_bounds_side_symmetry_and_size_scale(bid: int, ask: int, scale: int) -> None:
    result = ready(imbalance, bid, ask)
    if bid + ask == 0:
        assert result is None
    else:
        assert result is not None and -1 <= result <= 1
        assert ready(imbalance, ask, bid) == pytest.approx(-result)
        assert ready(imbalance, bid * scale, ask * scale) == pytest.approx(result)


@pytest.mark.parametrize(
    "bid,ask,expected",
    [
        (3900, 5900, -350),  # Bid down: -100; ask down: -250 => -350.
        (3900, 6000, -150),  # -100 + (200-250) = -150.
        (3900, 6100, 100),  # -100 + 200 = 100.
        (4000, 5900, -200),  # (150-100) - 250 = -200.
        (4000, 6000, 0),  # (150-100) + (200-250) = 0; both equality terms apply.
        (4000, 6100, 250),  # (150-100) + 200 = 250.
        (4100, 5900, -100),  # 150 - 250 = -100.
        (4100, 6000, 100),  # 150 + (200-250) = 100.
        (4100, 6100, 350),  # 150 + 200 = 350.
    ],
)
def test_cks_ofi_all_bid_and_ask_price_cases(bid: int, ask: int, expected: int) -> None:
    old = BookQuote(4000, 6000, 100, 200)
    new = BookQuote(bid, ask, 150, 250)
    result = ready(order_flow, old, new)
    assert type(result) is int and result == expected


def test_ofi_is_exact_for_large_quantities_and_allows_locked_quotes() -> None:
    # Same prices: bid adds one hundredth; ask does not change.
    old = BookQuote(5000, 5000, 2**53, 100)
    assert ready(order_flow, old, replace(old, bid_qty=2**53 + 1)) == 1
    # Both prices rise: +current bid +previous ask can exceed signed int64.
    q = 2**63 - 1
    assert ready(order_flow, BookQuote(4000, 6000, q, q), BookQuote(4001, 6001, q, q)) == 2 * q
    assert ready(order_flow, old, old) == 0


@pytest.mark.parametrize(
    "bad",
    [
        BookQuote(-1, 6000, 100, 100),
        BookQuote(4000, 10001, 100, 100),
        BookQuote(6001, 6000, 100, 100),
        BookQuote(4000, 6000, -1, 100),
        BookQuote(4000, 6000, 100, 2**63),
        BookQuote(4000, 6000, True, 100),
    ],
)
def test_ofi_rejects_malformed_quotes_on_either_side(bad: BookQuote) -> None:
    valid = BookQuote(4000, 6000, 100, 100)
    with pytest.raises(ValueError):
        ready(order_flow, bad, valid)
    with pytest.raises(ValueError):
        ready(order_flow, valid, bad)


@given(
    st.integers(0, 4000),
    st.integers(6000, 10000),
    st.integers(0, 4000),
    st.integers(6000, 10000),
    st.integers(1, 100000),
    st.integers(1, 100000),
    st.integers(1, 100000),
    st.integers(1, 100000),
    st.integers(1, 100),
)
def test_ofi_linear_quantity_scaling_and_yes_no_reflection(
    b0: int,
    a0: int,
    b1: int,
    a1: int,
    qb0: int,
    qa0: int,
    qb1: int,
    qa1: int,
    scale: int,
) -> None:
    old, new = BookQuote(b0, a0, qb0, qa0), BookQuote(b1, a1, qb1, qa1)
    value = ready(order_flow, old, new)
    assert (
        ready(
            order_flow,
            BookQuote(b0, a0, qb0 * scale, qa0 * scale),
            BookQuote(b1, a1, qb1 * scale, qa1 * scale),
        )
        == scale * value
    )
    # Complementing YES prices and swapping bid/ask reverses demand/supply.
    assert (
        ready(
            order_flow,
            BookQuote(10000 - a0, 10000 - b0, qa0, qb0),
            BookQuote(10000 - a1, 10000 - b1, qa1, qb1),
        )
        == -value
    )


def test_targets_skip_equal_mids_use_half_ticks_and_stop_at_each_utc_day() -> None:
    events = [
        event(0),
        event(1, bid_qty=300),
        event(2, bid=4001, bid_qty=200),
        event(3, bid=4000, ask=6001, ask_qty=300),
        event(4, bid=3999),
        event(5, bid=3999, bid_qty=200),
        event(DAY_NS, bid=5000, ask_qty=300),
        event(DAY_NS + 1, bid=5000, ask=6002, bid_qty=300),
    ]
    result = ready(build_samples, events)
    # Mids: 5000,5000,5000.5,5000.5,4999.5,4999.5 | 5500,5501.
    # Both trailing 4999.5 rows have no future same-day change, so neither is labeled.
    assert [s.timestamp_ns - BASE_NS for s in result] == [0, 1, 2, 3, DAY_NS]
    assert [s.target for s in result] == [1, 1, -1, -1, 1]
    assert [s.imbalance for s in result] == pytest.approx([0, 0.5, 1 / 3, -0.5, -0.5])
    # The current transition's flow is a feature; the future transition is only a target.
    # t1: +200; t2: +200; t3: -200+100=-100; new day has no predecessor.
    assert [s.ofi for s in result] == [None, 200, 200, -100, None]
    assert all(s.day == BASE_DAY for s in result[:-1])
    assert result[-1].day.toordinal() == BASE_DAY.toordinal() + 1
    assert events[1].quote == BookQuote(4000, 6000, 300, 100)


def test_interleaved_markets_have_independent_predecessors_and_targets() -> None:
    events = [
        event(0),
        event(0, bid=1000, ask=3000, market="B"),
        event(1, bid_qty=200),
        event(1, bid=999, ask=3000, market="B"),
        event(2, bid=4001),
    ]
    result = ready(build_samples, events)
    assert [(s.market, s.timestamp_ns - BASE_NS, s.target) for s in result] == [
        ("A", 0, 1),
        ("B", 0, -1),
        ("A", 1, 1),
    ]
    assert [s.ofi for s in result] == [None, None, 100]


@pytest.mark.parametrize("gap", [None, BookQuote(4000, 6000, 0, 0)])
def test_missing_or_empty_book_breaks_forward_labels_and_resets_ofi(gap: BookQuote | None) -> None:
    events = [event(0), BookEvent("A", BASE_NS + 1, gap), event(2, bid=4001), event(3, bid=4002)]
    result = ready(build_samples, events)
    assert len(result) == 1
    assert result[0].timestamp_ns == BASE_NS + 2 and result[0].target == 1
    assert result[0].ofi is None


def test_same_timestamp_events_keep_input_order() -> None:
    result = ready(build_samples, [event(0), event(0, bid=4001), event(0, bid=3999)])
    assert [s.target for s in result] == [1, -1]
    assert [s.ofi for s in result] == [None, 100]


def test_empty_singleton_and_constant_day_have_no_forward_samples() -> None:
    assert ready(build_samples, []) == ()
    assert ready(build_samples, [event(0)]) == ()
    assert ready(build_samples, [event(0), event(1, bid_qty=900), event(DAY_NS, bid=4001)]) == ()


@pytest.mark.parametrize(
    "events",
    [
        [event(1), event(0)],
        [event(0, market="")],
        [BookEvent("A", -1, BookQuote(4000, 6000, 100, 100))],
        [event(0, bid=6001)],
        [event(0, bid_qty=-1)],
    ],
)
def test_bad_event_order_metadata_or_quote_rejected(events: list[BookEvent]) -> None:
    with pytest.raises(ValueError):
        ready(build_samples, events)


def test_future_quantity_changes_cannot_change_earlier_features_or_targets() -> None:
    events = [event(0), event(1, bid_qty=200), event(2, bid=4001)]
    original = ready(build_samples, events)
    changed = ready(build_samples, events[:-1] + [event(2, bid=4001, bid_qty=999, ask_qty=777)])
    assert changed == original
