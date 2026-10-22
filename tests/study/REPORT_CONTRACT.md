# Stage 12 breakdown and report contract

This RED phase freezes `study.breakdowns`, `study.report`, and
`study.calibration` signatures. They contain deliberate NotImplementedError
stubs so imports succeed and tests fail on assertions. GREEN must preserve all
files under `tests/`, `engine/tests/`, and `fixtures/`.

## Horizons and integer book buckets

`parse_horizon` accepts a positive base-10 integer plus one lowercase unit:
`s`, `m`, `h`, or `d`; returns Unix nanoseconds as an int64-safe duration.
Whitespace, composite or fractional durations, unknown units, zero, negatives,
and overflow raise ValueError. The values 24h, 90m, and 2d are checked by
independent multiplication.

`spread_bucket` uses YES spread in integer $0.0001 ticks:
`None -> missing`, `0 -> locked`, `1..100 -> 1-100`, `101..500 -> 101-500`,
`501+ -> >500`. A negative spread raises. `liquidity_bucket` receives total
best-side visible quantity in hundredths of a contract:
`None -> missing`, `0 -> zero`, `1..9999 -> 1-9999`, `10000+ -> >=10000`.
A negative quantity raises. The optional source columns are `spread_ticks` and
`liquidity_qty`. A dataset from Stage 10 has neither; its missing-data groups
must not masquerade as spread or liquidity findings.

## Breakdown inference

Supported dimensions are category, horizon_ns, spread_bucket, and
liquidity_bucket. Each returned group reports row n. Groups with n below min_n
have status `insufficient n`, and null scores, gaps, intervals, and p-values.
Groups with fewer than two distinct event_ticker values have status
`insufficient events`, even if row n is large. Missing spread or liquidity
information has status `missing data`, with null results. All these statuses
have breaks_down=False. Invalid options and unknown dimensions raise ValueError.

For an eligible group, Brier is mean((p-y)^2), gap is mean(y-p), and the gap's
percentile interval resamples whole events via Stage 11 `cluster_bootstrap`.
The group p-value is a two-sided event-level sign-flip test of the sum of each
event's row residuals (y-p). For up to 16 events, enumerate all 2^G sign vectors
exactly, counting absolute signed sums at least as large as the observed sum.
The six positive-event fixture therefore has p=2/64=.03125. For larger G,
use a deterministic seeded Monte Carlo sign-flip approximation with a stated
nonzero minimum p-value. Treat p-values as descriptive calibration diagnostics;
the sign-flip test relies on event-level sign symmetry.

Holm's step-down correction uses all eligible groups returned by ONE breakdowns
call as a single comparison family, across requested dimensions. Insufficient
and missing-data groups do not count toward the family size. Adjusted values
retain original result order, are in [raw p,1], and are monotone in ascending
raw p. A group may have breaks_down=True only if its adjusted p-value is <=
alpha, default .05. Applying an uncorrected .03125 threshold to a two-group
family is wrong: its adjusted .0625 is not significant. The all-dimensions test
computes the full-family Holm oracle independently; a separate unit fixture
covers ties and the step-down cumulative maximum.

## Aggregate report and command

`write_report(rows, exclusions, output, *, horizons_ns, min_n, n_resamples,
seed)` writes `summary.json`, `summary.md`, and one
`reliability_<h>.png` for each requested horizon. `summary.json` returns from
the function as a Python dictionary. Horizons with no rows have n=0, null
scores and a present empty chart; groups below min_n have status
`insufficient n` and null scores. Re-running with fewer horizons removes
stale charts owned by the report. Reject duplicate/negative horizons and
nonpositive min_n. Graphs are valid PNGs with readable dimensions.

`study.calibration.main` reads the partitioned Parquet directory created by
Stage 10 and accepts `--input`, `--output`, `--horizons`, `--min-n`, `--bootstrap`,
and `--seed`. Its default horizons are 24h, 6h, and 1h, matching the repository
run command. A valid invocation returns 0 and writes the three artifact types.
All work here is local and offline; no live market data or API access is needed.

The report publishes only aggregate metrics, bin counts, confidence intervals,
exclusion counts, and counts of markets/events. It must not serialize source
rows, prices with outcome per market, tickers, event tickers, trade IDs, or
source timestamps. The tests recursively inspect JSON keys and search JSON and
Markdown for distinct sensitive source identifiers. The source fixture has
four rows with Brier (0.09+0.04+0.04+0.09)/4=.065 and two events. Existing
Stage 10 `write_dataset` creates the command fixture on disk.

Honest status and multiplicity correction matter more than an attractive plot:
empty or insufficient data appear plainly as such, and no profitability or
market-level claim is produced by this stage.
