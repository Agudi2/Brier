#include "brier/histogram.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <random>
#include <thread>
#include <type_traits>
#include <vector>

#include <gtest/gtest.h>

namespace histogram_tests {
using brier::LatencyHistogram;
using U64 = std::uint64_t;
constexpr auto max_count = std::numeric_limits<U64>::max();
static_assert(std::is_nothrow_default_constructible_v<LatencyHistogram>);
static_assert(noexcept(LatencyHistogram{}.record(1)));
static_assert(noexcept(LatencyHistogram{}.merge(LatencyHistogram{})));
static_assert(noexcept(LatencyHistogram{}.percentile(50)));
static_assert(noexcept(LatencyHistogram{}.clear()));

void expect_bucket(const LatencyHistogram& histogram, double percentile, U64 exact) {
    const auto estimate = histogram.percentile(percentile);
    ASSERT_TRUE(estimate.has_value()) << percentile;
    ASSERT_GE(*estimate, exact) << percentile;
    // Integer form of <=1% upper-bucket error; avoids overflow near UINT64_MAX.
    EXPECT_LE(*estimate-exact, exact/100) << percentile;
}

TEST(Histogram, EmptyAndInvalidPercentilesAreAbsentWithoutMutation) {
    LatencyHistogram histogram;
    EXPECT_EQ(histogram.count(),0U);
    for (const auto p : {0.0,1.0,50.0,99.0,100.0}) {
        EXPECT_FALSE(histogram.percentile(p));
    }
    ASSERT_TRUE(histogram.record(7));
    for (const auto p : {-0.01,100.01,std::numeric_limits<double>::quiet_NaN(),
                         std::numeric_limits<double>::infinity(),
                         -std::numeric_limits<double>::infinity()}) {
        EXPECT_FALSE(histogram.percentile(p));
    }
    EXPECT_EQ(histogram.count(),1U);
    expect_bucket(histogram,50,7);
}

TEST(Histogram, UniformDistributionUsesNearestRankWithinOnePercent) {
    LatencyHistogram histogram;
    for (U64 value=1; value<=1000; ++value) ASSERT_TRUE(histogram.record(value));
    ASSERT_EQ(histogram.count(),1000U);
    // ceil(p*1000/100) gives ranks 10,500,900,990,1000. p0 selects rank 1.
    for (const auto [p,exact] : std::array<std::pair<double,U64>,6>{
            {{0,1},{1,10},{50,500},{90,900},{99,990},{100,1000}}}) {
        expect_bucket(histogram,p,exact);
    }
    U64 previous=0;
    for (int p=0; p<=100; ++p) {
        const auto estimate=histogram.percentile(p);
        ASSERT_TRUE(estimate);
        EXPECT_GE(*estimate,previous);
        previous=*estimate;
    }
    EXPECT_EQ(histogram.count(),1000U); // Queries do not consume samples.
}

TEST(Histogram, SingleValuesCoverZeroPowersAndUint64Boundaries) {
    const std::array<U64,24> values{0,1,2,63,64,99,100,127,128,129,255,256,257,
        511,512,513,1000,65535,65536,1000000,1000000000,U64{1}<<63,max_count-1,max_count};
    for (const auto value : values) {
        SCOPED_TRACE(value);
        LatencyHistogram histogram;
        ASSERT_TRUE(histogram.record(value,3));
        EXPECT_EQ(histogram.count(),3U);
        for (const auto p : {0.0,0.1,50.0,99.9,100.0}) expect_bucket(histogram,p,value);
    }
}

TEST(Histogram, WeightedDistributionChecksRankTransitions) {
    LatencyHistogram histogram;
    ASSERT_TRUE(histogram.record(1,1));
    ASSERT_TRUE(histogram.record(10,49));
    ASSERT_TRUE(histogram.record(100,49));
    ASSERT_TRUE(histogram.record(1000,1));
    EXPECT_EQ(histogram.count(),100U); // 1+49+49+1, not four calls.
    // Rank 1 -> 1; ranks 2..50 -> 10; 51..99 -> 100; rank 100 -> 1000.
    expect_bucket(histogram,0,1);
    expect_bucket(histogram,1,1);
    expect_bucket(histogram,2,10);
    expect_bucket(histogram,50,10);
    expect_bucket(histogram,51,100);
    expect_bucket(histogram,99,100);
    expect_bucket(histogram,100,1000);
}

TEST(Histogram, EmptyMergeSelfMergeZeroWeightAndClearPermitReuse) {
    LatencyHistogram histogram, empty;
    ASSERT_TRUE(histogram.record(999,0));
    EXPECT_EQ(histogram.count(),0U);
    EXPECT_FALSE(histogram.percentile(50));
    ASSERT_TRUE(histogram.merge(empty));
    ASSERT_TRUE(histogram.record(42,3));
    const auto estimate=histogram.percentile(50);
    ASSERT_TRUE(histogram.merge(histogram));
    EXPECT_EQ(histogram.count(),6U); // Self-merge doubles three observations.
    EXPECT_EQ(histogram.percentile(50),estimate);
    ASSERT_TRUE(histogram.merge(empty));
    EXPECT_EQ(histogram.count(),6U);
    EXPECT_EQ(empty.count(),0U);
    histogram.clear();
    EXPECT_EQ(histogram.count(),0U);
    EXPECT_FALSE(histogram.percentile(0));
    EXPECT_FALSE(histogram.percentile(100));
    histogram.clear();
    ASSERT_TRUE(histogram.record(0));
    expect_bucket(histogram,100,0);
}

TEST(Histogram, OverflowIsAtomicForRecordAndMerge) {
    LatencyHistogram histogram, one, empty;
    ASSERT_TRUE(histogram.record(10,max_count-1));
    ASSERT_TRUE(histogram.record(20));
    ASSERT_EQ(histogram.count(),max_count);
    const auto low=histogram.percentile(50), high=histogram.percentile(100);
    ASSERT_TRUE(low); ASSERT_TRUE(high);
    EXPECT_EQ(*low,10U); EXPECT_EQ(*high,20U);
    EXPECT_FALSE(histogram.record(999));
    ASSERT_TRUE(one.record(30));
    EXPECT_FALSE(histogram.merge(one));
    EXPECT_FALSE(histogram.merge(histogram));
    EXPECT_EQ(histogram.count(),max_count);
    EXPECT_EQ(histogram.percentile(50),low);
    EXPECT_EQ(histogram.percentile(100),high);
    EXPECT_EQ(one.count(),1U); // Failed merge does not consume its source.
    ASSERT_TRUE(histogram.record(999,0));
    ASSERT_TRUE(histogram.merge(empty));
    EXPECT_EQ(histogram.count(),max_count);
    histogram.clear();
    ASSERT_TRUE(histogram.record(5)); // Failure did not poison later reuse.
    expect_bucket(histogram,100,5);
}

TEST(Histogram, SeededSkewedSamplesAndMergeOrdersMatchIndependentOracle) {
    std::mt19937_64 rng(0xB919U);
    std::vector<U64> values;
    values.reserve(16000);
    LatencyHistogram single;
    std::array<LatencyHistogram,7> parts;
    for (std::size_t i=0; i<16000; ++i) {
        // Most observations are small; occasional full-width values exercise long tails.
        const U64 value=i%19==0 ? rng() : rng()%10000;
        values.push_back(value);
        ASSERT_TRUE(single.record(value));
        ASSERT_TRUE(parts[i%parts.size()].record(value));
    }
    LatencyHistogram forward, reverse;
    for (const auto& part : parts) ASSERT_TRUE(forward.merge(part));
    for (auto it=parts.rbegin(); it!=parts.rend(); ++it) ASSERT_TRUE(reverse.merge(*it));
    EXPECT_EQ(single.count(),values.size());
    EXPECT_EQ(forward.count(),single.count());
    EXPECT_EQ(reverse.count(),single.count());
    std::sort(values.begin(),values.end());
    // Percentile is units/10; rank = ceil(N*units/1000), calculated with integers.
    for (const U64 units : {0U,1U,10U,500U,900U,990U,999U,1000U}) {
        const auto rank=units==0 ? U64{1} : (values.size()*units+999)/1000;
        const auto p=static_cast<double>(units)/10;
        expect_bucket(single,p,values[rank-1]);
        EXPECT_EQ(forward.percentile(p),single.percentile(p));
        EXPECT_EQ(reverse.percentile(p),single.percentile(p));
    }
    // Probe every integer percentile, rather than allowing extra error after a merge.
    for (int p=0; p<=100; ++p) {
        EXPECT_EQ(forward.percentile(p),single.percentile(p));
        EXPECT_EQ(reverse.percentile(p),single.percentile(p));
    }
}

TEST(Histogram, WorkerOwnedHistogramsMergeAfterJoining) {
    std::array<LatencyHistogram,4> parts;
    std::array<bool,4> success{true,true,true,true};
    {
        std::vector<std::jthread> workers;
        for (std::size_t id=0; id<parts.size(); ++id) {
            workers.emplace_back([&,id] {
                for (U64 i=1; i<=1000; ++i) {
                    if (!parts[id].record(i)) success[id]=false;
                }
            });
        }
    } // Join before reading or merging worker-owned state.
    LatencyHistogram merged;
    for (std::size_t id=0; id<parts.size(); ++id) {
        ASSERT_TRUE(success[id]);
        ASSERT_TRUE(merged.merge(parts[id]));
        EXPECT_EQ(parts[id].count(),1000U);
    }
    EXPECT_EQ(merged.count(),4000U); // Four copies of uniform 1..1000.
    expect_bucket(merged,50,500);
    expect_bucket(merged,99,990);
}
}  // namespace histogram_tests
