cmake_minimum_required(VERSION 3.10)
project(plc_project C)

# Enable position independent code globally (for shared libraries)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Include directories
include_directories(./core/generated ./core/generated/lib)

# Set debug flags
set(CMAKE_C_FLAGS_DEBUG "-g")

# Make compiler inline functions
add_compile_options(-O3)

# Step 1: Compile object files
add_library(plc_objs OBJECT
    core/generated/Res0.c
    core/generated/Config0.c
    core/generated/debug.c
)

# Step 2: Create shared library from object files + glueVars.c
add_library(plc SHARED
    $<TARGET_OBJECTS:plc_objs>
    core/generated/glueVars.c
)

add_compile_options(-Wall -Werror -pedantic -Wextra -fstack-protector-strong
		-D_FORTIFY_SOURCE=2 -O2 -Wformat
		-Werror=format-security -fPIC -fPIE)

# Step 3: Build the executable and link against the shared library
add_executable(plc_main
		core/src/plc_main.c
		core/src/log.c
        core/src/utils/utils.c
        core/src/image_tables.c
		core/src/watchdog.c)

target_link_libraries(plc_main plc dl pthread)  # dl needed for dlopen/dlsym
