// The settings and the places, by the same rules as millenai.py and the
// Tauri shell (shell/src-tauri/src/main.rs): data_home, real_app_dir,
// python_path, the log and bytecode folders. Step 1 of CPP_PLAN.md.
#pragma once

#include <chrono>
#include <optional>
#include <string>

namespace concorde {

// CONCORDE_SHELL_TIMING=1 prints each launch milestone's time since the
// process started to stderr, in the Tauri shell's exact format
// ("[shell-timing] %7.1f ms  what"), so one script measures both.
void mark(const std::string& what);
// The wall-clock epoch (ms) at process start, for converting a page's
// performance.timeOrigin-based paint time into the same clock.
double start_epoch_ms();
double since_start_ms();

std::optional<std::string> env(const char* name);
bool dev_copy();                      // MILLENAI_DEV=1 (6b319)
std::string expand_tilde(const std::string& p);
std::string real_app_dir();           // ~/Library/Application Support/MillenAI, %LOCALAPPDATA%\MillenAI
std::string data_home();              // a dev copy's MILLENAI_HOME, else the real folder
std::string python_path();            // CONCORDE_PYTHON, else the app's venv
std::string log_dir(const std::string& home);
std::string pycache_dir(const std::string& home);
std::string join(const std::string& a, const std::string& b);
bool file_exists(const std::string& p);
bool make_dirs(const std::string& p, int mode = 0700);
std::string read_file(const std::string& p);
std::string tail_of(const std::string& p, size_t lines);
// write via a temporary file and a rename, 0600 (the notes hold the key)
bool write_private_atomic(const std::string& p, const std::string& data);

// What each of the server's exit codes means (millenai.py's __main__).
const char* exit_meaning(int code, bool signalled);
// A stop worth a restart: not the updater's clean exit (0), a refused
// setting (2), another copy on the folder (3) or a launch that wanted its
// own window (4). Exactly the Tauri shell's rule (6b548).
bool restartable(int code, bool signalled);

}  // namespace concorde
