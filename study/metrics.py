"""Binary calibration scores, reliability bins, and logistic calibration."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

BinStrategy = Literal["equal_width", "quantile"]


@dataclass(frozen=True)
class LogLoss:
    value: float
    clipped: int


@dataclass(frozen=True)
class ReliabilityBin:
    index: int
    lower: float
    upper: float
    n: int
    mean_p: float | None
    observed: float | None


@dataclass(frozen=True)
class MurphyDecomposition:
    reliability: float
    resolution: float
    uncertainty: float


@dataclass(frozen=True)
class CalibrationFit:
    intercept: float | None
    slope: float | None
    converged: bool
    reason: str | None
    clipped: int


def _data(p: Sequence[float], y: Sequence[int]) -> tuple[NDArray[np.float64], NDArray[np.int64]]:
    try:
        forecast = np.asarray(p, dtype=np.float64)
        outcome = np.asarray(y)
    except (TypeError, ValueError) as error:
        raise ValueError("p and y must be numeric sequences") from error
    if (
        forecast.ndim != 1
        or outcome.ndim != 1
        or forecast.size == 0
        or forecast.size != outcome.size
        or not np.all(np.isfinite(forecast))
        or np.any((forecast < 0) | (forecast > 1))
        or outcome.dtype.kind not in "iu"
        or np.any((outcome != 0) & (outcome != 1))
    ):
        raise ValueError("p and y must be aligned, nonempty, finite binary observations")
    return forecast, outcome.astype(np.int64, copy=False)


def _eps(eps: float) -> None:
    if not isinstance(eps, (int, float)) or not math.isfinite(eps) or not 0 < eps < 0.5:
        raise ValueError("eps must be finite and strictly between 0 and 0.5")


def _bins(
    p: NDArray[np.float64],
    y: NDArray[np.int64],
    n_bins: int,
    strategy: BinStrategy,
) -> tuple[tuple[ReliabilityBin, ...], NDArray[np.int64]]:
    if not isinstance(n_bins, int) or isinstance(n_bins, bool) or n_bins <= 0:
        raise ValueError("n_bins must be a positive integer")
    if strategy == "equal_width":
        edges = np.arange(n_bins + 1, dtype=np.float64) / n_bins
        indices = np.minimum(np.floor(p * n_bins).astype(np.int64), n_bins - 1)
    elif strategy == "quantile":
        edges = np.unique(np.quantile(p, np.linspace(0, 1, n_bins + 1), method="linear"))
        if len(edges) == 1:
            edges = np.repeat(edges, 2)
            indices = np.zeros(len(p), dtype=np.int64)
        else:
            indices = np.minimum(np.searchsorted(edges, p, side="right") - 1, len(edges) - 2)
    else:
        raise ValueError("unknown reliability-bin strategy")
    counts = np.bincount(indices, minlength=len(edges) - 1)
    price_sums = np.bincount(indices, weights=p, minlength=len(edges) - 1)
    outcome_sums = np.bincount(indices, weights=y, minlength=len(edges) - 1)
    bins = tuple(
        ReliabilityBin(
            index=i,
            lower=float(edges[i]),
            upper=float(edges[i + 1]),
            n=int(counts[i]),
            mean_p=float(price_sums[i] / counts[i]) if counts[i] else None,
            observed=float(outcome_sums[i] / counts[i]) if counts[i] else None,
        )
        for i in range(len(edges) - 1)
    )
    return bins, indices


def brier_score(p: Sequence[float], y: Sequence[int]) -> float:
    forecast, outcome = _data(p, y)
    return float(np.mean(np.square(forecast - outcome)))


def log_loss(p: Sequence[float], y: Sequence[int], *, eps: float = 1e-4) -> LogLoss:
    _eps(eps)
    forecast, outcome = _data(p, y)
    clipped = np.clip(forecast, eps, 1 - eps)
    value = -np.mean(outcome * np.log(clipped) + (1 - outcome) * np.log1p(-clipped))
    return LogLoss(float(value), int(np.count_nonzero(clipped != forecast)))


def reliability_bins(
    p: Sequence[float],
    y: Sequence[int],
    *,
    n_bins: int = 10,
    strategy: BinStrategy = "equal_width",
) -> tuple[ReliabilityBin, ...]:
    forecast, outcome = _data(p, y)
    bins, _ = _bins(forecast, outcome, n_bins, strategy)
    return bins


def murphy_decomposition(
    p: Sequence[float],
    y: Sequence[int],
    *,
    n_bins: int = 10,
    strategy: BinStrategy = "equal_width",
) -> MurphyDecomposition:
    forecast, outcome = _data(p, y)
    bins, _ = _bins(forecast, outcome, n_bins, strategy)
    overall = float(np.mean(outcome))
    reliability = 0.0
    resolution = 0.0
    for item in bins:
        if item.n:
            assert item.mean_p is not None and item.observed is not None
            weight = item.n / len(forecast)
            reliability += weight * (item.mean_p - item.observed) ** 2
            resolution += weight * (item.observed - overall) ** 2
    return MurphyDecomposition(reliability, resolution, overall * (1 - overall))


def _logit(p: NDArray[np.float64], eps: float) -> NDArray[np.float64]:
    clipped = np.clip(p, eps, 1 - eps)
    return np.log(clipped) - np.log1p(-clipped)


def _irls_batch(
    x: NDArray[np.float64],
    y: NDArray[np.int64],
    codes: NDArray[np.int64],
    counts: NDArray[np.int64],
    *,
    max_iter: int = 100,
    tol: float = 1e-9,
) -> tuple[NDArray[np.float64], NDArray[np.float64], list[str | None]]:
    """Fit one logistic model per row of event multiplicities with bounded row chunks."""
    batch, groups = counts.shape
    group_n = np.bincount(codes, minlength=groups)
    group_y = np.bincount(codes, weights=y, minlength=groups)
    total = counts @ group_n
    successes = counts @ group_y
    reasons: list[str | None] = [None] * batch
    one_class = (successes == 0) | (successes == total)
    for i in np.flatnonzero(one_class):
        reasons[int(i)] = "one_class"

    # Event extrema detect constant predictors and (quasi-)separation before IRLS.
    present = counts > 0
    minimum = np.full(groups, np.inf)
    maximum = np.full(groups, -np.inf)
    min_zero = np.full(groups, np.inf)
    max_zero = np.full(groups, -np.inf)
    min_one = np.full(groups, np.inf)
    max_one = np.full(groups, -np.inf)
    np.minimum.at(minimum, codes, x)
    np.maximum.at(maximum, codes, x)
    zeros = y == 0
    np.minimum.at(min_zero, codes[zeros], x[zeros])
    np.maximum.at(max_zero, codes[zeros], x[zeros])
    np.minimum.at(min_one, codes[~zeros], x[~zeros])
    np.maximum.at(max_one, codes[~zeros], x[~zeros])
    x_min = np.min(np.where(present, minimum, np.inf), axis=1)
    x_max = np.max(np.where(present, maximum, -np.inf), axis=1)
    x0_min = np.min(np.where(present, min_zero, np.inf), axis=1)
    x0_max = np.max(np.where(present, max_zero, -np.inf), axis=1)
    x1_min = np.min(np.where(present, min_one, np.inf), axis=1)
    x1_max = np.max(np.where(present, max_one, -np.inf), axis=1)
    for row in range(batch):
        if reasons[row] is None:
            if x_min[row] == x_max[row]:
                reasons[row] = "constant_predictor"
            elif x0_max[row] <= x1_min[row] or x1_max[row] <= x0_min[row]:
                reasons[row] = "separation"

    intercept = np.zeros(batch, dtype=np.float64)
    slope = np.zeros(batch, dtype=np.float64)
    active = np.asarray([reason is None for reason in reasons], dtype=np.bool_)
    converged = np.zeros(batch, dtype=np.bool_)
    if not np.any(active):
        return intercept, slope, reasons
    # A 1-million-cell budget bounds each temporary even for large source tables.
    row_chunk = max(1, 1_000_000 // batch)
    for _ in range(max_iter):
        score_a = np.zeros(batch)
        score_b = np.zeros(batch)
        h_aa = np.zeros(batch)
        h_ab = np.zeros(batch)
        h_bb = np.zeros(batch)
        for start in range(0, len(x), row_chunk):
            stop = min(start + row_chunk, len(x))
            xs = x[start:stop]
            weights = counts[:, codes[start:stop]]
            linear = intercept[:, None] + slope[:, None] * xs[None, :]
            probability = 1 / (1 + np.exp(-np.clip(linear, -35, 35)))
            residual = weights * (y[None, start:stop] - probability)
            variance = weights * probability * (1 - probability)
            score_a += np.sum(residual, axis=1)
            score_b += residual @ xs
            h_aa += np.sum(variance, axis=1)
            h_ab += variance @ xs
            h_bb += variance @ (xs * xs)
        determinant = h_aa * h_bb - h_ab * h_ab
        solvable = active & np.isfinite(determinant) & (determinant > 0)
        failed = active & ~solvable
        for i in np.flatnonzero(failed):
            reasons[int(i)] = "non_convergence"
        active &= solvable
        if not np.any(active):
            break
        step_a = np.zeros(batch)
        step_b = np.zeros(batch)
        step_a[active] = (
            score_a[active] * h_bb[active] - score_b[active] * h_ab[active]
        ) / determinant[active]
        step_b[active] = (
            score_b[active] * h_aa[active] - score_a[active] * h_ab[active]
        ) / determinant[active]
        finite = np.isfinite(step_a) & np.isfinite(step_b)
        failed = active & ~finite
        for i in np.flatnonzero(failed):
            reasons[int(i)] = "non_convergence"
        active &= finite
        intercept[active] += step_a[active]
        slope[active] += step_b[active]
        just_converged = active & (np.maximum(np.abs(step_a), np.abs(step_b)) <= tol)
        converged |= just_converged
        active &= ~just_converged
        if not np.any(active):
            break
    for i in np.flatnonzero(active):
        reasons[int(i)] = "non_convergence"
    # Output arrays for failed fits are ignored by public APIs; no finite-looking
    # partial coefficient is exposed as an estimate.
    return intercept, slope, reasons


def calibration_fit(
    p: Sequence[float],
    y: Sequence[int],
    *,
    eps: float = 1e-4,
    max_iter: int = 100,
    tol: float = 1e-9,
) -> CalibrationFit:
    _eps(eps)
    if not isinstance(max_iter, int) or isinstance(max_iter, bool) or max_iter <= 0:
        raise ValueError("max_iter must be a positive integer")
    if not isinstance(tol, (int, float)) or not math.isfinite(tol) or tol <= 0:
        raise ValueError("tol must be finite and positive")
    forecast, outcome = _data(p, y)
    clipped = int(np.count_nonzero(np.clip(forecast, eps, 1 - eps) != forecast))
    x = _logit(forecast, eps)
    intercepts, slopes, reasons = _irls_batch(
        x,
        outcome,
        np.zeros(len(x), dtype=np.int64),
        np.ones((1, 1), dtype=np.int64),
        max_iter=max_iter,
        tol=tol,
    )
    reason = reasons[0]
    if reason is not None:
        return CalibrationFit(None, None, False, reason, clipped)
    return CalibrationFit(float(intercepts[0]), float(slopes[0]), True, None, clipped)
