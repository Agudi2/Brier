#include "brier/replay.hpp"
#include "brier/benchmark.hpp"

#include <algorithm>
#include <chrono>
#include <stdexcept>
#include <exception>
#include <numeric>
#include <optional>
#include <span>
#include <thread>
#include <tuple>

namespace brier {
namespace {
template<class T>
bool in_bounds(const std::vector<T>& pool, PoolRange range) noexcept {
    return range.offset <= pool.size() && range.count <= pool.size() - range.offset;
}

template<class T>
std::span<const T> slice(const std::vector<T>& pool, PoolRange range) noexcept {
    return std::span<const T>(pool).subspan(range.offset, range.count);
}

auto quote_key(const TobRecord& row) noexcept {
    return std::tuple{row.yes_bid, row.yes_ask, row.bid_qty, row.ask_qty, row.flags};
}

auto merge_key(const TobRecord& row) noexcept {
    return std::tuple{row.t_recv_ns, row.line_index, row.market_id};
}

bool same_ranges(std::span<const PriceRange> a, std::span<const PriceRange> b) {
    return std::equal(a.begin(), a.end(), b.begin(), b.end(),
        [](const auto& x, const auto& y) {
            return x.start == y.start && x.end == y.end && x.step == y.step;
        });
}

// A market and its book belong to exactly one worker. Receipt times never
// determine update order: the source-line order drives the book state.
bool replay_market(const FileParseResult& input, const std::vector<std::size_t>& indices,
                   MarketReplay& result, std::vector<TobRecord>* output, ReplayProfile* profile, std::size_t sample_every) {
    std::optional<Book> book;
    std::span<const PriceRange> ranges;
    std::size_t grid_size = 0;
    TobRecord previous;
    for (const auto index : indices) {
        const auto& event = input.events[index];
        switch (event.kind) {
        case EventKind::market_meta: {
            const auto next = slice(input.price_ranges, event.price_ranges);
            if (!book || !same_ranges(ranges, next)) {
                const auto grid = PriceGrid::from_ranges(next);
                if (!grid) return false;
                book.emplace(*grid);
                grid_size = grid->size();
                ranges = next;
                result.valid = false;
                result.error = BookError::invalid_book;
            }
            break;
        }
        case EventKind::snapshot:
            result.error = book
                ? book->apply_snapshot(slice(input.levels, event.yes_levels),
                                       slice(input.levels, event.no_levels))
                : BookError::invalid_book;
            result.valid = result.error == BookError::none;
            if (result.valid) ++result.snapshots;
            else ++result.invalid_updates;
            break;
        case EventKind::delta:
            if (!result.valid) {
                ++result.dropped_deltas;
            } else {
                if (profile && ++profile->eligible_updates % sample_every == 0) {
                    const auto start = std::chrono::steady_clock::now();
                    result.error = book->apply_delta(event.side, event.price_ticks, event.quantity);
                    const auto finish = std::chrono::steady_clock::now();
                    const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(finish - start).count();
                    (void)profile->latency.record(static_cast<std::uint64_t>(ns));
                } else {
                    result.error = book->apply_delta(event.side, event.price_ticks, event.quantity);
                }
                result.valid = result.error == BookError::none;
                if (result.valid) ++result.applied_deltas;
                else ++result.invalid_updates;
            }
            break;
        case EventKind::gap:
        case EventKind::resync:
            result.valid = false;
            result.error = BookError::invalid_book;
            break;
        default:
            break;
        }
        if (!output) continue;
        TobRecord row;
        row.t_recv_ns = event.t_recv_ns;
        row.line_index = event.line_index;
        row.market_id = result.market_id;
        if (result.valid) {
            row.flags = tob_valid | (book->crossed() ? tob_crossed : 0U)
                                  | (book->locked() ? tob_locked : 0U);
            if (const auto bid = book->best_yes_bid()) {
                row.yes_bid = bid->price;
                row.bid_qty = bid->quantity;
            }
            if (const auto ask = book->implied_yes_ask()) {
                row.yes_ask = ask->price;
                row.ask_qty = ask->quantity;
            }
        }
        if (quote_key(row) != quote_key(previous)) {
            output->push_back(row);
            previous = row;
        }
    }
    if (result.valid) {
        result.yes.resize(grid_size);
        result.no.resize(grid_size);
        result.yes.resize(book->depth(Side::yes, result.yes));
        result.no.resize(book->depth(Side::no, result.no));
    }
    return true;
}

struct Worker {
    std::vector<std::size_t> markets;
    std::size_t load = 0;
    std::vector<TobRecord> output;
    std::exception_ptr error;
    bool valid = true;
    ReplayProfile profile;
};
}  // namespace

ReplayResult replay_impl(const FileParseResult& input, ReplayOptions options,
                         ReplayProfile* profile, std::size_t sample_every) {
    ReplayResult result;
    if (options.threads == 0) {
        result.status = ReplayStatus::invalid_thread_count;
        return result;
    }
    result.status = ReplayStatus::invalid_input;
    if (input.status != ParseStatus::ok || input.tickers.size() > no_id) return result;

    // Partition references rather than copying events or their payloads. Gap
    // membership is deduplicated before fan-out, preserving one action per market.
    const auto count = input.tickers.size();
    std::vector<std::vector<std::size_t>> by_market(count);
    for (std::size_t index = 0; index < input.events.size(); ++index) {
        const auto& event = input.events[index];
        switch (event.kind) {
        case EventKind::market_meta:
            if (!in_bounds(input.price_ranges, event.price_ranges)) return result;
            break;
        case EventKind::snapshot:
            if (!in_bounds(input.levels, event.yes_levels) ||
                !in_bounds(input.levels, event.no_levels)) return result;
            break;
        case EventKind::delta:
            break;
        case EventKind::gap:
        case EventKind::resync: {
            if (!in_bounds(input.market_ids, event.markets)) return result;
            const auto ids = slice(input.market_ids, event.markets);
            std::vector<std::uint32_t> unique(ids.begin(), ids.end());
            std::sort(unique.begin(), unique.end());
            unique.erase(std::unique(unique.begin(), unique.end()), unique.end());
            for (const auto id : unique) {
                if (id >= count) return result;
                by_market[id].push_back(index);
            }
            continue;
        }
        default:
            continue; // REST samples, sessions and public trades do not mutate books.
        }
        if (event.market_id >= count) return result;
        by_market[event.market_id].push_back(index);
    }
    const auto source_order = [&](std::size_t a, std::size_t b) {
        return input.events[a].line_index < input.events[b].line_index;
    };
    for (auto& indices : by_market) {
        if (!std::is_sorted(indices.begin(), indices.end(), source_order)) {
            std::stable_sort(indices.begin(), indices.end(), source_order);
        }
    }
    result.markets.resize(count);
    if (count == 0) {
        result.status = ReplayStatus::ok;
        return result;
    }

    // LPT: largest event count first, then lowest market ID; greedy assignment
    // to the least-loaded worker, breaking ties by worker index.
    const auto worker_count = std::min({options.threads, count,
        static_cast<std::size_t>(std::max(1U, std::thread::hardware_concurrency()))});
    std::vector<Worker> workers(worker_count);
    std::vector<std::size_t> order(count);
    std::iota(order.begin(), order.end(), 0);
    std::sort(order.begin(), order.end(), [&](auto a, auto b) {
        if (by_market[a].size() != by_market[b].size()) {
            return by_market[a].size() > by_market[b].size();
        }
        return a < b;
    });
    for (const auto id : order) {
        auto& worker = *std::min_element(workers.begin(), workers.end(),
            [](const Worker& a, const Worker& b) { return a.load < b.load; });
        worker.markets.push_back(id);
        worker.load += by_market[id].size();
        result.markets[id].market_id = static_cast<std::uint32_t>(id);
    }
    for (auto& worker : workers) {
        // At most one TOB row per event per market; updates do not reallocate.
        if (options.tob) worker.output.reserve(worker.load);
    }
    {
        std::vector<std::jthread> threads;
        threads.reserve(worker_count);
        for (std::size_t i = 0; i < worker_count; ++i) {
            threads.emplace_back([&, i] {
                auto& worker = workers[i];
                try {
                    for (const auto id : worker.markets) {
                        if (!replay_market(input, by_market[id], result.markets[id],
                                           options.tob ? &worker.output : nullptr,
                                           profile ? &worker.profile : nullptr, sample_every)) {
                            worker.valid = false;
                            return;
                        }
                    }
                } catch (...) {
                    worker.error = std::current_exception();
                }
            });
        }
    } // Join before inspecting worker results, including during stack unwinding.
    for (const auto& worker : workers) {
        if (worker.error) std::rethrow_exception(worker.error);
        if (!worker.valid) return {ReplayStatus::invalid_input, {}, {}};
    }
    if (profile) {
        for (const auto& worker : workers) {
            profile->eligible_updates += worker.profile.eligible_updates;
            if (!profile->latency.merge(worker.profile.latency)) {
                throw std::overflow_error("Latency histogram count overflow");
            }
        }
    }
    std::size_t rows = 0;
    for (const auto& worker : workers) rows += worker.output.size();
    result.tob.reserve(rows);
    for (const auto& worker : workers) {
        result.tob.insert(result.tob.end(), worker.output.begin(), worker.output.end());
    }
    std::sort(result.tob.begin(), result.tob.end(), [](const auto& a, const auto& b) {
        return merge_key(a) < merge_key(b);
    });
    result.status = ReplayStatus::ok;
    return result;
}
ReplayResult replay(const FileParseResult& input, ReplayOptions options) {
    return replay_impl(input, options, nullptr, 1);
}

ReplayProfile profile_replay(const FileParseResult& input, ReplayOptions options,
                             std::size_t sample_every) {
    if (sample_every == 0) throw std::invalid_argument("sample_every must be positive");
    ReplayProfile profile;
    profile.result = replay_impl(input, options, &profile, sample_every);
    return profile;
}
}  // namespace brier
