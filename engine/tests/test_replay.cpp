#include "brier/replay.hpp"

#include <algorithm>
#include <array>
#include <cstdint>
#include <filesystem>
#include <limits>
#include <random>
#include <tuple>
#include <vector>

#include <gtest/gtest.h>

namespace replay_tests {
using brier::BookError;
using brier::Event;
using brier::EventKind;
using brier::FileParseResult;
using brier::Level;
using brier::ReplayStatus;
using brier::Side;
using brier::TobRecord;
constexpr std::array<std::size_t,3> threads{1,2,8};

std::filesystem::path golden() {
    return std::filesystem::path(__FILE__).parent_path().parent_path().parent_path()
        / "fixtures/replay/golden_small.ndjson";
}

void push(FileParseResult& input, Event event) {
    event.line_index = input.events.size();
    event.t_recv_ns = static_cast<std::int64_t>(input.events.size());
    input.events.push_back(event);
    input.physical_lines = input.events.size();
}

FileParseResult empty_market() {
    FileParseResult input;
    input.status = brier::ParseStatus::ok;
    input.tickers = {"A"};
    return input;
}

void metadata(FileParseResult& input, std::int32_t step = 100, std::uint32_t id = 0) {
    Event e;
    e.kind = EventKind::market_meta;
    e.market_id = id;
    e.price_ranges = {static_cast<std::uint32_t>(input.price_ranges.size()),1};
    input.price_ranges.push_back({0,10000,step});
    push(input,e);
}

void snapshot(FileParseResult& input, const std::vector<Level>& yes,
              const std::vector<Level>& no, std::uint32_t id = 0) {
    Event e;
    e.kind = EventKind::snapshot;
    e.market_id = id;
    e.yes_levels = {static_cast<std::uint32_t>(input.levels.size()),static_cast<std::uint32_t>(yes.size())};
    input.levels.insert(input.levels.end(),yes.begin(),yes.end());
    e.no_levels = {static_cast<std::uint32_t>(input.levels.size()),static_cast<std::uint32_t>(no.size())};
    input.levels.insert(input.levels.end(),no.begin(),no.end());
    push(input,e);
}

void delta(FileParseResult& input, Side side, std::int32_t price, std::int64_t qty,
           std::uint32_t id = 0) {
    Event e;
    e.kind = EventKind::delta;
    e.market_id = id;
    e.side = side;
    e.price_ticks = price;
    e.quantity = qty;
    push(input,e);
}

TEST(Replay, GoldenBooksCountersAndChangeOnlyTopOfBook) {
    const auto input = brier::parse_file(golden(),3);
    ASSERT_EQ(input.status,brier::ParseStatus::ok);
    ASSERT_TRUE(input.malformed.empty());
    ASSERT_EQ(input.tickers,(std::vector<std::string>{"B","A","C"}));
    // See tests/engine/REPLAY_CONTRACT.md for the line-by-line hand calculation.
    // Ask = 10000 - opposite best bid; flags = valid(1) | crossed(2) | locked(4).
    const std::vector<TobRecord> expected{
        {110,0,4000,5000,200,300,1,5},
        {110,1,3000,3200,155,200,1,6},
        {111,2,1000,2000,100,100,1,7},
        {113,0,4000,5000,250,300,1,9}, // 200 + 50 = 250.
        {114,1,3000,3200,100,200,1,10}, // 155 - 55 = 100.
        {120,0,-1,-1,0,0,0,12}, {120,1,-1,-1,0,0,0,12},
        {124,2,1000,2000,150,100,1,16}, // C stays valid: 100 + 50 = 150.
        {126,1,3100,3100,250,400,5,18}, // 3100 + 6900 = 10000.
        {127,1,3100,3000,250,1,3,19}, // 3100 + 7000 > 10000.
        {128,1,3100,3100,250,400,5,20},
        {130,0,4500,4600,100,200,1,22}, // 10000 - 5400 = 4600.
        {131,0,-1,4600,0,200,1,23},
        {132,0,-1,-1,0,0,1,24}, // Empty but valid differs from gap invalidity.
        {133,0,0,0,1,2,5,25}}; // Zero prices exist; 0 + 10000 is locked.
    for (const auto n : threads) {
        SCOPED_TRACE(n);
        const auto result = brier::replay(input,{n,true});
        ASSERT_EQ(result.status,ReplayStatus::ok);
        ASSERT_EQ(result.markets.size(),3U);
        EXPECT_EQ(result.tob,expected);
        const std::array<std::uint64_t,3> snaps{3,2,1}, applied{3,5,1}, dropped{1,3,0};
        const std::array yes{Level{0,1},Level{3100,250},Level{1000,150}};
        const std::array no{Level{10000,2},Level{6900,400},Level{8000,100}};
        for (std::size_t i=0; i<3; ++i) {
            const auto& m = result.markets[i];
            EXPECT_EQ(m.market_id,i);
            EXPECT_TRUE(m.valid);
            EXPECT_EQ(m.error,BookError::none);
            EXPECT_EQ(m.snapshots,snaps[i]);
            EXPECT_EQ(m.applied_deltas,applied[i]);
            EXPECT_EQ(m.dropped_deltas,dropped[i]);
            EXPECT_EQ(m.invalid_updates,0U);
            EXPECT_EQ(m.yes,(std::vector<Level>{yes[i]}));
            EXPECT_EQ(m.no,(std::vector<Level>{no[i]}));
        }
        const auto without_tob = brier::replay(input,{n,false});
        ASSERT_EQ(without_tob.status,ReplayStatus::ok);
        EXPECT_EQ(without_tob.markets,result.markets);
        EXPECT_TRUE(without_tob.tob.empty());
    }
}

TEST(Replay, InvalidBookUpdatesDropDeltasUntilValidSnapshot) {
    auto input = empty_market();
    metadata(input);
    snapshot(input,{{3000,100}},{{6000,200}});
    delta(input,Side::yes,3000,-101); // 100 - 101 = -1: invalidate, never clamp.
    delta(input,Side::yes,3000,10); // Drop.
    snapshot(input,{{5000,100}},{{4000,100}});
    input.events.back().kind = EventKind::rest_book; // Must not restore validity.
    delta(input,Side::yes,3000,20); // Drop after REST too.
    snapshot(input,{{3000,100}},{{6000,200}});
    delta(input,Side::yes,3001,1); // Off the cent grid.
    snapshot(input,{{3000,1},{3000,2}},{}); // Duplicate price is another failed update.
    snapshot(input,{},{});
    delta(input,Side::yes,0,std::numeric_limits<std::int64_t>::max());
    delta(input,Side::yes,0,1); // INT64_MAX + 1 must not wrap.
    delta(input,Side::yes,0,1); // Drop while invalid.
    const auto invalid = brier::replay(input,{2,true});
    ASSERT_EQ(invalid.status,ReplayStatus::ok);
    ASSERT_EQ(invalid.markets.size(),1U);
    EXPECT_FALSE(invalid.markets[0].valid);
    EXPECT_EQ(invalid.markets[0].error,BookError::quantity_overflow);
    EXPECT_TRUE(invalid.markets[0].yes.empty());
    EXPECT_TRUE(invalid.markets[0].no.empty());
    snapshot(input,{{4000,1}},{{6000,2}});
    for (const auto n : threads) {
        const auto result = brier::replay(input,{n,true});
        ASSERT_EQ(result.status,ReplayStatus::ok);
        ASSERT_EQ(result.markets.size(),1U);
        const auto& m = result.markets[0];
        EXPECT_TRUE(m.valid);
        EXPECT_EQ(m.error,BookError::none);
        EXPECT_EQ(m.snapshots,4U); // Successful snapshots at lines 1,6,9,13.
        EXPECT_EQ(m.applied_deltas,1U); // Only the INT64_MAX insertion succeeds.
        EXPECT_EQ(m.dropped_deltas,3U); // Lines 3,5,12.
        EXPECT_EQ(m.invalid_updates,4U); // Lines 2,7,8,11.
        EXPECT_EQ(m.yes,(std::vector<Level>{{4000,1}}));
        EXPECT_EQ(m.no,(std::vector<Level>{{6000,2}}));
        EXPECT_EQ(result.tob.size(),8U); // Repeated invalidity does not emit twice.
    }
}

TEST(Replay, MetadataIsRequiredAndGridChangesRequireFreshSnapshot) {
    auto input = empty_market();
    snapshot(input,{{3000,100}},{}); // No guessed/default grid.
    delta(input,Side::yes,3000,1);
    metadata(input);
    delta(input,Side::yes,3000,1); // Metadata alone cannot restore synchronization.
    snapshot(input,{{3000,100}},{{6000,100}});
    metadata(input); // Identical metadata keeps synchronization.
    delta(input,Side::yes,3000,1);
    metadata(input,10); // Changed grid invalidates.
    delta(input,Side::yes,3000,1); // Drop.
    snapshot(input,{{3001,200}},{}); // Still off-grid at step 10.
    snapshot(input,{{3010,200}},{{6980,300}});
    delta(input,Side::yes,3010,1); // 200 + 1 = 201.
    const auto result = brier::replay(input,{8,true});
    ASSERT_EQ(result.status,ReplayStatus::ok);
    ASSERT_EQ(result.markets.size(),1U);
    const auto& m = result.markets[0];
    EXPECT_TRUE(m.valid);
    EXPECT_EQ(m.snapshots,2U);
    EXPECT_EQ(m.applied_deltas,2U);
    EXPECT_EQ(m.dropped_deltas,3U);
    EXPECT_EQ(m.invalid_updates,2U);
    EXPECT_EQ(m.yes,(std::vector<Level>{{3010,201}}));
    EXPECT_EQ(m.no,(std::vector<Level>{{6980,300}}));
    ASSERT_EQ(result.tob.size(),5U); // Initial, qty change, invalidate, restore, qty change.
    EXPECT_EQ(result.tob[2].flags,0U);
    EXPECT_EQ(result.tob.back().yes_ask,3020); // 10000 - 6980.
}

TEST(Replay, ResyncInvalidatesAndAskQuantityChangesEmit) {
    auto input = empty_market();
    metadata(input);
    snapshot(input,{{3000,100}},{{6000,200}});
    delta(input,Side::no,6000,5); // Same ask price, quantity 200 + 5 = 205.
    delta(input,Side::no,1000,10); // Lower NO bid does not affect YES ask.
    Event resync;
    resync.kind = EventKind::resync;
    resync.markets = {0,2};
    input.market_ids = {0,0}; // Duplicate membership must not duplicate rows/counters.
    push(input,resync);
    delta(input,Side::yes,3000,10);
    snapshot(input,{{3000,100}},{{6000,205}});
    const auto result = brier::replay(input,{2,true});
    ASSERT_EQ(result.status,ReplayStatus::ok);
    ASSERT_EQ(result.markets.size(),1U);
    EXPECT_EQ(result.markets[0].applied_deltas,2U);
    EXPECT_EQ(result.markets[0].dropped_deltas,1U);
    EXPECT_EQ(result.tob,(std::vector<TobRecord>{
        {1,0,3000,4000,100,200,1,1}, {2,0,3000,4000,100,205,1,2},
        {4,0,-1,-1,0,0,0,4}, {6,0,3000,4000,100,205,1,6}}));
}

TEST(Replay, EmptyInputAndInvalidOptions) {
    FileParseResult input;
    input.status = brier::ParseStatus::ok;
    EXPECT_EQ(brier::replay(input,{0,true}).status,ReplayStatus::invalid_thread_count);
    for (const auto n : threads) {
        const auto result = brier::replay(input,{n,true});
        EXPECT_EQ(result.status,ReplayStatus::ok);
        EXPECT_TRUE(result.markets.empty());
        EXPECT_TRUE(result.tob.empty());
    }
    input.status = brier::ParseStatus::unsupported_pricing_mode;
    const auto result = brier::replay(input);
    EXPECT_EQ(result.status,ReplayStatus::invalid_input);
    EXPECT_TRUE(result.markets.empty());
    EXPECT_TRUE(result.tob.empty());
}

TEST(Replay, SeededSkewedMarketsPreserveSourceOrderAndMergeDeterministically) {
    FileParseResult input;
    input.status = brier::ParseStatus::ok;
    std::array<std::int64_t,37> quantities{};
    std::array<std::uint64_t,37> counts{};
    std::vector<TobRecord> expected;
    for (std::uint32_t id=0; id<37; ++id) {
        input.tickers.push_back("M"+std::to_string(id));
        metadata(input,100,id);
        snapshot(input,{{5000,1}},{{4000,10}},id);
        auto& event = input.events.back();
        event.t_recv_ns = static_cast<std::int64_t>(event.line_index%7);
        quantities[id] = 1;
        expected.push_back({event.t_recv_ns,id,5000,6000,1,10,1,event.line_index});
    }
    std::mt19937 rng(0xB71E27U);
    for (std::size_t i=0; i<2000; ++i) {
        const std::uint32_t id = rng()%2 == 0 ? 0 : rng()%37; // Skew towards market 0.
        const auto amount = static_cast<std::int64_t>(rng()%7)+1;
        delta(input,Side::yes,5000,amount,id);
        auto& event = input.events.back();
        event.t_recv_ns = static_cast<std::int64_t>(event.line_index%7);
        quantities[id] += amount; // Independent arithmetic oracle: 1 + sum of positive deltas.
        ++counts[id];
        expected.push_back({event.t_recv_ns,id,5000,6000,quantities[id],10,1,event.line_index});
    }
    const auto key = [](const TobRecord& row) {
        return std::tuple{row.t_recv_ns,row.line_index,row.market_id};
    };
    std::sort(expected.begin(),expected.end(),[&](const auto& a,const auto& b) { return key(a)<key(b); });
    for (const auto n : threads) {
        SCOPED_TRACE(n);
        const auto result = brier::replay(input,{n,true});
        ASSERT_EQ(result.status,ReplayStatus::ok);
        ASSERT_EQ(result.markets.size(),37U);
        EXPECT_EQ(result.tob,expected);
        for (std::size_t id=0; id<37; ++id) {
            const auto& m = result.markets[id];
            EXPECT_TRUE(m.valid);
            EXPECT_EQ(m.market_id,id);
            EXPECT_EQ(m.snapshots,1U);
            EXPECT_EQ(m.applied_deltas,counts[id]);
            EXPECT_EQ(m.dropped_deltas,0U);
            EXPECT_EQ(m.invalid_updates,0U);
            EXPECT_EQ(m.yes,(std::vector<Level>{{5000,quantities[id]}}));
            EXPECT_EQ(m.no,(std::vector<Level>{{4000,10}}));
        }
    }
}

}  // namespace replay_tests
