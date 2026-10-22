# Stage 10 dataset contract

These tests freeze the dataset and backfill interfaces for GREEN. Only deliberate
NotImplementedError stubs and data containers are present in production modules.
The helpers convert those exceptions into explicit pytest assertion failures.

## Time and price selection

All normalized times are integer Unix nanoseconds. Horizons are nonnegative and
lookbacks strictly positive. Scheduled close is supplied explicitly from metadata
known before settlement; actual close and current expiration fields never serve
as implicit substitutes. Missing anchors are counted as missing_scheduled_close.
This conservative rule can exclude markets whose original schedule was not saved.

Trade selection uses the last timestamp in (scheduled_close - horizon - lookback,
scheduled_close - horizon]. Input need not be sorted. Equal timestamps use the
lexicographically greatest trade ID, solely as a reproducible tie-break; it does
not imply exchange execution ordering. Identical duplicate IDs are coalesced;
conflicting duplicates and out-of-range prices raise ValueError. Integer prices
are YES ticks of $0.0001, including 0 and 10000. Future-data properties use valid
inputs; malformed data is rejected rather than silently ignored.

Book selection uses the latest quote within the same window, with input order as
the tie-break for identical timestamps (the binary stream preserves engine order).
Check validity before sides, crossing, and spread. No fallback to an older valid
quote after an invalidation, missing side, or wide spread. Locked books are allowed.
A midpoint can be a half tick. max_spread_ticks is nonnegative and inclusive.
The engine's 40-byte little-endian records and JSON ticker array are the adapter
inputs. Unknown IDs and incomplete records raise ValueError. Receive timestamps
are used as recorded; callers must use compatible clock/time units for anchors.

## Rows and persistence

build_dataset creates one YES-perspective row per accepted (ticker, horizon).
Duplicate markets or horizons are caller errors, not duplicate observations.
Rows sort by ticker then horizon. Outcome yes maps to 1, no to 0; every other
outcome is unsupported_outcome. Outcome exclusion takes precedence over missing
schedule and missing trade. Counts are per attempted (market, horizon), so rows
plus exclusion counts equal market count times horizon count.

The schema is asserted even for empty results. Price probabilities equal ticks /
10000, never complemented for a NO result. Keep event, series, category and the
scheduled/selected timestamps for downstream grouping and leakage audits.
write_dataset uses rows/horizon_ns=<integer>/*.parquet and exclusions.parquet.
Each partition retains the horizon column. Rewriting removes stale row partitions
owned by this dataset. An empty dataset writes its typed exclusion table and no
nonempty row partition. No study metrics or conclusions are computed in this stage.

## Backfill

backfill uses the existing read-only, rate-limited RestClient, follows every cursor,
and returns markets sorted by ticker, trades by timestamp/ID. It collects both
historical markets and live markets with status=settled, enriches from event data,
and obtains each market's trade history from BOTH storage tiers. Trade storage is
independent of market settlement storage. Preserve YES price regardless of taker
side. Parse timestamps without floating-point or microsecond truncation. Repeated
identical trades across pages are deduplicated by ID. Invalid prices fail clearly.

The caller supplies scheduled_closes by ticker; missing entries remain None. This
input may change on a cached rerun. Cache raw responses/data on disk, scoped to the
API origin, without credentials or request authentication headers. A complete
capture can be reused without any network call, including the cutoff call. Failed
or partial downloads must not masquerade as a complete cache. A new cache directory
requests a fresh capture; an existing completed capture is an intentional snapshot,
not a promise to discover newly settled markets. Live HTTP calls are never used by
these tests; httpx.MockTransport and the existing fake-clock limiter are used.

### Verified endpoint references (2026-10-21)

- https://docs.kalshi.com/getting_started/historical_data
- https://docs.kalshi.com/api-reference/historical/get-historical-markets
- https://docs.kalshi.com/api-reference/historical/get-historical-trades
- https://docs.kalshi.com/api-reference/market/get-market

GET /historical/cutoff exposes distinct market_settled_ts and trades_created_ts.
Older markets use /historical/markets, older trades /historical/trades; live trades
use /markets/trades. Events and series stay on their ordinary endpoints. This
matches the plan's requirement to verify older settled-data availability. Current
market responses expose close and expiration fields but do not by themselves
prove which original scheduled value was known before an early close. Explicit
anchors avoid making that unsupported inference. The trade route is used here;
candlestick approximations are not substituted for exact last-trade selection.
