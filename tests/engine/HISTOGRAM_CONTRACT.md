# Stage 9 histogram acceptance contract

`LatencyHistogram` records integer nanoseconds from 0 through UINT64_MAX in
fixed-memory log-linear buckets. Construction, recording, merging, clearing,
and querying are noexcept. Recording and querying do not allocate. Each replay
worker owns its histogram; merge only after workers have joined.

`record(value, occurrences=1)` adds weighted observations. Zero occurrences is a
successful no-op. `count()` counts occurrences, not calls. `merge(other)` adds
all bucket counts, supports empty histograms and self-merge, and leaves `other`
unchanged when it is a distinct object. If recording or merging would overflow
the uint64 total, return false without changing any bucket or the total. Otherwise
return true. `clear()` returns the histogram to its empty state for reuse.

`percentile(p)` accepts finite p in [0,100]. Empty histograms and invalid p
(negative, greater than 100, NaN, or either infinity) return nullopt. Queries do
not mutate the histogram. Use nearest rank: for p>0 select the smallest ranked
observation at rank ceil(p*count/100), with ranks starting at 1. p=0 selects the
minimum observation. Return that observation's bucket upper bound, capped at
UINT64_MAX. The result must be at least the exact selected observation and no
more than 1% above it. Zero must report exactly zero. Percentiles must be
nondecreasing. No interpolated values or rounding down to a bucket lower bound.

For integer test comparisons, the error bound is:
`estimate >= exact && estimate - exact <= exact / 100`.
The subtraction is evaluated only after checking estimate >= exact, avoiding
unsigned wraparound. This also requires exact answers for observations below
100, where integer 1% error is zero.

Independent hand oracles:

- Uniform 1..1000: nearest-rank p1=10, p50=500, p90=900, p99=990, p100=1000;
  p0=1. Thus p50 must lie in [500,505], including a possible upper-bucket bias.
- Weighted distribution 1x1, 10x49, 100x49, 1000x1 has 100 observations. Ranks
  1, 2..50, 51..99, 100 select 1, 10, 100, 1000 respectively.
- A seeded skewed distribution is sorted independently; integer rank arithmetic
  supplies exact order statistics before comparing the histogram approximation.
- Recording UINT64_MAX-1 copies of 10 then one copy of 20 fills the count exactly.
  Another occurrence, nonempty merge, or self-merge must fail atomically.

Merging per-thread histograms must give exactly the same count and percentile
answers as one histogram fed the concatenated samples, independent of merge
order or partition. This is exact histogram equality at the public query level,
not an extra tolerance on the merged result.

Stage 9 GREEN also adds synthetic generation and benchmark commands specified by
the build plan. Timing measurements must report sampling interval and clock
overhead; no machine-specific speed threshold is asserted by these tests.
