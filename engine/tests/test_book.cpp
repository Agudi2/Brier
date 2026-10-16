#include "brier/book.hpp"

#include <algorithm>
#include <array>
#include <cstdint>
#include <initializer_list>
#include <limits>
#include <map>
#include <random>
#include <vector>

#include <gtest/gtest.h>

namespace book_tests {
using brier::Book;
using brier::BookError;
using brier::Level;
using brier::PriceGrid;
using brier::PriceRange;
using brier::Side;
using Levels = std::vector<Level>;

PriceGrid grid(std::int32_t step = 100) {
    const std::array bands{PriceRange{0, 10000, step}};
    return PriceGrid::from_ranges(bands).value();
}

BookError snapshot(Book& book, std::initializer_list<Level> yes,
                   std::initializer_list<Level> no) {
    return book.apply_snapshot(std::span(yes.begin(), yes.size()),
                               std::span(no.begin(), no.size()));
}

void expect_depth(const Book& book, Side side, const Levels& expected) {
    // Exercise zero capacity, truncation, exact size, and unused output storage.
    for (const std::size_t capacity : {std::size_t{0}, std::size_t{1}, std::size_t{2},
                                       expected.size(), expected.size() + 2}) {
        SCOPED_TRACE(capacity);
        const Level sentinel{-123, -456};
        Levels output(capacity, sentinel);
        const auto count = std::min(capacity, expected.size());
        ASSERT_EQ(book.depth(side, output), count);
        for (std::size_t i = 0; i < count; ++i) EXPECT_EQ(output[i], expected[i]);
        for (std::size_t i = count; i < capacity; ++i) EXPECT_EQ(output[i], sentinel);
    }
}

void expect_state(const Book& book, const Levels& yes, const Levels& no) {
    EXPECT_TRUE(book.valid());
    EXPECT_EQ(book.error(), BookError::none);
    const auto y = yes.empty() ? std::nullopt : std::optional(yes.front());
    const auto n = no.empty() ? std::nullopt : std::optional(no.front());
    EXPECT_EQ(book.best_yes_bid(), y);
    EXPECT_EQ(book.best_no_bid(), n);
    // A bid on the opposite leg is an ask at 10000 minus its price, same size.
    EXPECT_EQ(book.implied_yes_ask(), n ? std::optional(Level{10000-n->price, n->quantity})
                                      : std::nullopt);
    EXPECT_EQ(book.implied_no_ask(), y ? std::optional(Level{10000-y->price, y->quantity})
                                     : std::nullopt);
    EXPECT_EQ(book.crossed(), y && n && y->price + n->price > 10000);
    EXPECT_EQ(book.locked(), y && n && y->price + n->price == 10000);
    expect_depth(book, Side::yes, yes);
    expect_depth(book, Side::no, no);
}

void expect_invalid(const Book& book, BookError error) {
    EXPECT_FALSE(book.valid());
    EXPECT_EQ(book.error(), error);
    EXPECT_FALSE(book.best_yes_bid());
    EXPECT_FALSE(book.best_no_bid());
    EXPECT_FALSE(book.implied_yes_ask());
    EXPECT_FALSE(book.implied_no_ask());
    EXPECT_FALSE(book.crossed());
    EXPECT_FALSE(book.locked());
    expect_depth(book, Side::yes, {});
    expect_depth(book, Side::no, {});
}

TEST(BookCore, ConstructedBookIsEmpty) {
    Book book(grid());
    expect_state(book, {}, {});
}

TEST(BookCore, FifteenHandCalculatedUpdates) {
    Book book(grid());
    // 1: 30c YES / 60c NO => YES ask 40c, NO ask 70c.
    ASSERT_EQ(snapshot(book, {{3000,1000},{2000,400}}, {{6000,700}}), BookError::none);
    expect_state(book, {{3000,1000},{2000,400}}, {{6000,700}});
    EXPECT_EQ(book.implied_yes_ask(), (Level{4000,700}));
    EXPECT_EQ(book.implied_no_ask(), (Level{7000,1000}));
    // 2: 1000 + 155 = 1155 hundredths (11.55 contracts).
    ASSERT_EQ(book.apply_delta(Side::yes,3000,155), BookError::none);
    expect_state(book, {{3000,1155},{2000,400}}, {{6000,700}});
    // 3: insert better YES; 3500 + 6000 = 9500, neither locked nor crossed.
    ASSERT_EQ(book.apply_delta(Side::yes,3500,200), BookError::none);
    expect_state(book, {{3500,200},{3000,1155},{2000,400}}, {{6000,700}});
    // 4: 200 - 50 = 150, price unchanged.
    ASSERT_EQ(book.apply_delta(Side::yes,3500,-50), BookError::none);
    expect_state(book, {{3500,150},{3000,1155},{2000,400}}, {{6000,700}});
    // 5: 3500 + 6500 = 10000: locked.
    ASSERT_EQ(book.apply_delta(Side::no,6500,100), BookError::none);
    expect_state(book, {{3500,150},{3000,1155},{2000,400}}, {{6500,100},{6000,700}});
    EXPECT_TRUE(book.locked());
    // 6: 3500 + 6600 = 10100: crossed.
    ASSERT_EQ(book.apply_delta(Side::no,6600,1), BookError::none);
    expect_state(book, {{3500,150},{3000,1155},{2000,400}}, {{6600,1},{6500,100},{6000,700}});
    EXPECT_TRUE(book.crossed());
    // 7: 1 - 1 = 0 removes NO best, restores lock at 6500.
    ASSERT_EQ(book.apply_delta(Side::no,6600,-1), BookError::none);
    expect_state(book, {{3500,150},{3000,1155},{2000,400}}, {{6500,100},{6000,700}});
    // 8: 150 - 150 = 0 removes YES best; 3000 + 6500 = 9500.
    ASSERT_EQ(book.apply_delta(Side::yes,3500,-150), BookError::none);
    expect_state(book, {{3000,1155},{2000,400}}, {{6500,100},{6000,700}});
    // 9: 1155 - 1155 = 0; fall back to 2000.
    ASSERT_EQ(book.apply_delta(Side::yes,3000,-1155), BookError::none);
    expect_state(book, {{2000,400}}, {{6500,100},{6000,700}});
    // 10: 400 - 400 = 0; no YES bid or implied NO ask.
    ASSERT_EQ(book.apply_delta(Side::yes,2000,-400), BookError::none);
    expect_state(book, {}, {{6500,100},{6000,700}});
    // 11: absent + 0 stays absent.
    ASSERT_EQ(book.apply_delta(Side::yes,5000,0), BookError::none);
    expect_state(book, {}, {{6500,100},{6000,700}});
    // 12: price zero is a real bid, not the missing-price sentinel.
    ASSERT_EQ(book.apply_delta(Side::yes,0,1), BookError::none);
    expect_state(book, {{0,1}}, {{6500,100},{6000,700}});
    // 13: 0 + 10000 = 10000, locked; YES ask = 0.
    ASSERT_EQ(book.apply_delta(Side::no,10000,2), BookError::none);
    expect_state(book, {{0,1}}, {{10000,2},{6500,100},{6000,700}});
    // 14: replacement clears every old level on BOTH sides.
    ASSERT_EQ(snapshot(book, {{4200,155}}, {{5700,250}}), BookError::none);
    expect_state(book, {{4200,155}}, {{5700,250}});
    EXPECT_EQ(book.implied_yes_ask(), (Level{4300,250})); // 10000 - 5700.
    // 15: an empty replacement clears both ladders.
    ASSERT_EQ(snapshot(book, {}, {}), BookError::none);
    expect_state(book, {}, {});
}

TEST(BookCore, UnorderedSnapshotsZerosAndOneSidedReplacement) {
    Book book(grid());
    ASSERT_EQ(snapshot(book, {{1000,3},{9000,2},{5000,0},{3000,4}}, {{7000,8}}), BookError::none);
    expect_state(book, {{9000,2},{3000,4},{1000,3}}, {{7000,8}});
    ASSERT_EQ(snapshot(book, {}, {{8000,9}}), BookError::none);
    expect_state(book, {}, {{8000,9}});
    ASSERT_EQ(snapshot(book, {{2000,7}}, {}), BookError::none);
    expect_state(book, {{2000,7}}, {});
}

TEST(BookCore, AllGridSizesAndSparseBoundaryLevels) {
    const std::vector<std::vector<PriceRange>> grids{
        {{0,10000,100}}, {{0,10000,10}}, {{0,10000,1}},
        {{0,1000,10},{1000,9000,100},{9000,10000,10}}};
    for (const auto& bands : grids) {
        const auto price_grid = PriceGrid::from_ranges(bands).value();
        SCOPED_TRACE(price_grid.size());
        Book book(price_grid);
        // Adjacent levels around word boundaries, plus a far-away final level.
        // These remain behavior checks: depth and best must match actual prices.
        Levels ascending;
        for (const std::size_t i : {std::size_t{0},std::size_t{1},std::size_t{63},
                                   std::size_t{64},std::size_t{65},price_grid.size()-1}) {
            ascending.push_back({price_grid.price_of(i).value(), static_cast<std::int64_t>(i+1)});
        }
        ASSERT_EQ(book.apply_snapshot(ascending, ascending), BookError::none);
        Levels descending(ascending.rbegin(), ascending.rend());
        expect_state(book, descending, descending);
        while (!descending.empty()) {
            const auto level = descending.front();
            ASSERT_EQ(book.apply_delta(Side::yes,level.price,-level.quantity), BookError::none);
            ASSERT_EQ(book.apply_delta(Side::no,level.price,-level.quantity), BookError::none);
            descending.erase(descending.begin());
            expect_state(book, descending, descending);
        }
    }
}

TEST(BookCore, InvalidDeltasRequireSnapshotRecovery) {
    struct Case { Side side; std::int32_t price; std::int64_t delta; BookError error; };
    const auto min = std::numeric_limits<std::int64_t>::min();
    const auto max = std::numeric_limits<std::int64_t>::max();
    const std::array cases{
        Case{Side::yes,3000,-156,BookError::negative_quantity}, // 155 - 156 = -1.
        Case{Side::no,6000,-201,BookError::negative_quantity}, // 200 - 201 = -1.
        Case{Side::yes,4000,-1,BookError::negative_quantity}, // absent level: 0 - 1.
        Case{Side::no,6000,min,BookError::negative_quantity},
        Case{Side::yes,3000,max,BookError::quantity_overflow}, // max + 155 > max.
        Case{Side::no,6000,max,BookError::quantity_overflow},
        Case{Side::yes,3001,1,BookError::off_grid},
        Case{Side::no,6001,0,BookError::off_grid},
        Case{Side::yes,-1,1,BookError::off_grid},
        Case{Side::no,10001,1,BookError::off_grid},
        Case{Side::yes,std::numeric_limits<std::int32_t>::min(),1,BookError::off_grid},
        Case{Side::no,std::numeric_limits<std::int32_t>::max(),1,BookError::off_grid},
        Case{static_cast<Side>(99),3000,1,BookError::invalid_side}};
    for (const auto& item : cases) {
        SCOPED_TRACE(static_cast<int>(item.error));
        SCOPED_TRACE(item.price);
        Book book(grid());
        ASSERT_EQ(snapshot(book, {{3000,155}}, {{6000,200}}), BookError::none);
        EXPECT_EQ(book.apply_delta(item.side,item.price,item.delta), item.error);
        expect_invalid(book, item.error);
        EXPECT_EQ(book.apply_delta(Side::yes,3000,100), BookError::invalid_book);
        expect_invalid(book, item.error);
        ASSERT_EQ(snapshot(book, {{2000,1}}, {}), BookError::none);
        expect_state(book, {{2000,1}}, {});
        ASSERT_EQ(book.apply_delta(Side::yes,2000,1), BookError::none);
        expect_state(book, {{2000,2}}, {}); // 1 + 1 = 2 after recovery.
    }
}

TEST(BookCore, MalformedSnapshotsInvalidateBothSides) {
    struct Case { Levels levels; BookError error; };
    const std::vector<Case> cases{
        {{{1000,1},{3001,1}},BookError::off_grid},
        {{{1000,1},{-1,1}},BookError::off_grid},
        {{{1000,1},{10001,1}},BookError::off_grid},
        {{{1000,1},{2000,-1}},BookError::negative_quantity},
        {{{1000,1},{2000,std::numeric_limits<std::int64_t>::min()}},BookError::negative_quantity},
        {{{1000,1},{1000,2}},BookError::duplicate_price}};
    for (const auto& item : cases) {
        for (const auto side : {Side::yes,Side::no}) {
            Book book(grid());
            ASSERT_EQ(snapshot(book, {{3000,155}}, {{6000,200}}), BookError::none);
            const Levels other{{9000,2}};
            EXPECT_EQ(side == Side::yes ? book.apply_snapshot(item.levels,other)
                                       : book.apply_snapshot(other,item.levels), item.error);
            expect_invalid(book, item.error);
            ASSERT_EQ(snapshot(book, {}, {}), BookError::none);
            expect_state(book, {}, {});
        }
    }
}

TEST(BookCore, ExactInt64QuantityLimits) {
    Book book(grid(1));
    const auto max = std::numeric_limits<std::int64_t>::max();
    ASSERT_EQ(snapshot(book, {{1,max}}, {{9999,max-1}}), BookError::none);
    expect_state(book, {{1,max}}, {{9999,max-1}});
    ASSERT_EQ(book.apply_delta(Side::no,9999,1), BookError::none);
    expect_state(book, {{1,max}}, {{9999,max}});
    ASSERT_EQ(book.apply_delta(Side::yes,1,-max), BookError::none);
    expect_state(book, {}, {{9999,max}});
    ASSERT_EQ(book.apply_delta(Side::yes,10000,max), BookError::none);
    expect_state(book, {{10000,max}}, {{9999,max}});
    EXPECT_EQ(book.apply_delta(Side::yes,10000,1), BookError::quantity_overflow);
    expect_invalid(book, BookError::quantity_overflow);
}

// Independent, deliberately simple oracle: maps contain positive quantities only.
// Generated updates are bounded and valid; malformed inputs have explicit tests above.
struct ReferenceBook {
    std::map<std::int32_t,std::int64_t> yes, no;
    void delta(Side side, std::int32_t price, std::int64_t amount) {
        auto& levels = side == Side::yes ? yes : no;
        levels[price] += amount;
        if (levels[price] == 0) levels.erase(price);
    }
    static Levels descending(const std::map<std::int32_t,std::int64_t>& levels) {
        Levels result;
        for (auto i = levels.rbegin(); i != levels.rend(); ++i)
            result.push_back({i->first,i->second});
        return result;
    }
};

TEST(BookCore, TenThousandSeededDeltasAgainstMapAfterEveryStep) {
    const std::array bands{PriceRange{0,1000,10},PriceRange{1000,9000,100},
                           PriceRange{9000,10000,10}};
    const auto price_grid = PriceGrid::from_ranges(bands).value();
    Book book(price_grid);
    ASSERT_EQ(snapshot(book, {}, {}), BookError::none);
    ReferenceBook reference;
    std::mt19937 rng(0xB12E25U);
    std::size_t additions = 0, reductions = 0, removals = 0, zeros = 0;
    for (std::size_t step = 0; step < 10000; ++step) {
        SCOPED_TRACE(step);
        const auto side = rng()%2 == 0 ? Side::yes : Side::no;
        const auto price = price_grid.price_of(rng()%price_grid.size()).value();
        auto& levels = side == Side::yes ? reference.yes : reference.no;
        const auto found = levels.find(price);
        const auto old = found == levels.end() ? 0 : found->second;
        std::int64_t delta = 0;
        switch (rng()%4) {
        case 0: delta = static_cast<std::int64_t>(rng()%1000)+1; ++additions; break;
        case 1: delta = -(old/2); if (delta != 0) ++reductions; break;
        case 2: delta = -old; if (old != 0) ++removals; break;
        default: ++zeros; break;
        }
        reference.delta(side,price,delta);
        ASSERT_EQ(book.apply_delta(side,price,delta), BookError::none);
        expect_state(book, ReferenceBook::descending(reference.yes),
                     ReferenceBook::descending(reference.no));
        if (::testing::Test::HasFailure()) return;
    }
    EXPECT_GT(additions,0U);
    EXPECT_GT(reductions,0U);
    EXPECT_GT(removals,0U);
    EXPECT_GT(zeros,0U);
}

}  // namespace book_tests
