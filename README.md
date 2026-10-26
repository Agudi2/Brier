# Brier

A C++ order book engine and calibration study for Kalshi prediction markets. Brier records live market data, rebuilds the book from the raw stream, and tests whether prices behave like honest probabilities.

> **Status:** the read-only recorder, replay, and study tools are implemented. Market-data results
> require a recorded dataset; none is present in this checkout.

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

As of 2026-10-26, this checkout has no `data/study` settled-market dataset or
`replay_out/validation.json`. Missing measurements below are **unavailable**, not
zero. The only measured figures are from the [saved synthetic smoke run](engine/bench/results/local-smoke.json);
they are not market-data findings.

| Metric | Result | Evidence |
|---|---|---|
| Settled-market sample: original scheduled-close window, markets, events, exclusions | Window and all counts unavailable | No `data/study`; generate it with the [backfill command](#run), then inspect the aggregate report and source date range |
| Book match rate vs REST snapshots | Unavailable | No recorded replay with `--validate` or `validation.json` |
| Parse-and-replay throughput | **6.50 million physical records/s**, synthetic, four workers | [Raw smoke run](engine/bench/results/local-smoke.json), reproduced by the commands below |
| Sampled update latency p50 / p99 | **0 / 83 ns**, synthetic, four workers, 948 samples; clock-limited | [Raw smoke run](engine/bench/results/local-smoke.json), reproduced by the commands below |
| Brier score (95% CI) | Unavailable | No settled-market dataset or calibration report |
| Calibration slope (95% CI) | Unavailable | No settled-market dataset or fitted study result |
| Imbalance hit rate, out of sample | Unavailable | No recorded top-of-book sample with a chronological holdout |

The two synthetic values are the four-worker row of a run made on 2026-10-21:
100,000 generated activity records, 64 markets, seed 20261021, 100,130 physical
lines, three timing repetitions, and every 100th eligible update sampled.
Throughput uses the median **parse plus replay** duration, not replay-only time.
The commands recorded for that run, from the repository root, were:

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBRIER_NATIVE=OFF -DBRIER_SANITIZER=none
cmake --build build --parallel 2
mkdir -p data
./build/brier_synth --out data/synth.ndjson --messages 100000 --markets 64 --seed 20261021
./build/brier_replay data/synth.ndjson --threads 4 --bench --bench-runs 3 --sample-every 100 --out build/bench-run
```

Hardware for those figures: Apple M5, 10 logical CPUs, 24 GiB RAM, arm64,
macOS 26.6.2 (25G83), Apple Clang 21.0.0, Release with ThinLTO,
`BRIER_NATIVE=OFF`, no sanitizer. The generated file was warm in the OS file
cache. CPU pinning was off; desktop load, frequency policy, and thermal state
were uncontrolled. A measured zero p50 reflects the clock's resolution and is
not a zero-cost update. See the [benchmark guide](engine/bench/README.md) for
the precise timing definitions and microbenchmarks.

For an empirical sample, first run the [backfill and calibration commands](#run).
Use the original scheduled-close timestamps in `data/study/rows` for the UTC
window, and `reports/summary.json` for aggregate market/event and exclusion
counts. Publish a score or interval only after its real input, command, horizon,
sample size, and exclusions are recorded alongside it.

After backfill, this aggregate-only inventory supplies the sample row for the
same three horizons used by the calibration command in [Run](#run):

```bash
uv run python - <<'PY'
from datetime import date, timedelta
from pathlib import Path
import polars as pl
from study.breakdowns import parse_horizon

files = sorted(Path("data/study/rows").glob("horizon_ns=*/*.parquet"))
if not files:
    raise SystemExit("No retained study rows")
rows = pl.concat(pl.read_parquet(path) for path in files)
horizons = [parse_horizon(value) for value in ("24h", "6h", "1h")]
rows = rows.filter(pl.col("horizon_ns").is_in(horizons))
if rows.is_empty():
    raise SystemExit("No retained rows at the requested horizons")
epoch = date(1970, 1, 1)
def as_utc_day(ns):
    return epoch + timedelta(days=int(ns) // 86_400_000_000_000)

closes = rows["scheduled_close_ns"]
print("Original scheduled-close UTC window:", as_utc_day(closes.min()), "to", as_utc_day(closes.max()))
print("Markets:", rows["ticker"].n_unique(), "Events:", rows["event_ticker"].n_unique())
exclusions = pl.read_parquet("data/study/exclusions.parquet")
print("Exclusions:", {reason: int(count) for reason, count in exclusions.iter_rows()})
PY
```

## Limitations

- The settled-market study depends on an original scheduled-close record and a
  usable earlier price. Missing schedules, unsupported outcomes, and absent
  trades or quotes are excluded, so the retained sample may differ from all
  listed markets.
- Top-of-book prices and visible quantities do not describe hidden liquidity,
  queue position, execution costs, or fill probability. The imbalance study
  predicts the next changed midpoint in event time, with a date-based holdout;
  its hit rate is not a trading return.
- Calibration intervals resample whole events. Small or single-event groups
  have null results, and breakdown labels require a Holm-adjusted test. These
  diagnostics do not establish a causal effect or profitability.
- The saved benchmark uses a short, generated, warm-cache workload. Its sampled
  nanosecond latency is limited by clock resolution and uncontrolled scheduling;
  neither throughput nor latency should be extrapolated to a live feed.
- Re-check Kalshi's current terms and data-publication rules before publishing
  any aggregate result or sharing recordings. The presence of this read-only
  code is not permission to redistribute market data.

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
python -m study.backfill --scheduled-closes config/scheduled_closes.json --output data/study
python -m study.calibration --horizons 24h 6h 1h
```

## References

- Snowberg and Wolfers, "Explaining the Favorite-Longshot Bias," JPE 2010
- Cont, Kukanov, and Stoikov, "The Price Impact of Order Book Events," 2014
- Kalshi API docs: https://docs.kalshi.com

## Development

Requires Python 3.12, uv, CMake 3.25+, and GCC 13+ or Clang 17+ on Linux or macOS.
The recorder, compactor, engine replay, REST validation, study dataset builder,
calibration report, and optional imbalance analysis are available.
Replace `N` with the desired positive worker limit. Replay writes `summary.json`
and `markets.json`; `--tob` also writes `tob.bin`. Outputs are deterministic across
thread counts. The default output directory is `replay_out`. `--validate` additionally writes
`validation.json` with full-depth and top-of-book match rates, excluded invalid
windows, and the first ten mismatch diffs. A REST sample matches any valid stream
state during its request window; mismatches are reported with a successful exit.
Validation requires nondecreasing receipt timestamps.
`brier_synth` generates seeded streams; `brier_replay --bench` reports throughput
and sampled update latency in `bench.json`. `brier_microbench` measures individual
book operations and single-line parsing. See the benchmark guide for commands and
measurement limits.
See [the data format](docs/DATA_FORMAT.md#engine-replay-and-outputs) for record
layout, counters, and exit codes.

Study backfill uses the read-only, rate-limited REST client and saves a reusable
capture under `data/backfill`. `config/scheduled_closes.json` is a ticker-to-time
JSON map of original scheduled close times, recorded before settlement, for example
`{"EXAMPLE":"2026-10-21T20:00:00Z"}`. Missing original schedules are excluded;
current close or expiration fields are not substituted. The command above writes
partitioned YES-perspective rows and exclusion counts under `data/study`. Add
`--tob-dir replay_out --max-spread-ticks 200` to use replay book midpoints, or omit
`--output` and `--scheduled-closes` to capture a reusable API snapshot only.

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
