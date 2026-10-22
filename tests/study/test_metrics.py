"""Scoring rules, bin accounting, and identifiable logistic calibration."""

import math

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from study.metrics import (
    brier_score,
    calibration_fit,
    log_loss,
    murphy_decomposition,
    reliability_bins,
)
from tests.study.metrics_helpers import ready


def test_brier_hand_calculation() -> None:
    # ((0.3-0)^2 + (0.8-1)^2)/2 = (0.09+0.04)/2 = 0.065.
    assert ready(brier_score, [0.3, 0.8], [0, 1]) == pytest.approx(0.065)
    assert ready(brier_score, [0.0, 1.0], [0, 1]) == 0.0
    assert ready(brier_score, [1.0, 0.0], [0, 1]) == 1.0


def test_log_loss_natural_log_and_clipped_row_count() -> None:
    result = ready(log_loss, [0.3, 0.8], [0, 1])
    # -(log(1-0.3)+log(0.8))/2, no clipping.
    assert result.value == pytest.approx(-(math.log(0.7) + math.log(0.8)) / 2)
    assert result.clipped == 0
    result = ready(log_loss, [0.0, 1.0, 0.0001, 0.9999], [1, 0, 0, 1])
    assert result.clipped == 2  # Values exactly at eps and 1-eps are unchanged.
    assert result.value == pytest.approx(-(2 * math.log(0.0001) + 2 * math.log(0.9999)) / 4)
    assert ready(log_loss, [0.0, 1.0], [0, 1], eps=0.01).value == pytest.approx(-math.log(0.99))


@pytest.mark.parametrize("eps", [0.0, -0.1, 0.5, 1.0, math.nan, math.inf])
def test_invalid_clipping_rejected(eps: float) -> None:
    for fn in (log_loss, calibration_fit):
        with pytest.raises(ValueError):
            ready(fn, [0.2, 0.8], [0, 1], eps=eps)


@pytest.mark.parametrize(
    "p,y",
    [
        ([], []),
        ([0.2], []),
        ([-0.01], [0]),
        ([1.01], [1]),
        ([math.nan], [1]),
        ([math.inf], [0]),
        ([0.2], [2]),
        ([0.2], [-1]),
    ],
)
def test_metrics_reject_bad_inputs(p: list[float], y: list[int]) -> None:
    for fn in (brier_score, log_loss, reliability_bins, murphy_decomposition, calibration_fit):
        with pytest.raises(ValueError):
            ready(fn, p, y)


def test_equal_width_endpoints_and_empty_bins() -> None:
    bins = ready(reliability_bins, [0.0, 0.1, 0.3, 0.9, 1.0], [0, 1, 0, 1, 0])
    assert len(bins) == 10
    assert [b.n for b in bins] == [1, 1, 0, 1, 0, 0, 0, 0, 0, 2]
    assert [b.index for b in bins] == list(range(10))
    np.testing.assert_allclose(
        [(b.lower, b.upper) for b in bins],
        [(i / 10, (i + 1) / 10) for i in range(10)],
        atol=1e-15,
        rtol=0,
    )
    assert bins[9].mean_p == pytest.approx(0.95)  # (0.9+1.0)/2.
    assert bins[9].observed == 0.5
    assert bins[2].mean_p is None and bins[2].observed is None


def test_quantile_edges_and_ties() -> None:
    bins = ready(
        reliability_bins, [0.1, 0.2, 0.3, 0.4], [0, 1, 1, 1], n_bins=2, strategy="quantile"
    )
    # Linear median is (0.2+0.3)/2 = 0.25; each bin has two observations.
    np.testing.assert_allclose(
        [(b.lower, b.upper) for b in bins],
        [(0.1, 0.25), (0.25, 0.4)],
        atol=1e-15,
        rtol=0,
    )
    assert [b.n for b in bins] == [2, 2]
    assert [b.mean_p for b in bins] == pytest.approx([0.15, 0.35])
    assert [b.observed for b in bins] == [0.5, 1.0]
    tied = ready(
        reliability_bins, [0.2, 0.2, 0.2, 0.8], [0, 0, 1, 1], n_bins=4, strategy="quantile"
    )
    # Quartiles [.2,.2,.2,.35,.8] collapse to [.2,.35,.8], not split ties.
    assert [b.n for b in tied] == [3, 1]
    constant = ready(reliability_bins, [0.4] * 3, [0, 1, 0], n_bins=10, strategy="quantile")
    assert len(constant) == 1
    assert constant[0].lower == constant[0].upper == 0.4
    assert constant[0].n == 3


@pytest.mark.parametrize("n_bins", [0, -1])
def test_invalid_bin_count(n_bins: int) -> None:
    with pytest.raises(ValueError):
        ready(reliability_bins, [0.5], [1], n_bins=n_bins)


def test_murphy_hand_calculation() -> None:
    p, y = [0.2] * 4 + [0.6] * 4, [0, 0, 0, 1, 0, 1, 1, 1]
    value = ready(murphy_decomposition, p, y)
    # Observed frequencies .25 and .75; overall .5. Each bin has weight 1/2.
    # REL=(.05^2+.15^2)/2=.0125; RES=(.25^2+.25^2)/2=.0625; UNC=.5*.5=.25.
    assert value.reliability == pytest.approx(0.0125)
    assert value.resolution == pytest.approx(0.0625)
    assert value.uncertainty == pytest.approx(0.25)
    assert value.reliability - value.resolution + value.uncertainty == pytest.approx(0.2)
    assert ready(brier_score, p, y) == pytest.approx(0.2)


def test_murphy_does_not_claim_exact_identity_for_varying_forecasts_in_one_bin() -> None:
    value = ready(murphy_decomposition, [0.1, 0.2], [0, 1], n_bins=1)
    # Grouped forecast .15 vs frequency .5: REL=.1225, RES=0, UNC=.25.
    assert value.reliability == pytest.approx(0.1225)
    assert value.resolution == 0.0
    assert value.uncertainty == 0.25
    # Actual Brier=(.01+.64)/2=.325; grouped decomposition=.3725.
    assert ready(brier_score, [0.1, 0.2], [0, 1]) == pytest.approx(0.325)


def test_logistic_fit_exact_two_level_solution() -> None:
    result = ready(calibration_fit, [0.25] * 4 + [0.75] * 4, [0, 0, 0, 1, 0, 1, 1, 1])
    # Empirical fractions equal forecasts at both levels, so a=0, b=1 solves the score equations.
    assert result.converged and result.reason is None
    assert result.intercept == pytest.approx(0, abs=1e-7)
    assert result.slope == pytest.approx(1, abs=1e-7)
    assert result.clipped == 0


@pytest.mark.parametrize("intercept,slope,seed", [(0.0, 1.0, 1101), (-0.2, 0.8, 1102)])
def test_logistic_simulation_recovers_generating_parameters(
    intercept: float, slope: float, seed: int
) -> None:
    rng = np.random.default_rng(seed)
    x = rng.uniform(-2.5, 2.5, 50_000)
    p = 1 / (1 + np.exp(-x))
    y = rng.binomial(1, 1 / (1 + np.exp(-(intercept + slope * x))))
    result = ready(calibration_fit, p.tolist(), y.tolist())
    assert result.converged
    # 50k independent observations with broad logits: .08 tolerances exceed several standard errors.
    assert result.intercept == pytest.approx(intercept, abs=0.08)
    assert result.slope == pytest.approx(slope, abs=0.08)


@pytest.mark.parametrize(
    "p,y,reason",
    [
        ([0.2, 0.8], [0, 0], "one_class"),
        ([0.5, 0.5], [0, 1], "constant_predictor"),
        ([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1], "separation"),
    ],
)
def test_nonidentifiable_fits_return_null(p: list[float], y: list[int], reason: str) -> None:
    result = ready(calibration_fit, p, y)
    assert not result.converged and result.reason == reason
    assert result.intercept is None and result.slope is None


def test_iteration_limit_does_not_publish_unconverged_coefficients() -> None:
    result = ready(calibration_fit, [0.25] * 4 + [0.75] * 4, [0, 0, 0, 1, 0, 1, 1, 1], max_iter=1)
    if result.converged:
        # An exact solution within the limit is allowed, but a partial iterate is not.
        assert result.reason is None
        assert result.intercept == pytest.approx(0, abs=1e-7)
        assert result.slope == pytest.approx(1, abs=1e-7)
    else:
        assert result.reason == "non_convergence"
        assert result.intercept is None and result.slope is None


@given(
    st.lists(
        st.tuples(st.floats(0, 1, allow_nan=False, allow_infinity=False), st.integers(0, 1)),
        min_size=1,
        max_size=100,
    )
)
def test_score_symmetry_and_bin_mass(rows: list[tuple[float, int]]) -> None:
    p, y = [r[0] for r in rows], [r[1] for r in rows]
    score = ready(brier_score, p, y)
    assert 0 <= score <= 1
    assert ready(brier_score, [1 - v for v in p], [1 - v for v in y]) == pytest.approx(
        score, abs=1e-14
    )
    bins = ready(reliability_bins, p, y)
    assert sum(b.n for b in bins) == len(rows)
    assert sum(b.n * (b.mean_p or 0) for b in bins) == pytest.approx(sum(p))
    assert sum(b.n * (b.observed or 0) for b in bins) == pytest.approx(sum(y))


def test_logistic_clipping_is_reported_without_complementing_probabilities() -> None:
    # At each of the two clipped predictor levels, half the labels are 1.
    result = ready(calibration_fit, [0.0, 0.0, 1.0, 1.0], [0, 1, 0, 1])
    assert result.converged and result.clipped == 4
    assert result.intercept == pytest.approx(0, abs=1e-7)
    assert result.slope == pytest.approx(0, abs=1e-7)


@pytest.mark.parametrize("iterations,tol", [(0, 1e-9), (-1, 1e-9), (10, 0.0), (10, math.nan)])
def test_invalid_solver_options(iterations: int, tol: float) -> None:
    with pytest.raises(ValueError):
        ready(
            calibration_fit,
            [0.25] * 4 + [0.75] * 4,
            [0, 0, 0, 1, 0, 1, 1, 1],
            max_iter=iterations,
            tol=tol,
        )


@given(st.lists(st.integers(0, 1), min_size=2, max_size=60))
def test_murphy_identity_for_constant_forecasts_within_bins(y: list[int]) -> None:
    # Three distinct forecasts occupy different bins regardless of class frequencies.
    p = [(0.15, 0.45, 0.85)[i % 3] for i in range(len(y))]
    decomposition = ready(murphy_decomposition, p, y)
    assert decomposition.reliability - decomposition.resolution + decomposition.uncertainty == (
        pytest.approx(ready(brier_score, p, y), abs=1e-12)
    )
