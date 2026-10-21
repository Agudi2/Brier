#pragma once

#include "brier/parse.hpp"

#include <cstddef>
#include <cstdint>
#include <vector>

namespace brier {

enum class ReplayStatus { ok, invalid_thread_count, invalid_input, not_implemented };
struct ReplayOptions { std::size_t threads = 1; bool tob = true; };
inline constexpr std::uint32_t tob_valid = 1, tob_crossed = 2, tob_locked = 4;

// On disk: little-endian fields through flags, packed into 40 bytes; line_index
// is an internal merge key and is not serialized. Never dump native struct bytes.
struct TobRecord {
    std::int64_t t_recv_ns = 0;
    std::uint32_t market_id = 0;
    std::int32_t yes_bid = -1, yes_ask = -1;
    std::int64_t bid_qty = 0, ask_qty = 0;
    std::uint32_t flags = 0;
    std::uint64_t line_index = 0;
    bool operator==(const TobRecord&) const = default;
};
struct MarketReplay {
    std::uint32_t market_id = no_id;
    bool valid = false;
    BookError error = BookError::invalid_book;
    std::uint64_t snapshots = 0, applied_deltas = 0, dropped_deltas = 0, invalid_updates = 0;
    std::vector<Level> yes, no; // Descending bids; empty when invalid.
    bool operator==(const MarketReplay&) const = default;
};
struct ReplayResult {
    ReplayStatus status = ReplayStatus::not_implemented;
    std::vector<MarketReplay> markets; // Dense market-ID order.
    std::vector<TobRecord> tob;
};

// Each market replays in source-line order and requires metadata plus a successful
// stream snapshot before applying deltas. Gaps/resyncs invalidate listed markets;
// REST books, trades, tickers and sessions never restore or mutate their ladders.
// Changed price ranges invalidate until a new snapshot; identical metadata does not.
// snapshots counts successful stream snapshots. applied_deltas includes successful
// zero/no-top-change updates. dropped_deltas counts updates while unsynchronized.
// invalid_updates counts failed book updates (including snapshots without a grid).
// Invalid updates suppress book queries until a successful stream snapshot.
// TOB starts from an unreported invalid state; emit only when prices, quantities,
// or flags change. Invalid rows have prices -1, quantities 0, flags 0. Valid empty
// books use flag 1. Merge rows by (t_recv_ns, line_index, market_id).
[[nodiscard]] ReplayResult replay(const FileParseResult&, ReplayOptions = {});

}  // namespace brier
