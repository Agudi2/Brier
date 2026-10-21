#pragma once

#include <cstdint>
#include <optional>

namespace brier {

// Latencies are unsigned integer nanoseconds. Percentiles use nearest rank and
// return a bucket upper bound with at most 1% relative bucket error. See the
// acceptance contract in tests/engine/HISTOGRAM_CONTRACT.md.
class LatencyHistogram {
public:
    // Deliberate RED sentinels: tests compile and fail on assertions.
    [[nodiscard]] bool record(std::uint64_t, std::uint64_t = 1) noexcept { return false; }
    [[nodiscard]] bool merge(const LatencyHistogram&) noexcept { return false; }
    [[nodiscard]] std::uint64_t count() const noexcept { return 0; }
    [[nodiscard]] std::optional<std::uint64_t> percentile(double) const noexcept {
        return std::nullopt;
    }
    void clear() noexcept {}
};

}  // namespace brier
