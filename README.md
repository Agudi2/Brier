# Brier

A C++ order book engine and calibration study for Kalshi prediction markets. Brier records live market data, rebuilds the book from the raw stream, and tests whether prices behave like honest probabilities.

> **Status:** in progress. Read-only: it never places orders. Results are TBD until measured.

## Questions

1. When a contract trades at 30 cents, does the event happen about 30% of the time?
2. Are cheap contracts systematically overpriced (favorite-longshot bias)?
3. Where does calibration break down: category, time to close, spread, liquidity?
4. Optional: does order book imbalance predict the next mid move out of sample?

## Components

- **Recorder (Python):** authenticated WebSocket for book deltas, trades, and ticker. Timestamps every message, detects sequence gaps, resyncs from REST snapshots, writes to Parquet. API key comes from env vars only.
- **Engine (C++20):** replays the stream, keeps YES and NO bid ladders, derives implied asks ($1 minus the opposite best bid), uses integer ticks with no floating point, and validates against REST snapshots.
- **Study (Python):** settled markets priced at fixed horizons before close. Produces reliability diagrams, Brier score, and log loss, with a cluster bootstrap by event and date-based splits for any predictive test.

## Results

| Metric | Value |
|---|---|
| Book match rate vs REST snapshots | TBD |
| Replay throughput | TBD msgs/s |
| Update latency p50 / p99 | TBD / TBD |
| Brier score (95% CI) | TBD |
| Calibration slope (95% CI) | TBD |
| Imbalance hit rate, out of sample | TBD |

Benchmark setup is in `engine/bench/README.md`. Null results are reported as null results.

## Rules

- No order, portfolio, or trading endpoints are called
- API rate limits are respected
- Only aggregate stats are published (check Kalshi's current terms first)
- No profitability claims

## Run

```bash
python -m recorder.run --markets config/markets.yaml
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build
./build/brier_replay data/2026-10-01.ndjson --validate
python -m study.calibration --horizons 24h 6h 1h
```

## References

- Snowberg and Wolfers, "Explaining the Favorite-Longshot Bias," JPE 2010
- Cont, Kukanov, and Stoikov, "The Price Impact of Order Book Events," 2014
- Kalshi API docs: https://docs.kalshi.com
