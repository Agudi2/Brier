#include <gtest/gtest.h>

TEST(Scaffold, Cxx20Available) {
    EXPECT_GE(__cplusplus, 202002L);
}

#include "test_fixed_point.cpp"

#include "test_grid.cpp"

#include "test_book.cpp"

#include "test_parse.cpp"
