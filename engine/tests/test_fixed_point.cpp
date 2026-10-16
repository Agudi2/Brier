#include <brier/fixed_point.hpp>
#include <gtest/gtest.h>

#include <array>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <limits>
#include <random>
#include <regex>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {

std::string fixture_text() {
    const auto path = std::filesystem::path(__FILE__).parent_path().parent_path()
        .parent_path() / "fixtures" / "fixed_point_cases.json";
    std::ifstream file(path);
    if (!file) {
        throw std::runtime_error("shared fixed-point fixture missing");
    }
    return {std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
}

std::string section(const std::string& source, std::string_view name) {
    const auto key = '"' + std::string(name) + "\": [";
    const auto start = source.find(key);
    if (start == std::string::npos) {
        throw std::runtime_error("shared fixture section missing");
    }
    const auto end = source.find(']', start + key.size());
    if (end == std::string::npos) {
        throw std::runtime_error("shared fixture section unterminated");
    }
    return source.substr(start + key.size(), end - start - key.size());
}

struct NumericCase { std::string input; std::int64_t expected; };
std::vector<NumericCase> numeric_cases(const std::string& source, std::string_view name) {
    const std::regex pattern(R"rx("input"\s*:\s*"([^"]*)"\s*,\s*"expected"\s*:\s*(-?[0-9]+))rx");
    const auto body = section(source, name);
    std::vector<NumericCase> result;
    for (std::sregex_iterator it(body.begin(), body.end(), pattern), end; it != end; ++it) {
        result.push_back({(*it)[1].str(), std::stoll((*it)[2].str())});
    }
    if (result.empty()) { throw std::runtime_error("shared numeric cases missing"); }
    return result;
}

std::vector<std::string> invalid_cases(const std::string& source, std::string_view name) {
    const std::regex pattern(R"rx("([^"]*)")rx");
    const auto body = section(source, name);
    std::vector<std::string> result;
    for (std::sregex_iterator it(body.begin(), body.end(), pattern), end; it != end; ++it) {
        result.push_back((*it)[1].str());
    }
    if (result.empty()) { throw std::runtime_error("shared invalid cases missing"); }
    return result;
}

TEST(FixedPoint, SharedPriceCases) {
    const auto json = fixture_text();
    for (const auto& item : numeric_cases(json, "price_valid")) {
        const auto actual = brier::parse_price(item.input);
        ASSERT_EQ(actual.error, brier::ParseError::none) << item.input;
        EXPECT_EQ(actual.value, item.expected) << item.input;
    }
    for (const auto& text : invalid_cases(json, "price_invalid")) {
        EXPECT_NE(brier::parse_price(text).error, brier::ParseError::none) << text;
    }
}

TEST(FixedPoint, SharedQuantityCases) {
    const auto json = fixture_text();
    // 10.00 * 100 = 1000; -3.50 * 100 = -350; extrema equal INT64_MAX/MIN.
    for (const auto& item : numeric_cases(json, "quantity_valid")) {
        const auto actual = brier::parse_quantity(item.input);
        ASSERT_EQ(actual.error, brier::ParseError::none) << item.input;
        EXPECT_EQ(actual.value, item.expected) << item.input;
    }
    for (const auto& text : invalid_cases(json, "quantity_invalid")) {
        EXPECT_NE(brier::parse_quantity(text).error, brier::ParseError::none) << text;
    }
}

TEST(FixedPoint, SharedPriceFormatting) {
    const auto json = fixture_text();
    const std::regex pattern(R"rx("input"\s*:\s*([0-9]+)\s*,\s*"expected"\s*:\s*"([^"]*)")rx");
    const auto body = section(json, "price_format");
    int cases = 0;
    for (std::sregex_iterator it(body.begin(), body.end(), pattern), end; it != end; ++it) {
        const auto value = std::stoi((*it)[1].str());
        const auto actual = brier::format_price(value);
        EXPECT_EQ(std::string(actual.data()), (*it)[2].str());
        ++cases;
    }
    ASSERT_GT(cases, 0);
}

TEST(FixedPoint, SeededRoundTrip) {
    std::mt19937 rng(20261015);
    std::uniform_int_distribution<std::int32_t> ticks(0, 10000);
    for (int i = 0; i < 500; ++i) {
        const auto original = ticks(rng);
        const auto formatted = brier::format_price(original);
        const auto parsed = brier::parse_price(formatted.data());
        ASSERT_EQ(parsed.error, brier::ParseError::none);
        EXPECT_EQ(parsed.value, original);
    }
}

TEST(FixedPoint, AllPriceTicksRoundTrip) {
    for (std::int32_t original = 0; original <= 10000; ++original) {
        const auto formatted = brier::format_price(original);
        const auto parsed = brier::parse_price(formatted.data());
        ASSERT_EQ(parsed.error, brier::ParseError::none);
        ASSERT_EQ(parsed.value, original);
    }
}

}  // namespace
