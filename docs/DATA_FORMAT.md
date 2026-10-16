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
