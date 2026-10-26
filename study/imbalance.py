"""Event-time order-book signals and a chronological out-of-sample diagnostic."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import numpy as np

Feature = Literal["imbalance", "ofi"]
IntervalStatus = Literal["result", "no data", "insufficient clusters"]
StudyStatus = Literal["result", "no training data", "no test data", "insufficient clusters"]
_DAY_NS = 86_400_000_000_000
_EPOCH = date(1970, 1, 1)
_MAX_INT64 = (1 << 63) - 1


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


@dataclass(frozen=True)
class _Pending:
    index: int
    event: BookEvent
    day: date
    value: float
    ofi: int | None


def _quantity(value: int) -> None:
    if type(value) is not int or not 0 <= value <= _MAX_INT64:
        raise ValueError("book quantity must be a nonnegative signed-int64 integer")


def _quote(quote: BookQuote) -> None:
    if not isinstance(quote, BookQuote):
        raise ValueError("quote must be a BookQuote")
    if (
        type(quote.bid_ticks) is not int
        or type(quote.ask_ticks) is not int
        or not 0 <= quote.bid_ticks <= quote.ask_ticks <= 10_000
    ):
        raise ValueError("quote prices must be ordered integer YES ticks")
    _quantity(quote.bid_qty)
    _quantity(quote.ask_qty)


def _event(event: BookEvent) -> date:
    if not isinstance(event, BookEvent) or not isinstance(event.market, str) or not event.market:
        raise ValueError("book event needs a nonempty market")
    if type(event.timestamp_ns) is not int or not 0 <= event.timestamp_ns <= _MAX_INT64:
        raise ValueError("book event timestamp must be nonnegative int64 nanoseconds")
    if event.quote is not None:
        _quote(event.quote)
    return _EPOCH + timedelta(days=event.timestamp_ns // _DAY_NS)


def imbalance(bid_qty: int, ask_qty: int) -> float | None:
    """YES best-depth imbalance, preserving integer precision until the division."""
    _quantity(bid_qty)
    _quantity(ask_qty)
    total = bid_qty + ask_qty
    return (bid_qty - ask_qty) / total if total else None


def order_flow(previous: BookQuote, current: BookQuote) -> int:
    """One Cont-Kukanov-Stoikov best-quote flow contribution in quantity units."""
    _quote(previous)
    _quote(current)
    bid = (current.bid_qty if current.bid_ticks >= previous.bid_ticks else 0) - (
        previous.bid_qty if current.bid_ticks <= previous.bid_ticks else 0
    )
    ask = (previous.ask_qty if current.ask_ticks >= previous.ask_ticks else 0) - (
        current.ask_qty if current.ask_ticks <= previous.ask_ticks else 0
    )
    return bid + ask


def build_samples(events: Sequence[BookEvent]) -> tuple[Sample, ...]:
    """Label each usable event with its next changed mid inside the same segment."""
    last_timestamp: dict[str, int] = {}
    active: dict[str, tuple[date, list[_Pending]]] = {}
    segments: list[list[_Pending]] = []
    for index, event in enumerate(events):
        day = _event(event)
        prior_time = last_timestamp.get(event.market)
        if prior_time is not None and event.timestamp_ns < prior_time:
            raise ValueError("event timestamps must not decrease within a market")
        last_timestamp[event.market] = event.timestamp_ns

        quote = event.quote
        if quote is None:
            active.pop(event.market, None)
            continue
        value = imbalance(quote.bid_qty, quote.ask_qty)
        if value is None:
            active.pop(event.market, None)
            continue
        prior = active.get(event.market)
        if prior is None or prior[0] != day:
            segment: list[_Pending] = []
            segments.append(segment)
            active[event.market] = (day, segment)
            flow = None
        else:
            segment = prior[1]
            prior_quote = segment[-1].event.quote
            assert prior_quote is not None
            flow = order_flow(prior_quote, quote)
        segment.append(_Pending(index, event, day, value, flow))

    labeled: list[tuple[int, Sample]] = []
    for segment in segments:
        mids = []
        for pending in segment:
            quote = pending.event.quote
            assert quote is not None
            mids.append(quote.bid_ticks + quote.ask_ticks)
        start = 0
        while start < len(segment):
            end = start + 1
            while end < len(segment) and mids[end] == mids[start]:
                end += 1
            if end < len(segment):
                target = 1 if mids[end] > mids[start] else -1
                for pending in segment[start:end]:
                    labeled.append(
                        (
                            pending.index,
                            Sample(
                                pending.event.market,
                                pending.event.timestamp_ns,
                                pending.day,
                                pending.value,
                                pending.ofi,
                                target,
                            ),
                        )
                    )
            start = end
    return tuple(sample for _, sample in sorted(labeled, key=lambda pair: pair[0]))


def _feature(feature: Feature) -> None:
    if feature not in ("imbalance", "ofi"):
        raise ValueError("feature must be imbalance or ofi")


def _direction(value: int) -> None:
    if type(value) is not int or value not in (-1, 1):
        raise ValueError("direction must be -1 or +1")


def _signal(feature: Feature, value: float | int | None) -> None:
    if value is None:
        return
    if feature == "ofi":
        if type(value) is not int:
            raise ValueError("OFI must be an integer")
    elif (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not -1 <= value <= 1
    ):
        raise ValueError("imbalance must be a finite value in [-1, 1]")


def _sample(row: Sample) -> None:
    if not isinstance(row, Sample) or not isinstance(row.market, str) or not row.market:
        raise ValueError("sample needs a nonempty market")
    if type(row.timestamp_ns) is not int or not 0 <= row.timestamp_ns <= _MAX_INT64:
        raise ValueError("sample timestamp must be nonnegative int64 nanoseconds")
    if type(row.day) is not date or row.day != _EPOCH + timedelta(days=row.timestamp_ns // _DAY_NS):
        raise ValueError("sample day must match the UTC timestamp")
    _signal("imbalance", row.imbalance)
    if row.imbalance is None:
        raise ValueError("sample imbalance cannot be missing")
    _signal("ofi", row.ofi)
    _direction(row.target)


def split_dates(samples: Sequence[Sample], *, train_through: date) -> DateSplit:
    if type(train_through) is not date:
        raise ValueError("training boundary must be a date")
    train: list[Sample] = []
    test: list[Sample] = []
    for row in samples:
        _sample(row)
        (train if row.day <= train_through else test).append(row)
    return DateSplit(tuple(train), tuple(test))


def fit_rule(samples: Sequence[Sample], *, feature: Feature = "imbalance") -> DirectionRule:
    _feature(feature)
    eligible: list[Sample] = []
    for row in samples:
        _sample(row)
        if getattr(row, feature) is not None:
            eligible.append(row)
    if not eligible:
        raise ValueError("no eligible training observations")
    ups = sum(row.target == 1 for row in eligible)
    majority = 1 if 2 * ups >= len(eligible) else -1
    positive_hits = 0
    negative_hits = 0
    for row in eligible:
        value = getattr(row, feature)
        assert value is not None
        positive = (1 if value > 0 else -1) if value else majority
        negative = -positive if value else majority
        positive_hits += positive == row.target
        negative_hits += negative == row.target
    return DirectionRule(feature, 1 if positive_hits >= negative_hits else -1, majority)


def predict(rule: DirectionRule, values: Sequence[float | int | None]) -> tuple[int | None, ...]:
    if not isinstance(rule, DirectionRule):
        raise ValueError("rule must be a DirectionRule")
    _feature(rule.feature)
    _direction(rule.polarity)
    _direction(rule.majority)
    output: list[int | None] = []
    for value in values:
        _signal(rule.feature, value)
        output.append(
            None
            if value is None
            else (rule.polarity if value > 0 else -rule.polarity if value < 0 else rule.majority)
        )
    return tuple(output)


def _interval_options(n_resamples: int, seed: int, confidence: float) -> None:
    if type(n_resamples) is not int or n_resamples < 2:
        raise ValueError("n_resamples must be an integer of at least two")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 < confidence < 1
    ):
        raise ValueError("confidence must be finite and between zero and one")


def hit_rate_interval(
    predictions: Sequence[int],
    targets: Sequence[int],
    market_days: Sequence[tuple[str, date]],
    *,
    n_resamples: int = 2000,
    seed: int = 0,
    confidence: float = 0.95,
) -> HitRateCI:
    _interval_options(n_resamples, seed, confidence)
    if len(predictions) != len(targets) or len(predictions) != len(market_days):
        raise ValueError("predictions, targets, and cluster keys must align")
    groups: dict[tuple[str, date], tuple[int, int]] = {}
    for prediction, target, key in zip(predictions, targets, market_days, strict=True):
        _direction(prediction)
        _direction(target)
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or not isinstance(key[0], str)
            or not key[0]
            or type(key[1]) is not date
        ):
            raise ValueError("cluster key must be (nonempty market, UTC date)")
        n, hits = groups.get(key, (0, 0))
        groups[key] = (n + 1, hits + (prediction == target))
    if not predictions:
        return HitRateCI(0, 0, None, None, None, (), "no data")
    total_hits = sum(hits for _, hits in groups.values())
    estimate = total_hits / len(predictions)
    count = len(groups)
    if count == 1:
        return HitRateCI(len(predictions), 1, estimate, None, None, (), "insufficient clusters")

    keys = sorted(groups)
    sizes = np.asarray([groups[key][0] for key in keys], dtype=np.int64)
    cluster_hits = np.asarray([groups[key][1] for key in keys], dtype=np.int64)
    probabilities = np.full(count, 1 / count)
    rng = np.random.default_rng(seed)
    replicates: list[float] = []
    batch_size = min(256, max(1, 4_000_000 // count))
    for offset in range(0, n_resamples, batch_size):
        draws = rng.multinomial(count, probabilities, size=min(batch_size, n_resamples - offset))
        replicates.extend(float(value) for value in (draws @ cluster_hits) / (draws @ sizes))
    alpha = (1 - confidence) / 2
    lower, upper = np.quantile(replicates, [alpha, 1 - alpha], method="linear")
    return HitRateCI(
        len(predictions), count, estimate, float(lower), float(upper), tuple(replicates), "result"
    )


def evaluate(
    train: Sequence[Sample],
    test: Sequence[Sample],
    *,
    feature: Feature = "imbalance",
    n_resamples: int = 2000,
    seed: int = 0,
    confidence: float = 0.95,
) -> ImbalanceStudy:
    _feature(feature)
    _interval_options(n_resamples, seed, confidence)
    for row in (*train, *test):
        _sample(row)
    if train and test and max(row.day for row in train) >= min(row.day for row in test):
        raise ValueError("training dates must strictly precede all test dates")
    train_eligible = [row for row in train if getattr(row, feature) is not None]
    test_eligible = [row for row in test if getattr(row, feature) is not None]
    n_train, n_test = len(train_eligible), len(test_eligible)
    if not train_eligible:
        return ImbalanceStudy(
            None, 0, n_test, None, 0.5, None, None, None, None, "no training data"
        )
    rule = fit_rule(train_eligible, feature=feature)
    if not test_eligible:
        return ImbalanceStudy(rule, n_train, 0, None, 0.5, None, None, None, None, "no test data")
    predicted = predict(rule, [getattr(row, feature) for row in test_eligible])
    assert all(value is not None for value in predicted)
    forecasts = [int(value) for value in predicted if value is not None]
    outcomes = [row.target for row in test_eligible]
    hit_rate = sum(a == b for a, b in zip(forecasts, outcomes, strict=True)) / n_test
    majority_hit_rate = sum(target == rule.majority for target in outcomes) / n_test
    interval = hit_rate_interval(
        forecasts,
        outcomes,
        [(row.market, row.day) for row in test_eligible],
        n_resamples=n_resamples,
        seed=seed,
        confidence=confidence,
    )
    status: StudyStatus = "result" if interval.status == "result" else "insufficient clusters"
    return ImbalanceStudy(
        rule,
        n_train,
        n_test,
        hit_rate,
        0.5,
        majority_hit_rate,
        hit_rate - 0.5,
        hit_rate - majority_hit_rate,
        interval,
        status,
    )
