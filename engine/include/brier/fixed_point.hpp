#pragma once

#include <array>
#include <cstdint>
#include <limits>
#include <string_view>

namespace brier {

enum class ParseError : std::uint8_t { none, invalid_format, out_of_range };

template <typename T>
struct ParseResult {
    T value;
    ParseError error;
    [[nodiscard]] constexpr explicit operator bool() const noexcept {
        return error == ParseError::none;
    }
};

namespace detail {

[[nodiscard]] constexpr ParseResult<std::uint64_t> parse_scaled(
    std::string_view text, std::uint32_t decimals, std::uint64_t limit,
    bool allow_negative, bool& negative) noexcept {
    if (text.empty()) {
        return {0, ParseError::invalid_format};
    }
    negative = text.front() == '-';
    if (negative && !allow_negative) {
        return {0, ParseError::invalid_format};
    }
    std::size_t position = negative ? 1 : 0;
    if (position == text.size() || text[position] < '0' || text[position] > '9') {
        return {0, ParseError::invalid_format};
    }
    if (text[position] == '0' && position + 1 < text.size() &&
        text[position + 1] >= '0' && text[position + 1] <= '9') {
        return {0, ParseError::invalid_format};
    }

    std::uint64_t scale = 1;
    for (std::uint32_t i = 0; i < decimals; ++i) {
        scale *= 10;
    }
    const auto whole_limit = limit / scale;
    std::uint64_t whole = 0;
    while (position < text.size() && text[position] >= '0' && text[position] <= '9') {
        const auto digit = static_cast<std::uint64_t>(text[position] - '0');
        if (whole > whole_limit / 10 ||
            (whole == whole_limit / 10 && digit > whole_limit % 10)) {
            return {0, ParseError::out_of_range};
        }
        whole = whole * 10 + digit;
        ++position;
    }

    std::uint64_t fractional = 0;
    std::uint32_t digits = 0;
    if (position < text.size() && text[position] == '.') {
        ++position;
        if (position == text.size()) {
            return {0, ParseError::invalid_format};
        }
        while (position < text.size() && text[position] >= '0' && text[position] <= '9') {
            if (++digits > decimals) {
                return {0, ParseError::invalid_format};
            }
            fractional = fractional * 10 + static_cast<std::uint64_t>(text[position] - '0');
            ++position;
        }
    }
    if (position != text.size()) {
        return {0, ParseError::invalid_format};
    }
    while (digits++ < decimals) {
        fractional *= 10;
    }
    const auto whole_units = whole * scale;
    if (whole_units > limit || fractional > limit - whole_units) {
        return {0, ParseError::out_of_range};
    }
    return {whole_units + fractional, ParseError::none};
}

}  // namespace detail

[[nodiscard]] constexpr ParseResult<std::int32_t> parse_price(std::string_view text) noexcept {
    bool negative = false;
    const auto result = detail::parse_scaled(text, 4, 10000, false, negative);
    return {static_cast<std::int32_t>(result.value), result.error};
}

[[nodiscard]] constexpr ParseResult<std::int64_t> parse_quantity(std::string_view text) noexcept {
    bool negative = false;
    const bool has_minus = !text.empty() && text.front() == '-';
    const auto positive_limit = static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
    const auto limit = positive_limit + static_cast<std::uint64_t>(has_minus);
    const auto result = detail::parse_scaled(text, 2, limit, true, negative);
    if (result.error != ParseError::none) {
        return {0, result.error};
    }
    if (negative && result.value == positive_limit + 1) {
        return {std::numeric_limits<std::int64_t>::min(), ParseError::none};
    }
    const auto value = static_cast<std::int64_t>(result.value);
    return {negative ? -value : value, ParseError::none};
}

// Valid input is 0..10000 ticks. The seven characters include the trailing NUL.
[[nodiscard]] constexpr std::array<char, 7> format_price(std::int32_t ticks) noexcept {
    return {
        static_cast<char>('0' + ticks / 10000), '.',
        static_cast<char>('0' + (ticks / 1000) % 10),
        static_cast<char>('0' + (ticks / 100) % 10),
        static_cast<char>('0' + (ticks / 10) % 10),
        static_cast<char>('0' + ticks % 10), '\0'
    };
}

}  // namespace brier
