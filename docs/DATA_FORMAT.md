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

Typed Parquet schemas will be specified with compaction in Stage 4. The packed
little-endian top-of-book stream, matching NumPy dtype, flags, and market mapping
will be specified and tested in Stage 7. Neither derived format is implemented yet.
