#include <brier/fixed_point.hpp>
#include <brier/grid.hpp>
#include <gtest/gtest.h>

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <optional>
#include <random>
#include <regex>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {

std::string grid_fixture() {
    const auto path = std::filesystem::path(__FILE__).parent_path().parent_path()
        .parent_path() / "fixtures" / "price_grids.json";
    std::ifstream file(path);
    if (!file) { throw std::runtime_error("shared grid fixture missing"); }
    return {std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
}

std::string case_body(const std::string& source, std::string_view name) {
    const auto marker = "\"name\": \"" + std::string(name) + "\"";
    const auto start = source.find(marker);
    if (start == std::string::npos) { throw std::runtime_error("grid case missing"); }
    const auto next = source.find("\"name\":", start + marker.size());
    return source.substr(start, next - start);
}

std::vector<brier::PriceRange> ranges(const std::string& source, std::string_view name) {
    const auto body = case_body(source, name);
    const std::regex pattern(R"rx("start":\s*"([^"]+)",\s*"end":\s*"([^"]+)",\s*"step":\s*"([^"]+)")rx");
    std::vector<brier::PriceRange> result;
    for (std::sregex_iterator it(body.begin(), body.end(), pattern), end; it != end; ++it) {
        const auto start = brier::parse_price((*it)[1].str());
        const auto stop = brier::parse_price((*it)[2].str());
        const auto step = brier::parse_price((*it)[3].str());
        if (start.error != brier::ParseError::none || stop.error != brier::ParseError::none ||
            step.error != brier::ParseError::none) {
            throw std::runtime_error("grid fixture has invalid fixed-point text");
        }
        result.push_back({start.value, stop.value, step.value});
    }
    return result;
}

int expected_count(const std::string& source, std::string_view name) {
    const auto body = case_body(source, name);
    const std::regex pattern(R"rx("count":\s*([0-9]+))rx");
    std::smatch match;
    if (!std::regex_search(body, match, pattern)) {
        throw std::runtime_error("grid fixture count missing");
    }
    return std::stoi(match[1].str());
}

TEST(Grid, SharedSizesAndWholeCents) {
    const auto json = grid_fixture();
    for (const auto name : {"linear_cent", "deci_cent", "center_centi_edge_centi_cent",
                            "tapered_deci_cent"}) {
        const auto bands = ranges(json, name);
        const auto grid = brier::PriceGrid::from_ranges(bands);
        ASSERT_TRUE(grid.has_value()) << name;
        // Tapered: 101 + 80 + 100 = 281; band boundaries occur once.
        EXPECT_EQ(grid->size(), static_cast<std::size_t>(expected_count(json, name))) << name;
        for (std::int32_t cents = 0; cents <= 100; ++cents) {
            EXPECT_TRUE(grid->is_valid(cents * 100)) << name << " at " << cents;
        }
        ASSERT_GT(grid->size(), 0U);
        EXPECT_EQ(grid->price_of(0), 0);
        EXPECT_EQ(grid->price_of(grid->size() - 1), 10000);
        EXPECT_FALSE(grid->price_of(grid->size()).has_value());
    }
}

TEST(Grid, SharedBoundaryIndicesAndOffGrid) {
    const auto json = grid_fixture();
    const auto linear = brier::PriceGrid::from_ranges(ranges(json, "linear_cent"));
    const auto tapered = brier::PriceGrid::from_ranges(ranges(json, "tapered_deci_cent"));
    ASSERT_TRUE(linear.has_value());
    ASSERT_TRUE(tapered.has_value());
    // Linear index = ticks / 100; tapered middle adds 80 points after index 100.
    EXPECT_EQ(linear->index_of(5000), 50);
    EXPECT_EQ(tapered->index_of(1000), 100);
    EXPECT_EQ(tapered->index_of(5000), 140);
    EXPECT_EQ(tapered->index_of(9000), 180);
    EXPECT_EQ(tapered->price_of(180), 9000);
    EXPECT_FALSE(linear->is_valid(1));
    EXPECT_FALSE(tapered->is_valid(1001));
    EXPECT_FALSE(tapered->index_of(-1).has_value());
    EXPECT_FALSE(tapered->index_of(10001).has_value());
}

TEST(Grid, RejectsMalformedRanges) {
    const auto json = grid_fixture();
    for (const auto name : {"empty", "step_does_not_divide", "gap", "unsorted",
                            "overlap", "zero_step", "incomplete_coverage"}) {
        const auto bands = ranges(json, name);
        EXPECT_FALSE(brier::PriceGrid::from_ranges(bands).has_value()) << name;
    }
}

TEST(Grid, SeededRoundTripAndStrictIncrease) {
    const auto json = grid_fixture();
    std::mt19937 rng(20261016);
    for (const auto name : {"linear_cent", "deci_cent", "center_centi_edge_centi_cent",
                            "tapered_deci_cent"}) {
        const auto grid = brier::PriceGrid::from_ranges(ranges(json, name));
        ASSERT_TRUE(grid.has_value());
        ASSERT_GT(grid->size(), 0U);
        std::uniform_int_distribution<std::size_t> index(0, grid->size() - 1);
        for (int i = 0; i < 1000; ++i) {
            const auto chosen = index(rng);
            const auto price = grid->price_of(chosen);
            ASSERT_TRUE(price.has_value());
            EXPECT_EQ(grid->index_of(*price), chosen);
        }
        for (std::size_t i = 1; i < grid->size(); ++i) {
            ASSERT_LT(grid->price_of(i - 1).value(), grid->price_of(i).value());
        }
    }
}

}  // namespace
