cmake_minimum_required(VERSION 3.10)
project(plc_library C)

set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Include directories
include_directories(${CMAKE_SOURCE_DIR}/core/generated/plc_lib
                    ${CMAKE_SOURCE_DIR}/core/generated/plc_lib/lib)

# Compiler options
add_compile_options(-pedantic -Wextra -fstack-protector-strong
    -D_FORTIFY_SOURCE=2 -O3 -Wformat
    -Werror=format-security -fPIC -fPIE)

# Step 1: Compile object files
add_library(plc_objs OBJECT
    ${CMAKE_SOURCE_DIR}/core/generated/plc_lib/Res0.c
    ${CMAKE_SOURCE_DIR}/core/generated/plc_lib/Config0.c
    ${CMAKE_SOURCE_DIR}/core/generated/plc_lib/debug.c
)

# Step 2: Create shared library
add_library(plc SHARED
    $<TARGET_OBJECTS:plc_objs>
    ${CMAKE_SOURCE_DIR}/core/generated/plc_lib/glueVars.c
)
