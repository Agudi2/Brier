#include "brier/benchmark.hpp"

#include <algorithm>
#include <chrono>
#include <stdexcept>
#include <thread>

namespace brier {
namespace {
using Clock = std::chrono::steady_clock;
void check(const FileParseResult& input, const ReplayResult& result) {
    if (input.status != ParseStatus::ok || result.status != ReplayStatus::ok) {
        throw std::runtime_error("Cannot benchmark invalid or inaccessible input");
    }
}
}  // namespace
BenchmarkReport benchmark_file(const std::filesystem::path& path, std::size_t max_threads,
                               std::size_t repetitions, std::size_t sample_every, bool tob) {
    if (max_threads == 0 || repetitions == 0 || sample_every == 0) {
        throw std::invalid_argument("Benchmark counts and sampling interval must be positive");
    }
    BenchmarkReport report;
    report.requested_threads = max_threads;
    report.repetitions = repetitions;
    report.sample_every = sample_every;
    report.tob = tob;
    for (std::size_t i = 0; i < 10001; ++i) {
        const auto start = Clock::now();
        const auto finish = Clock::now();
        const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(finish - start).count();
        (void)report.clock_overhead.record(static_cast<std::uint64_t>(ns));
    }
    const auto limit = std::min(max_threads, static_cast<std::size_t>(
        std::max(1U, std::thread::hardware_concurrency())));
    report.rows.resize(limit);
    for (std::size_t n = 1; n <= limit; ++n) {
        auto& row = report.rows[n - 1];
        row.threads = n;
        // No retained parse result during this sweep: avoid holding two large
        // event/payload pools at once. Destruction and output I/O are untimed.
        for (std::size_t run = 0; run < repetitions; ++run) {
            const auto start = Clock::now();
            const auto input = parse_file(path, n);
            const auto result = replay(input, {n, tob});
            const auto finish = Clock::now();
            check(input, result);
            if (n == 1 && run == 0) {
                report.physical_lines = input.physical_lines;
                report.accepted_events = input.events.size();
            } else if (input.physical_lines != report.physical_lines ||
                       input.events.size() != report.accepted_events) {
                throw std::runtime_error("Input changed during benchmark");
            }
            row.parse_replay_seconds.push_back(std::chrono::duration<double>(finish - start).count());
        }
    }
    const auto input = parse_file(path, limit);
    if (input.status != ParseStatus::ok || input.physical_lines != report.physical_lines ||
        input.events.size() != report.accepted_events) {
        throw std::runtime_error("Input unavailable or changed during benchmark");
    }
    for (auto& row : report.rows) {
        for (std::size_t run = 0; run < repetitions; ++run) {
            const auto start = Clock::now();
            const auto result = replay(input, {row.threads, tob});
            const auto finish = Clock::now();
            check(input, result);
            row.replay_only_seconds.push_back(std::chrono::duration<double>(finish - start).count());
        }
        const auto profile = profile_replay(input, {row.threads, tob}, sample_every);
        check(input, profile.result);
        row.latency = profile.latency;
        row.eligible_updates = profile.eligible_updates;
    }
    return report;
}
}  // namespace brier
