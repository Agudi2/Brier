"""Independent whole-event resampling oracles and percentile interval accounting."""

import math

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from study.bootstrap import Statistic, cluster_bootstrap, favorite_longshot
from tests.study.metrics_helpers import percentile, ready


def test_unequal_event_sizes_resample_whole_events_and_keep_row_weighting() -> None:
    # A has one perfect prediction, B has three errors. AB score=3/4, not 1/2.
    # Draws AA, AB, BB have scores 0, 3/4, 1, with probabilities 1/4, 1/2, 1/4.
    counts = np.random.default_rng(32).multinomial(2, [0.5, 0.5], size=127)
    expected = [3 * b / (a + 3 * b) for a, b in counts.tolist()]
    result = ready(
        cluster_bootstrap,
        [0.0, 1.0, 1.0, 1.0],
        [0, 0, 0, 0],
        ["A", "B", "B", "B"],
        n_resamples=127,
        seed=32,
        confidence=0.6,
        batch_size=7,
    )
    assert result.estimate == 0.75
    assert result.n_events == 2
    assert result.replicates == pytest.approx(expected)
    assert result.lower == pytest.approx(percentile(expected, 0.2))
    assert result.upper == pytest.approx(percentile(expected, 0.8))
    assert result.warnings == ()


@pytest.mark.parametrize("statistic", ["gap", "log_loss"])
def test_other_scalar_statistics_follow_same_event_draws(statistic: Statistic) -> None:
    # A=(.25,0), B=(.75,1): gaps -.25,+.25; both losses -log(.75).
    counts = np.random.default_rng(17).multinomial(2, [0.5, 0.5], size=101)
    expected = [
        (-0.25 * a + 0.25 * b) / 2 if statistic == "gap" else -math.log(0.75)
        for a, b in counts.tolist()
    ]
    result = ready(
        cluster_bootstrap,
        [0.25, 0.75],
        [0, 1],
        ["A", "B"],
        statistic=statistic,
        n_resamples=101,
        seed=17,
        confidence=0.8,
    )
    assert result.estimate == pytest.approx(0 if statistic == "gap" else -math.log(0.75))
    assert result.replicates == pytest.approx(expected)
    assert result.lower == pytest.approx(percentile(expected, 0.1))
    assert result.upper == pytest.approx(percentile(expected, 0.9))


def test_seed_batch_size_and_input_order_do_not_change_intervals() -> None:
    p, y, events = [0.0, 0.5, 1.0, 0.5], [0, 1, 0, 0], ["B", "A", "C", "A"]
    first = ready(cluster_bootstrap, p, y, events, n_resamples=103, seed=19, batch_size=1)
    again = ready(cluster_bootstrap, p, y, events, n_resamples=103, seed=19, batch_size=1)
    assert first == again
    for size in (7, 256):
        result = ready(
            cluster_bootstrap,
            p[::-1],
            y[::-1],
            events[::-1],
            n_resamples=103,
            seed=19,
            batch_size=size,
        )
        assert result == first  # Inputs are binary-exact, including sums.
    changed = ready(cluster_bootstrap, p, y, events, n_resamples=103, seed=20)
    assert changed.replicates != first.replicates
    assert p == [0.0, 0.5, 1.0, 0.5] and events == ["B", "A", "C", "A"]


def test_single_event_degenerate_interval_has_explicit_warning() -> None:
    result = ready(cluster_bootstrap, [0.3, 0.8], [0, 1], ["E", "E"], n_resamples=31)
    # Resampling the only event always repeats the original two rows: (.09+.04)/2.
    assert result.estimate == pytest.approx(0.065)
    assert result.lower == pytest.approx(0.065)
    assert result.upper == pytest.approx(0.065)
    assert result.replicates == pytest.approx([0.065] * 31)
    assert result.n_events == 1 and "single_event" in result.warnings


@pytest.mark.parametrize("statistic", ["intercept", "slope"])
def test_logistic_replicates_match_two_level_closed_form(statistic: Statistic) -> None:
    # Each event has 4 p=.25 and 4 p=.75 rows. Success counts by event:
    # A=(1,2), B=(2,3), C=(1,3). Every possible draw has both outcomes at both p levels.
    p = ([0.25] * 4 + [0.75] * 4) * 3
    y = [0, 0, 0, 1, 0, 0, 1, 1] + [0, 0, 1, 1, 0, 1, 1, 1] + [0, 0, 0, 1, 0, 1, 1, 1]
    events = ["A"] * 8 + ["B"] * 8 + ["C"] * 8
    counts = np.random.default_rng(41).multinomial(3, [1 / 3] * 3, size=127)
    expected = []
    for a, b, c in counts.tolist():
        q_low, q_high = (a + 2 * b + c) / 12, (2 * a + 3 * b + 3 * c) / 12
        low, high = math.log(q_low / (1 - q_low)), math.log(q_high / (1 - q_high))
        # x_low=-log(3), x_high=log(3); b=(z_high-z_low)/(2log3), a=(z_low+z_high)/2.
        expected.append(
            (low + high) / 2 if statistic == "intercept" else (high - low) / (2 * math.log(3))
        )
    for batch in (1, 17, 256):
        result = ready(
            cluster_bootstrap,
            p,
            y,
            events,
            statistic=statistic,
            n_resamples=127,
            seed=41,
            confidence=0.8,
            batch_size=batch,
        )
        assert result.estimate == pytest.approx(
            0 if statistic == "intercept" else math.log(2) / math.log(3), abs=1e-6
        )
        assert result.replicates == pytest.approx(expected, abs=1e-6)
        assert result.lower == pytest.approx(percentile(expected, 0.1), abs=1e-6)
        assert result.upper == pytest.approx(percentile(expected, 0.9), abs=1e-6)
        assert result.warnings == ()


def test_unidentifiable_bootstrap_fit_is_not_zero_or_nan() -> None:
    result = ready(
        cluster_bootstrap, [0.2, 0.8], [0, 0], ["A", "B"], statistic="slope", n_resamples=31
    )
    assert result.estimate is None and result.lower is None and result.upper is None
    assert result.replicates == (None,) * 31
    assert "undefined_estimate" in result.warnings
    assert "invalid_replicates" in result.warnings


@pytest.mark.parametrize(
    "n,confidence,batch,seed",
    [
        (1, 0.95, 1, 0),
        (0, 0.95, 1, 0),
        (10, 0.0, 1, 0),
        (10, 1.0, 1, 0),
        (10, math.nan, 1, 0),
        (10, 0.95, 0, 0),
        (10, 0.95, 1, -1),
    ],
)
def test_bad_bootstrap_options(n: int, confidence: float, batch: int, seed: int) -> None:
    with pytest.raises(ValueError):
        ready(
            cluster_bootstrap,
            [0.5, 0.5],
            [0, 1],
            ["A", "B"],
            n_resamples=n,
            confidence=confidence,
            batch_size=batch,
            seed=seed,
        )


@pytest.mark.parametrize(
    "p,y,events",
    [
        ([], [], []),
        ([0.5], [0], []),
        ([0.5], [2], ["A"]),
        ([math.nan], [1], ["A"]),
        ([0.5], [0], [""]),
    ],
)
def test_bad_bootstrap_data(p: list[float], y: list[int], events: list[str]) -> None:
    with pytest.raises(ValueError):
        ready(cluster_bootstrap, p, y, events)


def test_favorite_longshot_gaps_and_cluster_intervals() -> None:
    result = ready(
        favorite_longshot,
        [0.1, 0.1, 0.9, 0.9],
        [0, 1, 0, 1],
        ["A", "B", "A", "B"],
        n_resamples=101,
        seed=72,
        confidence=0.8,
    )
    assert len(result) == 10
    assert [r.bin.n for r in result] == [0, 2, 0, 0, 0, 0, 0, 0, 0, 2]
    counts = np.random.default_rng(72).multinomial(2, [0.5, 0.5], size=101)
    for index, price, gap in ((1, 0.1, 0.4), (9, 0.9, -0.4)):
        # Each selected event contributes one row to each populated bin: observed=count(B)/2.
        expected = [b / 2 - price for a, b in counts.tolist()]
        ci = result[index].gap
        assert ci.estimate == pytest.approx(gap)
        assert ci.replicates == pytest.approx(expected)
        assert ci.lower == pytest.approx(percentile(expected, 0.1))
        assert ci.upper == pytest.approx(percentile(expected, 0.9))
        assert ci.n_events == 2
    empty = result[0].gap
    assert empty.estimate is None and empty.lower is None and empty.upper is None
    assert "empty_bin" in empty.warnings


def test_empty_bin_replicates_are_reported_and_interval_withheld() -> None:
    result = ready(favorite_longshot, [0.1, 0.9], [0, 1], ["A", "B"], n_resamples=101, seed=72)
    ci = result[1].gap
    counts = np.random.default_rng(72).multinomial(2, [0.5, 0.5], size=101)
    expected = [None if a == 0 else -0.1 for a, b in counts.tolist()]
    assert ci.estimate == pytest.approx(-0.1)
    assert ci.replicates == pytest.approx(expected)
    assert ci.lower is None and ci.upper is None
    assert "invalid_replicates" in ci.warnings


@given(
    st.lists(
        st.tuples(st.floats(0, 1, allow_nan=False, allow_infinity=False), st.integers(0, 1)),
        min_size=2,
        max_size=15,
    ),
    st.integers(1, 5),
)
def test_uniform_row_replication_preserves_event_bootstrap(
    rows: list[tuple[float, int]], copies: int
) -> None:
    p, y = [r[0] for r in rows], [r[1] for r in rows]
    events = [f"E{i:02d}" for i in range(len(rows))]
    original = ready(cluster_bootstrap, p, y, events, n_resamples=61, seed=7)
    duplicated = ready(
        cluster_bootstrap,
        [v for v in p for _ in range(copies)],
        [v for v in y for _ in range(copies)],
        [e for e in events for _ in range(copies)],
        n_resamples=61,
        seed=7,
    )
    assert duplicated.n_events == original.n_events
    assert duplicated.estimate == pytest.approx(original.estimate)
    assert duplicated.replicates == pytest.approx(original.replicates, abs=1e-12)
    assert duplicated.lower == pytest.approx(original.lower, abs=1e-12)
    assert duplicated.upper == pytest.approx(original.upper, abs=1e-12)


@pytest.mark.slow
def test_cluster_percentile_coverage_over_200_simulated_datasets() -> None:
    covered = 0
    events = [f"E{i:03d}" for i in range(100) for _ in range(5)]
    # Each event has five identical outcomes, so effective n is 100, not 500.
    # Y~Bernoulli(.25), p=.25: E[(p-Y)^2]=.25*.75=.1875.
    for index in range(200):
        rng = np.random.default_rng(11000 + index)
        outcomes = np.repeat(rng.binomial(1, 0.25, size=100), 5).tolist()
        result = ready(
            cluster_bootstrap,
            [0.25] * 500,
            outcomes,
            events,
            n_resamples=999,
            seed=22000 + index,
            confidence=0.95,
            batch_size=128,
        )
        assert result.lower is not None and result.upper is not None
        covered += result.lower <= 0.1875 <= result.upper
    assert 0.90 <= covered / 200 <= 0.99


def test_failed_logistic_replicates_do_not_produce_a_conditional_interval() -> None:
    # Original data has 50% successes at each predictor, so a=b=0.
    # AA or BB has one outcome class and cannot identify a finite logistic fit.
    result = ready(
        cluster_bootstrap,
        [0.25, 0.75, 0.25, 0.75],
        [0, 0, 1, 1],
        ["A", "A", "B", "B"],
        statistic="slope",
        n_resamples=101,
        seed=72,
    )
    counts = np.random.default_rng(72).multinomial(2, [0.5, 0.5], size=101)
    expected = [None if a == 0 or b == 0 else 0.0 for a, b in counts.tolist()]
    assert result.estimate == pytest.approx(0, abs=1e-7)
    assert result.replicates == pytest.approx(expected, abs=1e-7)
    assert result.lower is None and result.upper is None
    assert "invalid_replicates" in result.warnings


def test_favorite_longshot_quantile_bins_are_fixed_from_original_sample() -> None:
    result = ready(
        favorite_longshot,
        [0.1, 0.2, 0.8, 0.9],
        [0, 0, 1, 1],
        ["A", "B", "A", "B"],
        n_bins=2,
        strategy="quantile",
        n_resamples=101,
        seed=72,
    )
    assert [entry.bin.n for entry in result] == [2, 2]
    assert [entry.gap.estimate for entry in result] == pytest.approx([-0.15, 0.15])
    # The original median edge is (.2+.8)/2=.5, including for AA and BB draws.
    assert result[0].bin.upper == pytest.approx(0.5)
    assert result[1].bin.lower == pytest.approx(0.5)
    counts = np.random.default_rng(72).multinomial(2, [0.5, 0.5], size=101)
    assert result[0].gap.replicates == pytest.approx(
        [-(0.1 * a + 0.2 * b) / 2 for a, b in counts.tolist()]
    )
    assert result[1].gap.replicates == pytest.approx(
        [(0.2 * a + 0.1 * b) / 2 for a, b in counts.tolist()]
    )
