#pragma once

#include "brier/grid.hpp"

#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>

namespace brier {

enum class Side { yes, no };
enum class BookError {
    none, off_grid, negative_quantity, quantity_overflow, duplicate_price,
    invalid_side, invalid_book, not_implemented
};

struct Level {
    std::int32_t price;
    std::int64_t quantity;
    bool operator==(const Level&) const = default;
};

// Contract: constructed books are valid and empty. Invalid updates invalidate the
// book; queries then return no levels/flags, and deltas return invalid_book until
// a successful snapshot restores validity. error() retains the original cause.
// Snapshots replace both sides, accept unordered unique prices and omit zero qty.
// Depth writes descending bid levels into caller storage, returning the count;
// storage beyond that count is untouched. Implied asks retain opposite bid qty.
// Deliberate RED stubs: operations return a sentinel until implementation.
class Book {
public:
    explicit Book(const PriceGrid&) {}
    [[nodiscard]] BookError apply_snapshot(std::span<const Level>,
                                           std::span<const Level>) noexcept {
        return BookError::not_implemented;
    }
    [[nodiscard]] BookError apply_delta(Side, std::int32_t, std::int64_t) noexcept {
        return BookError::not_implemented;
    }
    [[nodiscard]] bool valid() const noexcept { return false; }
    [[nodiscard]] BookError error() const noexcept { return BookError::not_implemented; }
    [[nodiscard]] std::optional<Level> best_yes_bid() const noexcept { return std::nullopt; }
    [[nodiscard]] std::optional<Level> best_no_bid() const noexcept { return std::nullopt; }
    [[nodiscard]] std::optional<Level> implied_yes_ask() const noexcept { return std::nullopt; }
    [[nodiscard]] std::optional<Level> implied_no_ask() const noexcept { return std::nullopt; }
    [[nodiscard]] std::size_t depth(Side, std::span<Level>) const noexcept { return 0; }
    [[nodiscard]] bool crossed() const noexcept { return false; }
    [[nodiscard]] bool locked() const noexcept { return false; }
};

}  // namespace brier
