cmake_minimum_required(VERSION 3.20)
project(fix-exchange CXX)

set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_library(QUICKFIX_LIB NAMES quickfix REQUIRED)
find_path(QUICKFIX_INCLUDE NAMES quickfix/Application.h REQUIRED)

file(GLOB_RECURSE SOURCES src/*.cpp)

add_executable(fix-exchange ${SOURCES})

target_include_directories(fix-exchange PRIVATE ${QUICKFIX_INCLUDE} src)

target_link_libraries(fix-exchange PRIVATE
    ${QUICKFIX_LIB}
    ssl
    crypto
    pthread
)

target_compile_options(fix-exchange PRIVATE -Wall -Wextra -Wpedantic)
