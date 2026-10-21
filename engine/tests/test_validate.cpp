#include "brier/validate.hpp"

#include <algorithm>
#include <array>
#include <filesystem>
#include <random>
#include <vector>

#include <gtest/gtest.h>

namespace validation_tests {
using namespace brier;

void append(FileParseResult& input, Event event, std::int64_t time) {
    event.line_index = input.events.size();
    event.t_recv_ns = time;
    input.events.push_back(event);
    input.physical_lines = input.events.size();
}
FileParseResult base() {
    FileParseResult input;
    input.status = ParseStatus::ok;
    input.tickers = {"A", "B"};
    input.price_ranges = {{0,10000,100}};
    for (std::uint32_t id=0; id<2; ++id) {
        Event e; e.kind=EventKind::market_meta; e.market_id=id; e.price_ranges={0,1};
        append(input,e,0);
    }
    return input;
}
void book_event(FileParseResult& input, EventKind kind, std::int64_t time,
                const std::vector<Level>& yes, const std::vector<Level>& no,
                std::int64_t send=-1, std::uint32_t id=0) {
    Event e; e.kind=kind; e.market_id=id; e.t_send_ns=send;
    e.yes_levels={static_cast<std::uint32_t>(input.levels.size()),static_cast<std::uint32_t>(yes.size())};
    input.levels.insert(input.levels.end(),yes.begin(),yes.end());
    e.no_levels={static_cast<std::uint32_t>(input.levels.size()),static_cast<std::uint32_t>(no.size())};
    input.levels.insert(input.levels.end(),no.begin(),no.end());
    append(input,e,time);
}
void delta(FileParseResult& input, std::int64_t time, Side side, std::int32_t price,
           std::int64_t quantity) {
    Event e; e.kind=EventKind::delta; e.market_id=0; e.side=side;
    e.price_ticks=price; e.quantity=quantity; append(input,e,time);
}
FileParseResult golden() {
    return parse_file(std::filesystem::path(__FILE__).parent_path().parent_path().parent_path()
                      / "fixtures/replay/validation_windows.ndjson",3);
}

TEST(Validation, GoldenWindowsRatesAndDeterministicDiffLimits) {
    const auto input=golden();
    ASSERT_EQ(input.status,ParseStatus::ok);
    ASSERT_TRUE(input.malformed.empty());
    ASSERT_EQ(input.events.size(),12U);
    std::vector<ValidationDiff> expected{
        {0,6,4,{{4000,150},{2000,999}},{{6500,300}},{{4000,150},{2000,200}},{{6500,300}}},
        {0,7,4,{{5000,150},{2000,200}},{{6500,300}},{{4000,150},{2000,200}},{{6500,300}}}};
    // Hand arithmetic and source-line identities: VALIDATION_CONTRACT.md.
    for (const auto threads : {1U,2U,8U}) {
        for (const auto limit : {0U,1U,10U}) {
            const auto result=validate(input,{threads,limit});
            ASSERT_EQ(result.status,ValidationStatus::ok);
            EXPECT_EQ(result.samples,7U);
            EXPECT_EQ(result.compared,5U);
            EXPECT_EQ(result.excluded_invalid,2U);
            EXPECT_EQ(result.full_depth_matches,3U);
            EXPECT_EQ(result.top_of_book_matches,4U);
            EXPECT_EQ(result.mismatches,2U);
            ASSERT_TRUE(result.full_depth_match_rate);
            ASSERT_TRUE(result.top_of_book_match_rate);
            EXPECT_DOUBLE_EQ(*result.full_depth_match_rate,3.0/5.0);
            EXPECT_DOUBLE_EQ(*result.top_of_book_match_rate,4.0/5.0);
            auto limited=expected;
            limited.resize(std::min<std::size_t>(limit,2));
            EXPECT_EQ(result.diffs,limited);
        }
    }
}

TEST(Validation, InclusiveBoundariesPersistentStatesAndNoFutureLookahead) {
    // At t=20 quantity changes from 100 to 150. The old state is active on
    // [10,20), the new state on [20,infinity). No update is needed inside a window.
    struct Case { std::int64_t send, recv, qty; bool match; };
    const std::array cases{Case{15,19,100,true},Case{15,20,100,true},
        Case{20,20,100,false},Case{20,20,150,true},Case{21,25,150,true},
        Case{1,9,100,false}};
    for (const auto& c : cases) {
        auto input=base();
        if (c.recv>=10) book_event(input,EventKind::snapshot,10,{{4000,100}},{{6500,300}});
        if (c.recv>=20) delta(input,20,Side::yes,4000,50);
        book_event(input,EventKind::rest_book,c.recv,{{4000,c.qty}},{{6500,300}},c.send);
        // Later source lines at the same timestamp must never retroactively match.
        book_event(input,EventKind::snapshot,c.recv,{{4000,c.qty}},{{6500,300}});
        const auto r=validate(input);
        ASSERT_EQ(r.status,ValidationStatus::ok);
        EXPECT_EQ(r.full_depth_matches,c.match?1U:0U);
        EXPECT_EQ(r.top_of_book_matches,c.match?1U:0U);
        EXPECT_EQ(r.excluded_invalid,c.recv<10?1U:0U);
        EXPECT_EQ(r.mismatches,(!c.match && c.recv>=10)?1U:0U);
    }
}

TEST(Validation, NeverMixesSidesTimesOrMarketsAndIncludesTopQuantities) {
    auto input=base();
    book_event(input,EventKind::snapshot,10,{{4000,100}},{{6500,300}});
    book_event(input,EventKind::snapshot,10,{{4000,100}},{{6500,400}},-1,1);
    delta(input,20,Side::yes,4000,50); // A: 150/300.
    delta(input,20,Side::no,6500,100); // A: 150/400. Never A: 100/400.
    book_event(input,EventKind::rest_book,30,{{4000,100}},{{6500,400}},15);
    const auto r=validate(input,{8,10});
    ASSERT_EQ(r.status,ValidationStatus::ok);
    EXPECT_EQ(r.compared,1U);
    EXPECT_EQ(r.full_depth_matches,0U);
    EXPECT_EQ(r.top_of_book_matches,0U); // Prices alone match; quantities do not.
    ASSERT_EQ(r.diffs.size(),1U);
    EXPECT_EQ(r.diffs[0].state_line_index,5U); // Last delta, not another market's snapshot.
    EXPECT_EQ(r.diffs[0].engine_yes,(std::vector<Level>{{4000,150}}));
    EXPECT_EQ(r.diffs[0].engine_no,(std::vector<Level>{{6500,400}}));
}

TEST(Validation, CanonicalDepthNoLegPricingEmptyBooksAndZeroPrices) {
    auto input=base();
    book_event(input,EventKind::snapshot,10,{{4000,100},{2000,200}},{{6500,300}});
    book_event(input,EventKind::rest_book,20,{{2000,200},{0,0},{4000,100}},{{6500,300}},15);
    book_event(input,EventKind::rest_book,30,{{4000,100},{2000,200}},{{3500,300}},25);
    // NO 6500 is a bid, not a YES ask. Complementing REST NO bids would hide this mismatch.
    book_event(input,EventKind::snapshot,40,{},{});
    book_event(input,EventKind::rest_book,50,{}, {},45);
    book_event(input,EventKind::snapshot,60,{{0,1}},{{10000,2}});
    book_event(input,EventKind::rest_book,70,{{0,1}},{{10000,2}},65);
    book_event(input,EventKind::rest_book,80,{}, {},75);
    const auto r=validate(input);
    ASSERT_EQ(r.status,ValidationStatus::ok);
    EXPECT_EQ(r.compared,5U);
    EXPECT_EQ(r.full_depth_matches,3U); // Canonical, empty, and price-zero samples.
    EXPECT_EQ(r.top_of_book_matches,3U);
    EXPECT_EQ(r.mismatches,2U);
}

TEST(Validation, InvalidUpdatesAndMetadataRequireStreamRecovery) {
    auto input=base();
    book_event(input,EventKind::snapshot,10,{{4000,100}},{{6500,300}});
    delta(input,20,Side::yes,4000,-101); // 100-101 is invalid, never clamped to zero.
    book_event(input,EventKind::rest_book,30,{{4000,100}},{{6500,300}},25);
    delta(input,40,Side::yes,4000,1); // Dropped; REST did not restore validity.
    book_event(input,EventKind::rest_book,50,{{4000,101}},{{6500,300}},45);
    input.price_ranges.push_back({0,10000,10});
    Event meta; meta.kind=EventKind::market_meta; meta.market_id=0; meta.price_ranges={1,1};
    append(input,meta,60);
    book_event(input,EventKind::rest_book,70,{}, {},65); // Metadata is not recovery.
    book_event(input,EventKind::snapshot,80,{},{});
    book_event(input,EventKind::rest_book,90,{}, {},75); // Valid recovery inside window.
    const auto r=validate(input);
    ASSERT_EQ(r.status,ValidationStatus::ok);
    EXPECT_EQ(r.samples,4U);
    EXPECT_EQ(r.excluded_invalid,3U);
    EXPECT_EQ(r.compared,1U);
    EXPECT_EQ(r.full_depth_matches,1U);
    EXPECT_EQ(r.mismatches,0U);
}

TEST(Validation, EmptyAndExcludedOnlyRatesAreAbsent) {
    auto input=base();
    for (int samples=0; samples<2; ++samples) {
        const auto r=validate(input);
        ASSERT_EQ(r.status,ValidationStatus::ok);
        EXPECT_EQ(r.samples,static_cast<std::uint64_t>(samples));
        EXPECT_EQ(r.excluded_invalid,static_cast<std::uint64_t>(samples));
        EXPECT_EQ(r.compared,0U);
        EXPECT_FALSE(r.full_depth_match_rate);
        EXPECT_FALSE(r.top_of_book_match_rate);
        EXPECT_TRUE(r.diffs.empty());
        book_event(input,EventKind::rest_book,10,{}, {},5);
    }
}

TEST(Validation, InvalidInputAndOptionsReturnNoPartialReport) {
    auto good=base();
    book_event(good,EventKind::snapshot,10,{{4000,100}},{{6500,300}});
    book_event(good,EventKind::rest_book,20,{{4000,100}},{{6500,300}},15);
    EXPECT_EQ(validate(good,{0,10}).status,ValidationStatus::invalid_thread_count);
    for (int mode=0; mode<8; ++mode) {
        auto input=good;
        if (mode==0) input.status=ParseStatus::io_error;
        if (mode==1) input.events.back().t_send_ns=21;
        if (mode==2) input.events.back().market_id=99;
        if (mode==3) input.events.back().yes_levels.offset=999;
        if (mode==4) input.levels.back().quantity=-1;
        if (mode==5) input.levels.back().price=6501; // Off-grid REST level.
        if (mode==6) {
            const auto duplicate=input.levels.back();
            input.levels.push_back(duplicate);
            input.events.back().no_levels.count=2;
        }
        if (mode==7) input.events.back().t_recv_ns=9; // Clock regresses after snapshot.
        const auto r=validate(input);
        EXPECT_EQ(r.status,ValidationStatus::invalid_input) << mode;
        EXPECT_EQ(r.samples,0U);
        EXPECT_TRUE(r.diffs.empty());
        EXPECT_FALSE(r.full_depth_match_rate);
    }
}

TEST(Validation, SeededWindowMembershipMatchesIndependentQuantityOracle) {
    std::mt19937 rng(0xB818U);
    for (int trial=0; trial<80; ++trial) {
        auto input=base();
        std::array<std::int64_t,6> quantities{100};
        book_event(input,EventKind::snapshot,10,{{4000,100}},{{6500,300}});
        for (std::size_t i=1; i<quantities.size(); ++i) {
            const auto change=static_cast<std::int64_t>(rng()%20)+1;
            quantities[i]=quantities[i-1]+change;
            delta(input,10+static_cast<std::int64_t>(i)*10,Side::yes,4000,change);
        }
        const auto first=static_cast<std::size_t>(rng()%6);
        const auto chosen=static_cast<std::size_t>(rng()%6);
        // State i starts at 10+10*i; send is 1ns later, excluding prior states.
        const auto send=11+static_cast<std::int64_t>(first)*10;
        const auto quantity=trial%3==0 ? quantities.back()+1 : quantities[chosen];
        const bool match=trial%3!=0 && chosen>=first;
        book_event(input,EventKind::rest_book,70,{{4000,quantity}},{{6500,300}},send);
        for (const auto threads : {1U,2U,8U}) {
            const auto r=validate(input,{threads,1});
            ASSERT_EQ(r.status,ValidationStatus::ok);
            EXPECT_EQ(r.compared,1U);
            EXPECT_EQ(r.full_depth_matches,match?1U:0U);
            EXPECT_EQ(r.top_of_book_matches,match?1U:0U);
            EXPECT_EQ(r.mismatches,match?0U:1U);
        }
    }
}
}  // namespace validation_tests
