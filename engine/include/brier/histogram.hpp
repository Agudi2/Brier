#pragma once

#include <array>
#include <bit>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>

namespace brier {

// Integer nanoseconds: exact buckets below 256, then 128 equal-width buckets
// per power-of-two range. Worst-case upper-bound error is less than 1/128.
class LatencyHistogram {
public:
    [[nodiscard]] bool record(std::uint64_t value, std::uint64_t occurrences = 1) noexcept {
        if (occurrences > std::numeric_limits<std::uint64_t>::max() - count_) return false;
        buckets_[index(value)] += occurrences;
        count_ += occurrences;
        return true;
    }
    [[nodiscard]] bool merge(const LatencyHistogram& other) noexcept {
        if (other.count_ > std::numeric_limits<std::uint64_t>::max() - count_) return false;
        for (std::size_t i = 0; i < buckets_.size(); ++i) buckets_[i] += other.buckets_[i];
        count_ += other.count_;
        return true;
    }
    [[nodiscard]] std::uint64_t count() const noexcept { return count_; }
    [[nodiscard]] std::optional<std::uint64_t> percentile(double p) const noexcept {
        if (count_ == 0 || !std::isfinite(p) || p < 0 || p > 100) return std::nullopt;
        std::uint64_t rank = 1;
        if (p > 0 && p == std::floor(p)) {
            const auto percent = static_cast<std::uint64_t>(p);
            // Exact nearest rank for integer percentiles, without p*count overflow.
            rank = (count_ / 100) * percent + ((count_ % 100) * percent + 99) / 100;
        } else if (p > 0) {
            auto position = static_cast<long double>(p) * static_cast<long double>(count_) / 100;
            // Decimal inputs such as 0.1 can land a rounding unit above an integer.
            const auto nearest = std::round(position);
            const auto uncertainty = 2 * std::numeric_limits<double>::epsilon() * std::abs(position);
            if (std::abs(position - nearest) <= uncertainty) position = nearest;
            position = std::ceil(position);
            if (position >= static_cast<long double>(count_)) rank = count_;
            else if (position > 1) rank = static_cast<std::uint64_t>(position);
        }
        for (std::size_t i = 0; i < buckets_.size(); ++i) {
            if (rank <= buckets_[i]) return upper_bound(i);
            rank -= buckets_[i];
        }
        return std::nullopt;
    }
    void clear() noexcept { buckets_.fill(0); count_ = 0; }

private:
    static std::size_t index(std::uint64_t value) noexcept {
        if (value < 256) return static_cast<std::size_t>(value);
        const auto exponent = static_cast<unsigned>(std::bit_width(value) - 1);
        const auto shift = exponent - 7;
        return 256 + (exponent - 8) * 128 + static_cast<std::size_t>((value >> shift) - 128);
    }
    static std::uint64_t upper_bound(std::size_t bucket) noexcept {
        if (bucket < 256) return bucket;
        const auto group = (bucket - 256) / 128;
        const auto mantissa = 128 + (bucket - 256) % 128;
        const auto shift = group + 1;
        return (static_cast<std::uint64_t>(mantissa) << shift) | ((std::uint64_t{1} << shift) - 1);
    }
    std::array<std::uint64_t, 256 + 56 * 128> buckets_{};
    std::uint64_t count_ = 0;
};

}  // namespace brier
