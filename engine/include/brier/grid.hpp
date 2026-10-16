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
    [[nodiscard]] static std::optional<PriceGrid> from_ranges(std::span<const PriceRange>) {
        return PriceGrid{};
    }

    [[nodiscard]] std::size_t size() const noexcept { return 0; }
    [[nodiscard]] std::optional<std::size_t> index_of(std::int32_t) const noexcept {
        return std::nullopt;
    }
    [[nodiscard]] std::optional<std::int32_t> price_of(std::size_t) const noexcept {
        return std::nullopt;
    }
    [[nodiscard]] bool is_valid(std::int32_t) const noexcept { return false; }

private:
    PriceGrid() = default;
    std::vector<std::int32_t> prices_;
    std::vector<std::int32_t> index_by_tick_;
};

}  // namespace brier
