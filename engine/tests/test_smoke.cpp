#include <gtest/gtest.h>

TEST(Scaffold, Cxx20Available) {
    EXPECT_GE(__cplusplus, 202002L);
}
