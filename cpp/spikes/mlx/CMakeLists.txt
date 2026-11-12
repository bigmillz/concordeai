# The MLX spike (CPP_PLAN.md §3.4, NOTES 6b611): arm64 only, linked against
# the MLX the app's venv already has (its wheel ships the C++ headers, the
# library and a CMake package), so nothing is downloaded or built from source.
#
#   cmake -S cpp/spikes/mlx -B <build> -G Ninja -DCMAKE_BUILD_TYPE=Release \
#     -DMLX_DIR="$HOME/Library/Application Support/MillenAI/venv/lib/python3.14/site-packages/mlx/share/cmake/MLX"
#   cmake --build <build>
cmake_minimum_required(VERSION 3.24)
set(CMAKE_OSX_ARCHITECTURES "arm64" CACHE STRING "MLX is Apple silicon only" FORCE)
project(concorde_mlx_spike LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(MLX CONFIG REQUIRED)
add_executable(llama_spike llama_spike.cpp)
target_link_libraries(llama_spike PRIVATE mlx)
# find libmlx.dylib (and its mlx.metallib beside it) where the wheel keeps it
get_target_property(_mlx_loc mlx IMPORTED_LOCATION_RELEASE)
if(_mlx_loc)
  get_filename_component(_mlx_dir "${_mlx_loc}" DIRECTORY)
  set_target_properties(llama_spike PROPERTIES BUILD_RPATH "${_mlx_dir}")
endif()
