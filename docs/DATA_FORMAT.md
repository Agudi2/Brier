# Data format v1

Raw NDJSON is the source of truth: UTF-8, one JSON object per line, terminated by
newline. Every envelope has integer `v: 1` and string `kind`. Server payloads are
preserved verbatim within `msg` or `book`; metadata fields are not an exhaustive
schema for server responses. The recorder compacts raw data into Parquet for analysis.

## Envelopes

These are illustrative valid JSON records; timestamps and tickers are examples.

```json
{"v":1,"kind":"session","t_recv_ns":1,"t_mono_ns":1,"conn":1,"event":"open","pricing_mode":"no_leg"}
{"v":1,"kind":"ws","t_recv_ns":2,"t_mono_ns":2,"conn":1,"msg":{"type":"example"}}
{"v":1,"kind":"gap","t_recv_ns":3,"conn":1,"sid":3,"expected_seq":12,"got_seq":15,"tickers":["EXAMPLE"]}
{"v":1,"kind":"resync","t_recv_ns":4,"tickers":["EXAMPLE"]}
{"v":1,"kind":"rest_book","t_send_ns":5,"t_recv_ns":6,"ticker":"EXAMPLE","book":{}}
{"v":1,"kind":"market_meta","t_recv_ns":7,"ticker":"EXAMPLE","price_ranges":[],"event_ticker":"EVENT"}
```

- `t_recv_ns` and `t_send_ns` are wall-clock UTC nanoseconds since the Unix epoch.
- `t_mono_ns` is monotonic nanoseconds, meaningful for elapsed time within a process.
- `conn` identifies a connection; session `event` is `open` or `close`.
- Every session line includes `pricing_mode`. Version 1 uses `no_leg`: NO bids
  are NO-leg prices. Implied YES ask = 10000 - best NO bid; implied NO ask =
  10000 - best YES bid. Unsupported pricing modes must be rejected by the engine.
- Sequence numbers are tracked per subscription ID (`sid`). A gap lists every
  ticker on that subscription so replay needs no subscription-membership state.
- `resync` identifies the affected tickers. A gap invalidates their books until
  the next snapshot; REST validation alone does not restore stream validity.
- `rest_book` preserves a response and its request/receipt time window.
- `market_meta` stores the market price ranges and event identity, plus additional
  market metadata as required by later stages.

## Numeric representation

Prices use integer ticks of $0.0001 (0 through 10000). Quantities use signed int64
hundredths of a contract; deltas may be negative. Raw server fixed-point strings
remain unchanged. Parsed engine data uses integers only. Market grids come from
`price_ranges`. Python analysis converts ticks to probabilities only at analysis time.

## Derived outputs

`python -m recorder.compact data/2026-10-01.ndjson` writes five Zstandard-compressed
Parquet files in `data/2026-10-01/`. Use `--out DIR` to choose another directory.
The source NDJSON is never changed. All five tables are created with fixed schemas,
including when they have no rows. Parsing uses batches of 10,000 source lines.

Every table has `line_index: int64` (zero-based source line identity) and
`t_recv_ns: int64`. Rows retain source order within each table. Validation errors
report one-based source line numbers. Unsupported envelope versions and session
pricing modes are rejected. Input is parsed and validated in a staging directory
before existing output tables are replaced. A publication I/O failure requires
regenerating the derived directory from the source.

| Table | Other columns and types |
|---|---|
| `envelopes.parquet` | `v: int32`, `kind: string`, `t_mono_ns: int64?`, `conn: int64?`, `message_type: string?`, `raw: string` |
| `deltas.parquet` | `conn: int64`, `sid: int64`, `seq: int64`, `market_ticker: string`, `side: string`, `price_ticks: int32`, `delta_qty: int64` |
| `trades.parquet` | `conn: int64`, `sid: int64`, `seq: int64?`, `market_ticker: string`, `trade_id: string?`, `yes_price_ticks: int32`, `no_price_ticks: int32`, `qty: int64`, `taker_side: string?`, `ts_ms: int64?` |
| `tickers.parquet` | `conn: int64`, `sid: int64`, `market_ticker: string`, `price_ticks: int32?`, `yes_bid_ticks: int32?`, `yes_ask_ticks: int32?`, `ts_ms: int64?` |
| `rest_books.parquet` | `t_send_ns: int64`, `market_ticker: string`, `yes_prices: list<int32>`, `yes_qty: list<int64>`, `no_prices: list<int32>`, `no_qty: list<int64>` |

`?` denotes nullable values. Missing or empty optional ticker quotes become null;
zero prices remain zero. Prices and quantities use the integer units above. Only
deltas may have negative quantities. Each REST-book row preserves its complete
level lists in the original order; empty sides are empty typed lists.

`envelopes` contains one row per source line, including control messages, snapshots,
metadata, gaps, and unknown public message types. `raw` preserves the original
JSON text without the line terminator. Specialized tables contain one row per
corresponding delta, trade, ticker update, or REST-book envelope. Snapshots and
unknown messages remain available in `envelopes` and the source NDJSON.

The packed little-endian top-of-book stream, matching NumPy dtype, flags, and market
mapping will be specified and tested in Stage 7.

## Recording and recovery

The recorder uses the public `orderbook_delta`, `trade`, and `ticker` channels.
It records initial market metadata and REST books, then samples REST books every
configured interval. The request-send and response-receipt timestamps bracket
each REST snapshot. REST bodies and WebSocket objects are spliced into envelopes
without reserializing their numeric fields or whitespace. Verbatim payloads must
be valid single-line JSON objects; malformed or multiline frames fail recording
rather than producing invalid NDJSON.

Command IDs increase across connections. Subscription acknowledgements associate
each sid with its channel and full ticker batch. Sequence discontinuities produce
a gap before the offending message, followed by resync, REST books, and fresh
subscriptions. A gap invalidates every market on that sid. Reconnects reset sid
sequence tracking and resync all selected markets. Every connection has an open
and close session line with `pricing_mode: no_leg`.

One dedicated thread owns buffered file I/O. The bounded queue blocks producers
when full. Files are opened for append and rotated by each record's receipt date,
so queued records crossing UTC midnight retain the correct day. Graceful SIGINT
or SIGTERM stops recording and drains accepted records before closing files.

## Engine parsing

The C++ `parse_file(path, chunks)` API reads a regular file through a read-only
memory mapping. Nominal byte chunks advance to newline boundaries; each worker
uses its own simdjson On-Demand parser and padded line buffer. Worker concurrency
is bounded by the hardware thread count. Results merge in source order, including
when timestamps repeat or move backward. Chunk count does not change accepted
events, ticker IDs, payload ordering, or diagnostics.

`FileParseResult` owns its events, ticker strings, metadata strings, level lists,
market-ID lists, and price ranges after the mapping is released. `Event` contains
integer fields and ranges into those pools. Ticker IDs are dense in first accepted
occurrence order; entries within a gap/resync ticker list preserve their order.
Snapshot and REST level lists preserve source order. Prices and quantities use
the shared exact fixed-point parsers. Optional prices use -1 for absence, so a
zero price remains distinct. Optional text/market IDs use `no_id`.

The parser recognizes sessions, market metadata, snapshots, deltas, trades, ticker
updates, gaps, resyncs, and REST books. Known messages retain fields needed for
replay; extra server fields remain available in raw NDJSON. Unknown WebSocket
types (including subscription acknowledgements) and unknown envelope kinds are
counted separately and ignored. Complete JSON syntax is checked even for ignored
records and fields; ignored numeric fields require no floating-point conversion.

Malformed lines produce one diagnostic each with a one-based physical line
number. Accepted events carry a zero-based physical line index. Rejected records
contribute no partial events, ticker IDs, or payloads. Blank lines are malformed;
a final newline terminates its record without adding another line. LF, CRLF,
empty files, and files without a final newline are supported. Unsupported envelope
versions are malformed records. An explicit unsupported session pricing mode
fails the entire parse with no partial output. Missing session fields are malformed.

File access errors and a zero chunk count return error statuses. Resource
exhaustion raises an exception after workers have joined; input buffers and the
mapping are released by their owners. Parse files that remain stable or are only
appended during the call; the mapped extent is the size observed when opening.
Concurrent truncation of a mapped file is unsupported.

## Engine replay and outputs

Run `brier_replay INPUT [--threads N] [--out DIR] [--tob] [--validate]`.
The default worker limit is 1 and the default output directory is `replay_out`,
relative to the working directory. Actual concurrency is bounded by hardware
threads and the number of markets. `--help` prints usage and exit codes.

Events are partitioned by market in source-line order. Gap and resync records
are applied once to each listed market, even if the list repeats an ID. Markets
are sorted by decreasing event count (ties by market ID), then assigned greedily
to the least-loaded worker (ties by worker index). Each worker owns its books
and output buffer. Book updates use no locks; output buffers are reserved before
replay. Receipt timestamps do not reorder book updates.

A market requires price-range metadata and a successful stream snapshot before
any deltas can apply. Gaps, resyncs, changed price ranges, and invalid updates
invalidate its state until a new successful snapshot. Identical metadata retains
the state. REST books, trades, ticker messages, and session boundaries do not
change or restore a book. Invalid books expose empty final ladders.

Outputs:

- `markets.json`: a JSON string array, indexed by the parser's dense market ID.
- `summary.json`: schema version 1, physical and malformed line counts,
  unknown-message and unknown-envelope counts, totals, and market records in
  market-ID order. Each market contains its ID, ticker, validity, error name,
  counters, and descending YES/NO bid ladders as `[price_ticks, quantity]` pairs.
- `tob.bin`: created only with `--tob`, using the layout below.

Counters are `snapshots` (successful stream snapshots), `applied_deltas` (all
successful deltas, including zero and non-top changes), `dropped_deltas` (deltas
while unsynchronized), and `invalid_updates` (failed updates, including snapshots
without metadata). Summary totals are the sums of market counters. Invalid
updates retain their `BookError` name; gaps and changed metadata use
`invalid_book`. A successful snapshot clears the error to `none`.

Outputs contain no thread counts, execution timings, input paths, or generation
timestamps. For a fixed input and flags they are byte-identical across worker
counts. Files of the same name in the chosen output directory are overwritten.
Without `--tob`, an existing `tob.bin` is left untouched; use a fresh directory
to avoid mixing artifacts from separate runs. A failed write can leave partial
output files; only exit code 0 indicates success.

| Exit code | Meaning |
| ---: | --- |
| 0 | Success or help |
| 2 | Invalid command-line arguments |
| 3 | Input access, unsupported pricing mode, or input/replay/validation failure |
| 4 | Output I/O failure |

`--validate` additionally writes `validation.json` as described below. Mismatches
are report data and do not change the successful exit code. Malformed individual
lines are counted and skipped, rather than fatal.

### Top-of-book binary layout

Each record is exactly 40 bytes, little-endian, without alignment padding or a
header. Fields are serialized individually; native struct memory is never dumped.
Prices are integer ticks of $0.0001; quantities are integer hundredths of a
contract. YES ask is 10000 minus the highest NO bid and retains its quantity.

| Field | Byte offset | Type |
| --- | ---: | --- |
| t_recv_ns | 0 | int64 |
| market_id | 8 | uint32 |
| yes_bid | 12 | int32 |
| yes_ask | 16 | int32 |
| bid_qty | 20 | int64 |
| ask_qty | 28 | int64 |
| flags | 36 | uint32 |

Flag bits are 1 for valid, 2 for crossed, and 4 for locked. Missing prices use -1;
zero is a valid price. Invalid rows have prices -1, quantities 0, and flags 0.
A valid empty book has flags 1. Emit only when a price, quantity, or flags value
changes; the initial invalid state is unreported. Rows are sorted by
`(t_recv_ns, source_line_index, market_id)` after replay. The last key resolves
multi-market invalidations from the same source line. The source-line index is
an internal merge key and is omitted from the binary file.

```python
import numpy as np

tob_dtype = np.dtype(
    [
        ("t_recv_ns", "<i8"),
        ("market_id", "<u4"),
        ("yes_bid", "<i4"),
        ("yes_ask", "<i4"),
        ("bid_qty", "<i8"),
        ("ask_qty", "<i8"),
        ("flags", "<u4"),
    ],
    align=False,
)
rows = np.fromfile("replay_out/tob.bin", dtype=tob_dtype)
```


## Validation against REST snapshots

`--validate` compares each accepted REST sample against earlier stream states of
its market during the inclusive `[t_send_ns, t_recv_ns]` window. The state active
at request time persists until a stream update replaces it. Updates at receipt
time are candidates only when they precede the REST sample in source-line order;
later source lines cannot retroactively match. A state replaced exactly at the
lower boundary is excluded, while states reached by updates on either boundary
are included. Multiple updates at the same timestamp retain their source order.

Full-depth matching requires both complete bid ladders to equal one candidate
state. Levels from different states or markets are never combined. Ladders are
canonicalized to descending prices with zero quantities omitted. Prices and
quantities compare exactly as integers. Top-of-book matching independently
compares the YES best bid and implied YES best ask, including quantities. Empty
valid books can match; missing prices differ from price zero.

REST `orderbook_fp.yes_dollars` and `no_dollars` are the respective YES and NO
bid ladders. Compare the NO bids directly; complement them only when deriving
YES asks. This convention is documented in Kalshi's
[orderbook responses](https://docs.kalshi.com/getting_started/orderbook_responses)
and [market orderbook endpoint](https://docs.kalshi.com/api-reference/market/get-market-orderbook).
Use full-depth REST responses; truncated snapshots cannot establish full-depth
equality.

Gaps, resyncs, metadata changes, invalid updates, and stream recovery follow the
replay rules above. Invalid states are ignored as candidates. Windows with no
valid candidate are counted as `excluded_invalid` and omitted from denominators.
A partially invalid window can still match a valid candidate, including a
successful recovery snapshot inside the window. REST data never changes stream
validity or quantities.

`validation.json` uses schema version 1 and contains:

- `samples`, `compared`, and `excluded_invalid`, where samples = compared + excluded.
- `full_depth_matches`, `top_of_book_matches`, and `mismatches`, where mismatches =
  compared - full_depth_matches.
- `full_depth_match_rate` and `top_of_book_match_rate`: the respective match count
  divided by compared, or JSON `null` when compared is zero.
- `diffs`: the first ten full-depth mismatches in REST source-line order. Each
  diff contains `market_id`, `rest_line_index`, `state_line_index`, `rest_yes`,
  `rest_no`, `engine_yes`, and `engine_no`. Ladders are `[price_ticks, quantity]`
  pairs. The reference engine state is the last valid candidate in source order.

The API's `max_diffs` option can change the diagnostic limit without affecting
counts. Validation workers own separate markets and merge reports in source order.
Outputs are byte-identical across worker counts, and ordinary replay artifacts
are identical with and without validation. The implementation evaluates active
request windows as states change; it does not retain every historical full book.

Validation requires source-line order with nondecreasing receipt timestamps.
Regressing clocks, reversed REST windows, invalid market IDs or pool ranges,
and invalid REST ladders (duplicates, negative quantities, or levels off the known
market grid) fail validation with no partial report. When metadata has not yet
arrived, REST levels receive basic price/quantity/duplicate checks and the stream
has no valid state. Malformed raw lines have already been rejected by the parser
and remain visible in the replay summary's malformed count.

The CLI validates before writing outputs and returns exit code 3 on invalid
validation input; write failures return 4 and may leave partial output files.
Without `--validate`, an existing `validation.json` is left untouched. Use a fresh
output directory when comparing runs.
