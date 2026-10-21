#pragma once

#include "brier/histogram.hpp"
#include "brier/replay.hpp"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <vector>

namespace brier {
struct ReplayProfile {
    ReplayResult result;
    LatencyHistogram latency;
    std::uint64_t eligible_updates = 0;
};
// Sample every kth delta attempt on synchronized books, independently per worker.
// Only Book::apply_delta is inside the sample clock pair. Normal replay is untimed.
[[nodiscard]] ReplayProfile profile_replay(const FileParseResult&, ReplayOptions,
                                         std::size_t sample_every);
struct BenchmarkRow {
    std::size_t threads = 1;
    std::vector<double> parse_replay_seconds, replay_only_seconds;
    LatencyHistogram latency;
    std::uint64_t eligible_updates = 0;
};
struct BenchmarkReport {
    std::size_t requested_threads = 1, repetitions = 3, sample_every = 1000;
    bool tob = false;
    std::uint64_t physical_lines = 0, accepted_events = 0;
    LatencyHistogram clock_overhead;
    std::vector<BenchmarkRow> rows;
};
// Results include raw durations. Throughput excludes latency profiling and output I/O.
[[nodiscard]] BenchmarkReport benchmark_file(const std::filesystem::path&, std::size_t max_threads,
                                            std::size_t repetitions, std::size_t sample_every,
                                            bool tob);
}  // namespace brier
