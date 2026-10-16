#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <vector>

namespace brier {

struct PriceRange {
    std::int32_t start;
    std::int32_t end;
    std::int32_t step;
};

class PriceGrid {
public:
    [[nodiscard]] static std::optional<PriceGrid> from_ranges(std::span<const PriceRange> ranges) {
        if (ranges.empty()) {
            return std::nullopt;
        }
        std::int32_t previous_end = 0;
        std::size_t count = 1;
        for (const auto& band : ranges) {
            // Validate bounds before arithmetic so malformed int32 inputs cannot overflow.
            if (band.start < 0 || band.end > 10000 || band.start >= band.end ||
                band.start != previous_end || band.step <= 0) {
                return std::nullopt;
            }
            const auto width = band.end - band.start;
            if (width % band.step != 0) {
                return std::nullopt;
            }
            count += static_cast<std::size_t>(width / band.step);
            previous_end = band.end;
        }
        if (previous_end != 10000) {
            return std::nullopt;
        }

        PriceGrid grid;
        grid.prices_.reserve(count);
        grid.index_by_tick_.assign(10001, -1);
        for (const auto& band : ranges) {
            for (auto price = band.start; price <= band.end; price += band.step) {
                if (!grid.prices_.empty() && price == grid.prices_.back()) {
                    continue;
                }
                grid.index_by_tick_[static_cast<std::size_t>(price)] =
                    static_cast<std::int32_t>(grid.prices_.size());
                grid.prices_.push_back(price);
            }
        }
        return grid;
    }

    [[nodiscard]] std::size_t size() const noexcept { return prices_.size(); }

    [[nodiscard]] std::optional<std::size_t> index_of(std::int32_t price) const noexcept {
        if (price < 0 || price > 10000) {
            return std::nullopt;
        }
        const auto index = index_by_tick_[static_cast<std::size_t>(price)];
        if (index < 0) {
            return std::nullopt;
        }
        return static_cast<std::size_t>(index);
    }

    [[nodiscard]] std::optional<std::int32_t> price_of(std::size_t index) const noexcept {
        if (index >= prices_.size()) {
            return std::nullopt;
        }
        return prices_[index];
    }

    [[nodiscard]] bool is_valid(std::int32_t price) const noexcept {
        return index_of(price).has_value();
    }

private:
    PriceGrid() = default;
    std::vector<std::int32_t> prices_;
    std::vector<std::int32_t> index_by_tick_;
};

}  // namespace brier
