"""Order-book prediction interfaces; deliberate Stage 13 RED stubs."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

Feature = Literal["imbalance", "ofi"]
IntervalStatus = Literal["result", "no data", "insufficient clusters"]
StudyStatus = Literal["result", "no training data", "no test data", "insufficient clusters"]


@dataclass(frozen=True)
class BookQuote:
    bid_ticks: int
    ask_ticks: int
    bid_qty: int
    ask_qty: int


@dataclass(frozen=True)
class BookEvent:
    market: str
    timestamp_ns: int
    quote: BookQuote | None


@dataclass(frozen=True)
class Sample:
    market: str
    timestamp_ns: int
    day: date
    imbalance: float
    ofi: int | None
    target: int


@dataclass(frozen=True)
class DateSplit:
    train: tuple[Sample, ...]
    test: tuple[Sample, ...]


@dataclass(frozen=True)
class DirectionRule:
    feature: Feature
    polarity: int
    majority: int


@dataclass(frozen=True)
class HitRateCI:
    n: int
    n_clusters: int
    estimate: float | None
    lower: float | None
    upper: float | None
    replicates: tuple[float, ...]
    status: IntervalStatus


@dataclass(frozen=True)
class ImbalanceStudy:
    rule: DirectionRule | None
    n_train: int
    n_test: int
    hit_rate: float | None
    chance_rate: float
    majority_hit_rate: float | None
    excess_vs_chance: float | None
    excess_vs_majority: float | None
    interval: HitRateCI | None
    status: StudyStatus


def imbalance(bid_qty: int, ask_qty: int) -> float | None:
    raise NotImplementedError


def order_flow(previous: BookQuote, current: BookQuote) -> int:
    raise NotImplementedError


def build_samples(events: Sequence[BookEvent]) -> tuple[Sample, ...]:
    raise NotImplementedError


def split_dates(samples: Sequence[Sample], *, train_through: date) -> DateSplit:
    raise NotImplementedError


def fit_rule(samples: Sequence[Sample], *, feature: Feature = "imbalance") -> DirectionRule:
    raise NotImplementedError


def predict(rule: DirectionRule, values: Sequence[float | int | None]) -> tuple[int | None, ...]:
    raise NotImplementedError


def hit_rate_interval(
    predictions: Sequence[int],
    targets: Sequence[int],
    market_days: Sequence[tuple[str, date]],
    *,
    n_resamples: int = 2000,
    seed: int = 0,
    confidence: float = 0.95,
) -> HitRateCI:
    raise NotImplementedError


def evaluate(
    train: Sequence[Sample],
    test: Sequence[Sample],
    *,
    feature: Feature = "imbalance",
    n_resamples: int = 2000,
    seed: int = 0,
    confidence: float = 0.95,
) -> ImbalanceStudy:
    raise NotImplementedError
