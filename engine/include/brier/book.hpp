#pragma once

#include "brier/grid.hpp"

#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <vector>

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

// Constructed books own their grid and start valid and empty. Invalid updates
// invalidate the book; queries then return no levels/flags, and deltas return
// invalid_book until a successful snapshot restores validity. error() retains
// the original cause. Snapshots replace both sides, accept unordered unique
// prices and omit zero quantities. Depth writes descending bids into caller
// storage, leaving storage beyond the returned count untouched. Implied asks
// retain the opposite bid's quantity. Only construction/copying allocates.
class Book {
public:
    explicit Book(const PriceGrid& grid);
    [[nodiscard]] BookError apply_snapshot(std::span<const Level> yes,
                                           std::span<const Level> no) noexcept;
    [[nodiscard]] BookError apply_delta(Side side, std::int32_t price,
                                        std::int64_t delta) noexcept;
    [[nodiscard]] bool valid() const noexcept { return error_ == BookError::none; }
    [[nodiscard]] BookError error() const noexcept { return error_; }
    [[nodiscard]] std::optional<Level> best_yes_bid() const noexcept;
    [[nodiscard]] std::optional<Level> best_no_bid() const noexcept;
    [[nodiscard]] std::optional<Level> implied_yes_ask() const noexcept;
    [[nodiscard]] std::optional<Level> implied_no_ask() const noexcept;
    [[nodiscard]] std::size_t depth(Side side, std::span<Level> output) const noexcept;
    [[nodiscard]] bool crossed() const noexcept;
    [[nodiscard]] bool locked() const noexcept;

private:
    struct Ladder {
        explicit Ladder(std::size_t size);
        void clear() noexcept;
        void set(std::size_t index, std::int64_t quantity) noexcept;
        std::vector<std::int64_t> quantities;
        std::vector<std::uint64_t> occupied;
    };

    [[nodiscard]] BookError load_snapshot(Ladder& ladder,
                                           std::span<const Level> levels) noexcept;
    [[nodiscard]] BookError invalidate(BookError cause) noexcept;
    [[nodiscard]] std::optional<Level> best(const Ladder& ladder) const noexcept;

    PriceGrid grid_;
    Ladder yes_;
    Ladder no_;
    BookError error_ = BookError::none;
};

}  // namespace brier
