# Benchmarks

Benchmarks use generated public-format data and never require private recordings.
Build Release without sanitizers for measurements. Keep the compiler, flags,
workload seed, market count, message count, sampling interval and TOB setting with
all results. There are no machine-specific performance pass/fail thresholds.

## Build and generate

From the repository root:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBRIER_NATIVE=OFF -DBRIER_SANITIZER=none
cmake --build build --parallel 2
mkdir -p data
./build/brier_synth --out data/synth.ndjson --messages 100000 --markets 64 --seed 20261021
```

The generator refuses existing output files. Use a new filename for each workload.
`--messages` counts activity records; session boundaries, one metadata record and
one initial snapshot per market add `2*markets+2` physical lines. Zero activity
records is supported. Defaults are one million activity records, 100 markets and
seed 1. Market counts must be between 1 and 100000. Output parents must exist.

The generator streams directly to buffered output, retaining only eight levels
per side per market. It can produce tens of millions of records without retaining
them in memory; replay still needs memory for parsed events and payloads. For a
large run, choose counts that fit available disk and replay memory:

```sh
./build/brier_synth --out data/synth-large.ndjson --messages 30000000 --markets 1000 --seed 20261021
```

Approximately 95% of activity is valid signed quantity deltas, 4% trades and 1%
replacement snapshots, selected by the seeded PRNG. Eighty percent of market
selection targets the first tenth of markets. Prices are on a cent grid with an
initial one-cent spread, variable depth quantities and occasional removals.
Snapshots reflect the generated book; quantities never become negative.
Book and trade subscription sequence numbers advance independently. Synthetic
receipt timestamps advance 1000 ns per physical line from a synthetic epoch.
`mt19937_64` plus integer modulo selection makes output reproducible for a fixed
seed, market count and activity count across supported platforms. The simple
skew and fixed price bands do not model the full behavior of a live exchange.

## Replay throughput and sampled latency

```sh
./build/brier_replay data/synth.ndjson --threads 4 --bench --bench-runs 3 --sample-every 100 --out build/bench-run
```

`--bench` adds `bench.json` to the ordinary replay outputs and sweeps worker limits
1 through N from `--threads N`, capped by hardware concurrency. Replay workers are
also capped by market count. `requested_threads` and `effective_max_threads` make
the sweep cap explicit. Defaults are three repetitions and sampling every 1000th
eligible update. Counts must be positive. `--bench-runs` and `--sample-every`
require `--bench`. TOB is off unless `--tob` is supplied; report that setting.

Each worker limit receives separate measurements:

- Parse plus replay: mmap/open, parsing, deterministic merge, market partitioning,
  book construction, worker startup/join, replay and final result construction.
  The numerator is physical input records, including malformed/ignored records.
- Replay only: the parsed input is reused outside the timing window. Partitioning,
  construction, worker startup/join, replay and final result construction remain
  timed. The numerator is accepted events. Parsing and all output I/O are excluded.
- Update latency: a separate profiling replay clocks only `Book::apply_delta` on
  synchronized books. Successful updates and invalidating attempts are eligible;
  snapshots and deltas dropped while invalid are excluded. Each worker samples
  its kth, 2kth, etc. eligible attempt, continuing across markets assigned to it.
  It owns its histogram, merged only after join. Histogram recording is outside
  the clock pair. Worker sample counts can differ by up to the per-worker remainder.

Throughput repetitions do not collect latency samples. Result destruction is
outside the throughput interval; mappings released within parse_file are included.
Raw durations and their median are written to JSON; rates use that median.
A zero-duration rate is null. The input file must remain unchanged throughout.
Only one large parsed result is retained at a time during the parse/replay sweep.

The latency histogram uses exact nanosecond buckets below 256, followed by 128
linear buckets per power-of-two range. Percentiles are nearest-rank bucket upper
bounds, with less than 1% relative bucket error. Count overflow is rejected without
mutation. No samples means null percentiles. Zero-weight records are no-ops.

A steady monotonic clock supplies raw update timings. `clock_overhead_ns` records
10001 back-to-back clock pairs and reports p50/p99. This overhead is **not
subtracted**: short operations can be below the clock's resolution, yielding zero
p50, or dominated by the clock pair. Zero is not evidence of zero update cost.
Use microbenchmarks that amortize timer overhead to study such small operations.
Sampling, scheduling, cache effects and instrumentation still affect latency.

## Google Benchmark microbenchmarks

```sh
./build/brier_microbench --benchmark_min_time=0.1s --benchmark_repetitions=3 --benchmark_out=build/micro.json --benchmark_out_format=json
```

- `BM_ApplyDelta`: alternate +1/-1 at an occupied price on a cent-grid book.
  Includes compiler barriers to retain state changes; setup is outside timing.
- `BM_BestBid`: highest occupied YES bid lookup, with a compiler barrier preventing
  hoisting of the constant book state.
- `BM_ParseLine`: one in-memory fixed-point delta envelope through the production
  parser, including parser setup, full syntax checking, decoding, ticker interning
  and owned result allocation/destruction. No filesystem or mmap work is included.

Google Benchmark reports CPU and wall times and repetitions. Its clock-rate
estimate or affinity setup may be unavailable on macOS; retain those warnings.
These workload-specific microbenchmarks do not predict complete replay throughput.

## Measurement controls

Record hardware, logical CPU count, RAM, OS/build, compiler/version, build type,
`BRIER_NATIVE`, sanitizer choice and exact command lines. Release engine flags are
`-O3 -DNDEBUG -std=c++20`, strict warnings, and IPO/LTO (ThinLTO on this Clang build).
`BRIER_NATIVE=ON` adds host tuning and changes comparability. Never use sanitizer
builds as comparable throughput measurements.

On Linux, record the current governor and frequency policy; a controlled run can
use a fixed performance governor if the machine owner configures it. For pinning,
wrap a run in `taskset -c 0-3 ...` using CPUs valid for that machine. Record the CPU
set, NUMA placement and power policy. On macOS this runner does not configure a
governor or pin threads; heterogeneous cores and OS scheduling affect results.

Generating or rereading a file warms the OS file cache. Label such runs warm.
Cold-cache measurements require a separate documented cache-eviction or reboot
procedure; this runner does not evict caches. Distinguish file-cache warmth from
instruction/data-cache and allocator warmth. Avoid background work, allow thermal
conditions to settle, and repeat runs. Increasing thread count need not improve a
skewed workload because one market is never split across workers.

## Local smoke measurement

[Raw durations, quantiles, repetitions and environment](results/local-smoke.json)
were recorded on 2026-10-21 using an Apple M5, 10 logical CPUs, 24 GiB RAM, macOS
26.6.2 (25G83), arm64, Apple Clang 21.0.0, Release/ThinLTO, `BRIER_NATIVE=OFF`, and
no sanitizer. CPU pinning was off; OS frequency policy and background desktop load
were uncontrolled. The generated file was in the warm OS file cache. The exact
build, generator and replay commands are above; the microbenchmark used 0.1 s and
three repetitions as shown. Paths may differ without changing the workload.

The workload has 100000 activity records, 64 markets, seed 20261021, and 100130
physical records. The recorded SHA-256 identifies its exact bytes. These short
runs verify the tools and are not a production capacity claim. The clock-overhead
and sampled-latency fields must be read together; this clock can quantize short
updates to zero. The benchmark library could not determine CPU frequency or set
thread affinity; its estimated frequency is not a measured processor frequency.

Performance refactoring and before/after optimization comparisons are a separate,
explicitly requested phase with tests frozen.
