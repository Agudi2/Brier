"""Calibration metric interfaces; deliberate RED stubs."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

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


def brier_score(p: Sequence[float], y: Sequence[int]) -> float:
    raise NotImplementedError


def log_loss(p: Sequence[float], y: Sequence[int], *, eps: float = 1e-4) -> LogLoss:
    raise NotImplementedError


def reliability_bins(
    p: Sequence[float],
    y: Sequence[int],
    *,
    n_bins: int = 10,
    strategy: BinStrategy = "equal_width",
) -> tuple[ReliabilityBin, ...]:
    raise NotImplementedError


def murphy_decomposition(
    p: Sequence[float],
    y: Sequence[int],
    *,
    n_bins: int = 10,
    strategy: BinStrategy = "equal_width",
) -> MurphyDecomposition:
    raise NotImplementedError


def calibration_fit(
    p: Sequence[float],
    y: Sequence[int],
    *,
    eps: float = 1e-4,
    max_iter: int = 100,
    tol: float = 1e-9,
) -> CalibrationFit:
    raise NotImplementedError
