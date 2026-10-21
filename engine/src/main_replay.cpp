#include "brier/replay.hpp"
#include "brier/validate.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iomanip>
#include <limits>
#include <locale>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <type_traits>

namespace {
constexpr std::string_view usage =
    "Usage: brier_replay INPUT [--threads N] [--out DIR] [--tob] [--validate]\n"
    "Replay recorded market data; default output directory: replay_out.\n"
    "  --threads N  Positive worker limit (default: 1)\n"
    "  --out DIR    Write summary.json and markets.json here\n"
    "  --tob        Also write packed little-endian top-of-book records to tob.bin\n"
    "  --validate   Compare REST windows and write validation.json\n"
    "  --help       Show this help\n"
    "Exit codes: 0 success/help, 2 usage, 3 input/validation, 4 output.\n";

struct Arguments {
    std::filesystem::path input, output = "replay_out";
    std::size_t threads = 1;
    bool tob = false, validate = false, help = false;
};

std::optional<Arguments> arguments(int argc, char** argv) {
    Arguments args;
    bool has_threads = false, has_output = false;
    for (int i = 1; i < argc; ++i) {
        const std::string_view arg(argv[i]);
        if (arg == "--help") {
            args.help = true;
        } else if (arg == "--threads") {
            if (has_threads || ++i == argc) return std::nullopt;
            has_threads = true;
            const std::string_view value(argv[i]);
            const auto parsed = std::from_chars(value.data(), value.data() + value.size(),
                                                args.threads);
            if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size() ||
                args.threads == 0) return std::nullopt;
        } else if (arg == "--out") {
            if (has_output || ++i == argc || std::string_view(argv[i]).empty()) {
                return std::nullopt;
            }
            has_output = true;
            args.output = argv[i];
        } else if (arg == "--tob") {
            if (args.tob) return std::nullopt;
            args.tob = true;
        } else if (arg == "--validate") {
            if (args.validate) return std::nullopt;
            args.validate = true;
        } else {
            if (arg.empty() || arg.starts_with('-') || !args.input.empty()) {
                return std::nullopt;
            }
            args.input = arg;
        }
    }
    if (args.input.empty() && !args.help) return std::nullopt;
    return args;
}

void json_string(std::ostream& out, std::string_view value) {
    constexpr char hex[] = "0123456789abcdef";
    out.put('"');
    for (const unsigned char byte : value) {
        if (byte == '"' || byte == '\\') {
            out.put('\\');
            out.put(static_cast<char>(byte));
        } else if (byte < 0x20) {
            out << "\\u00" << hex[byte >> 4] << hex[byte & 0xf];
        } else {
            out.put(static_cast<char>(byte));
        }
    }
    out.put('"');
}

std::string_view error_name(brier::BookError error) {
    using brier::BookError;
    switch (error) {
    case BookError::none: return "none";
    case BookError::off_grid: return "off_grid";
    case BookError::negative_quantity: return "negative_quantity";
    case BookError::quantity_overflow: return "quantity_overflow";
    case BookError::duplicate_price: return "duplicate_price";
    case BookError::invalid_side: return "invalid_side";
    case BookError::invalid_book: return "invalid_book";
    case BookError::not_implemented: return "not_implemented";
    }
    return "invalid_book";
}

std::ofstream open_output(const std::filesystem::path& path) {
    std::ofstream stream;
    stream.exceptions(std::ios::failbit | std::ios::badbit);
    stream.imbue(std::locale::classic());
    stream.open(path, std::ios::binary | std::ios::trunc);
    return stream;
}

void counts(std::ostream& out, const brier::MarketReplay& market) {
    out << "\"snapshots\":" << market.snapshots
        << ",\"applied_deltas\":" << market.applied_deltas
        << ",\"dropped_deltas\":" << market.dropped_deltas
        << ",\"invalid_updates\":" << market.invalid_updates;
}

void levels(std::ostream& out, const std::vector<brier::Level>& values) {
    out << '[';
    bool first = true;
    for (const auto& level : values) {
        if (!first) out << ',';
        first = false;
        out << '[' << level.price << ',' << level.quantity << ']';
    }
    out << ']';
}

void write_summary(const std::filesystem::path& path, const brier::FileParseResult& input,
                   const brier::ReplayResult& replay) {
    auto out = open_output(path);
    brier::MarketReplay totals;
    for (const auto& market : replay.markets) {
        totals.snapshots += market.snapshots;
        totals.applied_deltas += market.applied_deltas;
        totals.dropped_deltas += market.dropped_deltas;
        totals.invalid_updates += market.invalid_updates;
    }
    out << "{\"schema_version\":1,\"physical_lines\":" << input.physical_lines
        << ",\"malformed_lines\":" << input.malformed.size()
        << ",\"unknown_messages\":" << input.unknown_messages
        << ",\"unknown_envelopes\":" << input.unknown_envelopes << ",\"totals\":{";
    counts(out, totals);
    out << "},\"markets\":[";
    bool first = true;
    for (const auto& market : replay.markets) {
        if (!first) out << ',';
        first = false;
        out << "{\"market_id\":" << market.market_id << ",\"ticker\":";
        json_string(out, input.tickers[market.market_id]);
        out << ",\"valid\":" << (market.valid ? "true" : "false") << ",\"error\":";
        json_string(out, error_name(market.error));
        out << ',';
        counts(out, market);
        out << ",\"yes\":";
        levels(out, market.yes);
        out << ",\"no\":";
        levels(out, market.no);
        out << '}';
    }
    out << "]}\n";
    out.close(); // Surface delayed write failures before reporting success.
}

void write_validation(const std::filesystem::path& path,
                      const brier::ValidationResult& result) {
    auto out = open_output(path);
    out << std::setprecision(std::numeric_limits<double>::max_digits10);
    out << "{\"schema_version\":1,\"samples\":" << result.samples
        << ",\"compared\":" << result.compared
        << ",\"excluded_invalid\":" << result.excluded_invalid
        << ",\"full_depth_matches\":" << result.full_depth_matches
        << ",\"top_of_book_matches\":" << result.top_of_book_matches
        << ",\"mismatches\":" << result.mismatches
        << ",\"full_depth_match_rate\":";
    if (result.full_depth_match_rate) out << *result.full_depth_match_rate;
    else out << "null";
    out << ",\"top_of_book_match_rate\":";
    if (result.top_of_book_match_rate) out << *result.top_of_book_match_rate;
    else out << "null";
    out << ",\"diffs\":[";
    bool first = true;
    for (const auto& diff : result.diffs) {
        if (!first) out << ',';
        first = false;
        out << "{\"market_id\":" << diff.market_id
            << ",\"rest_line_index\":" << diff.rest_line_index
            << ",\"state_line_index\":" << diff.state_line_index << ",\"rest_yes\":";
        levels(out, diff.rest_yes);
        out << ",\"rest_no\":";
        levels(out, diff.rest_no);
        out << ",\"engine_yes\":";
        levels(out, diff.engine_yes);
        out << ",\"engine_no\":";
        levels(out, diff.engine_no);
        out << '}';
    }
    out << "]}\n";
    out.close();
}

template<class T>
void little_endian(std::ostream& out, T value) {
    // Conversion to unsigned is modulo 2^N, including the -1 price sentinel.
    auto bits = static_cast<std::make_unsigned_t<T>>(value);
    std::array<char, sizeof(T)> bytes{};
    for (auto& byte : bytes) {
        byte = static_cast<char>(bits & 0xffU);
        bits >>= 8;
    }
    out.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
}

void write_outputs(const Arguments& args, const brier::FileParseResult& input,
                   const brier::ReplayResult& replay) {
    std::filesystem::create_directories(args.output);
    write_summary(args.output / "summary.json", input, replay);
    auto markets = open_output(args.output / "markets.json");
    markets << '[';
    for (std::size_t i = 0; i < input.tickers.size(); ++i) {
        if (i != 0) markets << ',';
        json_string(markets, input.tickers[i]);
    }
    markets << "]\n";
    markets.close();
    if (args.tob) {
        auto tob = open_output(args.output / "tob.bin");
        for (const auto& row : replay.tob) {
            little_endian(tob, row.t_recv_ns);
            little_endian(tob, row.market_id);
            little_endian(tob, row.yes_bid);
            little_endian(tob, row.yes_ask);
            little_endian(tob, row.bid_qty);
            little_endian(tob, row.ask_qty);
            little_endian(tob, row.flags);
        }
        tob.close();
    }
}
}  // namespace

int main(int argc, char** argv) {
    const auto args = arguments(argc, argv);
    if (!args) {
        std::cerr << "Invalid arguments.\n" << usage;
        return 2;
    }
    if (args->help) {
        std::cout << usage;
        return 0;
    }
    brier::FileParseResult input;
    brier::ReplayResult replay;
    std::optional<brier::ValidationResult> validation;
    try {
        // Avoid allocating an unbounded chunk table for a user-supplied limit.
        const auto chunks = std::min(args->threads, static_cast<std::size_t>(
            std::max(1U, std::thread::hardware_concurrency())));
        input = brier::parse_file(args->input, chunks);
        if (input.status != brier::ParseStatus::ok) {
            std::cerr << "Cannot read input or unsupported input pricing mode.\n";
            return 3;
        }
        replay = brier::replay(input, {args->threads, args->tob});
        if (replay.status != brier::ReplayStatus::ok) {
            std::cerr << "Invalid replay input.\n";
            return 3;
        }
        if (args->validate) {
            validation = brier::validate(input, {args->threads, 10});
            if (validation->status != brier::ValidationStatus::ok) {
                std::cerr << "Invalid validation input (including regressing receipt times).\n";
                return 3;
            }
        }
    } catch (const std::exception& error) {
        std::cerr << "Input/replay failure: " << error.what() << '\n';
        return 3;
    }
    try {
        write_outputs(*args, input, replay);
        if (validation) write_validation(args->output / "validation.json", *validation);
    } catch (const std::exception& error) {
        std::cerr << "Output failure: " << error.what() << '\n';
        return 4;
    }
    return 0;
}
