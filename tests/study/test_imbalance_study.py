"""Chronological holdout, training-only fitted rules, and honest hit-rate comparisons."""

import math
from dataclasses import replace
from datetime import timedelta
from itertools import permutations
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from study.imbalance import (
    DirectionRule,
    Feature,
    Sample,
    build_samples,
    evaluate,
    fit_rule,
    predict,
    split_dates,
)
from tests.study.imbalance_helpers import BASE_DAY, DAY_NS, event, ready, sample


def training() -> list[Sample]:
    # Majority is down (3/4). Positive polarity gets 3/4, negative gets 1/4.
    return [
        sample(0, -0.8, -1, ofi=8),
        sample(0, -0.4, -1, ofi=4),
        sample(1, 0.2, -1, ofi=-2),
        sample(1, 0.9, 1, ofi=-9),
    ]


def holdout() -> list[Sample]:
    # Training rule predicts +,+,-,+. Hits=2/4; training-majority down hits=1/4.
    # Test majority is up (3/4), so fitting that baseline on test would be leakage.
    return [
        sample(2, 0.1, 1),
        sample(2, 0.2, 1),
        sample(2, -0.3, 1, market="B"),
        sample(3, 0.4, -1),
    ]


def test_date_split_keeps_complete_dates_and_preserves_input_order() -> None:
    rows = [
        sample(2, 0.2, 1),
        sample(0, -0.2, -1),
        sample(1, 0.5, 1, market="B"),
        sample(2, -0.5, -1, market="B"),
        sample(1, -0.5, -1),
    ]
    split = ready(split_dates, rows, train_through=BASE_DAY + timedelta(days=1))
    assert split.train == (rows[1], rows[2], rows[4])
    assert split.test == (rows[0], rows[3])
    assert max(s.day for s in split.train) < min(s.day for s in split.test)
    assert len(split.train) + len(split.test) == len(rows)


@given(st.lists(st.integers(0, 10), max_size=50), st.integers(0, 10))
def test_split_never_places_a_date_in_both_partitions(days: list[int], cutoff: int) -> None:
    rows = [sample(day, 0.5, 1, market=f"M{i}") for i, day in enumerate(days)]
    boundary = BASE_DAY + timedelta(days=cutoff)
    split = ready(split_dates, rows, train_through=boundary)
    assert split.train == tuple(row for row in rows if row.day <= boundary)
    assert split.test == tuple(row for row in rows if row.day > boundary)
    assert {s.day for s in split.train}.isdisjoint(s.day for s in split.test)


@pytest.mark.parametrize("feature,polarity", [("imbalance", 1), ("ofi", -1)])
def test_polarity_and_majority_are_fit_from_training_only(feature: Feature, polarity: int) -> None:
    result = ready(fit_rule, training(), feature=feature)
    assert result == DirectionRule(feature, polarity, -1)
    assert ready(fit_rule, list(reversed(training())), feature=feature) == result


def test_majority_and_polarity_ties_have_fixed_training_only_resolution() -> None:
    # One positive-signal up and one positive-signal down: both polarities hit 1/2.
    result = ready(fit_rule, [sample(0, 0.5, 1), sample(0, 0.5, -1)])
    assert result == DirectionRule("imbalance", 1, 1)
    # Zero signals always use the train majority; no division or regression failure.
    zero = ready(fit_rule, [sample(0, 0.0, -1), sample(0, 0.0, -1), sample(0, 0.0, 1)])
    assert zero == DirectionRule("imbalance", 1, -1)


@pytest.mark.parametrize(
    "rule,values,expected",
    [
        (DirectionRule("imbalance", 1, -1), [-0.5, 0.0, 0.5, None], (-1, -1, 1, None)),
        (DirectionRule("imbalance", -1, 1), [-0.5, 0.0, 0.5], (1, 1, -1)),
        (DirectionRule("ofi", -1, -1), [-100, 0, 100], (1, -1, -1)),
        (DirectionRule("imbalance", 1, 1), [], ()),
    ],
)
def test_predict_sign_zero_and_missing_values(
    rule: DirectionRule,
    values: list[float | int | None],
    expected: tuple[int | None, ...],
) -> None:
    assert ready(predict, rule, values) == expected


def test_holdout_scores_compare_with_chance_and_training_majority() -> None:
    result = ready(evaluate, training(), holdout(), n_resamples=127, seed=9)
    assert result.status == "result"
    assert result.rule == DirectionRule("imbalance", 1, -1)
    assert (result.n_train, result.n_test) == (4, 4)
    assert result.hit_rate == 2 / 4
    assert result.chance_rate == 0.5
    assert result.majority_hit_rate == 1 / 4  # Constant down from train, not 3/4 from test.
    assert result.excess_vs_chance == 0.0
    assert result.excess_vs_majority == 1 / 4
    assert result.interval is not None and result.interval.n_clusters == 3
    assert result.interval.estimate == result.hit_rate
    assert result.interval.lower is not None and result.interval.upper is not None


def test_shuffling_test_labels_never_changes_the_fitted_rule() -> None:
    # A one-row train set fixes polarity +1. Some test permutations strongly favor
    # -1, enough to flip a rule accidentally fit on train+test as well as test alone.
    train = [sample(0, 0.5, 1)]
    test = [sample(1, -0.8, 1), sample(1, -0.4, 1), sample(2, 0.2, -1), sample(2, 0.9, -1)]
    before_train, before_test = tuple(train), tuple(test)
    original = ready(evaluate, train, test, n_resamples=31, seed=3)
    assert original.rule == DirectionRule("imbalance", 1, 1)
    values = [s.imbalance for s in test]
    predictions = ready(predict, original.rule, values)
    for labels in sorted(set(permutations(s.target for s in test))):
        shuffled = [replace(row, target=label) for row, label in zip(test, labels, strict=True)]
        result = ready(evaluate, train, shuffled, n_resamples=31, seed=3)
        assert result.rule == original.rule
        assert result.rule is not None
        assert ready(predict, result.rule, values) == predictions
    # Check the original sequences were not changed while fitting/evaluating.
    assert tuple(train) == before_train and tuple(test) == before_test


def test_heldout_feature_changes_do_not_refit_the_rule() -> None:
    train = [sample(0, 0.5, 1)]
    test = [sample(1, 0.5, 1), sample(1, -0.5, -1), sample(2, 0.5, 1), sample(2, -0.5, -1)]
    changed = [replace(row, imbalance=-row.imbalance, ofi=10**6) for row in test]
    original = ready(evaluate, train, test, n_resamples=31)
    result = ready(evaluate, train, changed, n_resamples=31)
    assert result.rule == original.rule
    assert result.majority_hit_rate == original.majority_hit_rate
    assert original.hit_rate == 1.0 and result.hit_rate == 0.0


def test_missing_ofi_is_excluded_from_training_majority_and_test_denominators() -> None:
    train = [sample(0, 0.5, 1, ofi=10), sample(0, -0.5, -1, ofi=-10), sample(0, 0.5, -1, ofi=None)]
    test = [
        sample(1, 0.5, 1, ofi=100),
        sample(1, -0.5, -1, ofi=None),
        sample(2, -0.5, -1, ofi=-100),
    ]
    result = ready(evaluate, train, test, feature="ofi", n_resamples=31)
    assert result.rule == DirectionRule("ofi", 1, 1)
    assert result.n_train == result.n_test == 2
    assert result.hit_rate == 1.0 and result.majority_hit_rate == 0.5
    assert result.interval is not None and result.interval.n_clusters == 2


def test_events_to_date_split_to_holdout_metrics() -> None:
    events = []
    for day in range(4):
        # Train days 0,1 and test day 3: positive depth precedes up, negative depth down.
        # Test day 2 reverses both moves. Four test observations: two hits, two misses.
        middle_bid = 3999 if day == 2 else 4001
        events.extend(
            [
                event(day * DAY_NS, bid_qty=300),
                event(day * DAY_NS + 1, bid=middle_bid, ask_qty=300),
                event(day * DAY_NS + 2),
            ]
        )
    rows = ready(build_samples, events)
    split = ready(split_dates, rows, train_through=BASE_DAY + timedelta(days=1))
    result = ready(evaluate, split.train, split.test, n_resamples=63, seed=8)
    assert result.rule == DirectionRule("imbalance", 1, 1)
    assert result.n_train == result.n_test == 4
    assert result.hit_rate == result.majority_hit_rate == result.chance_rate == 0.5
    assert result.excess_vs_chance == result.excess_vs_majority == 0.0
    assert result.interval is not None and result.interval.n_clusters == 2
    assert set(result.interval.replicates) == {0.0, 0.5, 1.0}


@pytest.mark.parametrize("train_day,test_day", [(1, 1), (2, 1)])
def test_overlapping_or_reversed_dates_are_rejected_even_across_markets(
    train_day: int,
    test_day: int,
) -> None:
    with pytest.raises(ValueError):
        ready(evaluate, [sample(train_day, 0.5, 1)], [sample(test_day, 0.5, 1, market="B")])


def test_missing_features_cannot_hide_date_overlap() -> None:
    train = [sample(0, 0.5, 1, ofi=10), sample(2, 0.5, 1, ofi=None)]
    with pytest.raises(ValueError):
        ready(evaluate, train, [sample(1, 0.5, 1, ofi=10)], feature="ofi")


def test_no_train_or_test_produces_null_metrics_and_explicit_status() -> None:
    no_train = ready(evaluate, [], holdout(), n_resamples=31)
    assert no_train.status == "no training data" and no_train.rule is None
    assert no_train.n_train == 0 and no_train.n_test == 4
    no_test = ready(evaluate, training(), [], n_resamples=31)
    assert no_test.status == "no test data" and no_test.rule is not None
    assert no_test.n_train == 4 and no_test.n_test == 0
    for result in (no_train, no_test):
        assert result.hit_rate is None and result.majority_hit_rate is None
        assert result.excess_vs_chance is None and result.excess_vs_majority is None
        assert result.interval is None


def test_all_missing_ofi_is_explicitly_no_eligible_data() -> None:
    train, test = [sample(0, 0.5, 1, ofi=None)], [sample(1, 0.5, 1, ofi=10)]
    no_train = ready(evaluate, train, test, feature="ofi", n_resamples=31)
    assert no_train.status == "no training data" and no_train.n_train == 0
    no_test = ready(evaluate, test, [sample(2, 0.5, 1, ofi=None)], feature="ofi", n_resamples=31)
    assert no_test.status == "no test data" and no_test.n_test == 0
    assert no_train.hit_rate is None and no_test.hit_rate is None


def test_one_market_day_keeps_point_scores_but_withholds_uncertainty() -> None:
    result = ready(evaluate, training(), [sample(2, 0.5, 1)] * 10, n_resamples=31)
    assert result.status == "insufficient clusters" and result.hit_rate == 1.0
    assert result.interval is not None and result.interval.n_clusters == 1
    assert result.interval.lower is None and result.interval.upper is None


def test_losing_holdout_result_remains_negative() -> None:
    test = [sample(2, 0.5, -1), sample(3, -0.5, 1)]
    result = ready(evaluate, training(), test, n_resamples=31)
    assert result.status == "result"
    assert result.hit_rate == 0.0 and result.majority_hit_rate == 0.5
    assert result.excess_vs_chance == result.excess_vs_majority == -0.5
    assert result.interval is not None
    assert result.interval.lower == result.interval.upper == 0.0


@pytest.mark.parametrize("rows", [[], [sample(0, 0.5, 1, ofi=None)]])
def test_fit_rejects_no_eligible_training_rows(rows: list[Sample]) -> None:
    with pytest.raises(ValueError):
        ready(fit_rule, rows, feature="ofi")


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 1.01, -1.01])
def test_invalid_imbalance_features_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        ready(fit_rule, [sample(0, value, 1)])
    with pytest.raises(ValueError):
        ready(predict, DirectionRule("imbalance", 1, 1), [value])


@pytest.mark.parametrize("target", [0, 2, True])
def test_invalid_direction_labels_rejected(target: int) -> None:
    with pytest.raises(ValueError):
        ready(fit_rule, [sample(0, 0.5, target)])
    with pytest.raises(ValueError):
        ready(evaluate, training(), [sample(2, 0.5, target)])


def test_unknown_feature_is_rejected() -> None:
    unknown = cast(Feature, "future_mid")
    with pytest.raises(ValueError):
        ready(fit_rule, training(), feature=unknown)
    with pytest.raises(ValueError):
        ready(evaluate, training(), holdout(), feature=unknown)


@pytest.mark.parametrize(
    "rule",
    [
        DirectionRule("imbalance", 0, 1),
        DirectionRule("imbalance", 1, 0),
        DirectionRule(cast(Feature, "unknown"), 1, 1),
    ],
)
def test_malformed_rule_is_rejected(rule: DirectionRule) -> None:
    with pytest.raises(ValueError):
        ready(predict, rule, [0.5])


@pytest.mark.parametrize("value", [1.5, math.nan, True])
def test_ofi_must_be_integer(value: int) -> None:
    with pytest.raises(ValueError):
        ready(fit_rule, [sample(0, 0.5, 1, ofi=value)], feature="ofi")
    with pytest.raises(ValueError):
        ready(predict, DirectionRule("ofi", 1, 1), [value])
