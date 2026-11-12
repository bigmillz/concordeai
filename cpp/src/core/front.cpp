#include "front.hpp"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "rewrite.hpp"

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")
using sock_t = SOCKET;
static const sock_t BAD_SOCK = INVALID_SOCKET;
static int close_sock(sock_t s) { return closesocket(s); }
static int poll_socks(WSAPOLLFD* f, ULONG n, int ms) { return WSAPoll(f, n, ms); }
using pollfd_t = WSAPOLLFD;
#define SHUT_WR_ SD_SEND
#define SHUT_RDWR_ SD_BOTH
using ssz = int;
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <fcntl.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <unistd.h>
#ifdef __APPLE__
#include <pthread/qos.h>
#endif

#include <cerrno>
using sock_t = int;
static const sock_t BAD_SOCK = -1;
static int close_sock(sock_t s) { return ::close(s); }
static int poll_socks(struct pollfd* f, nfds_t n, int ms) { return ::poll(f, n, ms); }
using pollfd_t = struct pollfd;
#define SHUT_WR_ SHUT_WR
#define SHUT_RDWR_ SHUT_RDWR
using ssz = ssize_t;
#endif

namespace concorde {

namespace {

// A head is read whole before anything is forwarded; past this it is
// refused (http.server's own limits are 64 KiB a line, 100 headers).
constexpr size_t MAX_HEAD = 1 << 20;
constexpr size_t BUF = size_t{64} * 1024;

#ifdef _WIN32
struct WsaInit {
  WsaInit() {
    WSADATA d;
    WSAStartup(MAKEWORD(2, 2), &d);
  }
} wsa_init;
#else
struct NoSigpipe {
  NoSigpipe() { ::signal(SIGPIPE, SIG_IGN); }
} no_sigpipe;
#endif

// never inherited by the server or anything it starts (belt and braces
// beside POSIX_SPAWN_CLOEXEC_DEFAULT; on Windows, not inheritable)
void no_inherit(sock_t s) {
#ifdef _WIN32
  SetHandleInformation(reinterpret_cast<HANDLE>(s), HANDLE_FLAG_INHERIT, 0);
#else
  ::fcntl(s, F_SETFD, FD_CLOEXEC);
#endif
}

void tune(sock_t s) {
  no_inherit(s);
  int one = 1;
  // frames are small and must not wait for Nagle (a heartbeat, a STEP)
  ::setsockopt(s, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&one), sizeof one);
#ifdef SO_NOSIGPIPE
  ::setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
}

ssz rd(sock_t s, char* b, size_t n) {
  for (;;) {
#ifdef _WIN32
    int r = ::recv(s, b, static_cast<int>(n), 0);
    return r;
#else
    ssz r = ::recv(s, b, n, 0);
    if (r < 0 && errno == EINTR) continue;
    return r;
#endif
  }
}

bool wr_all(sock_t s, const char* b, size_t n) {
  while (n) {
#ifdef _WIN32
    int w = ::send(s, b, static_cast<int>(n), 0);
#else
    ssz w = ::send(s, b, n, 0);
    if (w < 0 && errno == EINTR) continue;
#endif
    if (w <= 0) return false;
    b += w;
    n -= static_cast<size_t>(w);
  }
  return true;
}

sock_t connect_loopback(int port) {
  sock_t s = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (s == BAD_SOCK) return BAD_SOCK;
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_port = htons(static_cast<unsigned short>(port));
  a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  if (::connect(s, reinterpret_cast<sockaddr*>(&a), sizeof a) != 0) {
    close_sock(s);
    return BAD_SOCK;
  }
  tune(s);
  return s;
}

void answer_plain(sock_t c, const char* status, const std::string& body) {
  std::string r = std::string("HTTP/1.0 ") + status +
                  "\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\n"
                  "Connection: close\r\nContent-Length: " +
                  std::to_string(body.size()) + "\r\n\r\n" + body;
  wr_all(c, r.data(), r.size());
}

}  // namespace

int pick_free_port() {
  sock_t s = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (s == BAD_SOCK) return 0;
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_port = 0;
  a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  int port = 0;
  if (::bind(s, reinterpret_cast<sockaddr*>(&a), sizeof a) == 0) {
    socklen_t len = sizeof a;
    if (::getsockname(s, reinterpret_cast<sockaddr*>(&a), &len) == 0) port = ntohs(a.sin_port);
  }
  close_sock(s);
  return port;
}

bool port_open(int port) {
  sock_t s = connect_loopback(port);
  if (s == BAD_SOCK) return false;
  close_sock(s);
  return true;
}

Front::Front() = default;
Front::~Front() { stop(); }

bool Front::bind(int port, std::string* err) {
  sock_t s = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (s == BAD_SOCK) {
    if (err) *err = "socket() failed";
    return false;
  }
  no_inherit(s);
  int one = 1;
#ifdef _WIN32
  // Windows' SO_REUSEADDR lets a second process take a listening port;
  // exclusive use forbids it (as millenai.py's bind_backend does)
  ::setsockopt(s, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, reinterpret_cast<const char*>(&one), sizeof one);
#else
  ::setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
#endif
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_port = htons(static_cast<unsigned short>(port));
  a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);  // 127.0.0.1 only, like the server
  if (::bind(s, reinterpret_cast<sockaddr*>(&a), sizeof a) != 0 || ::listen(s, 128) != 0) {
    if (err) *err = "127.0.0.1:" + std::to_string(port) + " is taken";
    close_sock(s);
    return false;
  }
  socklen_t len = sizeof a;
  ::getsockname(s, reinterpret_cast<sockaddr*>(&a), &len);
  port_ = ntohs(a.sin_port);
  listen_fd_ = static_cast<long long>(s);
  return true;
}

void Front::start() {
  acceptor_ = std::thread([this] { accept_loop(); });
}

void Front::stop() {
  if (stopping_.exchange(true)) return;
  if (listen_fd_ != -1) {
    sock_t s = static_cast<sock_t>(listen_fd_);
    ::shutdown(s, SHUT_RDWR_);
    close_sock(s);
  }
  if (acceptor_.joinable()) acceptor_.join();
}

// The front's threads carry the page's requests, so they run at the
// priority of work a person is waiting on (6b617): under load (a model
// decoding on every core) a default-QoS thread was the one left waiting.
static void interactive_qos() {
#ifdef __APPLE__
  pthread_set_qos_class_self_np(QOS_CLASS_USER_INITIATED, 0);
#endif
}

void Front::accept_loop() {
  interactive_qos();
  sock_t ls = static_cast<sock_t>(listen_fd_);
  while (!stopping_.load()) {
    sock_t c = ::accept(ls, nullptr, nullptr);
    if (c == BAD_SOCK) {
      if (stopping_.load()) return;
#ifndef _WIN32
      if (errno == EINTR || errno == ECONNABORTED || errno == EMFILE || errno == ENFILE) {
        if (errno == EMFILE || errno == ENFILE) ::usleep(50 * 1000);
        continue;
      }
#endif
      return;
    }
    tune(c);
    connections++;
    std::thread([this, c] { serve(static_cast<long long>(c)); }).detach();
  }
}

// CONCORDE_FRONT_TRACE=1: each connection's milestones in microseconds on stderr
static const bool g_trace = std::getenv("CONCORDE_FRONT_TRACE") != nullptr;
static long long us_now() {
  return std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

void Front::serve(long long cfd) {
  interactive_qos();
  const long long t_start = g_trace ? us_now() : 0;
  long long t_head = 0, t_conn = 0, t_first = 0;
  sock_t c = static_cast<sock_t>(cfd);
  std::string head;
  head.reserve(4096);
  std::vector<char> buf(BUF);
  size_t end = std::string::npos;
  // 1. the request head, whole
  while (end == std::string::npos) {
    ssz r = rd(c, buf.data(), buf.size());
    if (r <= 0) {
      close_sock(c);
      return;
    }
    head.append(buf.data(), static_cast<size_t>(r));
    end = find_head_end(head);
    if (end == std::string::npos && head.size() > MAX_HEAD) {
      answer_plain(c, "431 Request Header Fields Too Large", "Request header too large.");
      close_sock(c);
      return;
    }
  }
  if (g_trace) t_head = us_now();
  const int priv = upstream_.load();
  if (priv <= 0) {
    answer_plain(c, "503 Service Unavailable", "ConcordeAI's server is starting.");
    close_sock(c);
    return;
  }
  sock_t u = connect_loopback(priv);
  if (u == BAD_SOCK) {
    answer_plain(c, "503 Service Unavailable", "ConcordeAI's server is starting.");
    close_sock(c);
    return;
  }
  if (g_trace) t_conn = us_now();
  const PortMap pm{port_, priv};
  std::string out = rewrite_request_head(std::string_view(head).substr(0, end), pm);
  out.append(head, end, std::string::npos);  // any body bytes already read
  if (!wr_all(u, out.data(), out.size())) {
    close_sock(u);
    close_sock(c);
    return;
  }
  bytes_up += static_cast<long>(out.size());

  // 2. both ways at once until the server closes. The response head is
  // gathered and rewritten (Set-Cookie, Location); every byte after it
  // is passed the moment it arrives.
  std::string rhead;
  bool rhead_done = false, client_eof = false;
  for (;;) {
    pollfd_t p[2]{};
    p[0].fd = u;
    p[0].events = POLLIN;
    p[1].fd = c;
    p[1].events = client_eof ? 0 : POLLIN;
    int n = poll_socks(p, client_eof ? 1 : 2, -1);
    if (n < 0) {
#ifndef _WIN32
      if (errno == EINTR) continue;
#endif
      break;
    }
    if (p[0].revents & (POLLIN | POLLHUP | POLLERR)) {
      ssz r = rd(u, buf.data(), buf.size());
      if (r <= 0) {
        if (!rhead_done && !rhead.empty()) wr_all(c, rhead.data(), rhead.size());
        break;  // the server closed: the answer is complete
      }
      if (g_trace && !t_first) t_first = us_now();
      if (rhead_done) {
        if (!wr_all(c, buf.data(), static_cast<size_t>(r))) break;  // the page went away
        bytes_down += r;
      } else {
        rhead.append(buf.data(), static_cast<size_t>(r));
        size_t he = find_head_end(rhead);
        if (he != std::string::npos || rhead.size() > MAX_HEAD) {
          std::string o;
          if (he != std::string::npos) {
            o = rewrite_response_head(std::string_view(rhead).substr(0, he), pm);
            o.append(rhead, he, std::string::npos);
          } else {
            o.swap(rhead);
          }
          rhead_done = true;
          rhead.clear();
          if (!wr_all(c, o.data(), o.size())) break;
          bytes_down += static_cast<long>(o.size());
        }
      }
    }
    if (!client_eof && (p[1].revents & (POLLIN | POLLHUP | POLLERR))) {
      ssz r = rd(c, buf.data(), buf.size());
      if (r <= 0) {
        // the page closed its side: pass the half-close on and keep
        // relaying, so a half-closing client still gets its answer; a page
        // that went away for good fails the next write and ends it
        client_eof = true;
        ::shutdown(u, SHUT_WR_);
      } else {
        if (!wr_all(u, buf.data(), static_cast<size_t>(r))) break;
        bytes_up += r;
      }
    }
  }
  // closing the server's side is what tells it the page has gone (Stop):
  // its next write fails, as it would have with the page itself
  ::shutdown(c, SHUT_WR_);
  close_sock(u);
  close_sock(c);
  if (g_trace)
    std::fprintf(stderr, "[front] head %lld conn %lld first %lld done %lld us\n", t_head - t_start, t_conn - t_start,
                 t_first ? t_first - t_start : -1, us_now() - t_start);
}

}  // namespace concorde
