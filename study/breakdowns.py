"""Aggregate calibration breakdowns with event-level inference and Holm correction."""

import hashlib
import math
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import product
from typing import Literal, cast

import numpy as np
import polars as pl

from study.bootstrap import cluster_bootstrap
from study.metrics import brier_score

BreakdownStatus = Literal["result", "insufficient n", "insufficient events", "missing data"]
_UNITS = {"s": 1_000_000_000, "m": 60_000_000_000, "h": 3_600_000_000_000, "d": 86_400_000_000_000}
_DIMENSIONS = ("category", "horizon_ns", "spread_bucket", "liquidity_bucket")
_MONTE_CARLO_DRAWS = 9_999  # Add-one estimate has minimum p-value 1/10,000.


@dataclass(frozen=True)
class Breakdown:
    dimension: str
    group: str
    n: int
    brier: float | None
    gap: float | None
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    p_adjusted: float | None
    status: BreakdownStatus
    breaks_down: bool


def parse_horizon(value: str) -> int:
    """Parse an ASCII decimal duration into positive int64 nanoseconds."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+[smhd]", value) is None:
        raise ValueError("horizon must be a positive integer followed by s, m, h, or d")
    nanoseconds = int(value[:-1]) * _UNITS[value[-1]]
    if not 0 < nanoseconds <= (1 << 63) - 1:
        raise ValueError("horizon is zero or exceeds int64 nanoseconds")
    return nanoseconds


def spread_bucket(spread_ticks: int | None) -> str:
    if spread_ticks is None:
        return "missing"
    if not isinstance(spread_ticks, int) or isinstance(spread_ticks, bool) or spread_ticks < 0:
        raise ValueError("spread ticks must be a nonnegative integer")
    if spread_ticks == 0:
        return "locked"
    if spread_ticks <= 100:
        return "1-100"
    return "101-500" if spread_ticks <= 500 else ">500"


def liquidity_bucket(quantity: int | None) -> str:
    if quantity is None:
        return "missing"
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
        raise ValueError("liquidity quantity must be a nonnegative integer")
    if quantity == 0:
        return "zero"
    return "1-9999" if quantity < 10_000 else ">=10000"


def holm_adjust(p_values: Sequence[float]) -> tuple[float, ...]:
    """Holm step-down adjusted p-values in the caller's original order."""
    if any(
        not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1 for p in p_values
    ):
        raise ValueError("p-values must be finite probabilities")
    adjusted = [0.0] * len(p_values)
    running = 0.0
    for rank, index in enumerate(sorted(range(len(p_values)), key=lambda i: p_values[i])):
        running = max(running, (len(p_values) - rank) * p_values[index])
        adjusted[index] = min(1.0, running)
    return tuple(adjusted)


def _sign_flip_p(residuals: Sequence[float], event_ids: Sequence[str], seed: int) -> float:
    event_sums: defaultdict[str, float] = defaultdict(float)
    for residual, event_id in zip(residuals, event_ids, strict=True):
        event_sums[event_id] += residual
    sums = np.asarray([event_sums[key] for key in sorted(event_sums)], dtype=np.float64)
    observed = abs(float(np.sum(sums)))
    tolerance = 1e-12 * max(1.0, float(np.sum(np.abs(sums))))
    if len(sums) <= 16:
        extreme = sum(
            abs(float(np.dot(signs, sums))) + tolerance >= observed
            for signs in product((-1, 1), repeat=len(sums))
        )
        return extreme / (1 << len(sums))
    rng = np.random.default_rng(seed)
    extreme = 0
    for offset in range(0, _MONTE_CARLO_DRAWS, 256):
        count = min(256, _MONTE_CARLO_DRAWS - offset)
        signs = rng.integers(0, 2, size=(count, len(sums)), dtype=np.int8) * 2 - 1
        extreme += int(np.count_nonzero(np.abs(signs @ sums) + tolerance >= observed))
    return (extreme + 1) / (_MONTE_CARLO_DRAWS + 1)


def _group_seed(seed: int, dimension: str, group: str) -> int:
    digest = hashlib.sha256(f"{seed}\\0{dimension}\\0{group}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def breakdowns(
    rows: pl.DataFrame,
    *,
    dimensions: Sequence[str] = ("category", "horizon_ns", "spread_bucket", "liquidity_bucket"),
    min_n: int = 30,
    alpha: float = 0.05,
    n_resamples: int = 2000,
    seed: int = 0,
) -> tuple[Breakdown, ...]:
    if not isinstance(min_n, int) or isinstance(min_n, bool) or min_n <= 0:
        raise ValueError("min_n must be positive")
    if not isinstance(alpha, (int, float)) or not math.isfinite(alpha) or not 0 < alpha < 1:
        raise ValueError("alpha must be strictly between zero and one")
    if any(dimension not in _DIMENSIONS for dimension in dimensions):
        raise ValueError("unknown breakdown dimension")
    if not isinstance(n_resamples, int) or isinstance(n_resamples, bool) or n_resamples < 2:
        raise ValueError("n_resamples must be at least two")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be nonnegative")
    required = {"p", "y", "event_ticker"}
    for dimension in dimensions:
        if dimension in ("category", "horizon_ns"):
            required.add(dimension)
    if not required.issubset(rows.columns):
        raise ValueError("breakdown rows lack required columns")

    results: list[Breakdown] = []
    for dimension in dimensions:
        groups: defaultdict[str, list[int]] = defaultdict(list)
        if dimension == "spread_bucket":
            values = (
                rows["spread_ticks"].to_list() if "spread_ticks" in rows else [None] * len(rows)
            )
            keys = [spread_bucket(value) for value in values]
        elif dimension == "liquidity_bucket":
            values = (
                rows["liquidity_qty"].to_list() if "liquidity_qty" in rows else [None] * len(rows)
            )
            keys = [liquidity_bucket(value) for value in values]
        else:
            keys = [str(value) for value in rows[dimension].to_list()]
        for index, group in enumerate(keys):
            groups[group].append(index)
        for group in sorted(groups):
            indices = groups[group]
            n = len(indices)
            events = [rows["event_ticker"][i] for i in indices]
            status: BreakdownStatus
            if dimension in ("spread_bucket", "liquidity_bucket") and group == "missing":
                status = "missing data"
            elif n < min_n:
                status = "insufficient n"
            elif len(set(events)) < 2:
                status = "insufficient events"
            else:
                status = "result"
            if status != "result":
                results.append(
                    Breakdown(
                        dimension, group, n, None, None, None, None, None, None, status, False
                    )
                )
                continue
            p = [rows["p"][i] for i in indices]
            y = [rows["y"][i] for i in indices]
            group_seed = _group_seed(seed, dimension, group)
            ci = cluster_bootstrap(
                p, y, events, statistic="gap", n_resamples=n_resamples, seed=group_seed
            )
            residuals = [outcome - forecast for forecast, outcome in zip(p, y, strict=True)]
            results.append(
                Breakdown(
                    dimension,
                    group,
                    n,
                    brier_score(p, y),
                    float(np.mean(residuals)),
                    ci.lower,
                    ci.upper,
                    _sign_flip_p(residuals, events, group_seed),
                    None,
                    "result",
                    False,
                )
            )
    eligible = [i for i, item in enumerate(results) if item.status == "result"]
    corrected = holm_adjust([cast(float, results[i].p_value) for i in eligible])
    for index, p_adjusted in zip(eligible, corrected, strict=True):
        results[index] = replace(
            results[index], p_adjusted=p_adjusted, breaks_down=p_adjusted <= alpha
        )
    return tuple(results)
