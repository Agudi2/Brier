#pragma once

#include "brier/parse.hpp"

#include <cstddef>
#include <cstdint>
#include <optional>
#include <vector>

namespace brier {
enum class ValidationStatus { ok, invalid_input, invalid_thread_count, not_implemented };
struct ValidationOptions { std::size_t threads = 1; std::size_t max_diffs = 10; };
struct ValidationDiff {
    std::uint32_t market_id = no_id;
    std::uint64_t rest_line_index = 0, state_line_index = 0;
    std::vector<Level> rest_yes, rest_no, engine_yes, engine_no;
    bool operator==(const ValidationDiff&) const = default;
};
struct ValidationResult {
    ValidationStatus status = ValidationStatus::not_implemented;
    std::uint64_t samples = 0, compared = 0, excluded_invalid = 0;
    std::uint64_t full_depth_matches = 0, top_of_book_matches = 0, mismatches = 0;
    std::optional<double> full_depth_match_rate, top_of_book_match_rate;
    std::vector<ValidationDiff> diffs;
};
// Match against any valid stream state in the inclusive REST request window.
// Windows with no valid state are excluded. Rates are absent if none compare.
// Invalid input returns no partial report; worker exceptions propagate after join.
[[nodiscard]] ValidationResult validate(const FileParseResult&, ValidationOptions = {});
}  // namespace brier
