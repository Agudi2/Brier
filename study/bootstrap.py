"""Event-cluster bootstrap interfaces; deliberate RED stubs."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from study.metrics import BinStrategy, ReliabilityBin

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
    raise NotImplementedError


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
    raise NotImplementedError
