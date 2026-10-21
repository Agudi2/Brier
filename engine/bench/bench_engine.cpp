#include "brier/book.hpp"
#include "brier/parse.hpp"

#include <array>
#include <string_view>

#include <benchmark/benchmark.h>

namespace {
const brier::PriceGrid& grid() {
    static const auto value = brier::PriceGrid::from_ranges(
        std::array{brier::PriceRange{0,10000,100}}).value();
    return value;
}
void BM_ApplyDelta(benchmark::State& state) {
    brier::Book book(grid());
    (void)book.apply_snapshot(std::array{brier::Level{4000,10000}}, {});
    std::int64_t change = 1;
    for (auto _ : state) {
        benchmark::DoNotOptimize(book);
        auto result = book.apply_delta(brier::Side::yes, 4000, change);
        benchmark::DoNotOptimize(result);
        benchmark::ClobberMemory();
        change = -change;
    }
    state.SetItemsProcessed(state.iterations());
}
void BM_BestBid(benchmark::State& state) {
    brier::Book book(grid());
    (void)book.apply_snapshot(std::array{brier::Level{4000,100}}, {});
    for (auto _ : state) {
        benchmark::DoNotOptimize(book);
        auto bid = book.best_yes_bid();
        benchmark::DoNotOptimize(bid);
    }
    state.SetItemsProcessed(state.iterations());
}
void BM_ParseLine(benchmark::State& state) {
    constexpr std::string_view line = R"({"v":1,"kind":"ws","t_recv_ns":1000,"conn":1,"msg":{"type":"orderbook_delta","sid":1,"seq":1,"msg":{"market_ticker":"SYNTH-0","price_dollars":"0.4","delta_fp":"1.00","side":"yes"}}})";
    for (auto _ : state) {
        const auto result = brier::parse_line(line);
        if (result.status != brier::ParseStatus::ok || result.events.size() != 1 || !result.malformed.empty()) {
            state.SkipWithError("Single-line parse failed");
            break;
        }
        benchmark::DoNotOptimize(result.events.data());
    }
    state.SetItemsProcessed(state.iterations());
    state.SetBytesProcessed(state.iterations() * static_cast<std::int64_t>(line.size()));
}
BENCHMARK(BM_ApplyDelta);
BENCHMARK(BM_BestBid);
BENCHMARK(BM_ParseLine);
}  // namespace
