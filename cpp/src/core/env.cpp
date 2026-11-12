#include "env.hpp"

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <vector>

#ifdef _WIN32
#include <direct.h>
#include <io.h>
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <unistd.h>
#endif

namespace concorde {

namespace {
const auto T0 = std::chrono::steady_clock::now();
double epoch_now_ms() {
  using namespace std::chrono;
  return duration<double, std::milli>(system_clock::now().time_since_epoch()).count();
}
const double T0_EPOCH = epoch_now_ms();
}  // namespace

double since_start_ms() {
  return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - T0).count();
}
double start_epoch_ms() { return T0_EPOCH; }

void mark(const std::string& what) {
  if (!env("CONCORDE_SHELL_TIMING")) return;
  std::fprintf(stderr, "[shell-timing] %7.1f ms  %s\n", since_start_ms(), what.c_str());
  std::fflush(stderr);
}

std::optional<std::string> env(const char* name) {
  const char* v = std::getenv(name);
  if (!v) return std::nullopt;
  return std::string(v);
}

bool dev_copy() { return env("MILLENAI_DEV").value_or("") == "1"; }

std::string expand_tilde(const std::string& p) {
  if (!p.empty() && p[0] == '~') {
    auto home = env("HOME");
    if (!home) home = env("USERPROFILE");
    if (home) return *home + p.substr(1);
  }
  return p;
}

static std::string trim(const std::string& s) {
  size_t a = s.find_first_not_of(" \t\r\n");
  if (a == std::string::npos) return "";
  size_t b = s.find_last_not_of(" \t\r\n");
  return s.substr(a, b - a + 1);
}

std::string join(const std::string& a, const std::string& b) {
#ifdef _WIN32
  const char sep = '\\';
#else
  const char sep = '/';
#endif
  if (a.empty()) return b;
  if (a.back() == '/' || a.back() == '\\') return a + b;
  return a + sep + b;
}

std::string real_app_dir() {
#ifdef _WIN32
  auto base = env("LOCALAPPDATA");
  if (!base) base = env("USERPROFILE");
  return join(base.value_or("."), "MillenAI");
#else
  return expand_tilde("~/Library/Application Support/MillenAI");
#endif
}

std::string data_home() {
  if (dev_copy()) {
    auto h = env("MILLENAI_HOME");
    if (h) {
      std::string t = trim(*h);
      if (!t.empty()) return expand_tilde(t);
    }
  }
  return real_app_dir();
}

std::string python_path() {
  if (auto p = env("CONCORDE_PYTHON")) return *p;
#ifdef _WIN32
  return join(join(join(real_app_dir(), "venv"), "Scripts"), "python.exe");
#else
  return join(join(join(real_app_dir(), "venv"), "bin"), "python3");
#endif
}

std::string log_dir(const std::string& home) {
  if (dev_copy()) return join(home, "logs");
#ifdef __APPLE__
  return expand_tilde("~/Library/Logs/MillenAI");
#else
  return join(home, "logs");
#endif
}

std::string pycache_dir(const std::string& home) {
#ifdef __APPLE__
  if (!dev_copy()) return expand_tilde("~/Library/Caches/MillenAI/pyc");
#endif
  return join(join(home, "cache"), "pyc");
}

bool file_exists(const std::string& p) {
#ifdef _WIN32
  return _access(p.c_str(), 0) == 0;
#else
  struct stat st;
  return ::stat(p.c_str(), &st) == 0;
#endif
}

bool make_dirs(const std::string& p, int mode) {
  if (p.empty()) return false;
  std::string cur;
  for (size_t i = 0; i <= p.size(); ++i) {
    if (i == p.size() || ((p[i] == '/' || p[i] == '\\') && i > 0)) {
      cur = p.substr(0, i);
#ifdef _WIN32
      _mkdir(cur.c_str());
      (void)mode;
#else
      ::mkdir(cur.c_str(), static_cast<mode_t>(mode));
#endif
    }
  }
  return file_exists(p);
}

std::string read_file(const std::string& p) {
  std::ifstream f(p, std::ios::binary);
  if (!f) return "";
  std::ostringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

std::string tail_of(const std::string& p, size_t n) {
  std::string s = read_file(p);
  std::vector<std::string> lines;
  std::istringstream is(s);
  for (std::string l; std::getline(is, l);) lines.push_back(l);
  size_t from = lines.size() > n ? lines.size() - n : 0;
  std::string out;
  for (size_t i = from; i < lines.size(); ++i) {
    out += lines[i];
    if (i + 1 < lines.size()) out += "\n";
  }
  return out;
}

bool write_private_atomic(const std::string& p, const std::string& data) {
  std::string tmp = p + ".cpp.tmp";
#ifdef _WIN32
  {
    std::ofstream f(tmp, std::ios::binary | std::ios::trunc);
    if (!f) return false;
    f << data;
  }
  return MoveFileExA(tmp.c_str(), p.c_str(), MOVEFILE_REPLACE_EXISTING) != 0;
#else
  int fd = ::open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
  if (fd < 0) return false;
  ::fchmod(fd, 0600);
  size_t off = 0;
  while (off < data.size()) {
    ssize_t w = ::write(fd, data.data() + off, data.size() - off);
    if (w < 0) {
      if (errno == EINTR) continue;
      ::close(fd);
      ::unlink(tmp.c_str());
      return false;
    }
    off += static_cast<size_t>(w);
  }
  ::close(fd);
  if (::rename(tmp.c_str(), p.c_str()) != 0) {
    ::unlink(tmp.c_str());
    return false;
  }
  return true;
#endif
}

const char* exit_meaning(int code, bool signalled) {
  if (signalled) return "it crashed";
  switch (code) {
    case 1: return "no free port";
    case 2: return "a refused setting (MILLENAI_DEV, MILLENAI_HOME, MILLENAI_SHELL)";
    case 3: return "another copy is using this data folder";
    case 4: return "it wanted a window of its own (not running in shell mode)";
    case 0: return "it stopped on its own";
    default: return "it crashed";
  }
}

bool restartable(int code, bool signalled) {
  if (signalled) return true;
  return !(code == 0 || code == 2 || code == 3 || code == 4);
}

}  // namespace concorde
