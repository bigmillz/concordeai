#include "sidecar.hpp"

#include <chrono>
#include <cerrno>
#include <cstring>
#include <thread>
#include <vector>

#ifdef _WIN32
#include <windows.h>
#else
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>
extern char** environ;
#endif

namespace concorde {

// THE SERVER'S BYTECODE, CACHED (6b548): the Tauri shell's script, the same
// bytes, so the server and everything that relaunches it see what they saw.
const char* const BOOT_PY = R"PY(import os as _cb_os, sys as _cb_sys
_cb_p = _cb_sys.argv[1]
_cb_sys.argv = _cb_sys.argv[1:]
_cb_sys.path[0] = _cb_os.path.dirname(_cb_p)
_cb_pre = _cb_os.environ.pop("CONCORDE_PYCACHE", "")
_cb_code = _cb_ld = None
try:
    import importlib.machinery as _cb_m
    _cb_ld = _cb_m.SourceFileLoader("__main__", _cb_p)
    if _cb_pre:
        _cb_old = (_cb_sys.pycache_prefix, _cb_sys.dont_write_bytecode)
        _cb_sys.pycache_prefix, _cb_sys.dont_write_bytecode = _cb_pre, False
        try:
            _cb_code = _cb_ld.get_code("__main__")
        finally:
            _cb_sys.pycache_prefix, _cb_sys.dont_write_bytecode = _cb_old
except Exception:
    _cb_code = None
if _cb_code is None:
    with open(_cb_p, "rb") as _cb_f:
        _cb_code = compile(_cb_f.read(), _cb_p, "exec")
_cb_g = _cb_sys.modules["__main__"].__dict__
_cb_g.update(__file__=_cb_p, __cached__=None)
if _cb_ld is not None:
    _cb_g["__loader__"] = _cb_ld
exec(_cb_code, _cb_g)
)PY";

std::string dirname_of(const std::string& p) {
  size_t i = p.find_last_of("/\\");
  if (i == std::string::npos) return ".";
  if (i == 0) return p.substr(0, 1);
  return p.substr(0, i);
}

#ifndef _WIN32

int run_and_wait(const std::vector<std::string>& args) {
  std::vector<std::string> a = args;
  std::vector<char*> argv;
  for (auto& x : a) argv.push_back(x.data());
  argv.push_back(nullptr);
  posix_spawn_file_actions_t fa;
  posix_spawn_file_actions_init(&fa);
  posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0);
  posix_spawn_file_actions_addopen(&fa, 1, "/dev/null", O_WRONLY, 0);
  posix_spawn_file_actions_addopen(&fa, 2, "/dev/null", O_WRONLY, 0);
  posix_spawnattr_t at;
  posix_spawnattr_init(&at);
  short flags = 0;
#ifdef POSIX_SPAWN_CLOEXEC_DEFAULT
  flags |= POSIX_SPAWN_CLOEXEC_DEFAULT;
#endif
  posix_spawnattr_setflags(&at, flags);
  pid_t pid = 0;
  int rc = posix_spawn(&pid, a[0].c_str(), &fa, &at, argv.data(), environ);
  posix_spawn_file_actions_destroy(&fa);
  posix_spawnattr_destroy(&at);
  if (rc != 0) return -1;
  int st = 0;
  while (::waitpid(pid, &st, 0) < 0) {
    if (errno != EINTR) return -1;
  }
  return WIFEXITED(st) ? WEXITSTATUS(st) : -1;
}

bool Sidecar::spawn(const SidecarSpec& s, std::string* err) {
  // the environment: ours, with the spec's names set (replaced, not doubled)
  std::vector<std::string> envs;
  for (char** e = environ; e && *e; ++e) {
    std::string kv(*e);
    std::string k = kv.substr(0, kv.find('='));
    if (s.set_env.count(k)) continue;
    envs.push_back(kv);
  }
  for (auto& [k, v] : s.set_env) envs.push_back(k + "=" + v);
  std::vector<char*> envp;
  for (auto& kv : envs) envp.push_back(kv.data());
  envp.push_back(nullptr);

  std::vector<std::string> args = {s.python, "-c", BOOT_PY, s.script};
  std::vector<char*> argv;
  for (auto& a : args) argv.push_back(a.data());
  argv.push_back(nullptr);

  posix_spawn_file_actions_t fa;
  posix_spawn_file_actions_init(&fa);
  posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0);
  if (!s.log_path.empty()) {
    posix_spawn_file_actions_addopen(&fa, 1, s.log_path.c_str(), O_WRONLY | O_CREAT | O_APPEND, 0600);
    posix_spawn_file_actions_adddup2(&fa, 1, 2);
  }
#if defined(__APPLE__) || defined(__GLIBC__)
  if (!s.cwd.empty()) posix_spawn_file_actions_addchdir_np(&fa, s.cwd.c_str());
#endif
  posix_spawnattr_t at;
  posix_spawnattr_init(&at);
  // the child starts with default signal handling (this process ignores
  // SIGPIPE for its sockets) and an empty mask
  sigset_t def, none;
  sigemptyset(&def);
  sigaddset(&def, SIGPIPE);
  sigemptyset(&none);
  posix_spawnattr_setsigdefault(&at, &def);
  posix_spawnattr_setsigmask(&at, &none);
  short flags = POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK;
#ifdef POSIX_SPAWN_CLOEXEC_DEFAULT
  // nothing of this process but stdin/stdout/stderr reaches the server:
  // an inherited listening socket would let it accept the front's
  // connections itself (found by e2e_front.py: two processes on one port)
  flags |= POSIX_SPAWN_CLOEXEC_DEFAULT;
#endif
  posix_spawnattr_setflags(&at, flags);
  pid_t pid = 0;
  int rc = posix_spawn(&pid, s.python.c_str(), &fa, &at, argv.data(), envp.data());
  posix_spawn_file_actions_destroy(&fa);
  posix_spawnattr_destroy(&at);
  if (rc != 0) {
    if (err) *err = std::string("posix_spawn: ") + std::strerror(rc);
    return false;
  }
  pid_ = pid;
  exited_ = false;
  return true;
}

Sidecar::~Sidecar() { disarm(); }

void Sidecar::disarm() {
  if (watch_fd_ < 0) return;
  const char msg[] = "disarm\n";
  ssize_t w = ::write(watch_fd_, msg, sizeof msg - 1);
  (void)w;
  ::close(watch_fd_);
  watch_fd_ = -1;
}

bool Sidecar::arm_watchdog() {
  if (pid_ <= 0 || watch_fd_ >= 0) return false;
  int fds[2];
  if (::pipe(fds) != 0) return false;
  ::fcntl(fds[1], F_SETFD, FD_CLOEXEC);  // the write end is this process's alone
  std::string script = "read -r x; [ \"$x\" = disarm ] || kill -9 " + std::to_string(pid_) + " 2>/dev/null";
  std::vector<std::string> a = {"/bin/sh", "-c", script};
  std::vector<char*> argv;
  for (auto& x : a) argv.push_back(x.data());
  argv.push_back(nullptr);
  posix_spawn_file_actions_t fa;
  posix_spawn_file_actions_init(&fa);
  posix_spawn_file_actions_adddup2(&fa, fds[0], 0);
  posix_spawn_file_actions_addopen(&fa, 1, "/dev/null", O_WRONLY, 0);
  posix_spawn_file_actions_addopen(&fa, 2, "/dev/null", O_WRONLY, 0);
  posix_spawnattr_t at;
  posix_spawnattr_init(&at);
  short flags = 0;
#ifdef POSIX_SPAWN_CLOEXEC_DEFAULT
  flags |= POSIX_SPAWN_CLOEXEC_DEFAULT;
#endif
  posix_spawnattr_setflags(&at, flags);
  pid_t wpid = 0;
  int rc = posix_spawn(&wpid, "/bin/sh", &fa, &at, argv.data(), environ);
  posix_spawn_file_actions_destroy(&fa);
  posix_spawnattr_destroy(&at);
  ::close(fds[0]);
  if (rc != 0) {
    ::close(fds[1]);
    return false;
  }
  watch_fd_ = fds[1];
  // the watchdog is reaped by a thread of its own, so it never lingers as a zombie
  std::thread([wpid] {
    int st;
    while (::waitpid(wpid, &st, 0) < 0 && errno == EINTR) {
    }
  }).detach();
  return true;
}

bool Sidecar::try_wait(int* code, bool* signalled) {
  if (pid_ <= 0) return false;
  if (!exited_) {
    int st = 0;
    pid_t r = ::waitpid(static_cast<pid_t>(pid_), &st, WNOHANG);
    if (r == 0) return false;
    if (r < 0) {
      exited_ = true;  // reaped elsewhere, or gone
      code_ = -1;
      signalled_ = true;
    } else {
      exited_ = true;
      signalled_ = WIFSIGNALED(st);
      // the exit code, or the signal's number when a signal ended it
      code_ = WIFEXITED(st) ? WEXITSTATUS(st) : (WIFSIGNALED(st) ? WTERMSIG(st) : -1);
    }
    disarm();  // its pid may be reused from here on
  }
  if (code) *code = code_;
  if (signalled) *signalled = signalled_;
  return true;
}

void Sidecar::stop(double grace_s) {
  if (pid_ <= 0 || exited_) return;
  ::kill(static_cast<pid_t>(pid_), SIGTERM);
  auto t0 = std::chrono::steady_clock::now();
  while (std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count() < grace_s) {
    if (try_wait(nullptr, nullptr)) return;
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
  ::kill(static_cast<pid_t>(pid_), SIGKILL);
  int st;
  ::waitpid(static_cast<pid_t>(pid_), &st, 0);
  exited_ = true;
  signalled_ = true;
  disarm();
}

#else  // _WIN32

Sidecar::~Sidecar() = default;
void Sidecar::disarm() {}
bool Sidecar::arm_watchdog() { return false; }  // TODO(cpp-win): a Job object (kill on close) does this

int run_and_wait(const std::vector<std::string>&) { return -1; }  // TODO(cpp-win)

// TODO(cpp-win): untested. CreateProcessW with an environment block, the
// log as stdout/stderr, CREATE_NO_WINDOW, and a Job object with
// JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE so the server can't outlive the shell
// (Windows has no getppid() watch; the server's own parent watch is POSIX).
static std::wstring widen(const std::string& s) {
  if (s.empty()) return L"";
  int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
  std::wstring w(n, L'\0');
  MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), w.data(), n);
  return w;
}

static std::wstring quote_arg(const std::wstring& a) {
  std::wstring o = L"\"";
  size_t bs = 0;
  for (wchar_t ch : a) {
    if (ch == L'\\') {
      ++bs;
      continue;
    }
    if (ch == L'"') o.append(bs * 2 + 1, L'\\');
    else o.append(bs, L'\\');
    bs = 0;
    o += ch;
  }
  o.append(bs * 2, L'\\');
  return o + L"\"";
}

bool Sidecar::spawn(const SidecarSpec& s, std::string* err) {
  std::wstring cmd = quote_arg(widen(s.python)) + L" -c " + quote_arg(widen(BOOT_PY)) + L" " + quote_arg(widen(s.script));
  std::wstring block;
  LPWCH cur = GetEnvironmentStringsW();
  for (LPWCH p = cur; p && *p; p += wcslen(p) + 1) {
    std::wstring kv(p);
    std::wstring k = kv.substr(0, kv.find(L'=', 1));
    bool replaced = false;
    for (auto& [ek, ev] : s.set_env)
      if (_wcsicmp(widen(ek).c_str(), k.c_str()) == 0) replaced = true;
    if (!replaced) block += kv + L'\0';
  }
  if (cur) FreeEnvironmentStringsW(cur);
  for (auto& [k, v] : s.set_env) block += widen(k) + L"=" + widen(v) + L'\0';
  block += L'\0';
  SECURITY_ATTRIBUTES sa{sizeof sa, nullptr, TRUE};
  HANDLE log = INVALID_HANDLE_VALUE;
  if (!s.log_path.empty())
    log = CreateFileW(widen(s.log_path).c_str(), FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                      OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
  STARTUPINFOW si{};
  si.cb = sizeof si;
  if (log != INVALID_HANDLE_VALUE) {
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = si.hStdError = log;
    si.hStdInput = nullptr;
  }
  PROCESS_INFORMATION pi{};
  BOOL ok = CreateProcessW(nullptr, cmd.data(), nullptr, nullptr, TRUE,
                           CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW, block.data(),
                           s.cwd.empty() ? nullptr : widen(s.cwd).c_str(), &si, &pi);
  if (log != INVALID_HANDLE_VALUE) CloseHandle(log);
  if (!ok) {
    if (err) *err = "CreateProcess failed: " + std::to_string(GetLastError());
    return false;
  }
  CloseHandle(pi.hThread);
  process_ = pi.hProcess;
  pid_ = static_cast<long>(pi.dwProcessId);
  exited_ = false;
  return true;
}

bool Sidecar::try_wait(int* code, bool* signalled) {
  if (!process_) return false;
  if (!exited_) {
    if (WaitForSingleObject(process_, 0) != WAIT_OBJECT_0) return false;
    DWORD c = 0;
    GetExitCodeProcess(process_, &c);
    exited_ = true;
    code_ = static_cast<int>(c);
    signalled_ = false;
  }
  if (code) *code = code_;
  if (signalled) *signalled = signalled_;
  return true;
}

void Sidecar::stop(double grace_s) {
  // TODO(cpp-win): a graceful stop needs the server's own shutdown route or
  // a console control event; until then, terminate.
  (void)grace_s;
  if (!process_ || exited_) return;
  TerminateProcess(process_, 1);
  WaitForSingleObject(process_, 5000);
  exited_ = true;
}

#endif

}  // namespace concorde
