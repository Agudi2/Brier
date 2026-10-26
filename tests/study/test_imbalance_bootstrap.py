"""Independent market-day resampling oracles and uncertainty edge cases."""

import math
from datetime import date, timedelta

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from study.imbalance import evaluate, hit_rate_interval
from tests.study.imbalance_helpers import BASE_DAY, ready
from tests.study.metrics_helpers import percentile


def cluster_data() -> tuple[list[int], list[int], list[tuple[str, date]]]:
    day1 = BASE_DAY + timedelta(days=1)
    # Four different (market,day) clusters; neither market alone nor day alone is valid.
    # A/day0: 1/1 hit. A/day1: 0/3. B/day0: 2/2. B/day1: 0/2.
    return (
        [1] * 8,
        [1, -1, -1, -1, 1, 1, -1, -1],
        [("A", BASE_DAY)] + [("A", day1)] * 3 + [("B", BASE_DAY)] * 2 + [("B", day1)] * 2,
    )


def test_whole_market_days_keep_unequal_cluster_row_weighting() -> None:
    predictions, targets, keys = cluster_data()
    # Each draw samples four whole clusters. Counts a,b,c,d contribute
    # a+2c hits out of a+3b+2c+2d rows. Original=3/8, not cluster average 1/2.
    counts = np.random.default_rng(32).multinomial(4, [0.25] * 4, size=127)
    expected = [(a + 2 * c) / (a + 3 * b + 2 * c + 2 * d) for a, b, c, d in counts.tolist()]
    result = ready(
        hit_rate_interval,
        predictions,
        targets,
        keys,
        n_resamples=127,
        seed=32,
        confidence=0.6,
    )
    assert result.status == "result" and result.n == 8 and result.n_clusters == 4
    assert result.estimate == 3 / 8
    assert result.replicates == pytest.approx(expected)
    assert result.lower == pytest.approx(percentile(expected, 0.2))
    assert result.upper == pytest.approx(percentile(expected, 0.8))


def test_seed_and_row_permutation_are_reproducible() -> None:
    predictions, targets, keys = cluster_data()
    original = ready(hit_rate_interval, predictions, targets, keys, n_resamples=127, seed=9)
    again = ready(hit_rate_interval, predictions, targets, keys, n_resamples=127, seed=9)
    permuted = ready(
        hit_rate_interval,
        predictions[::-1],
        targets[::-1],
        keys[::-1],
        n_resamples=127,
        seed=9,
    )
    assert original == again == permuted
    other = ready(hit_rate_interval, predictions, targets, keys, n_resamples=127, seed=10)
    assert other.replicates != original.replicates


@given(st.integers(1, 10))
def test_uniform_row_replication_preserves_interval_and_cluster_count(copies: int) -> None:
    predictions, targets, keys = cluster_data()
    original = ready(hit_rate_interval, predictions, targets, keys, n_resamples=63, seed=2)
    replicated = ready(
        hit_rate_interval,
        [p for p in predictions for _ in range(copies)],
        [y for y in targets for _ in range(copies)],
        [k for k in keys for _ in range(copies)],
        n_resamples=63,
        seed=2,
    )
    assert replicated.n == copies * original.n
    assert replicated.n_clusters == original.n_clusters == 4
    assert replicated.estimate == original.estimate
    assert replicated.replicates == pytest.approx(original.replicates)
    assert replicated.lower == pytest.approx(original.lower)
    assert replicated.upper == pytest.approx(original.upper)


def test_empty_and_single_cluster_have_explicit_null_intervals() -> None:
    empty = ready(hit_rate_interval, [], [], [], n_resamples=31)
    assert empty.status == "no data" and empty.n == empty.n_clusters == 0
    assert empty.estimate is None and empty.lower is None and empty.upper is None
    assert empty.replicates == ()
    single = ready(
        hit_rate_interval,
        [1] * 4,
        [1, 1, 1, -1],
        [("A", BASE_DAY)] * 4,
        n_resamples=31,
    )
    assert single.status == "insufficient clusters" and single.n_clusters == 1
    assert single.estimate == 3 / 4
    assert single.lower is None and single.upper is None and single.replicates == ()


@pytest.mark.parametrize("target,expected", [(1, 1.0), (-1, 0.0)])
def test_constant_correctness_gives_exact_degenerate_interval(target: int, expected: float) -> None:
    result = ready(
        hit_rate_interval,
        [1, 1],
        [target, target],
        [("A", BASE_DAY), ("B", BASE_DAY)],
        n_resamples=31,
    )
    assert result.status == "result"
    assert result.estimate == result.lower == result.upper == expected
    assert result.replicates == (expected,) * 31


@pytest.mark.parametrize(
    "predictions,targets,keys",
    [
        ([1], [], [("A", BASE_DAY)]),
        ([1], [1], []),
        ([0], [1], [("A", BASE_DAY)]),
        ([1], [0], [("A", BASE_DAY)]),
        ([True], [1], [("A", BASE_DAY)]),
        ([1], [2], [("A", BASE_DAY)]),
        ([1], [1], [("", BASE_DAY)]),
    ],
)
def test_invalid_predictions_labels_or_cluster_keys_are_rejected(
    predictions: list[int],
    targets: list[int],
    keys: list[tuple[str, date]],
) -> None:
    with pytest.raises(ValueError):
        ready(hit_rate_interval, predictions, targets, keys)


@pytest.mark.parametrize(
    "n,seed,confidence",
    [
        (0, 0, 0.95),
        (1, 0, 0.95),
        (31, -1, 0.95),
        (31, 0, 0.0),
        (31, 0, 1.0),
        (31, 0, math.nan),
        (True, 0, 0.95),
    ],
)
def test_options_are_validated_even_for_empty_inputs(n: int, seed: int, confidence: float) -> None:
    with pytest.raises(ValueError):
        ready(hit_rate_interval, [], [], [], n_resamples=n, seed=seed, confidence=confidence)
    with pytest.raises(ValueError):
        ready(evaluate, [], [], n_resamples=n, seed=seed, confidence=confidence)
