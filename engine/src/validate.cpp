#include "brier/validate.hpp"

#include <algorithm>
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
bool same_ranges(std::span<const PriceRange> a, std::span<const PriceRange> b) {
    return std::equal(a.begin(), a.end(), b.begin(), b.end(), [](const auto& x, const auto& y) {
        return x.start == y.start && x.end == y.end && x.step == y.step;
    });
}
bool canonical(std::span<const Level> source, const std::optional<PriceGrid>& grid,
               std::vector<Level>& target) {
    target.assign(source.begin(), source.end());
    std::sort(target.begin(), target.end(), [](const auto& a, const auto& b) {
        return a.price > b.price;
    });
    for (std::size_t i = 0; i < target.size(); ++i) {
        const auto& level = target[i];
        if (level.price < 0 || level.price > 10000 || level.quantity < 0 ||
            (grid && !grid->is_valid(level.price)) ||
            (i != 0 && target[i - 1].price == level.price)) return false;
    }
    std::erase_if(target, [](const auto& level) { return level.quantity == 0; });
    return true;
}
// The highest NO bid determines the YES ask bijectively, including quantity.
// Comparing both best bids therefore compares both YES quotes without floating point.
auto top(const std::vector<Level>& yes, const std::vector<Level>& no) {
    const auto bid = yes.empty() ? Level{-1,0} : yes.front();
    const auto opposite = no.empty() ? Level{-1,0} : no.front();
    return std::tuple{bid.price, bid.quantity, opposite.price, opposite.quantity};
}
struct Request {
    std::size_t event_index = 0;
    std::vector<Level> yes, no, last_yes, last_no;
    bool has_valid = false, full_match = false, top_match = false;
    std::uint64_t state_line = 0;
};

bool validate_market(const FileParseResult& input, const std::vector<std::size_t>& indices,
                     ValidationOptions options, ValidationResult& report) {
    // Canonicalize samples against metadata in effect when each response arrived.
    // This pass completes before comparisons, so bad REST data cannot yield a
    // partial report. A market without metadata can only supply excluded states.
    std::vector<Request> requests;
    std::optional<PriceGrid> grid;
    for (const auto index : indices) {
        const auto& event = input.events[index];
        if (event.kind == EventKind::market_meta) {
            grid = PriceGrid::from_ranges(slice(input.price_ranges, event.price_ranges));
            if (!grid) return false;
        } else if (event.kind == EventKind::rest_book) {
            Request request;
            request.event_index = index;
            if (!canonical(slice(input.levels, event.yes_levels), grid, request.yes) ||
                !canonical(slice(input.levels, event.no_levels), grid, request.no)) return false;
            requests.push_back(std::move(request));
        }
    }
    if (requests.empty()) return true;
    std::vector<std::size_t> opens(requests.size());
    std::iota(opens.begin(), opens.end(), 0);
    std::stable_sort(opens.begin(), opens.end(), [&](auto a, auto b) {
        return input.events[requests[a].event_index].t_send_ns <
               input.events[requests[b].event_index].t_send_ns;
    });
    std::vector<std::size_t> active;
    active.reserve(requests.size());
    std::size_t next_open = 0, next_close = 0, grid_size = 0;
    std::optional<Book> book;
    std::span<const PriceRange> ranges;
    bool valid = false, dirty = false;
    std::uint64_t state_line = 0;
    std::vector<Level> yes, no;
    const auto compare = [&](Request& request) {
        if (!valid) return;
        if (dirty) {
            yes.resize(grid_size);
            no.resize(grid_size);
            yes.resize(book->depth(Side::yes, yes));
            no.resize(book->depth(Side::no, no));
            dirty = false;
        }
        request.has_valid = true;
        request.state_line = state_line;
        request.full_match = request.full_match || (yes == request.yes && no == request.no);
        request.top_match = request.top_match || top(yes, no) == top(request.yes, request.no);
        if (!request.full_match && options.max_diffs != 0) {
            request.last_yes = yes;
            request.last_no = no;
        } else {
            request.last_yes.clear();
            request.last_no.clear();
        }
    };
    for (const auto index : indices) {
        const auto& event = input.events[index];
        while (next_open < opens.size()) {
            const auto id = opens[next_open];
            const auto send = input.events[requests[id].event_index].t_send_ns;
            if (send > event.t_recv_ns) break;
            ++next_open;
            active.push_back(id);
            // In a quiet interval, the prior state persists at send. At an
            // exact update boundary, observe the new state after that update.
            if (send < event.t_recv_ns) compare(requests[id]);
        }
        bool transition = false;
        switch (event.kind) {
        case EventKind::market_meta: {
            const auto next = slice(input.price_ranges, event.price_ranges);
            if (!book || !same_ranges(ranges, next)) {
                const auto next_grid = PriceGrid::from_ranges(next);
                if (!next_grid) return false;
                book.emplace(*next_grid);
                grid_size = next_grid->size();
                ranges = next;
                valid = false;
                transition = true;
            }
            break;
        }
        case EventKind::snapshot:
            valid = book && book->apply_snapshot(slice(input.levels, event.yes_levels),
                slice(input.levels, event.no_levels)) == BookError::none;
            transition = true;
            break;
        case EventKind::delta:
            if (valid) {
                valid = book->apply_delta(event.side, event.price_ticks, event.quantity) == BookError::none;
                transition = true;
            }
            break;
        case EventKind::gap:
        case EventKind::resync:
            valid = false;
            transition = true;
            break;
        case EventKind::rest_book: {
            auto& request = requests[next_close];
            compare(request); // Also handles a zero-duration window with no stream update.
            ++report.samples;
            if (!request.has_valid) {
                ++report.excluded_invalid;
            } else {
                ++report.compared;
                if (request.full_match) ++report.full_depth_matches;
                if (request.top_match) ++report.top_of_book_matches;
                if (!request.full_match) {
                    ++report.mismatches;
                    if (report.diffs.size() < options.max_diffs) {
                        report.diffs.push_back({event.market_id, event.line_index, request.state_line,
                            std::move(request.yes), std::move(request.no),
                            std::move(request.last_yes), std::move(request.last_no)});
                    }
                }
            }
            // Release completed sample payloads; no historical stream copies are retained.
            request = {};
            active.erase(std::find(active.begin(), active.end(), next_close));
            ++next_close;
            break;
        }
        default:
            break;
        }
        if (transition) {
            state_line = event.line_index;
            dirty = true;
            for (const auto id : active) compare(requests[id]);
        }
    }
    return true;
}

struct Worker {
    std::vector<std::size_t> markets;
    std::size_t load = 0;
    std::exception_ptr error;
};
}  // namespace

ValidationResult validate(const FileParseResult& input, ValidationOptions options) {
    if (options.threads == 0) {
        ValidationResult result;
        result.status = ValidationStatus::invalid_thread_count;
        return result;
    }
    ValidationResult invalid;
    invalid.status = ValidationStatus::invalid_input;
    if (input.status != ParseStatus::ok || input.tickers.size() > no_id) return invalid;
    const auto count = input.tickers.size();
    std::vector<std::vector<std::size_t>> by_market(count);
    for (std::size_t index = 0; index < input.events.size(); ++index) {
        const auto& event = input.events[index];
        if (index != 0 && (event.t_recv_ns < input.events[index - 1].t_recv_ns ||
            event.line_index <= input.events[index - 1].line_index)) return invalid;
        switch (event.kind) {
        case EventKind::market_meta:
            if (!in_bounds(input.price_ranges, event.price_ranges)) return invalid;
            break;
        case EventKind::snapshot:
        case EventKind::rest_book:
            if (!in_bounds(input.levels, event.yes_levels) ||
                !in_bounds(input.levels, event.no_levels)) return invalid;
            if (event.kind == EventKind::rest_book && event.t_send_ns > event.t_recv_ns) return invalid;
            break;
        case EventKind::delta:
            break;
        case EventKind::gap:
        case EventKind::resync: {
            if (!in_bounds(input.market_ids, event.markets)) return invalid;
            const auto ids = slice(input.market_ids, event.markets);
            std::vector<std::uint32_t> unique(ids.begin(), ids.end());
            std::sort(unique.begin(), unique.end());
            unique.erase(std::unique(unique.begin(), unique.end()), unique.end());
            for (const auto id : unique) {
                if (id >= count) return invalid;
                by_market[id].push_back(index);
            }
            continue;
        }
        case EventKind::session:
        case EventKind::trade:
        case EventKind::ticker:
            continue;
        default:
            return invalid;
        }
        if (event.market_id >= count) return invalid;
        by_market[event.market_id].push_back(index);
    }
    ValidationResult result;
    result.status = ValidationStatus::ok;
    if (count == 0) return result;
    std::vector<ValidationResult> reports(count);
    const auto worker_count = std::min({options.threads, count,
        static_cast<std::size_t>(std::max(1U, std::thread::hardware_concurrency()))});
    std::vector<Worker> workers(worker_count);
    std::vector<std::size_t> order(count);
    std::iota(order.begin(), order.end(), 0);
    std::sort(order.begin(), order.end(), [&](auto a, auto b) {
        if (by_market[a].size() != by_market[b].size()) return by_market[a].size() > by_market[b].size();
        return a < b;
    });
    for (const auto id : order) {
        auto& worker = *std::min_element(workers.begin(), workers.end(),
            [](const auto& a, const auto& b) { return a.load < b.load; });
        worker.markets.push_back(id);
        worker.load += by_market[id].size();
    }
    {
        std::vector<std::jthread> threads;
        threads.reserve(worker_count);
        for (std::size_t i = 0; i < worker_count; ++i) {
            threads.emplace_back([&, i] {
                auto& worker = workers[i];
                try {
                    for (const auto id : worker.markets) {
                        reports[id].status = validate_market(input, by_market[id], options, reports[id])
                            ? ValidationStatus::ok : ValidationStatus::invalid_input;
                    }
                } catch (...) {
                    worker.error = std::current_exception();
                }
            });
        }
    }
    for (const auto& worker : workers) {
        if (worker.error) std::rethrow_exception(worker.error);
    }
    for (auto& report : reports) {
        if (report.status != ValidationStatus::ok) return invalid;
        result.samples += report.samples;
        result.compared += report.compared;
        result.excluded_invalid += report.excluded_invalid;
        result.full_depth_matches += report.full_depth_matches;
        result.top_of_book_matches += report.top_of_book_matches;
        result.mismatches += report.mismatches;
        for (auto& diff : report.diffs) result.diffs.push_back(std::move(diff));
    }
    std::sort(result.diffs.begin(), result.diffs.end(), [](const auto& a, const auto& b) {
        return std::tie(a.rest_line_index, a.market_id) < std::tie(b.rest_line_index, b.market_id);
    });
    if (result.diffs.size() > options.max_diffs) result.diffs.resize(options.max_diffs);
    if (result.compared != 0) {
        result.full_depth_match_rate = static_cast<double>(result.full_depth_matches) /
                                      static_cast<double>(result.compared);
        result.top_of_book_match_rate = static_cast<double>(result.top_of_book_matches) /
                                       static_cast<double>(result.compared);
    }
    return result;
}
}  // namespace brier
