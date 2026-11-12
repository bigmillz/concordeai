// THE LAUNCH (CPP_PLAN.md step 1): the front on the public port, the
// Python server behind it on a private one, the notes, the watch and the
// restart rule. Shared by the window (src/mac, src/win) and the windowless
// concorde-front the gauntlet and the conformance recorder drive.
//
// The notes. The server writes run/instance.json (its private port, the
// key, the token, its pid) and, in shell mode, run/boot.json (the one-time
// code). Once the front is pointed at it, the note is written again with
// the PUBLIC port and THIS process's pid, so every native caller (the
// gauntlet's Instance, CLAUDE.md's dev recipe, a second launch's hand-off)
// finds the front and never the private port. The key and the token are
// the server's own; the front never mints a secret in step 1.
#pragma once

#include <atomic>
#include <functional>
#include <memory>
#include <mutex>
#include <string>

#include "front.hpp"
#include "sidecar.hpp"

namespace concorde {

struct HostCallbacks {
  // the page's first URL, http://127.0.0.1:<pub>/?boot=<code> (windowed)
  std::function<void(const std::string& url)> ready;
  // a plain error page: title, why, the log's tail
  std::function<void(const std::string&, const std::string&, const std::string&)> error;
  // the "Reconnecting…" veil, before a restart
  std::function<void()> reconnecting;
  // the server is gone for good: 0 is the updater's hand-off and the app
  // quits (6b537); a windowless front ends as the server did: its exit
  // code, or (negative) the signal that ended it
  std::function<void(int code)> exited;
  // THE FIRST-RUN SETUP (beta-4's shell, 6b630): the setup page, its
  // progress (step i of n, the step's name, percent, the item), and a
  // failure the person can retry or copy the details of
  std::function<void()> setup_page;
  std::function<void(int i, int n, const std::string& name, int pct, const std::string& item)> setup_progress;
  std::function<void(const std::string& title, const std::string& why)> setup_failed;
};

class Host {
 public:
  explicit Host(bool windowed);
  ~Host();
  // Bind the public port: MILLENAI_PORT, else 8889 (step 1 runs only as a
  // dev copy, which always names its port).
  bool bind_front(std::string* err);
  // Start (or restart) the server on its own thread; callbacks on that thread.
  void start(HostCallbacks cb);
  // Quit: nothing restarts it after this; SIGTERM, 8 s, SIGKILL.
  void stop();

  int public_port() const { return front_.port(); }
  // the setup page's "Try again", and its "Copy details" text
  void retry_setup() { retry_.store(true); }
  std::string setup_details() const;
  int private_port() const { return front_.upstream(); }
  std::string token() const;  // the API token, once the server is up
  std::string home() const { return home_; }
  long sidecar_pid() const;
  Front& front() { return front_; }

 private:
  void run_once();
  int bootstrap_venv(const std::string& py);  // 1 ok, 0 failed, -1 nothing to run
  bool ensure_python(const std::string& py);
  void watch(std::chrono::steady_clock::time_point since);
  void recover(int code, bool sig);
  std::string server_script() const;

  const bool windowed_;
  std::string home_;
  Front front_;
  HostCallbacks cb_;
  mutable std::mutex mu_;
  std::unique_ptr<Sidecar> child_;
  std::string token_;
  std::atomic<bool> quitting_{false};
  std::atomic<int> restarts_{0};
  std::atomic<bool> retry_{false};
  std::atomic<long> boot_pgid_{0};
  std::string details_;
  std::string log_path_;
};

// The dev-only guard of the C++ track (CPP_PLAN.md: a separate track).
// Empty when this launch may run; otherwise why not.
// gauntlet_ok: the windowless front, which the gauntlet runs on its own 9901-9903
std::string preview_guard(bool gauntlet_ok = false);

}  // namespace concorde
