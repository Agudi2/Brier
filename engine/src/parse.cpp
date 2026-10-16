#include "brier/parse.hpp"
#include "brier/fixed_point.hpp"

#include <simdjson.h>

#include <algorithm>
#include <atomic>
#include <cstring>
#include <exception>
#include <fcntl.h>
#include <limits>
#include <optional>
#include <stdexcept>
#include <string_view>
#include <sys/mman.h>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include <unordered_map>
#include <utility>

namespace brier {
namespace {
namespace od = simdjson::ondemand;

struct InvalidRecord { MalformedReason reason = MalformedReason::invalid_record; };
struct UnsupportedMode {};

class Mapping {
public:
    explicit Mapping(const std::filesystem::path& path) {
        const int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
        if (fd < 0) return;
        struct stat info {};
        if (::fstat(fd, &info) == 0 && S_ISREG(info.st_mode) && info.st_size >= 0 &&
            static_cast<std::uintmax_t>(info.st_size) <= std::numeric_limits<std::size_t>::max()) {
            size_ = static_cast<std::size_t>(info.st_size);
            if (size_ == 0) {
                ok_ = true;
            } else {
                address_ = ::mmap(nullptr, size_, PROT_READ, MAP_PRIVATE, fd, 0);
                ok_ = address_ != MAP_FAILED;
            }
        }
        ::close(fd);
    }
    ~Mapping() { if (address_ != MAP_FAILED) ::munmap(address_, size_); }
    Mapping(const Mapping&) = delete;
    Mapping& operator=(const Mapping&) = delete;
    bool ok() const noexcept { return ok_; }
    std::string_view bytes() const noexcept {
        return size_ == 0 ? std::string_view{} : std::string_view(static_cast<const char*>(address_), size_);
    }
private:
    void* address_ = MAP_FAILED;
    std::size_t size_ = 0;
    bool ok_ = false;
};

bool whitespace(char c) noexcept { return c == ' ' || c == '\t' || c == '\r' || c == '\n'; }
bool digit(char c) noexcept { return c >= '0' && c <= '9'; }

// Unknown JSON numbers need syntax validation, not floating-point conversion.
bool number_syntax(std::string_view token) noexcept {
    while (!token.empty() && whitespace(token.back())) token.remove_suffix(1);
    std::size_t i = 0;
    if (i < token.size() && token[i] == '-') ++i;
    if (i == token.size()) return false;
    if (token[i] == '0') {
        ++i;
    } else {
        if (token[i] < '1' || token[i] > '9') return false;
        while (i < token.size() && digit(token[i])) ++i;
    }
    if (i < token.size() && token[i] == '.') {
        const auto begin = ++i;
        while (i < token.size() && digit(token[i])) ++i;
        if (i == begin) return false;
    }
    if (i < token.size() && (token[i] == 'e' || token[i] == 'E')) {
        ++i;
        if (i < token.size() && (token[i] == '+' || token[i] == '-')) ++i;
        const auto begin = i;
        while (i < token.size() && digit(token[i])) ++i;
        if (i == begin) return false;
    }
    return i == token.size();
}

template<class Value>
void validate_json(Value& value, std::size_t depth = 0) {
    if (depth >= 1024) throw InvalidRecord{MalformedReason::invalid_json};
    switch (value.type().value()) {
    case od::json_type::object: {
        auto object = value.get_object().value();
        for (auto field_result : object) {
            (void)field_result.unescaped_key().value();
            auto child = field_result.value().value();
            validate_json(child, depth + 1);
        }
        break;
    }
    case od::json_type::array: {
        auto array = value.get_array().value();
        for (auto child_result : array) {
            auto child = child_result.value();
            validate_json(child, depth + 1);
        }
        break;
    }
    case od::json_type::string: (void)value.get_string().value(); break;
    case od::json_type::boolean: (void)value.get_bool().value(); break;
    case od::json_type::null:
        if (!value.is_null().value()) throw InvalidRecord{MalformedReason::invalid_json};
        break;
    case od::json_type::number:
        if (!number_syntax(value.raw_json().value()))
            throw InvalidRecord{MalformedReason::invalid_json};
        break;
    }
}

std::optional<od::value> field(od::object& object, std::string_view name) {
    auto found = object.find_field_unordered(name);
    if (found.error() == simdjson::NO_SUCH_FIELD) return std::nullopt;
    return found.value();
}

std::string string_value(od::value value) {
    const auto text = value.get_string().value();
    if (text.empty()) throw InvalidRecord{};
    return std::string(text);
}

std::string string_field(od::object& object, std::string_view name, bool optional = false) {
    auto value = field(object, name);
    if (!value) {
        if (optional) return {};
        throw InvalidRecord{};
    }
    if (optional && value->is_null().value()) return {};
    return string_value(*value);
}

std::int64_t integer(od::object& object, std::string_view name, bool optional = false) {
    auto value = field(object, name);
    if (!value) {
        if (optional) return -1;
        throw InvalidRecord{};
    }
    if (optional && value->is_null().value()) return -1;
    return value->get_int64().value();
}

std::int32_t price_text(std::string_view text) {
    const auto parsed = parse_price(text);
    if (!parsed) throw InvalidRecord{MalformedReason::invalid_fixed_point};
    return parsed.value;
}

std::int32_t price(od::object& object, std::string_view name, bool optional = false) {
    auto value = field(object, name);
    if (!value) {
        if (optional) return -1;
        throw InvalidRecord{};
    }
    if (optional && value->is_null().value()) return -1;
    const auto text = value->get_string().value();
    if (optional && text.empty()) return -1;
    return price_text(text);
}

std::int64_t quantity_text(std::string_view text, bool signed_delta = false) {
    const auto parsed = parse_quantity(text);
    if (!parsed || (!signed_delta && parsed.value < 0))
        throw InvalidRecord{MalformedReason::invalid_fixed_point};
    return parsed.value;
}

std::vector<Level> levels(od::object& object, std::string_view name) {
    std::vector<Level> result;
    auto array = object.find_field_unordered(name).get_array().value();
    for (auto item : array) {
        Level level{};
        std::size_t count = 0;
        auto pair = item.get_array().value();
        for (auto part : pair) {
            if (count == 0) level.price = price_text(part.get_string().value());
            else if (count == 1) level.quantity = quantity_text(part.get_string().value());
            else throw InvalidRecord{};
            ++count;
        }
        if (count != 2) throw InvalidRecord{};
        result.push_back(level);
    }
    return result;
}

// A line commits only after every field has been checked. Failed records leave
// no payload fragments or interned strings in a chunk's result.
struct Pending {
    Event event;
    std::string market, trade_id, event_ticker, structure;
    std::vector<std::string> markets;
    std::vector<Level> yes, no;
    std::vector<PriceRange> ranges;
};
enum class Disposition { event, unknown_message, unknown_envelope };

Disposition decode(od::object root, Pending& pending) {
    if (integer(root, "v") != 1) throw InvalidRecord{MalformedReason::unsupported_version};
    const auto kind = string_field(root, "kind");
    auto& event = pending.event;
    event.t_recv_ns = integer(root, "t_recv_ns");
    event.t_mono_ns = integer(root, "t_mono_ns", true);
    event.conn = integer(root, "conn", true);
    if (kind == "session") {
        event.kind = EventKind::session;
        event.conn = integer(root, "conn");
        const auto action = string_field(root, "event");
        if (action == "open") event.session_action = SessionAction::open;
        else if (action == "close") event.session_action = SessionAction::close;
        else throw InvalidRecord{};
        if (string_field(root, "pricing_mode") != "no_leg") throw UnsupportedMode{};
    } else if (kind == "market_meta") {
        event.kind = EventKind::market_meta;
        pending.market = string_field(root, "ticker");
        pending.event_ticker = string_field(root, "event_ticker", true);
        pending.structure = string_field(root, "price_level_structure", true);
        auto ranges = root.find_field_unordered("price_ranges").get_array().value();
        for (auto item : ranges) {
            auto band = item.get_object().value();
            const auto start = price(band, "start");
            const auto end = price(band, "end");
            const auto step = price(band, "step");
            pending.ranges.push_back({start, end, step});
        }
        if (!PriceGrid::from_ranges(pending.ranges)) throw InvalidRecord{};
    } else if (kind == "gap" || kind == "resync") {
        event.kind = kind == "gap" ? EventKind::gap : EventKind::resync;
        if (kind == "gap") {
            event.conn = integer(root, "conn");
            event.sid = integer(root, "sid");
            event.expected_seq = integer(root, "expected_seq");
            event.got_seq = integer(root, "got_seq");
        }
        auto tickers = root.find_field_unordered("tickers").get_array().value();
        for (auto item : tickers)
            pending.markets.push_back(string_value(item.value()));
    } else if (kind == "rest_book") {
        event.kind = EventKind::rest_book;
        pending.market = string_field(root, "ticker");
        event.t_send_ns = integer(root, "t_send_ns");
        if (event.t_send_ns > event.t_recv_ns) throw InvalidRecord{};
        auto book = root.find_field_unordered("book").get_object().value();
        auto orderbook = book.find_field_unordered("orderbook_fp").get_object().value();
        pending.yes = levels(orderbook, "yes_dollars");
        pending.no = levels(orderbook, "no_dollars");
    } else if (kind == "ws") {
        auto message = root.find_field_unordered("msg").get_object().value();
        const auto type = string_field(message, "type");
        if (type != "orderbook_snapshot" && type != "orderbook_delta" &&
            type != "trade" && type != "ticker") return Disposition::unknown_message;
        // All envelope accesses are complete before descending into msg.
        if (event.conn == -1) throw InvalidRecord{};
        event.sid = integer(message, "sid");
        event.seq = integer(message, "seq", type == "trade" || type == "ticker");
        auto payload = message.find_field_unordered("msg").get_object().value();
        pending.market = string_field(payload, "market_ticker");
        if (type == "orderbook_snapshot") {
            event.kind = EventKind::snapshot;
            pending.yes = levels(payload, "yes_dollars_fp");
            pending.no = levels(payload, "no_dollars_fp");
        } else if (type == "orderbook_delta") {
            event.kind = EventKind::delta;
            const auto side = string_field(payload, "side");
            if (side == "yes") event.side = Side::yes;
            else if (side == "no") event.side = Side::no;
            else throw InvalidRecord{};
            event.price_ticks = price(payload, "price_dollars");
            event.quantity = quantity_text(string_field(payload, "delta_fp"), true);
        } else if (type == "trade") {
            event.kind = EventKind::trade;
            event.price_ticks = price(payload, "yes_price_dollars");
            event.no_price_ticks = price(payload, "no_price_dollars");
            event.quantity = quantity_text(string_field(payload, "count_fp"));
            pending.trade_id = string_field(payload, "trade_id", true);
            const auto taker = string_field(payload, "taker_side", true);
            if (taker == "yes") event.taker_side = TakerSide::yes;
            else if (taker == "no") event.taker_side = TakerSide::no;
            else if (!taker.empty()) throw InvalidRecord{};
            event.ts_ms = integer(payload, "ts_ms", true);
        } else {
            event.kind = EventKind::ticker;
            event.price_ticks = price(payload, "price_dollars", true);
            event.yes_bid_ticks = price(payload, "yes_bid_dollars", true);
            event.yes_ask_ticks = price(payload, "yes_ask_dollars", true);
            event.ts_ms = integer(payload, "ts_ms", true);
        }
    } else {
        return Disposition::unknown_envelope;
    }
    return Disposition::event;
}

std::uint32_t checked_id(std::size_t value) {
    if (value >= no_id) throw std::length_error("Parsed payload exceeds uint32 capacity");
    return static_cast<std::uint32_t>(value);
}

using InternMap = std::unordered_map<std::string, std::uint32_t>;
std::uint32_t intern(const std::string& text, FileParseResult& out, InternMap& ids) {
    const auto found = ids.find(text);
    if (found != ids.end()) return found->second;
    const auto id = checked_id(out.tickers.size());
    out.tickers.push_back(text);
    ids.emplace(text, id);
    return id;
}

std::uint32_t store_text(const std::string& text, FileParseResult& out) {
    if (text.empty()) return no_id;
    const auto id = checked_id(out.texts.size());
    out.texts.push_back(text);
    return id;
}

template<class T>
PoolRange append(std::vector<T>& pool, const std::vector<T>& values) {
    if (values.empty()) return {};
    const auto offset = checked_id(pool.size());
    const auto count = checked_id(values.size());
    if (values.size() > no_id - pool.size()) throw std::length_error("Parsed payload pool full");
    pool.insert(pool.end(), values.begin(), values.end());
    return {offset, count};
}

void commit(Pending& p, FileParseResult& out, InternMap& ids) {
    auto& event = p.event;
    if (!p.market.empty()) event.market_id = intern(p.market, out, ids);
    event.trade_id = store_text(p.trade_id, out);
    event.event_ticker = store_text(p.event_ticker, out);
    event.price_level_structure = store_text(p.structure, out);
    event.yes_levels = append(out.levels, p.yes);
    event.no_levels = append(out.levels, p.no);
    event.price_ranges = append(out.price_ranges, p.ranges);
    if (!p.markets.empty()) {
        const auto offset = checked_id(out.market_ids.size());
        const auto count = checked_id(p.markets.size());
        if (p.markets.size() > no_id - out.market_ids.size())
            throw std::length_error("Parsed market list pool full");
        for (const auto& market : p.markets) out.market_ids.push_back(intern(market, out, ids));
        event.markets = {offset, count};
    }
    out.events.push_back(event);
}

struct Boundary { std::size_t offset; std::uint64_t line; };

std::vector<Boundary> boundaries(std::string_view bytes, std::size_t count) {
    count = std::min(count, bytes.size());
    std::vector<Boundary> result{{0, 0}};
    std::size_t part = 1;
    std::uint64_t line = 0;
    // Nominal byte boundaries are advanced to the next line, with duplicates
    // coalesced when several boundaries fall inside one long record.
    const auto target = [&](std::size_t p) {
        return (bytes.size() / count) * p + std::min(p, bytes.size() % count);
    };
    for (std::size_t i = 0; i < bytes.size(); ++i) {
        if (bytes[i] != '\n') continue;
        ++line;
        if (part < count && i + 1 >= target(part)) {
            if (i + 1 < bytes.size()) result.push_back({i + 1, line});
            do { ++part; } while (part < count && i + 1 >= target(part));
        }
    }
    result.push_back({bytes.size(), line});
    return result;
}

FileParseResult parse_chunk(std::string_view bytes, std::uint64_t first_line) {
    FileParseResult result;
    result.status = ParseStatus::ok;
    InternMap ids;
    od::parser parser;
    std::string padded;
    std::size_t offset = 0;
    while (offset < bytes.size()) {
        auto end = bytes.find('\n', offset);
        if (end == std::string_view::npos) end = bytes.size();
        auto line = bytes.substr(offset, end - offset);
        offset = end == bytes.size() ? end : end + 1;
        const auto line_index = first_line + result.physical_lines++;
        if (line.size() > std::numeric_limits<std::size_t>::max() - simdjson::SIMDJSON_PADDING)
            throw std::length_error("JSON line too large");
        padded.resize(line.size() + simdjson::SIMDJSON_PADDING);
        std::memcpy(padded.data(), line.data(), line.size());
        std::fill(padded.begin() + static_cast<std::ptrdiff_t>(line.size()), padded.end(), '\0');
        bool syntax_valid = false;
        try {
            auto document = parser.iterate(simdjson::padded_string_view(
                padded.data(), line.size(), padded.size())).value();
            validate_json(document);
            if (!document.at_end()) throw InvalidRecord{MalformedReason::invalid_json};
            syntax_valid = true;
            document.rewind();
            Pending pending;
            pending.event.line_index = line_index;
            switch (decode(document.get_object().value(), pending)) {
            case Disposition::event: commit(pending, result, ids); break;
            case Disposition::unknown_message: ++result.unknown_messages; break;
            case Disposition::unknown_envelope: ++result.unknown_envelopes; break;
            }
        } catch (const UnsupportedMode&) {
            FileParseResult failed;
            failed.status = ParseStatus::unsupported_pricing_mode;
            return failed;
        } catch (const InvalidRecord& error) {
            result.malformed.push_back({line_index + 1, error.reason});
        } catch (const simdjson::simdjson_error& error) {
            if (error.error() == simdjson::MEMALLOC) throw std::bad_alloc{};
            result.malformed.push_back({line_index + 1, syntax_valid
                ? MalformedReason::invalid_record : MalformedReason::invalid_json});
        }
    }
    return result;
}

void rebase(PoolRange& range, std::size_t base) {
    if (range.count != 0) range.offset = checked_id(base + range.offset);
}

FileParseResult merge(std::vector<FileParseResult>& chunks) {
    FileParseResult out;
    out.status = ParseStatus::ok;
    InternMap ids;
    for (auto& chunk : chunks) {
        if (chunk.status != ParseStatus::ok) {
            // No partial output is usable after an unsupported pricing mode.
            FileParseResult failed;
            failed.status = chunk.status;
            return failed;
        }
        std::vector<std::uint32_t> remap;
        remap.reserve(chunk.tickers.size());
        for (const auto& ticker : chunk.tickers) remap.push_back(intern(ticker, out, ids));
        const auto text_base = out.texts.size();
        const auto level_base = out.levels.size();
        const auto market_base = out.market_ids.size();
        const auto range_base = out.price_ranges.size();
        (void)append(out.texts, chunk.texts);
        (void)append(out.levels, chunk.levels);
        (void)append(out.price_ranges, chunk.price_ranges);
        for (auto& id : chunk.market_ids) id = remap[id];
        (void)append(out.market_ids, chunk.market_ids);
        for (auto event : chunk.events) {
            if (event.market_id != no_id) event.market_id = remap[event.market_id];
            for (auto* id : {&event.trade_id, &event.event_ticker, &event.price_level_structure})
                if (*id != no_id) *id = checked_id(text_base + *id);
            rebase(event.yes_levels, level_base);
            rebase(event.no_levels, level_base);
            rebase(event.markets, market_base);
            rebase(event.price_ranges, range_base);
            out.events.push_back(event);
        }
        out.physical_lines += chunk.physical_lines;
        out.unknown_messages += chunk.unknown_messages;
        out.unknown_envelopes += chunk.unknown_envelopes;
        out.malformed.insert(out.malformed.end(), chunk.malformed.begin(), chunk.malformed.end());
        chunk = {}; // Release worker payloads as soon as their merge is complete.
    }
    return out;
}
}  // namespace

FileParseResult parse_file(const std::filesystem::path& path, std::size_t chunks) {
    FileParseResult result;
    if (chunks == 0) { result.status = ParseStatus::invalid_chunk_count; return result; }
    const Mapping mapping(path);
    if (!mapping.ok()) { result.status = ParseStatus::io_error; return result; }
    const auto bytes = mapping.bytes();
    if (bytes.empty()) { result.status = ParseStatus::ok; return result; }
    const auto cuts = boundaries(bytes, chunks);
    const auto count = cuts.size() - 1;
    if (count == 1) return parse_chunk(bytes, 0);
    std::vector<FileParseResult> results(count);
    std::vector<std::exception_ptr> errors(count);
    std::atomic<std::size_t> next{0};
    const auto workers = std::min(count, static_cast<std::size_t>(
        std::max(1U, std::thread::hardware_concurrency())));
    {
        std::vector<std::jthread> threads;
        threads.reserve(workers);
        for (std::size_t i = 0; i < workers; ++i) {
            threads.emplace_back([&] {
                for (;;) {
                    const auto index = next.fetch_add(1, std::memory_order_relaxed);
                    if (index >= count) return;
                    try {
                        results[index] = parse_chunk(bytes.substr(cuts[index].offset,
                            cuts[index + 1].offset - cuts[index].offset), cuts[index].line);
                    } catch (...) {
                        errors[index] = std::current_exception();
                    }
                }
            });
        }
    } // Join all readers before merging or releasing the mapping.
    for (const auto& error : errors) if (error) std::rethrow_exception(error);
    return merge(results);
}

}  // namespace brier
