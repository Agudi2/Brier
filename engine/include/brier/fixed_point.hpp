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

[[nodiscard]] constexpr ParseResult<std::int32_t> parse_price(std::string_view) noexcept {
    return {std::numeric_limits<std::int32_t>::min(), ParseError::none};
}

[[nodiscard]] constexpr ParseResult<std::int64_t> parse_quantity(std::string_view) noexcept {
    return {std::numeric_limits<std::int64_t>::min(), ParseError::none};
}

// The seven characters include the terminating NUL byte.
[[nodiscard]] constexpr std::array<char, 7> format_price(std::int32_t) noexcept {
    return {'0', '.', '0', '0', '0', '0', '\0'};
}

}  // namespace brier
