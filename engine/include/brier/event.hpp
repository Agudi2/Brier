#pragma once

#include "brier/book.hpp"
#include "brier/grid.hpp"

#include <cstdint>
#include <limits>
#include <type_traits>

namespace brier {

inline constexpr auto no_id = std::numeric_limits<std::uint32_t>::max();
enum class EventKind : std::uint8_t {
    session, market_meta, snapshot, delta, trade, ticker, gap, resync, rest_book
};
enum class SessionAction : std::uint8_t { none, open, close };
enum class TakerSide : std::uint8_t { none, yes, no };

// Ranges address owned arrays in FileParseResult. Empty ranges have count zero.
struct PoolRange {
    std::uint32_t offset = 0;
    std::uint32_t count = 0;
    bool operator==(const PoolRange&) const = default;
};

// Integer-only event record; variable-length data lives in owned result pools.
// -1 means an absent optional timestamp/identifier or price (zero is valid).
// no_id means an absent market/text ID. Irrelevant fields keep these defaults.
struct Event {
    EventKind kind = EventKind::session;
    SessionAction session_action = SessionAction::none;
    Side side = Side::yes;
    TakerSide taker_side = TakerSide::none;
    std::uint64_t line_index = 0;
    std::int64_t t_recv_ns = 0;
    std::int64_t t_mono_ns = -1;
    std::int64_t conn = -1;
    std::int64_t sid = -1;
    std::int64_t seq = -1;
    std::int64_t t_send_ns = -1;
    std::int64_t ts_ms = -1;
    std::int64_t expected_seq = -1;
    std::int64_t got_seq = -1;
    std::int64_t quantity = 0;
    std::uint32_t market_id = no_id;
    std::uint32_t trade_id = no_id;
    std::uint32_t event_ticker = no_id;
    std::uint32_t price_level_structure = no_id;
    std::int32_t price_ticks = -1; // YES price for trades and ticker updates.
    std::int32_t no_price_ticks = -1;
    std::int32_t yes_bid_ticks = -1;
    std::int32_t yes_ask_ticks = -1;
    PoolRange yes_levels;
    PoolRange no_levels;
    PoolRange markets;
    PoolRange price_ranges;
    bool operator==(const Event&) const = default;
};

static_assert(std::is_trivially_copyable_v<Event> && std::is_standard_layout_v<Event>);

}  // namespace brier
