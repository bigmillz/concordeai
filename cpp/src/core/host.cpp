#include "host.hpp"

#include <chrono>
#include <vector>
#include <cstdio>
#include <cerrno>
#include <thread>

#include "env.hpp"
#include "notes.hpp"

#ifndef _WIN32
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>
extern char** environ;
#else
#include <process.h>
#endif

namespace concorde {

// set by the platform's main: the bundle's Resources (Mac), the exe's folder (Windows)
std::string g_resource_dir;

namespace {
constexpr auto SERVER_START_TIMEOUT = std::chrono::seconds(180);
constexpr int MAX_RESTARTS = 3;
constexpr auto STEADY = std::chrono::seconds(60);

long self_pid() {
#ifdef _WIN32
  return static_cast<long>(_getpid());
#else
  return static_cast<long>(::getpid());
#endif
}

std::string exit_label(int code, bool sig) { return sig ? "signal" : std::to_string(code); }

// digits only, 1..65535, else 0 (what int(os.environ["MILLENAI_PORT"]) accepts, minus the sign)
int parse_port(const std::string& s) {
  if (s.empty() || s.size() > 5) return 0;
  long v = 0;
  for (char c : s) {
    if (c < '0' || c > '9') return 0;
    v = v * 10 + (c - '0');
  }
  return (v > 0 && v <= 65535) ? static_cast<int>(v) : 0;
}
// THE PUBLIC PORT'S SETTING. The server runs with MILLENAI_PORT set to the
// PRIVATE port, and everything it starts inherits that, the updater's swap
// helper and its `open -n` relaunch included (found by updater_proof.py:
// `open` hands its environment to the app it opens, so the relaunched app
// bound the old private port). The sidecar's environment therefore also
// carries CONCORDE_PUBLIC_PORT, and a launch that finds it takes it over
// MILLENAI_PORT. Nothing else sets it.
const char* public_port_name() { return env("CONCORDE_PUBLIC_PORT") ? "CONCORDE_PUBLIC_PORT" : "MILLENAI_PORT"; }
}  // namespace

std::string preview_guard(bool gauntlet_ok) {
#ifdef CONCORDE_ALLOW_REAL_APP
  return "";
#else
  if (!dev_copy()) {
    return "This C++ preview runs only as a dev copy: set MILLENAI_DEV=1, MILLENAI_HOME "
           "(a folder of its own) and MILLENAI_PORT (CLAUDE.md). It never opens the real data folder.";
  }
  auto h = env("MILLENAI_HOME");
  if (!h || h->find_first_not_of(" \t") == std::string::npos) return "MILLENAI_DEV=1 needs MILLENAI_HOME.";
  auto p = env(public_port_name());
  if (!p) return "The C++ preview needs MILLENAI_PORT (a dev port such as 9893).";
  int port = parse_port(*p);
  if (port <= 0 || port > 65535) return "MILLENAI_PORT is not a port.";
  if (port == 8889 || (port >= 18890 && port <= 18898))
    return "MILLENAI_PORT " + *p + " is the desktop app's; pick a dev port.";
  if (port == 9897 || (!gauntlet_ok && port >= 9901 && port <= 9903))
    return "MILLENAI_PORT " + *p + " belongs to ConcordeGo or the gauntlet.";
  if (port >= 8884 && port <= 8999) return "MILLENAI_PORT " + *p + " is in the model engines' range.";
  return "";
#endif
}

Host::Host(bool windowed) : windowed_(windowed), home_(data_home()) {}

Host::~Host() { stop(); }

bool Host::bind_front(std::string* err) {
  int port = 8889;
  bool named = false;
  if (auto p = env(public_port_name())) {
    port = parse_port(*p);
    named = true;
  }
  if (port == 0) {
    if (err) *err = "MILLENAI_PORT is not a port";
    return false;
  }
  if (front_.bind(port, err)) return true;
  // THE APP A PERSON OPENED MOVES OFF A TAKEN 8889 (millenai.py's
  // bind_backend, 6b310): another login's ConcordeAI may hold it, so the
  // app tries 18890-18898 in turn. A dev copy named its port and fails.
  if (named || dev_copy()) return false;
  for (int p = 18890; p <= 18898; ++p)
    if (front_.bind(p, nullptr)) return true;
  return false;
}

std::string Host::token() const {
  std::lock_guard<std::mutex> g(mu_);
  return token_;
}

long Host::sidecar_pid() const {
  std::lock_guard<std::mutex> g(mu_);
  return child_ ? child_->pid() : 0;
}

std::string Host::server_script() const {
  if (auto p = env("CONCORDE_SERVER")) return *p;
  if (!g_resource_dir.empty()) {
    std::string r = join(g_resource_dir, "millenai.py");
    if (file_exists(r)) return r;
  }
#ifdef CONCORDE_REPO_DIR
  {
    std::string r = join(CONCORDE_REPO_DIR, "millenai.py");
    if (file_exists(r)) return r;
  }
#endif
  return "millenai.py";
}

void Host::start(HostCallbacks cb) {
  cb_ = std::move(cb);
  front_.start();
  std::thread([this] { run_once(); }).detach();
}

std::string Host::setup_details() const {
  std::lock_guard<std::mutex> g(mu_);
  return details_;
}

void Host::stop() {
  quitting_.store(true);
#ifndef _WIN32
  // a first-run setup still running goes with the app (its whole group)
  if (long pg = boot_pgid_.exchange(0); pg > 0) ::kill(-static_cast<pid_t>(pg), SIGTERM);
#endif
  std::unique_ptr<Sidecar> c;
  {
    std::lock_guard<std::mutex> g(mu_);
    c = std::move(child_);
  }
  if (c) c->stop(8.0);
  front_.set_upstream(0);
}

// FIRST RUN ON A FRESH MAC (main.rs bootstrap_venv, 6b537; beta-4's setup
// page and its rules, 6b630). The bundle carries the Python app's own
// launch.sh; with --shell it makes the venv, installs the engine and says
// how far it is on stdout, one "@@steps n", "@@step i name", "@@pct p" or
// "@@item text" line at a time, which the setup page shows. An older
// launch.sh without --shell runs as --bootstrap, silently. macOS only, never
// with CONCORDE_PYTHON; the script writes under $HOME's Library.
static bool venv_ready(const std::string& py) {
  if (!file_exists(py)) return false;
  std::string venv = dirname_of(dirname_of(py));
  if (file_exists(join(venv, ".setup-unfinished"))) return false;  // a setup cut short
  // a venv from a Python older than 3.11 is rebuilt (beta-4's rule)
  std::string cfg = read_file(join(venv, "pyvenv.cfg"));
  size_t at = 0;
  while ((at = cfg.find("version", at)) != std::string::npos) {
    size_t eq = cfg.find('=', at), nl = cfg.find('\n', at);
    if (eq != std::string::npos && (nl == std::string::npos || eq < nl)) {
      std::string v = cfg.substr(eq + 1, (nl == std::string::npos ? cfg.size() : nl) - eq - 1);
      int major = 0, minor = 0;
      if (std::sscanf(v.c_str(), " %d.%d", &major, &minor) == 2) return !(major == 3 && minor < 11);
    }
    at += 7;
  }
  return true;
}

int Host::bootstrap_venv(const std::string& py) {
#ifdef __APPLE__
  if (env("CONCORDE_PYTHON") || g_resource_dir.empty()) return -1;
  const std::string script = join(g_resource_dir, "launch.sh");
  if (!file_exists(script)) return -1;
  const bool shell_proto = read_file(script).find("--shell") != std::string::npos;
  if (cb_.setup_page) cb_.setup_page();
  mark("bootstrap started");
  int fds[2];
  if (::pipe(fds) != 0) return 0;
  std::vector<std::string> a = {"/bin/zsh", script, shell_proto ? "--shell" : "--bootstrap"};
  std::vector<char*> argv;
  for (auto& x : a) argv.push_back(x.data());
  argv.push_back(nullptr);
  posix_spawn_file_actions_t fa;
  posix_spawn_file_actions_init(&fa);
  posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0);
  posix_spawn_file_actions_adddup2(&fa, fds[1], 1);
  posix_spawn_file_actions_addopen(&fa, 2, "/dev/null", O_WRONLY, 0);
  posix_spawnattr_t at;
  posix_spawnattr_init(&at);
  posix_spawnattr_setpgroup(&at, 0);  // its own group, so Quit stops all of it
  short flags = POSIX_SPAWN_SETPGROUP;
#ifdef POSIX_SPAWN_CLOEXEC_DEFAULT
  flags |= POSIX_SPAWN_CLOEXEC_DEFAULT;
#endif
  posix_spawnattr_setflags(&at, flags);
  pid_t pid = 0;
  int rc = posix_spawn(&pid, "/bin/zsh", &fa, &at, argv.data(), environ);
  posix_spawn_file_actions_destroy(&fa);
  posix_spawnattr_destroy(&at);
  ::close(fds[1]);
  if (rc != 0) {
    ::close(fds[0]);
    return 0;
  }
  boot_pgid_.store(pid);
  int i = 0, n = 0, pct = 0;
  std::string name, item, line;
  auto sent = std::chrono::steady_clock::now() - std::chrono::seconds(1);
  char buf[4096];
  for (;;) {
    ssize_t r = ::read(fds[0], buf, sizeof buf);
    if (r <= 0) break;
    for (ssize_t k = 0; k < r; ++k) {
      if (buf[k] != '\n') {
        line += buf[k];
        continue;
      }
      std::string tag = line.substr(0, line.find(' '));
      std::string rest = line.size() > tag.size() ? line.substr(tag.size() + 1) : "";
      bool now = false, known = true;
      if (tag == "@@steps") n = std::atoi(rest.c_str());
      else if (tag == "@@step") {
        i = std::atoi(rest.c_str());
        size_t sp = rest.find(' ');
        name = sp == std::string::npos ? "" : rest.substr(sp + 1);
        pct = 0;
        item.clear();
        now = true;
        mark("bootstrap step " + std::to_string(i) + " " + name);
      } else if (tag == "@@pct") pct = std::atoi(rest.c_str());
      else if (tag == "@@item") item = rest;
      else known = false;
      line.clear();
      if (known && cb_.setup_progress &&
          (now || std::chrono::steady_clock::now() - sent > std::chrono::milliseconds(120))) {
        sent = std::chrono::steady_clock::now();
        cb_.setup_progress(i, n, name, pct, item);
      }
    }
  }
  ::close(fds[0]);
  int st = 0;
  while (::waitpid(pid, &st, 0) < 0 && errno == EINTR) {
  }
  boot_pgid_.store(0);
  const bool ok = WIFEXITED(st) && WEXITSTATUS(st) == 0;
  mark(std::string("bootstrap ended (") + (ok ? "ok" : "failed") + ")");
  if (ok && file_exists(py)) return 1;
  std::lock_guard<std::mutex> g(mu_);
  details_ = "ConcordeAI setup\nThe first-run setup stopped.\n\n" +
             tail_of(expand_tilde("~/Library/Logs/MillenAI/bootstrap.log"), 60) + "\n";
  return 0;
#else
  (void)py;
  return -1;
#endif
}

// Python, ready, or the setup page until it is: a failure waits for "Try
// again" (or Quit) and runs the setup again (beta-4's ensure_python)
bool Host::ensure_python(const std::string& py) {
  for (;;) {
    if (venv_ready(py)) return true;
    int r = bootstrap_venv(py);
    if (r == 1) return true;
    if (r < 0 && file_exists(py)) return true;
    std::string title, why;
    if (r < 0) {
      {
        std::lock_guard<std::mutex> g(mu_);
        details_ = "ConcordeAI setup\nNo Python at " + py + ".\n";
      }
      if (cb_.setup_page) cb_.setup_page();
      title = "ConcordeAI isn\u2019t set up yet";
      why = "Something it needs is missing. Download ConcordeAI again and replace this copy.";
    } else {
      title = "Setup didn\u2019t finish";
      why = "Check your internet connection, then try again.";
    }
    if (cb_.setup_failed) cb_.setup_failed(title, why);
    retry_.store(false);
    while (!retry_.load()) {
      if (quitting_.load()) return false;
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    retry_.store(false);
  }
}

void Host::run_once() {
  const std::string py = python_path();
  const std::string script = server_script();
  if (!ensure_python(py)) return;  // Quit came first
  if (!file_exists(script)) {
    if (cb_.error)
      cb_.error("ConcordeAI is missing a file", "Download ConcordeAI again and replace this copy.",
                "No server at " + script + ".");
    return;
  }
  const std::string run_dir = join(home_, "run");
  const std::string note_path = join(run_dir, "instance.json");
  const std::string boot_path = join(run_dir, "boot.json");
  const std::string logs = log_dir(home_);
  // the window logs to the folder, as the Tauri shell does; the windowless
  // front hands its own stdout and stderr on, so a harness reads the server
  // where it always did
  log_path_ = (!windowed_ || env("CONCORDE_SHELL_LOG_INHERIT")) ? "" : join(logs, "shell-server.log");
  if (!log_path_.empty()) make_dirs(logs);

  const int priv = pick_free_port();
  SidecarSpec spec;
  spec.python = py;
  spec.script = script;
  spec.cwd = dirname_of(script);
  spec.log_path = log_path_;
  spec.set_env["MILLENAI_PORT"] = std::to_string(priv);  // the private port; PORT inside the server
  spec.set_env["CONCORDE_PUBLIC_PORT"] = std::to_string(front_.port());  // for a relaunch it starts
  spec.set_env["PYTHONUNBUFFERED"] = "1";
  spec.set_env["CONCORDE_PYCACHE"] = pycache_dir(home_);
  // the window's server is a native shell's sidecar: no window of its own,
  // a boot note, it follows its parent (6b532). The name stays "tauri"
  // until the server learns another (one line in _dev_home; CPP_PLAN.md).
  if (windowed_) spec.set_env["MILLENAI_SHELL"] = "tauri";
  if (quitting_.load()) return;
  auto child = std::make_unique<Sidecar>();
  std::string err;
  if (!child->spawn(spec, &err)) {
    if (cb_.error) cb_.error("ConcordeAI couldn't start its server", err, "");
    return;
  }
  mark("sidecar spawned");
  // the window's server follows its parent itself (MILLENAI_SHELL, 6b532);
  // a windowless one does not, so a watchdog ends it with the front (6b613)
  if (!windowed_) child->arm_watchdog();
  const long pid = child->pid();
  {
    std::lock_guard<std::mutex> g(mu_);
    child_ = std::move(child);
  }
  const auto t0 = std::chrono::steady_clock::now();
  std::optional<InstanceNote> note;
  std::optional<BootNote> boot;
  while (std::chrono::steady_clock::now() - t0 < SERVER_START_TIMEOUT) {
    if (quitting_.load()) return;  // stop() has the child
    int code = 0;
    bool sig = false;
    bool gone;
    {
      std::lock_guard<std::mutex> g(mu_);
      gone = child_ && child_->try_wait(&code, &sig);
    }
    if (gone && !windowed_) {
      // THE WINDOWLESS FRONT IS THE SERVER'S STAND-IN (6b613): it never
      // restarts it and says nothing of its own; it ends the way the server
      // ended (its code, or its signal), as the gauntlet's crash checks
      // expect of the process they started (ISO-13's os._exit(86))
      if (cb_.exited) cb_.exited(sig ? -code : code);
      return;
    }
    if (gone) {
      // a stop before it was ready is tried again like any other (a port
      // still held by the copy that just died); one that will stop the
      // same way every time is said at once
      if (!restartable(code, sig) || restarts_.load() >= MAX_RESTARTS) {
        if (cb_.error)
          cb_.error("ConcordeAI couldn't start",
                    std::string("The server stopped before it was ready: ") + exit_meaning(code, sig) + " (exit " +
                        exit_label(code, sig) + ").",
                    tail_of(log_path_, 30));
        // a windowless front has nothing left to do: hand the exit on
        if (!windowed_ && cb_.exited && !restartable(code, sig)) cb_.exited(sig ? 1 : code);
      } else {
        recover(code, sig);
      }
      return;
    }
    if (!note) {
      auto n = read_instance_note(note_path);
      if (n && n->pid == pid) note = n;
    }
    if (note && !boot) {
      auto b = read_boot_note(boot_path);
      if (b && b->pid == pid) boot = b;
    }
    // the window needs the boot code; a windowless front needs only the note
    if (note && (boot || !windowed_)) break;
    std::this_thread::sleep_for(std::chrono::milliseconds(15));
  }
  if (!note || (windowed_ && !boot)) {
    if (cb_.error)
      cb_.error("ConcordeAI's server didn't answer", "It started but never wrote run/instance.json and run/boot.json.",
                tail_of(log_path_, 30));
    std::lock_guard<std::mutex> g(mu_);
    if (child_) child_->stop(2.0);
    return;
  }
  mark("sidecar ready (notes read)");
  {
    std::lock_guard<std::mutex> g(mu_);
    token_ = note->token;
  }
  front_.set_upstream(note->port);
  // the note again, for every native caller: the public port, this pid
  InstanceNote pubnote = *note;
  pubnote.port = front_.port();
  pubnote.pid = self_pid();
  write_private_atomic(note_path, instance_note_json(pubnote));
  if (windowed_) {
    // the boot code is spent by the first load; it is never kept here
    std::remove(boot_path.c_str());
  } else if (boot) {
    // the gauntlet's boot-code hook: the same code, under this pid
    BootNote b = *boot;
    b.pid = self_pid();
    write_private_atomic(boot_path, boot_note_json(b));
  }
  watch(std::chrono::steady_clock::now());
  if (windowed_ && cb_.ready)
    cb_.ready("http://127.0.0.1:" + std::to_string(front_.port()) + "/?boot=" + boot->code);
}

void Host::watch(std::chrono::steady_clock::time_point since) {
  std::thread([this, since] {
    for (;;) {
      std::this_thread::sleep_for(std::chrono::milliseconds(200));
      int code = 0;
      bool sig = false;
      {
        std::lock_guard<std::mutex> g(mu_);
        if (!child_) return;  // Quit took it
        if (!child_->try_wait(&code, &sig)) {
          // a run that held up this long was healthy: a fresh tally
          if (std::chrono::steady_clock::now() - since > STEADY) restarts_.store(0);
          continue;
        }
        child_.reset();
      }
      front_.set_upstream(0);
      if (!windowed_) {  // the server's stand-in: no restart, the same end (6b613)
        if (cb_.exited) cb_.exited(sig ? -code : code);
        return;
      }
      if (!sig && code == 0) {
        if (cb_.exited) cb_.exited(0);
      } else {
        recover(code, sig);
      }
      return;
    }
  }).detach();
}

void Host::recover(int code, bool sig) {
  if (quitting_.load()) return;
  int n = ++restarts_;
  if (!restartable(code, sig) || n > MAX_RESTARTS) {
    std::string why =
        restartable(code, sig)
            ? "It kept stopping, even after " + std::to_string(MAX_RESTARTS) + " restarts: " + exit_meaning(code, sig) +
                  " (exit " + exit_label(code, sig) + "). Quit ConcordeAI and open it again; the log below says why."
            : std::string("The server stopped: ") + exit_meaning(code, sig) + " (exit " + exit_label(code, sig) + ").";
    if (cb_.error) cb_.error("ConcordeAI's server stopped", why, tail_of(log_path_, 30));
    if (!windowed_ && cb_.exited) cb_.exited(sig ? 1 : (code ? code : 1));
    return;
  }
  mark("sidecar stopped (exit " + exit_label(code, sig) + "), restart " + std::to_string(n) + " of " +
       std::to_string(MAX_RESTARTS));
  if (cb_.reconnecting) cb_.reconnecting();
  std::this_thread::sleep_for(std::chrono::seconds(1 << (n - 1)));
  run_once();
}

}  // namespace concorde
