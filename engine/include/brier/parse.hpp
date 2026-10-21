#pragma once

#include "brier/event.hpp"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <string>
#include <string_view>
#include <vector>

namespace brier {

enum class ParseStatus {
    ok, io_error, invalid_chunk_count, unsupported_pricing_mode, not_implemented
};
enum class MalformedReason {
    invalid_json, invalid_record, invalid_fixed_point, unsupported_version
};
struct ParseDiagnostic {
    std::uint64_t line_number; // One-based physical line; events use zero-based indices.
    MalformedReason reason;
    bool operator==(const ParseDiagnostic&) const = default;
};
struct FileParseResult {
    ParseStatus status = ParseStatus::not_implemented;
    std::uint64_t physical_lines = 0;
    std::uint64_t unknown_messages = 0;
    std::uint64_t unknown_envelopes = 0;
    std::vector<Event> events;
    std::vector<std::string> tickers;
    std::vector<std::string> texts; // Trade IDs and optional market metadata strings.
    std::vector<Level> levels;
    std::vector<std::uint32_t> market_ids;
    std::vector<PriceRange> price_ranges;
    std::vector<ParseDiagnostic> malformed;
};

// File-order results, independent of chunk count. Market IDs are dense in first
// occurrence order among accepted records (ticker-list order breaks ties).
// Skip malformed lines, reporting one diagnostic each, without committing any
// events, IDs or pool data. Unknown valid types are counted and ignored.
// Explicit unsupported session pricing modes are fatal; absent/invalid fields
// are malformed. All session lines must use no_leg; it need not be the first
// record in a daily file. LF and CRLF are accepted; a final LF adds no empty line.
// Result pools own their data after the source file/mapping has been released.
// Resource exhaustion raises an exception; malformed records are diagnostics.
[[nodiscard]] FileParseResult parse_file(const std::filesystem::path& path,
                                        std::size_t chunks = 1);

// One in-memory JSON record without a line terminator; no mapping or file I/O.
// Includes parser setup, syntax validation, decoding, interning and owned results.
[[nodiscard]] FileParseResult parse_line(std::string_view line);

}  // namespace brier
