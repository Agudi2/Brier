# Brier

A C++ order book engine and calibration study for Kalshi prediction markets. Brier records live market data, rebuilds the book from the raw stream, and tests whether prices behave like honest probabilities.

> **Status:** in progress. Read-only: it never places orders. Results are TBD until measured.

## Questions

1. When a contract trades at 30 cents, does the event happen about 30% of the time?
2. Are cheap contracts systematically overpriced (favorite-longshot bias)?
3. Where does calibration break down: category, time to close, spread, liquidity?
4. Optional: does order book imbalance predict the next mid move out of sample?

## Components

- **Recorder (Python):** authenticated WebSocket for book deltas, trades, and ticker. Timestamps every message, detects sequence gaps, resyncs from REST snapshots, writes raw NDJSON as the source of truth, which `recorder.compact` converts to Parquet for analysis. API key comes from env vars only.
- **Engine (C++20):** replays markets in parallel across threads, keeps YES and NO bid ladders on each market's price grid (from Kalshi's `price_ranges`), derives implied asks ($1 minus the opposite best bid in Kalshi's default no-leg pricing), uses integer prices in $0.0001 ticks and quantities in hundredths of a contract (no floating point), and validates against REST snapshots.
- **Study (Python):** settled markets come from live recordings plus a read-only REST backfill, priced at fixed horizons before the SCHEDULED close, so an early close can't leak the outcome. Produces reliability diagrams, Brier score, and log loss, with a cluster bootstrap by event and date-based splits for any predictive test.

## Results

| Metric | Value |
|---|---|
| Sample (markets / events / date range) | TBD |
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
python -m recorder.compact data/2026-10-01.ndjson
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build
./build/brier_replay data/2026-10-01.ndjson --threads N --out replay_out --tob --validate
python -m study.backfill
python -m study.calibration --horizons 24h 6h 1h
```

## References

- Snowberg and Wolfers, "Explaining the Favorite-Longshot Bias," JPE 2010
- Cont, Kukanov, and Stoikov, "The Price Impact of Order Book Events," 2014
- Kalshi API docs: https://docs.kalshi.com

## Development

Requires Python 3.12, uv, CMake 3.25+, and GCC 13+ or Clang 17+ on Linux or macOS.
The recorder, compactor, engine replay, and validation against REST snapshots are
available. The study commands will arrive in later stages.
Replace `N` with the desired positive worker limit. Replay writes `summary.json`
and `markets.json`; `--tob` also writes `tob.bin`. Outputs are deterministic across
thread counts. The default output directory is `replay_out`. `--validate` additionally writes
`validation.json` with full-depth and top-of-book match rates, excluded invalid
windows, and the first ten mismatch diffs. A REST sample matches any valid stream
state during its request window; mismatches are reported with a successful exit.
Validation requires nondecreasing receipt timestamps.
See [the data format](docs/DATA_FORMAT.md#engine-replay-and-outputs) for record
layout, counters, and exit codes.

```bash
uv sync
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel
ctest --test-dir build --output-on-failure
uv run pytest -m 'not slow'
uv run ruff check .
uv run ruff format --check .
uv run mypy
git config core.hooksPath .githooks
```

Run `uv run pre-commit run --all-files` before committing; the repository commit
message hook is enabled by the hooks-path command. The pre-commit checks can be
run manually with that command regardless of the configured hooks path.
Optional event-loop support: `uv sync --extra uvloop`. Release builds use `-O3`
and IPO/LTO; add `-DBRIER_NATIVE=ON` to tune for the build machine.

Separate sanitizer builds:

```bash
cmake -S . -B build-asan -DCMAKE_BUILD_TYPE=Debug -DBRIER_SANITIZER=address
cmake --build build-asan --parallel
ctest --test-dir build-asan --output-on-failure
cmake -S . -B build-tsan -DCMAKE_BUILD_TYPE=Debug -DBRIER_SANITIZER=thread
cmake --build build-tsan --parallel
ctest --test-dir build-tsan --output-on-failure
```

Test-first development proceeds one stage and phase at a time:

1. RED: write tests, fixtures, and minimal stubs; verify the expected failures.
2. Review and commit RED, then tag it: `git tag stage-NN-red` (for example, `stage-01-red`).
3. Independently review the tests before starting GREEN.
4. GREEN: implement with tests, fixtures, and tested signatures unchanged.
5. Run CTest, pytest, sanitizer builds, ruff, mypy, and
   `scripts/check_tests_frozen.sh stage-NN-red`.
6. Review the changes and commit GREEN. Any test correction gets a separate
   test commit and review before implementation continues.

CI runs Release, ASan/UBSan, and TSan builds plus Python tests and checks.
Tests marked `slow` (pytest marker or CTest label) run only on the nightly schedule.

Before recording, fill `config/markets.yaml` with tickers, series, or events. Set
`KALSHI_API_KEY_ID` and exactly one of `KALSHI_PRIVATE_KEY_PATH` or
`KALSHI_PRIVATE_KEY_PEM` in the environment. Credentials are never written to raw
records. Stop with Ctrl-C to drain buffered records before exit.

Recording defaults to `data/`, batches of 100 markets, and a REST snapshot every
60 seconds per market. The default REST budget is 50 tokens/second with capacity
10; a default-cost request uses 10 tokens. Configure `--output`, `--batch-size`,
`--snapshot-interval`, `--token-rate`, and `--token-capacity` as needed within your
API budget. The recorder uses uvloop when installed. Compaction writes typed
Parquet into a directory named after the input without its `.ndjson` suffix;
`--out DIR` overrides that location. See `docs/DATA_FORMAT.md` for schemas.
