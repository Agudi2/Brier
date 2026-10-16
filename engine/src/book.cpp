#include "brier/book.hpp"

#include <algorithm>
#include <bit>
#include <limits>

namespace brier {
namespace {
constexpr std::size_t word_bits = 64;

std::size_t highest_bit(std::uint64_t word) noexcept {
    // Callers supply a nonzero word; unsigned arithmetic keeps shifts defined.
    return word_bits - 1 - static_cast<std::size_t>(std::countl_zero(word));
}

std::optional<Level> complement(std::optional<Level> bid) noexcept {
    if (bid) {
        bid->price = 10000 - bid->price;
    }
    return bid;
}
}  // namespace

Book::Ladder::Ladder(std::size_t size)
    : quantities(size, 0), occupied((size + word_bits - 1) / word_bits, 0) {}

void Book::Ladder::clear() noexcept {
    std::fill(quantities.begin(), quantities.end(), 0);
    std::fill(occupied.begin(), occupied.end(), 0);
}

void Book::Ladder::set(std::size_t index, std::int64_t quantity) noexcept {
    quantities[index] = quantity;
    const auto mask = std::uint64_t{1} << (index % word_bits);
    auto& word = occupied[index / word_bits];
    if (quantity == 0) {
        word &= ~mask;
    } else {
        word |= mask;
    }
}

Book::Book(const PriceGrid& grid) : grid_(grid), yes_(grid.size()), no_(grid.size()) {}

BookError Book::invalidate(BookError cause) noexcept {
    error_ = cause;
    return cause;
}

BookError Book::load_snapshot(Ladder& ladder, std::span<const Level> levels) noexcept {
    // During loading, occupied also marks zero-quantity entries as seen so
    // duplicate prices cannot silently overwrite them. No queries run mid-call.
    for (const auto& level : levels) {
        const auto index = grid_.index_of(level.price);
        if (!index) {
            return BookError::off_grid;
        }
        if (level.quantity < 0) {
            return BookError::negative_quantity;
        }
        const auto mask = std::uint64_t{1} << (*index % word_bits);
        auto& word = ladder.occupied[*index / word_bits];
        if ((word & mask) != 0) {
            return BookError::duplicate_price;
        }
        word |= mask;
        ladder.quantities[*index] = level.quantity;
    }
    for (const auto& level : levels) {
        if (level.quantity == 0) {
            ladder.set(*grid_.index_of(level.price), 0);
        }
    }
    return BookError::none;
}

BookError Book::apply_snapshot(std::span<const Level> yes,
                               std::span<const Level> no) noexcept {
    yes_.clear();
    no_.clear();
    auto result = load_snapshot(yes_, yes);
    if (result == BookError::none) {
        result = load_snapshot(no_, no);
    }
    // Failed replacements may leave internal partial data, but invalid books
    // expose no levels. The next replacement clears both ladders before loading.
    error_ = result;
    return result;
}

BookError Book::apply_delta(Side side, std::int32_t price, std::int64_t delta) noexcept {
    if (!valid()) {
        return BookError::invalid_book;
    }
    if (side != Side::yes && side != Side::no) {
        return invalidate(BookError::invalid_side);
    }
    const auto index = grid_.index_of(price);
    if (!index) {
        return invalidate(BookError::off_grid);
    }
    auto& ladder = side == Side::yes ? yes_ : no_;
    const auto old = ladder.quantities[*index];
    // old is nonnegative. Negating it is safe even at INT64_MAX; never negate
    // delta, which may be INT64_MIN, and check bounds before signed addition.
    if (delta < -old) {
        return invalidate(BookError::negative_quantity);
    }
    if (delta > std::numeric_limits<std::int64_t>::max() - old) {
        return invalidate(BookError::quantity_overflow);
    }
    ladder.set(*index, old + delta);
    return BookError::none;
}

std::optional<Level> Book::best(const Ladder& ladder) const noexcept {
    if (!valid()) {
        return std::nullopt;
    }
    for (auto word_index = ladder.occupied.size(); word_index > 0;) {
        --word_index;
        const auto word = ladder.occupied[word_index];
        if (word != 0) {
            const auto index = word_index * word_bits + highest_bit(word);
            return Level{*grid_.price_of(index), ladder.quantities[index]};
        }
    }
    return std::nullopt;
}

std::optional<Level> Book::best_yes_bid() const noexcept { return best(yes_); }
std::optional<Level> Book::best_no_bid() const noexcept { return best(no_); }
std::optional<Level> Book::implied_yes_ask() const noexcept { return complement(best(no_)); }
std::optional<Level> Book::implied_no_ask() const noexcept { return complement(best(yes_)); }

std::size_t Book::depth(Side side, std::span<Level> output) const noexcept {
    if (!valid() || output.empty() || (side != Side::yes && side != Side::no)) {
        return 0;
    }
    const auto& ladder = side == Side::yes ? yes_ : no_;
    std::size_t count = 0;
    for (auto word_index = ladder.occupied.size(); word_index > 0;) {
        --word_index;
        auto word = ladder.occupied[word_index];
        while (word != 0) {
            const auto bit = highest_bit(word);
            const auto index = word_index * word_bits + bit;
            output[count++] = Level{*grid_.price_of(index), ladder.quantities[index]};
            if (count == output.size()) {
                return count;
            }
            word &= ~(std::uint64_t{1} << bit);
        }
    }
    return count;
}

bool Book::crossed() const noexcept {
    const auto yes = best(yes_);
    const auto no = best(no_);
    return yes && no && yes->price + no->price > 10000;
}

bool Book::locked() const noexcept {
    const auto yes = best(yes_);
    const auto no = best(no_);
    return yes && no && yes->price + no->price == 10000;
}

}  // namespace brier
