#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <locale>
#include <optional>
#include <random>
#include <string>
#include <string_view>
#include <vector>

namespace {
constexpr std::string_view usage =
    "Usage: brier_synth --out FILE [--messages N] [--markets M] [--seed S]\n"
    "Defaults: 1000000 activity records, 100 markets, seed 1.\n"
    "Output also includes session, metadata and initial snapshots.\n"
    "Exit codes: 0 success/help, 2 usage, 4 output failure. Existing files are refused.\n";
struct Args {
    std::filesystem::path out;
    std::uint64_t messages = 1000000, seed = 1;
    std::size_t markets = 100;
};
std::optional<Args> arguments(int argc, char** argv) {
    Args args;
    std::array<bool,4> seen{};
    for (int i = 1; i < argc; ++i) {
        const std::string_view key(argv[i]);
        const auto slot = key == "--out" ? 0 : key == "--messages" ? 1 :
                          key == "--markets" ? 2 : key == "--seed" ? 3 : -1;
        if (slot < 0 || seen[static_cast<std::size_t>(slot)] || ++i == argc) return std::nullopt;
        seen[static_cast<std::size_t>(slot)] = true;
        const std::string_view value(argv[i]);
        if (slot == 0) {
            if (value.empty()) return std::nullopt;
            args.out = value;
        } else {
            std::uint64_t number = 0;
            const auto parsed = std::from_chars(value.data(), value.data() + value.size(), number);
            if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size()) return std::nullopt;
            if (slot == 1) args.messages = number;
            if (slot == 2) {
                if (number == 0 || number > 100000) return std::nullopt;
                args.markets = static_cast<std::size_t>(number);
            }
            if (slot == 3) args.seed = number;
        }
    }
    // Deterministic receipt clock advances 1000 ns per physical line.
    const auto overhead = 2 * static_cast<std::uint64_t>(args.markets) + 2;
    if (args.out.empty() || args.messages >
        static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()) / 1000 - overhead) {
        return std::nullopt;
    }
    return args;
}
std::string decimal(std::int64_t hundredths) {
    const auto magnitude = static_cast<std::uint64_t>(hundredths < 0 ? -hundredths : hundredths);
    return (hundredths < 0 ? "-" : "") + std::to_string(magnitude / 100) + "." +
        (magnitude % 100 < 10 ? "0" : "") + std::to_string(magnitude % 100);
}
struct Market {
    std::array<std::array<std::int64_t,8>,2> quantity{};
    std::int32_t yes = 0, no = 0;
};
std::int32_t price(const Market& market, std::size_t side, std::size_t level) {
    return (side == 0 ? market.yes : market.no) - static_cast<std::int32_t>(level);
}
}  // namespace

int main(int argc, char** argv) {
    if (argc == 2 && std::string_view(argv[1]) == "--help") { std::cout << usage; return 0; }
    const auto args = arguments(argc, argv);
    if (!args) { std::cerr << usage; return 2; }
    try {
        if (std::filesystem::exists(args->out)) {
            std::cerr << "Output already exists: " << args->out << '\n';
            return 4;
        }
        std::vector<Market> markets(args->markets);
        std::mt19937_64 rng(args->seed);
        for (auto& market : markets) {
            market.yes = 20 + static_cast<std::int32_t>(rng() % 51);
            market.no = 99 - market.yes; // Initial spread is one cent.
            for (auto& side : market.quantity) for (auto& q : side) q = 100 + static_cast<std::int64_t>(rng() % 9901);
        }
        std::array<char,1 << 20> buffer{};
        std::ofstream out;
        out.rdbuf()->pubsetbuf(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        out.exceptions(std::ios::failbit | std::ios::badbit);
        out.imbue(std::locale::classic());
        out.open(args->out, std::ios::binary);
        std::uint64_t line = 0, book_seq = 0, trade_seq = 0;
        const auto prefix = [&](std::string_view kind) {
            out << "{\"v\":1,\"kind\":\"" << kind << "\",\"t_recv_ns\":" << ++line * 1000;
        };
        const auto snapshot = [&](std::size_t id) {
            const auto& market = markets[id];
            out << "{\"market_ticker\":\"SYNTH-" << id << '\"';
            for (std::size_t side = 0; side < 2; ++side) {
                out << (side == 0 ? ",\"yes_dollars_fp\":[" : ",\"no_dollars_fp\":[");
                for (std::size_t level = 0; level < 8; ++level) {
                    if (level != 0) out << ',';
                    out << "[\"" << decimal(price(market, side, level)) << "\",\""
                        << decimal(market.quantity[side][level]) << "\"]";
                }
                out << ']';
            }
            out << '}';
        };
        prefix("session"); out << ",\"conn\":1,\"event\":\"open\",\"pricing_mode\":\"no_leg\"}\n";
        for (std::size_t id = 0; id < markets.size(); ++id) {
            prefix("market_meta");
            out << ",\"ticker\":\"SYNTH-" << id
                << "\",\"price_ranges\":[{\"start\":\"0\",\"end\":\"1\",\"step\":\"0.01\"}]}\n";
            prefix("ws"); out << ",\"conn\":1,\"msg\":{\"type\":\"orderbook_snapshot\",\"sid\":1,\"seq\":" << ++book_seq << ",\"msg\":";
            snapshot(id); out << "}}\n";
        }
        const auto hot = std::max<std::size_t>(1, markets.size() / 10);
        for (std::uint64_t i = 0; i < args->messages; ++i) {
            // 80% of activity goes to the first 10% of markets. Modulo sampling
            // and mt19937_64 avoid implementation-dependent standard distributions.
            const auto id = static_cast<std::size_t>(rng() % 10 < 8 ? rng() % hot : rng() % markets.size());
            auto& market = markets[id];
            const auto type = rng() % 100;
            prefix("ws");
            out << ",\"conn\":1,\"msg\":{\"type\":\""
                << (type < 95 ? "orderbook_delta" : type < 99 ? "trade" : "orderbook_snapshot")
                << "\",\"sid\":" << (type >= 95 && type < 99 ? 2 : 1)
                << ",\"seq\":" << (type >= 95 && type < 99 ? ++trade_seq : ++book_seq) << ",\"msg\":";
            if (type < 95) {
                const auto side = static_cast<std::size_t>(rng() % 2);
                const auto level = static_cast<std::size_t>(rng() % 8);
                auto& quantity = market.quantity[side][level];
                const auto next = static_cast<std::int64_t>(rng() % 10001); // Includes removals.
                const auto delta = next - quantity;
                quantity = next;
                out << "{\"market_ticker\":\"SYNTH-" << id << "\",\"side\":\"" << (side == 0 ? "yes" : "no")
                    << "\",\"price_dollars\":\"" << decimal(price(market, side, level))
                    << "\",\"delta_fp\":\"" << decimal(delta) << "\"}";
            } else if (type < 99) {
                out << "{\"market_ticker\":\"SYNTH-" << id << "\",\"yes_price_dollars\":\"" << decimal(market.yes)
                    << "\",\"no_price_dollars\":\"" << decimal(100 - market.yes)
                    << "\",\"count_fp\":\"1.00\",\"taker_side\":\"yes\",\"trade_id\":\"SYNTH-" << i << "\"}";
            } else snapshot(id);
            out << "}}\n";
        }
        prefix("session"); out << ",\"conn\":1,\"event\":\"close\",\"pricing_mode\":\"no_leg\"}\n";
        out.close();
        std::cout << "Wrote " << line << " records to " << args->out << '\n';
    } catch (const std::exception& error) {
        std::cerr << "Synthetic output failure: " << error.what() << '\n';
        return 4;
    }
    return 0;
}
