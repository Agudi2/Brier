"""The shared JSON cases specify decimal text, not binary float behavior."""

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from brierlib.fixed_point import format_price, parse_price, parse_quantity

CASES = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures/fixed_point_cases.json").read_text()
)


def _result(call: object, value: object) -> object:
    # RED stubs are explicitly unimplemented; convert that to an assertion failure.
    try:
        return call(value)  # type: ignore[operator]
    except NotImplementedError:
        return object()


def _assert_rejected(call: object, text: str) -> None:
    try:
        call(text)  # type: ignore[operator]
    except ValueError:
        return
    except NotImplementedError:
        pytest.fail("fixed-point parser is not implemented")
    pytest.fail(f"malformed input was accepted: {text!r}")


@pytest.mark.parametrize("case", CASES["price_valid"])
def test_price_cases(case: dict[str, object]) -> None:
    assert _result(parse_price, case["input"]) == case["expected"]


@pytest.mark.parametrize("text", CASES["price_invalid"])
def test_price_rejects_malformed(text: str) -> None:
    _assert_rejected(parse_price, text)


@pytest.mark.parametrize("case", CASES["quantity_valid"])
def test_quantity_cases(case: dict[str, object]) -> None:
    # 10.00 -> 1000, 1.5 -> 150, -3.50 -> -350 by multiplying by 100.
    # 92233720368547758.07 * 100 = INT64_MAX; the negative .08 = INT64_MIN.
    assert _result(parse_quantity, case["input"]) == case["expected"]


@pytest.mark.parametrize("text", CASES["quantity_invalid"])
def test_quantity_rejects_malformed_or_overflow(text: str) -> None:
    _assert_rejected(parse_quantity, text)


@pytest.mark.parametrize("case", CASES["price_format"])
def test_price_format(case: dict[str, object]) -> None:
    assert _result(format_price, case["input"]) == case["expected"]


@given(st.integers(min_value=0, max_value=10000))
def test_price_round_trip_for_any_valid_tick(ticks: int) -> None:
    try:
        rendered = format_price(ticks)
        parsed = parse_price(rendered)
    except NotImplementedError:
        pytest.fail("fixed-point functions are not implemented")
    assert parsed == ticks
