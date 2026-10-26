# Stage 13 RED: order-book imbalance prediction

This contract freezes the public signatures in `study/imbalance.py`. This phase
adds tests and deliberately failing interfaces only. GREEN must preserve these
tests, fixtures, and public signatures. All observations are synthetic; no study
finding or profitability claim follows from them.

## Quotes and features

`BookQuote` contains YES bid/ask prices in $0.0001 ticks and bid/ask quantities in
hundredths of a contract. Prices are integers in [0,10000], bid <= ask (locked
quotes are allowed). Quantities are integers in [0,2^63-1]. Reject negatives,
out-of-range values, floats and booleans with ValueError when consuming a quote.
The dataclasses are plain immutable records; validation belongs in the functions.

`imbalance(Qb,Qa)` returns (Qb-Qa)/(Qb+Qa), or None when both quantities are zero.
Perform the integer subtraction and addition before converting to float. A
one-sided zero quantity is allowed and gives +/-1. Intermediate arithmetic is
not constrained to int64 when its inputs fit int64.

`order_flow(previous,current)` returns one event's CKS flow contribution as an
exact integer in quantity hundredths:

    e = 1[B >= Bprev] Qb - 1[B <= Bprev] Qbprev
        - 1[A <= Aprev] Qa + 1[A >= Aprev] Qaprev

Equality activates both terms for that side. All nine bid/ask movement pairs
have handwritten expected numbers in the tests. Summing contributions would
produce interval OFI; this stage's predictive feature is the latest event's e.
Reference: Cont, Kukanov and Stoikov, *The Price Impact of Order Book Events*,
Section 2.1, [original paper](https://arxiv.org/pdf/1011.6402).

## Event-time samples

`BookEvent` pairs a nonempty market string and a nonnegative int64 Unix-nanosecond
timestamp with a quote. `quote=None` is an explicit unavailable/invalid-book
barrier. The caller must represent replay invalid/missing/crossed books with
that barrier instead of silently deleting them. Malformed non-None quotes raise
ValueError. A zero-total-depth quote is also a barrier because its imbalance is
undefined. No target or OFI transition crosses a barrier.

`build_samples` preserves input order. Timestamps must be nondecreasing within
each market; equal timestamps retain receipt order. Different markets may be
interleaved. Days are UTC dates computed from integer nanoseconds, independently
of the host timezone. Each market/day is a separate sequence.

At an eligible event i, retain its current imbalance and the OFI of the transition
from the immediately previous valid event in that segment. OFI is None for the
first event of a market/day or after a barrier; imbalance is still available.
The target is sign(mid[j]-mid[i]), where j is the first later valid event in the
same segment with a different mid. Compare bid+ask directly to retain half-tick
moves. Unchanged mids are skipped in the forward search; their observations are
not all discarded. Never use future quantities to construct current features.

Every terminal event is dropped, including an entire trailing equal-mid plateau
with no subsequent same-day change. No overnight, cross-market, or gap-spanning
labels are created. Empty and constant sequences return an empty tuple. Sample
targets are exactly -1 or +1; zero is not a direction class.

## Dates and fitted rule

`split_dates(..., train_through=date)` keeps all dates <= the boundary in train
and all later dates in test, preserving each partition's input order. Empty
partitions are permitted. `evaluate` also validates chronology when given
partitions directly: max(train dates) must be strictly less than min(test dates).
This check uses all supplied rows before filtering missing features, including
rows from different markets. It prevents a missing OFI value from hiding overlap.

The plan leaves the form of the predictive rule open. This stage specifies a
small, deterministic sign rule separately for the chosen feature, `imbalance`
or `ofi`. For each eligible training row, prediction is polarity*sign(feature).
Zero signals predict the majority direction among eligible training targets.
Fit chooses polarity +1 or -1 by greatest training hit count. A polarity tie
chooses +1; a training-majority tie chooses +1. No threshold tuning, test-label
selection, or test-majority fitting is involved. Evaluation never switches
feature based on test performance.

Imbalance must be finite in [-1,1], OFI an integer, and targets integer +/-1
(booleans are invalid). Missing OFI rows are omitted from training, the training
majority count, and test metrics. `predict` preserves positions, returning None
for a missing feature. `fit_rule` rejects empty eligible training data. Invalid
features, labels, rule fields, or feature names raise ValueError.

## Holdout metrics and cluster intervals

`evaluate` reports eligible n_train/n_test, the fitted rule, test hit rate, the
fixed 0.5 chance reference, and test hit rate of the constant training-majority
prediction. It reports the two arithmetic differences without clamping negative
values. These comparisons are descriptive; no significance or trading claim is
inferred by comparing an interval with an estimated baseline.

`hit_rate_interval` clusters by the tuple (market, UTC date), never by market
alone, date alone, or individual row. Resample K whole clusters with replacement
and compute total hits / total rows, retaining unequal cluster sizes. The frozen
reproducibility convention follows Stage 11: lexicographically sort tuple keys,
use NumPy default_rng(seed).multinomial(K,[1/K]*K,size=n_resamples), and linearly
interpolate percentile endpoints. Tests independently calculate replicate scores
from handwritten cluster counts and order-statistic interpolation.

Seed must be a nonnegative integer, n_resamples an integer >=2, and confidence
finite and strictly between 0 and 1. Validate options even with no data. Labels,
predictions and cluster keys must align; market strings must be nonempty and
dates actual `date` objects. Returned n_clusters counts distinct tuple keys.

With no evaluated observations, HitRateCI has status `no data`, zero counts,
null estimate/endpoints and no replicates. With only one cluster, retain its
point hit rate but return null endpoints and no replicates, with status
`insufficient clusters`. Multiple clusters with identical correctness may have
a legitimate degenerate interval. Seeded intervals must be invariant to input
row order and uniform within-cluster replication.

For `evaluate`, no eligible training observations gives `no training data`, no
rule, and null empirical metrics/interval. No eligible test observations gives
`no test data`, a rule if train exists, and null empirical metrics/interval.
The theoretical chance reference remains 0.5. One test cluster gives
`insufficient clusters` with point metrics and the null-endpoint interval.
Otherwise status is `result`, including zero or negative improvement. Shuffling
held-out labels or changing held-out features cannot change the fitted rule.
