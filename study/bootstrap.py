"""Seeded event-cluster percentile intervals from bounded multinomial batches."""

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from study.metrics import (
    BinStrategy,
    ReliabilityBin,
    _bins,
    _data,
    _irls_batch,
    _logit,
)

Statistic = Literal["brier", "log_loss", "gap", "intercept", "slope"]


@dataclass(frozen=True)
class BootstrapCI:
    estimate: float | None
    lower: float | None
    upper: float | None
    replicates: tuple[float | None, ...]
    n_events: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class FavoriteLongshotBin:
    bin: ReliabilityBin
    gap: BootstrapCI


def _options(n_resamples: int, seed: int, confidence: float, batch_size: int) -> None:
    if not isinstance(n_resamples, int) or isinstance(n_resamples, bool) or n_resamples < 2:
        raise ValueError("n_resamples must be an integer of at least two")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if (
        not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 < confidence < 1
    ):
        raise ValueError("confidence must be finite and strictly between zero and one")
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer")


def _events(event_ids: Sequence[str], n_rows: int) -> tuple[NDArray[np.int64], int]:
    if len(event_ids) != n_rows or any(not isinstance(e, str) or not e for e in event_ids):
        raise ValueError("event IDs must be aligned nonempty strings")
    _, inverse = np.unique(np.asarray(event_ids, dtype=str), return_inverse=True)
    return inverse.astype(np.int64, copy=False), int(np.max(inverse) + 1)


def _draws(
    groups: int, n_resamples: int, seed: int, batch_size: int
) -> Iterator[NDArray[np.int64]]:
    rng = np.random.default_rng(seed)
    probabilities = np.full(groups, 1 / groups)
    # At most four million event weights live in one batch, even for many events.
    limit = min(batch_size, max(1, 4_000_000 // groups))
    for offset in range(0, n_resamples, limit):
        yield rng.multinomial(groups, probabilities, size=min(limit, n_resamples - offset))


def _interval(
    estimate: float | None,
    replicates: Sequence[float | None],
    groups: int,
    confidence: float,
    *,
    empty: bool = False,
) -> BootstrapCI:
    warnings: list[str] = []
    if groups == 1:
        warnings.append("single_event")
    if empty:
        warnings.append("empty_bin")
    elif estimate is None:
        warnings.append("undefined_estimate")
    if any(value is None for value in replicates):
        if not empty:
            warnings.append("invalid_replicates")
        return BootstrapCI(estimate, None, None, tuple(replicates), groups, tuple(warnings))
    if estimate is None:
        return BootstrapCI(None, None, None, tuple(replicates), groups, tuple(warnings))
    values = np.asarray(replicates, dtype=np.float64)
    alpha = (1 - confidence) / 2
    lower, upper = np.quantile(values, [alpha, 1 - alpha], method="linear")
    return BootstrapCI(
        estimate, float(lower), float(upper), tuple(replicates), groups, tuple(warnings)
    )


def cluster_bootstrap(
    p: Sequence[float],
    y: Sequence[int],
    event_ids: Sequence[str],
    *,
    statistic: Statistic = "brier",
    n_resamples: int = 2000,
    seed: int = 0,
    confidence: float = 0.95,
    batch_size: int = 256,
) -> BootstrapCI:
    _options(n_resamples, seed, confidence, batch_size)
    forecast, outcome = _data(p, y)
    codes, groups = _events(event_ids, len(forecast))
    if statistic not in ("brier", "log_loss", "gap", "intercept", "slope"):
        raise ValueError("unknown bootstrap statistic")
    results: list[float | None] = []
    estimate: float | None
    if statistic in ("intercept", "slope"):
        x = _logit(forecast, 1e-4)
        original = np.ones((1, groups), dtype=np.int64)
        a, b, reason = _irls_batch(x, outcome, codes, original)
        estimate = (
            None if reason[0] is not None else float(a[0] if statistic == "intercept" else b[0])
        )
        for counts in _draws(groups, n_resamples, seed, batch_size):
            intercepts, slopes, reasons = _irls_batch(x, outcome, codes, counts)
            selected = intercepts if statistic == "intercept" else slopes
            results.extend(
                None if status is not None else float(selected[i])
                for i, status in enumerate(reasons)
            )
    else:
        if statistic == "brier":
            row_values = np.square(forecast - outcome)
        elif statistic == "gap":
            row_values = outcome - forecast
        else:
            clipped = np.clip(forecast, 1e-4, 1 - 1e-4)
            row_values = -(outcome * np.log(clipped) + (1 - outcome) * np.log1p(-clipped))
        estimate = float(np.mean(row_values))
        group_n = np.bincount(codes, minlength=groups)
        group_sum = np.bincount(codes, weights=row_values, minlength=groups)
        for counts in _draws(groups, n_resamples, seed, batch_size):
            values = (counts @ group_sum) / (counts @ group_n)
            results.extend(float(value) for value in values)
    return _interval(estimate, results, groups, confidence)


def favorite_longshot(
    p: Sequence[float],
    y: Sequence[int],
    event_ids: Sequence[str],
    *,
    n_bins: int = 10,
    strategy: BinStrategy = "equal_width",
    n_resamples: int = 2000,
    seed: int = 0,
    confidence: float = 0.95,
    batch_size: int = 256,
) -> tuple[FavoriteLongshotBin, ...]:
    _options(n_resamples, seed, confidence, batch_size)
    forecast, outcome = _data(p, y)
    codes, groups = _events(event_ids, len(forecast))
    bins, indices = _bins(forecast, outcome, n_bins, strategy)
    width = len(bins)
    cell = codes * width + indices
    group_bin_n = np.bincount(cell, minlength=groups * width).reshape(groups, width)
    group_bin_gap = np.bincount(cell, weights=outcome - forecast, minlength=groups * width).reshape(
        groups, width
    )
    all_replicates: list[list[float | None]] = [[] for _ in bins]
    for counts in _draws(groups, n_resamples, seed, batch_size):
        ns = counts @ group_bin_n
        sums = counts @ group_bin_gap
        for index in range(width):
            all_replicates[index].extend(
                float(total / n) if n else None
                for total, n in zip(sums[:, index], ns[:, index], strict=True)
            )
    output = []
    for index, item in enumerate(bins):
        estimate = float(np.sum(group_bin_gap[:, index]) / item.n) if item.n else None
        ci = _interval(estimate, all_replicates[index], groups, confidence, empty=item.n == 0)
        output.append(FavoriteLongshotBin(item, ci))
    return tuple(output)
