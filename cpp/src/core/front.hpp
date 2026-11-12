// THE FRONT SERVER (CPP_PLAN.md step 1): a tiny byte-level reverse proxy
// on the public port, in front of the Python server on a private loopback
// port. One thread per connection, as http.server's ThreadingTCPServer
// does. The request head is read whole and rewritten (rewrite.hpp: Host,
// Origin, the launch cookie's name); then both directions are spliced
// byte for byte until the server closes (it speaks HTTP/1.0 and closes
// after every response, so the stream's end is the answer's end, and the
// \0TAG:json\0 frames and the heartbeat pass the moment they are written).
// Step 2 grows a router in front of the splice: a route the C++ engine
// owns is answered here, everything else keeps going to Python.
#pragma once

#include <atomic>
#include <functional>
#include <string>
#include <thread>

namespace concorde {

class Front {
 public:
  Front();
  ~Front();
  Front(const Front&) = delete;
  Front& operator=(const Front&) = delete;

  // Bind 127.0.0.1:port (0 = any). False and a reason when it is taken.
  bool bind(int port, std::string* err);
  int port() const { return port_; }
  // The server's private port; 0 while there is none (a restart), when
  // the front answers 503 itself.
  void set_upstream(int priv) { upstream_.store(priv); }
  int upstream() const { return upstream_.load(); }
  void start();
  void stop();

  // Counters for the measurements and the tests.
  std::atomic<long> connections{0};
  std::atomic<long> bytes_up{0}, bytes_down{0};

 private:
  void accept_loop();
  void serve(long long fd);
  long long listen_fd_ = -1;
  int port_ = 0;
  std::atomic<int> upstream_{0};
  std::atomic<bool> stopping_{false};
  std::thread acceptor_;
};

// A free loopback port, picked by the OS (bound, read and released).
int pick_free_port();
// Whether something listens on 127.0.0.1:port.
bool port_open(int port);

}  // namespace concorde
