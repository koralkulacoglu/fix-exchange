cmake_minimum_required(VERSION 3.20)
project(fix-exchange CXX)

set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)
FetchContent_Declare(
  abseil-cpp
  GIT_REPOSITORY https://github.com/abseil/abseil-cpp.git
  GIT_TAG        20240722.0
  GIT_SHALLOW    TRUE
)
set(ABSL_ENABLE_INSTALL OFF)
set(ABSL_PROPAGATE_CXX_STD ON)
FetchContent_MakeAvailable(abseil-cpp)

find_library(QUICKFIX_LIB NAMES quickfix REQUIRED)
find_path(QUICKFIX_INCLUDE NAMES quickfix/Application.h REQUIRED)
find_library(SQLITE3_LIB NAMES sqlite3 REQUIRED)

file(GLOB_RECURSE SOURCES src/*.cpp)

add_executable(fix-exchange ${SOURCES})

target_include_directories(fix-exchange PRIVATE ${QUICKFIX_INCLUDE} src)

target_link_libraries(fix-exchange PRIVATE
    ${QUICKFIX_LIB}
    ${SQLITE3_LIB}
    ssl
    crypto
    pthread
    absl::btree
    absl::flat_hash_map
)

target_compile_options(fix-exchange PRIVATE -Wall -Wextra -Wpedantic)
