// concorde-front: the front server and its Python sidecar with no window
// (CPP_PLAN.md step 1). What the gauntlet and the conformance recorder run
// when they test "behind the C++ front": the environment is passed to the
// server untouched (MILLENAI_NOWINDOW, MILLENAI_TEST_HOOKS…), the front
// owns MILLENAI_PORT, and run/instance.json (and run/boot.json, under the
// boot-code hook) name the front's port and this process's pid.
//
//   MILLENAI_DEV=1 MILLENAI_HOME=… MILLENAI_NOWINDOW=1 MILLENAI_PORT=9893 \
//     CONCORDE_SERVER=$PWD/millenai.py build/concorde-front
//
// SIGTERM or SIGINT stops the server (SIGTERM, 8 s, SIGKILL), then exits 0;
// the server's own exit ends this process with the same code.
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <thread>

#include "core/env.hpp"
#include "core/host.hpp"

static std::atomic<int> g_exit{0};
static std::atomic<bool> g_done{false};
static std::atomic<bool> g_signal{false};

extern "C" void on_signal(int) { g_signal.store(true); }

int main() {
  concorde::mark("process start");
  std::string why = concorde::preview_guard(/*gauntlet_ok=*/true);
  if (!why.empty()) {
    std::fprintf(stderr, "\n  %s\n\n", why.c_str());
    return 2;
  }
  concorde::Host host(/*windowed=*/false);
  std::string err;
  if (!host.bind_front(&err)) {
    std::fprintf(stderr, "\n  the front can't listen: %s\n\n", err.c_str());
    return 1;  // the server's own code for "no free port"
  }
  std::signal(SIGTERM, on_signal);
  std::signal(SIGINT, on_signal);
  concorde::HostCallbacks cb;
  cb.error = [](const std::string& t, const std::string& w, const std::string& log) {
    std::fprintf(stderr, "\n  %s\n  %s\n%s\n", t.c_str(), w.c_str(), log.c_str());
  };
  cb.exited = [](int code) {
    g_exit.store(code);
    g_done.store(true);
  };
  cb.ready = [](const std::string&) {};
  host.start(cb);
  while (!g_done.load() && !g_signal.load()) std::this_thread::sleep_for(std::chrono::milliseconds(50));
  host.stop();
  if (g_signal.load()) return 0;
  const int code = g_exit.load();
  if (code < 0) {
    // the server was ended by a signal: so is its stand-in (the same one)
    std::signal(-code, SIG_DFL);
    std::raise(-code);
    return 128 - code;
  }
  return code;
}
