#include "brier/parse.hpp"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <limits>
#include <random>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>
#include <unistd.h>

#include <gtest/gtest.h>

namespace parse_tests {
using brier::EventKind;
using brier::Level;
using brier::FileParseResult;
using brier::ParseStatus;
constexpr std::array<std::size_t,5> chunk_counts{1,2,3,7,16};

std::filesystem::path fixture(std::string_view name) {
    return std::filesystem::path(__FILE__).parent_path().parent_path().parent_path()
        / "fixtures" / "replay" / name;
}

std::string read_file(const std::filesystem::path& path) {
    std::ifstream file(path, std::ios::binary);
    if (!file) throw std::runtime_error("parser test input missing");
    return {std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
}

class InputFile {
public:
    explicit InputFile(std::string_view content) {
        auto pattern = (std::filesystem::temp_directory_path() / "brier-parse-XXXXXX").string();
        const int fd = ::mkstemp(pattern.data());
        if (fd < 0) throw std::runtime_error("cannot create parser test input");
        ::close(fd);
        path_ = pattern;
        std::ofstream file(path_, std::ios::binary);
        file.write(content.data(), static_cast<std::streamsize>(content.size()));
        if (!file) throw std::runtime_error("cannot write parser test input");
    }
    ~InputFile() { std::error_code ignored; std::filesystem::remove(path_, ignored); }
    InputFile(const InputFile&) = delete;
    InputFile& operator=(const InputFile&) = delete;
    const std::filesystem::path& path() const { return path_; }
private:
    std::filesystem::path path_;
};

template<class T>
std::vector<T> slice(const std::vector<T>& pool, brier::PoolRange range) {
    if (range.offset > pool.size() || range.count > pool.size()-range.offset) {
        ADD_FAILURE() << "payload range outside owned pool";
        return {};
    }
    return {pool.begin()+range.offset, pool.begin()+range.offset+range.count};
}

std::string text(const FileParseResult& result, std::uint32_t id) {
    if (id >= result.texts.size()) { ADD_FAILURE() << "missing text ID"; return {}; }
    return result.texts[id];
}

void equivalent(const FileParseResult& a, const FileParseResult& b) {
    EXPECT_EQ(a.status,b.status);
    EXPECT_EQ(a.physical_lines,b.physical_lines);
    EXPECT_EQ(a.unknown_messages,b.unknown_messages);
    EXPECT_EQ(a.unknown_envelopes,b.unknown_envelopes);
    EXPECT_EQ(a.tickers,b.tickers);
    EXPECT_EQ(a.texts,b.texts);
    EXPECT_EQ(a.events,b.events);
    EXPECT_EQ(a.levels,b.levels);
    EXPECT_EQ(a.market_ids,b.market_ids);
    EXPECT_EQ(a.malformed,b.malformed);
    ASSERT_EQ(a.price_ranges.size(),b.price_ranges.size());
    for (std::size_t i=0; i<a.price_ranges.size(); ++i) {
        EXPECT_EQ(a.price_ranges[i].start,b.price_ranges[i].start);
        EXPECT_EQ(a.price_ranges[i].end,b.price_ranges[i].end);
        EXPECT_EQ(a.price_ranges[i].step,b.price_ranges[i].step);
    }
}

TEST(Parse, EveryEnvelopeKindAndExactPayloads) {
    const auto result = brier::parse_file(fixture("parse_all_kinds.ndjson"));
    ASSERT_EQ(result.status,ParseStatus::ok);
    EXPECT_EQ(result.physical_lines,16U);
    ASSERT_EQ(result.events.size(),13U); // 16 - 2 unknown messages - 1 unknown envelope.
    EXPECT_EQ(result.unknown_messages,2U);
    EXPECT_EQ(result.unknown_envelopes,1U);
    EXPECT_TRUE(result.malformed.empty());
    EXPECT_EQ(result.tickers,(std::vector<std::string>{"ZETA","ALPHA"}));
    const std::array kinds{EventKind::market_meta,EventKind::session,EventKind::snapshot,
        EventKind::snapshot,EventKind::delta,EventKind::trade,EventKind::ticker,
        EventKind::gap,EventKind::resync,EventKind::rest_book,EventKind::session,
        EventKind::delta,EventKind::ticker};
    const std::array<std::uint64_t,13> indices{0,1,2,3,4,5,6,7,8,9,12,14,15};
    const std::array<std::int64_t,13> times{100,100,99,101,102,103,104,105,106,107,110,112,113};
    const std::array<std::int64_t,13> mono{-1,200,201,202,203,204,205,-1,-1,-1,208,209,210};
    const std::array<std::int64_t,13> conns{-1,7,7,7,7,7,7,7,-1,-1,7,8,8};
    for (std::size_t i=0; i<kinds.size(); ++i) {
        SCOPED_TRACE(i);
        EXPECT_EQ(result.events[i].kind,kinds[i]);
        EXPECT_EQ(result.events[i].line_index,indices[i]);
        EXPECT_EQ(result.events[i].t_recv_ns,times[i]);
        EXPECT_EQ(result.events[i].t_mono_ns,mono[i]);
        EXPECT_EQ(result.events[i].conn,conns[i]);
    }
    const auto& meta = result.events[0];
    EXPECT_EQ(meta.market_id,0U);
    EXPECT_EQ(text(result,meta.event_ticker),"EVENT-Z");
    EXPECT_EQ(text(result,meta.price_level_structure),"tapered_deci_cent");
    const auto bands = slice(result.price_ranges,meta.price_ranges);
    ASSERT_EQ(bands.size(),3U);
    // Fixed-point dollars * 10000: 0.1 -> 1000; 0.001 -> 10; 0.01 -> 100.
    const std::array expected_bands{brier::PriceRange{0,1000,10},
        brier::PriceRange{1000,9000,100},brier::PriceRange{9000,10000,10}};
    for (std::size_t i=0; i<bands.size(); ++i) {
        EXPECT_EQ(bands[i].start,expected_bands[i].start);
        EXPECT_EQ(bands[i].end,expected_bands[i].end);
        EXPECT_EQ(bands[i].step,expected_bands[i].step);
    }
    EXPECT_EQ(result.events[1].session_action,brier::SessionAction::open);
    EXPECT_EQ(result.events[1].market_id,brier::no_id);
    const auto& snap = result.events[2];
    EXPECT_EQ(snap.market_id,1U);
    EXPECT_EQ(snap.sid,2);
    EXPECT_EQ(snap.seq,1);
    // Dollars * 10000, contracts * 100; original level order is retained.
    EXPECT_EQ(slice(result.levels,snap.yes_levels),
              (std::vector<Level>{{3000,155},{0,1},{10000,200}}));
    EXPECT_EQ(slice(result.levels,snap.no_levels),(std::vector<Level>{{6800,200}}));
    EXPECT_EQ(result.events[3].market_id,0U);
    EXPECT_EQ(result.events[3].sid,2);
    EXPECT_EQ(result.events[3].seq,2);
    EXPECT_TRUE(slice(result.levels,result.events[3].yes_levels).empty());
    EXPECT_TRUE(slice(result.levels,result.events[3].no_levels).empty());
    const auto& delta = result.events[4];
    EXPECT_EQ(delta.market_id,1U);
    EXPECT_EQ(delta.sid,2);
    EXPECT_EQ(delta.seq,3);
    EXPECT_EQ(delta.side,brier::Side::yes);
    EXPECT_EQ(delta.price_ticks,3000); // 0.3000 * 10000.
    EXPECT_EQ(delta.quantity,-155); // -1.55 * 100.
    const auto& trade = result.events[5];
    EXPECT_EQ(trade.market_id,1U);
    EXPECT_EQ(trade.sid,3);
    EXPECT_EQ(trade.seq,1);
    EXPECT_EQ(trade.price_ticks,3010); // 0.3010 * 10000.
    EXPECT_EQ(trade.no_price_ticks,6990); // 0.6990 * 10000.
    EXPECT_EQ(trade.quantity,155); // 1.55 * 100.
    EXPECT_EQ(text(result,trade.trade_id),"T-1");
    EXPECT_EQ(trade.taker_side,brier::TakerSide::no);
    EXPECT_EQ(trade.ts_ms,9007199254740993LL); // 2^53 + 1, cannot round through double.
    const auto& ticker = result.events[6];
    EXPECT_EQ(ticker.market_id,0U);
    EXPECT_EQ(ticker.sid,4);
    EXPECT_EQ(ticker.seq,-1);
    EXPECT_EQ(ticker.price_ticks,0);
    EXPECT_EQ(ticker.yes_bid_ticks,900); // 0.0900 * 10000.
    EXPECT_EQ(ticker.yes_ask_ticks,1000); // 0.1000 * 10000.
    EXPECT_EQ(ticker.ts_ms,123);
    const auto& gap = result.events[7];
    EXPECT_EQ(gap.sid,2);
    EXPECT_EQ(gap.expected_seq,4);
    EXPECT_EQ(gap.got_seq,6);
    EXPECT_EQ(gap.market_id,brier::no_id);
    EXPECT_EQ(slice(result.market_ids,gap.markets),(std::vector<std::uint32_t>{1,0}));
    EXPECT_EQ(slice(result.market_ids,result.events[8].markets),
              (std::vector<std::uint32_t>{0,1}));
    const auto& rest = result.events[9];
    EXPECT_EQ(rest.market_id,0U);
    EXPECT_EQ(rest.t_send_ns,105);
    EXPECT_EQ(slice(result.levels,rest.yes_levels),(std::vector<Level>{{990,350}}));
    EXPECT_EQ(slice(result.levels,rest.no_levels),(std::vector<Level>{{9000,1}}));
    // REST: 0.0990 * 10000 = 990, 3.50 * 100 = 350; 0.01 contracts = 1.
    EXPECT_EQ(result.events[10].session_action,brier::SessionAction::close);
    EXPECT_EQ(result.events[11].market_id,0U);
    EXPECT_EQ(result.events[11].sid,2);
    EXPECT_EQ(result.events[11].seq,1); // Parser preserves reconnect sequence reset.
    EXPECT_EQ(result.events[11].side,brier::Side::no);
    EXPECT_EQ(result.events[11].price_ticks,1); // 0.0001 * 10000.
    EXPECT_EQ(result.events[11].quantity,1); // 0.01 * 100.
    const auto& absent = result.events[12];
    EXPECT_EQ(absent.price_ticks,-1);
    EXPECT_EQ(absent.yes_bid_ticks,-1);
    EXPECT_EQ(absent.yes_ask_ticks,-1);
    EXPECT_EQ(absent.ts_ms,-1);
}

TEST(Parse, MalformedLinesSkippedWithPhysicalLineNumbersAndNoPartialPayloads) {
    for (const auto chunks : chunk_counts) {
        SCOPED_TRACE(chunks);
        const auto result = brier::parse_file(fixture("parse_malformed.ndjson"),chunks);
        ASSERT_EQ(result.status,ParseStatus::ok);
        EXPECT_EQ(result.physical_lines,13U);
        ASSERT_EQ(result.malformed.size(),10U); // Lines 2 through 11 inclusive.
        for (std::size_t i=0; i<10; ++i) EXPECT_EQ(result.malformed[i].line_number,i+2);
        ASSERT_EQ(result.events.size(),3U);
        EXPECT_EQ(result.events[0].line_index,0U);
        EXPECT_EQ(result.events[1].line_index,11U);
        EXPECT_EQ(result.events[2].line_index,12U);
        EXPECT_EQ(result.tickers,(std::vector<std::string>{"GOOD","NEXT"}));
        EXPECT_EQ(result.events[1].market_id,1U);
        EXPECT_TRUE(result.levels.empty()); // Reject partial snapshot/REST payloads.
        EXPECT_TRUE(result.price_ranges.empty());
        EXPECT_TRUE(result.texts.empty());
        EXPECT_EQ(result.market_ids,(std::vector<std::uint32_t>{0}));
        EXPECT_EQ(result.unknown_messages,0U); // Trailing garbage is malformed, not unknown.
    }
}

TEST(Parse, ParallelChunksMatchSingleChunkAcrossAllHandWrittenFixtures) {
    for (const auto name : {"parse_all_kinds.ndjson","parse_malformed.ndjson"}) {
        const auto path = fixture(name);
        const auto reference = brier::parse_file(path,1);
        ASSERT_EQ(reference.status,ParseStatus::ok);
        for (const auto chunks : chunk_counts) {
            SCOPED_TRACE(name);
            SCOPED_TRACE(chunks);
            equivalent(reference,brier::parse_file(path,chunks));
        }
    }
}

TEST(Parse, UnsupportedModeIsFatalEvenInALaterChunk) {
    for (const auto chunks : chunk_counts) {
        SCOPED_TRACE(chunks);
        EXPECT_EQ(brier::parse_file(fixture("parse_unsupported_mode.ndjson"),chunks).status,
                  ParseStatus::unsupported_pricing_mode);
        // Close lines must also be checked; new/unknown explicit modes are unsafe.
        for (const auto mode : {"yes_leg","future_mode"}) {
            InputFile file(std::string(R"({"v":1,"kind":"session","t_recv_ns":1,"conn":1,"event":"close","pricing_mode":")")
                + mode + "\"}\n");
            EXPECT_EQ(brier::parse_file(file.path(),chunks).status,
                      ParseStatus::unsupported_pricing_mode);
        }
    }
}

TEST(Parse, EmptySingleLineNoFinalNewlineAndCrlf) {
    constexpr auto line = R"({"v":1,"kind":"resync","t_recv_ns":9007199254740993,"tickers":["A"]})";
    for (const auto chunks : chunk_counts) {
        InputFile empty("");
        const auto e = brier::parse_file(empty.path(),chunks);
        ASSERT_EQ(e.status,ParseStatus::ok);
        EXPECT_EQ(e.physical_lines,0U);
        EXPECT_TRUE(e.events.empty());
        EXPECT_TRUE(e.malformed.empty());
        EXPECT_TRUE(e.tickers.empty());
        for (const auto ending : {"","\n","\r\n"}) {
            InputFile single(std::string(line)+ending);
            const auto result = brier::parse_file(single.path(),chunks);
            ASSERT_EQ(result.status,ParseStatus::ok);
            ASSERT_EQ(result.events.size(),1U);
            EXPECT_EQ(result.physical_lines,1U);
            EXPECT_EQ(result.events[0].line_index,0U);
            EXPECT_EQ(result.events[0].t_recv_ns,9007199254740993LL);
            EXPECT_TRUE(result.malformed.empty());
        }
        InputFile blank(std::string(line)+"\n\n");
        const auto result = brier::parse_file(blank.path(),chunks);
        ASSERT_EQ(result.status,ParseStatus::ok);
        EXPECT_EQ(result.physical_lines,2U); // Final LF terminates blank physical line 2.
        ASSERT_EQ(result.malformed.size(),1U);
        EXPECT_EQ(result.malformed[0].line_number,2U);
    }
    auto source = read_file(fixture("parse_all_kinds.ndjson"));
    ASSERT_EQ(source.back(),'\n');
    source.pop_back();
    InputFile unterminated(source);
    const auto reference = brier::parse_file(fixture("parse_all_kinds.ndjson"));
    ASSERT_EQ(reference.status,ParseStatus::ok);
    for (const auto chunks : chunk_counts) equivalent(reference,brier::parse_file(unterminated.path(),chunks));
}

std::string ws_line(std::string_view type, std::string_view payload,
                    std::string_view sequence = ",\"seq\":1") {
    return std::string(R"({"v":1,"kind":"ws","t_recv_ns":1,"t_mono_ns":2,"conn":1,"msg":{"type":")")
        + std::string(type) + "\",\"sid\":1" + std::string(sequence)
        + ",\"msg\":" + std::string(payload) + "}}";
}

TEST(Parse, RejectsMalformedStructureWithoutAbortingFollowingLines) {
    const std::vector<std::string> bad{
        "null", "true", "1", "{", "{}", "{\"v\":1,\"kind\":\"ws\",\"t_recv_ns\":1,}",
        R"({"v":true,"kind":"resync","t_recv_ns":1,"tickers":["GHOST"]})",
        R"({"v":1,"kind":"resync","t_recv_ns":1.5,"tickers":["GHOST"]})",
        R"({"v":1,"kind":"resync","t_recv_ns":9223372036854775808,"tickers":["GHOST"]})",
        R"({"v":1,"kind":"resync","t_recv_ns":1,"tickers":[""]})",
        R"({"v":1,"kind":"gap","t_recv_ns":1,"conn":1,"sid":"1","expected_seq":1,"got_seq":3,"tickers":["GHOST"]})",
        R"({"v":1,"kind":"ws","t_recv_ns":1,"conn":1,"msg":[]})",
        ws_line("orderbook_delta",R"({"market_ticker":"GHOST","price_dollars":"0.3000","delta_fp":"1.00","side":"maybe"})"),
        ws_line("orderbook_delta",R"({"market_ticker":"GHOST","price_dollars":"0.3000","delta_fp":"1.00","side":"yes"})",""),
        ws_line("orderbook_delta",R"({"market_ticker":"GHOST","price_dollars":0.3,"delta_fp":"1.00","side":"yes"})"),
        ws_line("orderbook_delta",R"({"market_ticker":"GHOST","price_dollars":"0.3000","delta_fp":1,"side":"yes"})"),
        ws_line("orderbook_snapshot",R"({"market_ticker":"GHOST","yes_dollars_fp":[["0.3000"]],"no_dollars_fp":[]})"),
        ws_line("orderbook_snapshot",R"({"market_ticker":"GHOST","yes_dollars_fp":[["0.3000","1.00","extra"]],"no_dollars_fp":[]})"),
        ws_line("orderbook_snapshot",R"({"market_ticker":"GHOST","yes_dollars_fp":5,"no_dollars_fp":[]})"),
        R"({"v":1,"kind":"rest_book","t_recv_ns":1,"t_send_ns":2,"ticker":"GHOST","book":{"orderbook_fp":{"yes_dollars":[],"no_dollars":[]}}})",
        R"({"v":1,"kind":"rest_book","t_recv_ns":1,"t_send_ns":0,"ticker":"GHOST","book":{}})",
        R"({"v":1,"kind":"session","t_recv_ns":1,"conn":1,"event":"open"})",
        R"({"v":1,"kind":"session","t_recv_ns":1,"conn":1,"event":"other","pricing_mode":"no_leg"})",
        R"({"v":1,"kind":"market_meta","t_recv_ns":1,"ticker":"GHOST","price_ranges":[{"start":"0","end":"1","step":"oops"}]})"
    };
    const std::string good = R"({"v":1,"kind":"resync","t_recv_ns":2,"tickers":["OK"]})";
    for (const auto& line : bad) {
        SCOPED_TRACE(line);
        InputFile file(line+"\n"+good);
        const auto result = brier::parse_file(file.path(),3);
        ASSERT_EQ(result.status,ParseStatus::ok);
        ASSERT_EQ(result.malformed.size(),1U);
        EXPECT_EQ(result.malformed[0].line_number,1U);
        ASSERT_EQ(result.events.size(),1U);
        EXPECT_EQ(result.events[0].line_index,1U);
        EXPECT_EQ(result.tickers,(std::vector<std::string>{"OK"}));
    }
}

TEST(Parse, StrictFixedPointAndSignedInt64Boundaries) {
    struct Case { const char* price; const char* quantity; bool valid; std::int32_t ticks; std::int64_t qty; };
    const auto max = std::numeric_limits<std::int64_t>::max();
    const auto min = std::numeric_limits<std::int64_t>::min();
    const std::array cases{
        Case{"0","0",true,0,0}, Case{"1.0000","0.01",true,10000,1},
        Case{"0.0001","-1.55",true,1,-155},
        // 92233720368547758 * 100 + 7 = INT64_MAX; negative ...08 = INT64_MIN.
        Case{"0.9999","92233720368547758.07",true,9999,max},
        Case{"0.5","-92233720368547758.08",true,5000,min},
        Case{"1.0001","1",false,0,0}, Case{"-0.01","1",false,0,0},
        Case{"0.12345","1",false,0,0}, Case{".5","1",false,0,0},
        Case{"1e-2","1",false,0,0}, Case{" 0.1","1",false,0,0},
        Case{"0.1","0.001",false,0,0}, Case{"0.1","92233720368547758.08",false,0,0},
        Case{"0.1","-92233720368547758.09",false,0,0}, Case{"0.1","+1",false,0,0}};
    for (const auto& item : cases) {
        SCOPED_TRACE(item.price);
        SCOPED_TRACE(item.quantity);
        const auto payload = std::string("{\"market_ticker\":\"A\",\"side\":\"no\",\"price_dollars\":\"")
            + item.price + "\",\"delta_fp\":\"" + item.quantity + "\"}";
        InputFile file(ws_line("orderbook_delta",payload));
        const auto result = brier::parse_file(file.path(),7);
        ASSERT_EQ(result.status,ParseStatus::ok);
        if (item.valid) {
            ASSERT_EQ(result.events.size(),1U);
            EXPECT_TRUE(result.malformed.empty());
            EXPECT_EQ(result.events[0].price_ticks,item.ticks);
            EXPECT_EQ(result.events[0].quantity,item.qty);
            EXPECT_EQ(result.events[0].side,brier::Side::no);
        } else {
            EXPECT_TRUE(result.events.empty());
            ASSERT_EQ(result.malformed.size(),1U);
            EXPECT_EQ(result.malformed[0].line_number,1U);
            EXPECT_TRUE(result.tickers.empty());
        }
    }
    // Absolute quantities are never signed, even though deltas are.
    for (const auto type : {"orderbook_snapshot","trade"}) {
        const auto payload = std::string(type) == "trade"
            ? R"({"market_ticker":"A","yes_price_dollars":"0.5","no_price_dollars":"0.5","count_fp":"-0.01"})"
            : R"({"market_ticker":"A","yes_dollars_fp":[["0.5","-0.01"]],"no_dollars_fp":[]})";
        InputFile file(ws_line(type,payload));
        const auto result = brier::parse_file(file.path());
        ASSERT_EQ(result.status,ParseStatus::ok);
        EXPECT_TRUE(result.events.empty());
        EXPECT_EQ(result.malformed.size(),1U);
    }
}

TEST(Parse, DenseTickerIdsDecodeEscapesAndOwnPayloadAfterFileRemoval) {
    FileParseResult result;
    {
        InputFile file(
            R"({"v":1,"kind":"resync","t_recv_ns":1,"tickers":["Z","KX\u002e5","caf\u00e9","Z"]})" "\n"
            R"({"v":1,"kind":"resync","t_recv_ns":2,"tickers":["KX.5","café","A"]})" "\n"
            R"({"v":1,"kind":"ws","t_recv_ns":3,"conn":1,"msg":{"type":"trade","sid":1,"msg":{"market_ticker":"A","trade_id":"quote\"/slash\\","yes_price_dollars":"0.5","no_price_dollars":"0.5","count_fp":"0.01","taker_side":"yes"}}})");
        result = brier::parse_file(file.path(),16);
    }
    ASSERT_EQ(result.status,ParseStatus::ok);
    EXPECT_EQ(result.tickers,(std::vector<std::string>{"Z","KX.5","café","A"}));
    ASSERT_EQ(result.events.size(),3U);
    EXPECT_EQ(slice(result.market_ids,result.events[0].markets),
              (std::vector<std::uint32_t>{0,1,2,0}));
    EXPECT_EQ(slice(result.market_ids,result.events[1].markets),
              (std::vector<std::uint32_t>{1,2,3}));
    const auto& trade = result.events[2];
    EXPECT_EQ(trade.market_id,3U);
    EXPECT_EQ(trade.seq,-1);
    EXPECT_EQ(trade.ts_ms,-1);
    EXPECT_EQ(trade.taker_side,brier::TakerSide::yes);
    EXPECT_EQ(text(result,trade.trade_id),"quote\"/slash\\");
}

TEST(Parse, UnknownTypesCountedAndIgnoredWithoutInventingMarkets) {
    InputFile file(
        R"({"v":1,"kind":"ws","t_recv_ns":1,"conn":1,"msg":{"type":"subscribed","msg":{"sid":1}}})" "\n"
        R"({"v":1,"kind":"ws","t_recv_ns":2,"conn":1,"msg":{"type":"future_public","msg":{"market_ticker":"GHOST","price_dollars":"not a known schema"}}})" "\n"
        R"({"v":1,"kind":"future_kind","t_recv_ns":3,"ticker":"GHOST"})" "\n"
        R"({"v":1,"kind":"resync","t_recv_ns":4,"tickers":["A"]})");
    const auto result = brier::parse_file(file.path(),16);
    ASSERT_EQ(result.status,ParseStatus::ok);
    EXPECT_EQ(result.physical_lines,4U);
    EXPECT_EQ(result.unknown_messages,2U);
    EXPECT_EQ(result.unknown_envelopes,1U);
    EXPECT_TRUE(result.malformed.empty());
    EXPECT_EQ(result.tickers,(std::vector<std::string>{"A"}));
    ASSERT_EQ(result.events.size(),1U);
    EXPECT_EQ(result.events[0].line_index,3U);
}

TEST(Parse, IoAndChunkErrorsAreResults) {
    InputFile file("");
    EXPECT_EQ(brier::parse_file(file.path(),0).status,ParseStatus::invalid_chunk_count);
    auto absent = file.path();
    absent += ".missing";
    EXPECT_EQ(brier::parse_file(absent).status,ParseStatus::io_error);
}

TEST(Parse, SeededVariableLengthInputHasExactEventsAcrossChunkBoundaries) {
    std::mt19937 rng(20261016);
    std::string source;
    std::vector<brier::Event> expected;
    std::vector<std::uint64_t> bad_lines;
    std::uint64_t unknown = 0;
    for (std::uint64_t i=0; i<301; ++i) {
        if (i%17 == 0) {
            source += "{broken";
            bad_lines.push_back(i+1);
        } else if (i%13 == 0) {
            source += ws_line("future_public","{\"ignored\":true}");
            ++unknown;
        } else {
            const auto ticks = static_cast<std::int32_t>(rng()%10001);
            const auto qty = static_cast<std::int64_t>(rng()%20001)-10000;
            const auto side = rng()%2 == 0 ? brier::Side::yes : brier::Side::no;
            // Generate decimal strings with integer division; expected ticks/qty
            // are the independently chosen integers, never parsed by production.
            auto decimal = [](std::int64_t value, std::int64_t scale, std::size_t digits) {
                const bool negative = value < 0;
                const auto magnitude = negative ? -value : value;
                auto fractional = std::to_string(magnitude%scale);
                return std::string(negative ? "-" : "") + std::to_string(magnitude/scale)
                    + "." + std::string(digits-fractional.size(),'0') + fractional;
            };
            const auto recv = static_cast<std::int64_t>(i%5); // Tied, out-of-order timestamps.
            const auto market = static_cast<std::uint32_t>(expected.size()%4);
            auto payload = std::string("{\"side\":\"") + (side == brier::Side::yes ? "yes" : "no")
                + "\",\"delta_fp\":\"" + decimal(qty,100,2) + "\",\"extra\":\""
                + std::string(i == 149 ? 70001 : rng()%150,'x')
                + "\",\"market_ticker\":\"M" + std::to_string(market)
                + "\",\"price_dollars\":\"" + decimal(ticks,10000,4) + "\"}";
            // Put msg before envelope metadata to avoid relying on object field order.
            source += "{\"msg\":{\"msg\":" + payload
                + ",\"seq\":" + std::to_string(i+1) + ",\"sid\":9,\"type\":\"orderbook_delta\"},"
                + "\"conn\":2,\"t_mono_ns\":" + std::to_string(i+100)
                + ",\"t_recv_ns\":" + std::to_string(recv) + ",\"kind\":\"ws\",\"v\":1}";
            brier::Event event;
            event.kind = EventKind::delta;
            event.line_index = i;
            event.t_recv_ns = recv;
            event.t_mono_ns = static_cast<std::int64_t>(i+100);
            event.conn = 2;
            event.sid = 9;
            event.seq = static_cast<std::int64_t>(i+1);
            event.market_id = market;
            event.side = side;
            event.price_ticks = ticks;
            event.quantity = qty;
            expected.push_back(event);
        }
        if (i != 300) source += i%2 == 0 ? "\r\n" : "\n";
    }
    InputFile file(source); // Deliberately no final newline.
    const auto reference = brier::parse_file(file.path(),1);
    ASSERT_EQ(reference.status,ParseStatus::ok);
    EXPECT_EQ(reference.physical_lines,301U);
    EXPECT_EQ(reference.events,expected);
    EXPECT_EQ(reference.tickers,(std::vector<std::string>{"M0","M1","M2","M3"}));
    EXPECT_EQ(reference.unknown_messages,unknown);
    ASSERT_EQ(reference.malformed.size(),bad_lines.size());
    for (std::size_t i=0; i<bad_lines.size(); ++i)
        EXPECT_EQ(reference.malformed[i].line_number,bad_lines[i]);
    for (const auto chunks : chunk_counts) {
        SCOPED_TRACE(chunks);
        equivalent(reference,brier::parse_file(file.path(),chunks));
    }
}

}  // namespace parse_tests
