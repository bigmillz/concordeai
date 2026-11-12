// THE PYTHON SERVER AS A SIDECAR (CPP_PLAN.md step 1), started exactly the
// way the Tauri shell starts it (main.rs start_sidecar, 6b532/6b548): the
// venv's python3 running BOOT_PY (the cached-bytecode bootstrap) over
// millenai.py, in its own folder, with MILLENAI_* passed through untouched
// (the dev-copy rules are the server's) plus MILLENAI_SHELL=tauri for the
// windowed app. One difference: the server binds a private port the front
// picks (MILLENAI_PORT), and the front owns the public one.
#pragma once

#include <map>
#include <optional>
#include <string>
#include <vector>

namespace concorde {

// The Tauri shell's BOOT_PY, byte for byte (6b548).
extern const char* const BOOT_PY;

struct SidecarSpec {
  std::string python;
  std::string script;  // millenai.py
  std::string cwd;     // the script's folder
  std::map<std::string, std::string> set_env;  // added to (or replacing in) this process's environment
  std::string log_path;  // stdout and stderr appended here; empty = inherit
};

class Sidecar {
 public:
  bool spawn(const SidecarSpec& s, std::string* err);
  long pid() const { return pid_; }
  bool running() const { return pid_ > 0 && !exited_; }
  // true once it has exited (then code/signalled say how); never blocks
  bool try_wait(int* code, bool* signalled);
  // SIGTERM (the server's handler saves a streaming answer, stops its
  // engines, drops its note), then SIGKILL after `grace` seconds.
  void stop(double grace_s = 8.0);
  // THE SERVER DIES WITH THE FRONT (6b613). A windowless server (no
  // MILLENAI_SHELL) does not watch its parent, so a front killed outright
  // (SIGKILL: the gauntlet's "crash") would leave it running, holding the
  // folder's lock and its port. A tiny /bin/sh watchdog holds the read end
  // of a pipe whose write end only this process holds: when this process
  // ends any way at all the read sees EOF and the watchdog SIGKILLs the
  // server, which is what the crash would have done to a server that was
  // the app. A clean stop disarms it first. POSIX only.
  bool arm_watchdog();
  ~Sidecar();

 private:
  void disarm();
  int watch_fd_ = -1;
  long pid_ = 0;
  bool exited_ = false;
  int code_ = 0;
  bool signalled_ = false;
#ifdef _WIN32
  void* process_ = nullptr;
#endif
};

std::string dirname_of(const std::string& p);
// Run a command to its end, stdin and output to /dev/null; its exit code,
// or -1 when it couldn't start or was signalled.
int run_and_wait(const std::vector<std::string>& argv);

}  // namespace concorde
