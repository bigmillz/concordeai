# ConcordeAI in C++ (CPP_PLAN.md). Step 1: the native shell and a front
# server that proxies every route to the Python server (millenai.py).
#
#   cmake -S cpp -B cpp/build -G Ninja -DCMAKE_BUILD_TYPE=Release
#   cmake --build cpp/build && ctest --test-dir cpp/build
#
# On a Mac the default is a universal build (arm64 + x86_64); pass
# -DCMAKE_OSX_ARCHITECTURES=arm64 for a faster dev build. Windows: x64 or
# ARM64 with MSVC (cmake -A x64 / -A ARM64); the window there is a stub
# (src/win/app_win.cpp, TODO(cpp-win)).
cmake_minimum_required(VERSION 3.24)

if(APPLE AND NOT CMAKE_OSX_ARCHITECTURES)
  set(CMAKE_OSX_ARCHITECTURES "arm64;x86_64" CACHE STRING "universal by default")
endif()
if(APPLE AND NOT CMAKE_OSX_DEPLOYMENT_TARGET)
  # the Tauri shell's minimum is 11.0; WKDownload needs 11.3
  set(CMAKE_OSX_DEPLOYMENT_TARGET "11.3" CACHE STRING "")
endif()

project(ConcordeAI LANGUAGES C CXX)
if(APPLE)
  enable_language(OBJCXX)
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_OBJCXX_STANDARD 20)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)  # clang-tidy reads it

get_filename_component(CONCORDE_REPO "${CMAKE_CURRENT_SOURCE_DIR}/.." ABSOLUTE)
# WHERE THE SERVER COMES FROM (6b615): the bundle's millenai.py, fonts/, vfx/
# and first-run files are taken from this tree. The repo by default; the
# preview points it at a snapshot of the newest beta (git archive of its head)
# so the page Patrick sees is the current one while cpp-core's base is older.
set(CONCORDE_SERVER_ROOT "${CONCORDE_REPO}" CACHE PATH "the tree the bundled server, page and assets come from")

# The version comes from millenai.py, the one place it is stored (CLAUDE.md).
file(STRINGS "${CONCORDE_SERVER_ROOT}/millenai.py" _ver REGEX "^APP_VERSION = " LIMIT_COUNT 1)
file(STRINGS "${CONCORDE_SERVER_ROOT}/millenai.py" _bld REGEX "^APP_BUILD = " LIMIT_COUNT 1)
string(REGEX REPLACE "^APP_VERSION = \"([^\"]+)\".*" "\\1" CONCORDE_VERSION "${_ver}")
string(REGEX REPLACE "^APP_BUILD = ([0-9]+).*" "\\1" CONCORDE_BUILD "${_bld}")
if(NOT CONCORDE_VERSION)
  set(CONCORDE_VERSION "0.0.0")
endif()
if(NOT CONCORDE_BUILD)
  set(CONCORDE_BUILD "0")
endif()

option(CONCORDE_PROBE "the measuring hook (CONCORDE_SHELL_PROBE); off for a release" ON)
option(CONCORDE_DEVTOOLS "the web inspector" ON)
option(CONCORDE_ALLOW_REAL_APP "let the preview open the real data folder (never, on the separate track)" OFF)
option(CONCORDE_SANITIZE "ASan + UBSan on every target (a Debug build)" OFF)
option(CONCORDE_FUZZ "the libFuzzer target (needs a clang with libFuzzer)" OFF)
set(CONCORDE_BUNDLE_ID "com.millen.millenai.cpp" CACHE STRING "the preview's own bundle id")
# THE INSTALLABLE PREVIEW (6b614): a double-clickable app that runs as a dev
# copy of its own (MillenAI-cpp, its own port, the updater off) so it never
# touches the real app or its data. Its name is the menus' and the Dock's.
option(CONCORDE_PREVIEW "build the installable C++ preview" OFF)
# the C++ engine (6b614): a built cpp/spikes/mlx llama_spike and the MLX it
# links (the venv wheel's lib folder), copied into Resources/engine when set
set(CONCORDE_ENGINE_BIN "" CACHE FILEPATH "a built llama_spike to bundle as Resources/engine/concorde-llama")
set(CONCORDE_MLX_LIB "" CACHE PATH "the folder holding libmlx.dylib, libjaccl.dylib and mlx.metallib")
if(CONCORDE_PREVIEW)
  set(CONCORDE_APP_NAME "ConcordeAI C++ Preview")
else()
  set(CONCORDE_APP_NAME "ConcordeAI")
endif()

if(MSVC)
  add_compile_options(/W4 /permissive- /utf-8)
else()
  add_compile_options(-Wall -Wextra -Wno-unused-parameter)
endif()
if(CONCORDE_SANITIZE AND NOT MSVC)
  add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer)
  add_link_options(-fsanitize=address,undefined)
endif()

# ---------------------------------------------------------------- the core
add_library(concorde_core STATIC
  src/core/env.cpp
  src/core/notes.cpp
  src/core/rewrite.cpp
  src/core/front.cpp
  src/core/sidecar.cpp
  src/core/host.cpp)
target_include_directories(concorde_core PUBLIC src)
target_compile_definitions(concorde_core PRIVATE CONCORDE_REPO_DIR="${CONCORDE_REPO}")
if(CONCORDE_ALLOW_REAL_APP)
  target_compile_definitions(concorde_core PRIVATE CONCORDE_ALLOW_REAL_APP=1)
endif()
find_package(Threads REQUIRED)
target_link_libraries(concorde_core PUBLIC Threads::Threads)
if(WIN32)
  target_link_libraries(concorde_core PUBLIC ws2_32)
endif()

# the windowless front: what the gauntlet and the conformance recorder drive
add_executable(concorde-front src/front_main.cpp)
target_link_libraries(concorde-front PRIVATE concorde_core)

# ---------------------------------------------------------------- the window
if(APPLE)
  set(ICON "${CONCORDE_REPO}/shell/src-tauri/icons/icon.icns")
  # DICTATION, NATIVE (6b642): voice.mm (capture, the bridge, the fallback
  # recognizer) and voice_sa.swift (SpeechAnalyzer, Swift-only) in this same
  # binary. The Swift file is compiled once per architecture by swiftc and
  # the objects lipo'd into one, so the universal build needs nothing of
  # CMake's Swift support; it links against the OS's own Swift runtime.
  find_program(SWIFTC swiftc REQUIRED)
  execute_process(COMMAND xcrun --show-sdk-path OUTPUT_VARIABLE _sdk OUTPUT_STRIP_TRAILING_WHITESPACE)
  set(VOICE_SWIFT "${CMAKE_CURRENT_SOURCE_DIR}/src/mac/voice_sa.swift")
  set(VOICE_OBJ "${CMAKE_CURRENT_BINARY_DIR}/voice_sa.o")
  set(_vobjs "")
  set(_vcmds "")
  foreach(_a IN LISTS CMAKE_OSX_ARCHITECTURES)
    list(APPEND _vobjs "${CMAKE_CURRENT_BINARY_DIR}/voice_sa-${_a}.o")
    list(APPEND _vcmds COMMAND "${SWIFTC}" -parse-as-library -O -whole-module-optimization
      -sdk "${_sdk}" -target "${_a}-apple-macos${CMAKE_OSX_DEPLOYMENT_TARGET}"
      -module-name ConcordeVoice -c "${VOICE_SWIFT}" -o "${CMAKE_CURRENT_BINARY_DIR}/voice_sa-${_a}.o")
  endforeach()
  add_custom_command(OUTPUT "${VOICE_OBJ}" ${_vcmds}
    COMMAND lipo -create ${_vobjs} -output "${VOICE_OBJ}"
    DEPENDS "${VOICE_SWIFT}" COMMENT "swiftc voice_sa.swift (${CMAKE_OSX_ARCHITECTURES})" VERBATIM)
  set_source_files_properties("${VOICE_OBJ}" PROPERTIES EXTERNAL_OBJECT TRUE GENERATED TRUE)
  add_executable(ConcordeAI MACOSX_BUNDLE src/mac/app.mm src/mac/engine_panel.mm src/mac/voice.mm "${VOICE_OBJ}" "${ICON}")
  set_source_files_properties("${ICON}" PROPERTIES MACOSX_PACKAGE_LOCATION Resources)
  target_compile_options(ConcordeAI PRIVATE -fobjc-arc)
  get_filename_component(_swiftbin "${SWIFTC}" REALPATH)
  execute_process(COMMAND xcrun --find swiftc OUTPUT_VARIABLE _xswiftc OUTPUT_STRIP_TRAILING_WHITESPACE)
  get_filename_component(_tc "${_xswiftc}" DIRECTORY)
  target_link_options(ConcordeAI PRIVATE "-L${_sdk}/usr/lib/swift" "-L${_tc}/../lib/swift/macosx"
    "-Wl,-rpath,/usr/lib/swift")
  target_link_libraries(ConcordeAI PRIVATE concorde_core "-framework Cocoa" "-framework WebKit"
    "-framework AVFoundation" "-framework Speech" "-framework CoreMedia")
  if(CONCORDE_PROBE)
    target_compile_definitions(ConcordeAI PRIVATE CONCORDE_PROBE=1)
  endif()
  if(CONCORDE_DEVTOOLS)
    target_compile_definitions(ConcordeAI PRIVATE CONCORDE_DEVTOOLS=1)
  endif()
  if(CONCORDE_PREVIEW)
    target_compile_definitions(ConcordeAI PRIVATE CONCORDE_PREVIEW=1)
  endif()
  configure_file(src/mac/Info.plist.in "${CMAKE_CURRENT_BINARY_DIR}/Info.plist" @ONLY)
  set_target_properties(ConcordeAI PROPERTIES
    MACOSX_BUNDLE_INFO_PLIST "${CMAKE_CURRENT_BINARY_DIR}/Info.plist"
    OUTPUT_NAME ConcordeAI)
  # Resources: the server and what it serves, the helpers when they are
  # built (shell/dictate, shell/pdftext: millenai.py looks beside itself)
  # the first-run files (6b537): launch.sh, extracted from build_macos_app.sh,
  # and the hash-pinned crypto-requirements.txt, written by shell_stamp.py
  # into shell/src-tauri/sidecar/ (git-ignored) exactly as for the Tauri bundle
  find_package(Python3 COMPONENTS Interpreter)
  if(Python3_FOUND)
    execute_process(COMMAND "${Python3_EXECUTABLE}" "${CONCORDE_SERVER_ROOT}/packaging/shell_stamp.py"
      WORKING_DIRECTORY "${CONCORDE_SERVER_ROOT}" OUTPUT_QUIET RESULT_VARIABLE _stamp)
  endif()
  set(SIDECAR "${CONCORDE_SERVER_ROOT}/shell/src-tauri/sidecar")
  set(RES "$<TARGET_BUNDLE_CONTENT_DIR:ConcordeAI>/Resources")
  add_custom_command(TARGET ConcordeAI POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E make_directory "${RES}"
    COMMAND ${CMAKE_COMMAND} -E copy_if_different "${CONCORDE_SERVER_ROOT}/millenai.py" "${RES}/millenai.py"
    COMMAND ${CMAKE_COMMAND} -E copy_directory "${CONCORDE_SERVER_ROOT}/fonts" "${RES}/fonts"
    COMMAND ${CMAKE_COMMAND} -E copy_directory "${CONCORDE_SERVER_ROOT}/vfx" "${RES}/vfx"
    COMMAND /bin/sh -c "for f in launch.sh crypto-requirements.txt engine-requirements.txt; do \
      [ -f '${SIDECAR}/'$f ] && cp -f '${SIDECAR}/'$f '${RES}/' || true; done"
    COMMAND /bin/sh -c "if [ -n '${CONCORDE_ENGINE_BIN}' ] && [ -x '${CONCORDE_ENGINE_BIN}' ]; then \
      mkdir -p '${RES}/engine' && cp -f '${CONCORDE_ENGINE_BIN}' '${RES}/engine/concorde-llama' && \
      cp -f '${CONCORDE_MLX_LIB}/libmlx.dylib' '${CONCORDE_MLX_LIB}/libjaccl.dylib' '${CONCORDE_MLX_LIB}/mlx.metallib' '${RES}/engine/' && \
      (install_name_tool -delete_rpath '${CONCORDE_MLX_LIB}' '${RES}/engine/concorde-llama' 2>/dev/null || true) && \
      (install_name_tool -add_rpath @executable_path '${RES}/engine/concorde-llama' 2>/dev/null || true) && \
      codesign --force --sign - '${RES}/engine/concorde-llama' '${RES}/engine/libmlx.dylib' '${RES}/engine/libjaccl.dylib'; fi"
    COMMAND /bin/sh -c "for h in dictate/concorde-dictate pdftext/concorde-pdftext; do \
      [ -x '${CONCORDE_SERVER_ROOT}/shell/'$h ] && cp -f '${CONCORDE_SERVER_ROOT}/shell/'$h '${RES}/' || true; done"
    # ad hoc now; build_shell_dmg.sh's Developer ID modes (6b549) sign this
    # bundle the same way once the C++ app ships
    COMMAND codesign --force --sign - --timestamp=none
      --entitlements "${CONCORDE_REPO}/shell/src-tauri/entitlements.plist"
      "$<TARGET_BUNDLE_DIR:ConcordeAI>"
    VERBATIM)
elseif(WIN32)
  add_executable(ConcordeAI WIN32 src/win/app_win.cpp)
  target_link_libraries(ConcordeAI PRIVATE concorde_core shell32 user32)
  target_compile_definitions(ConcordeAI PRIVATE UNICODE _UNICODE)
endif()

# ---------------------------------------------------------------- tests
enable_testing()
add_executable(rewrite_test tests/rewrite_test.cpp)
target_link_libraries(rewrite_test PRIVATE concorde_core)
add_test(NAME rewrite COMMAND rewrite_test)
# not a ctest: it means something only beside the desktop app on 8889 (6b618)
add_executable(port_fallback_check tests/port_fallback_check.cpp)
target_link_libraries(port_fallback_check PRIVATE concorde_core)
add_executable(voice_text_test tests/voice_text_test.cpp)
target_include_directories(voice_text_test PRIVATE src)
add_test(NAME voice_text COMMAND voice_text_test)

if(CONCORDE_FUZZ)
  add_executable(fuzz_rewrite tests/fuzz_rewrite.cpp src/core/rewrite.cpp src/core/notes.cpp src/core/env.cpp)
  target_include_directories(fuzz_rewrite PRIVATE src)
  # fuzzer + UBSan; add ASan with CONCORDE_SANITIZE (Homebrew LLVM 21's ASan
  # runtime hung at start on the macOS 27 beta; Apple clang's ASan works but
  # Apple clang ships no libFuzzer)
  target_compile_options(fuzz_rewrite PRIVATE -fsanitize=fuzzer,undefined -fno-sanitize-recover=undefined -g -O1)
  target_link_options(fuzz_rewrite PRIVATE -fsanitize=fuzzer,undefined)
endif()
