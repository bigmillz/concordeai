// The app a person opens moves off a taken 8889 to 18890-18898 (6b618), as
// millenai.py's bind_backend does. Run ONLY while something already holds
// 8889 (the desktop app): it never binds 8889 itself, and it starts no server.
// It binds the front alone, says which port it got, and lets it go.
#include <cstdio>
#include <cstdlib>

#include "core/front.hpp"
#include "core/host.hpp"

int main() {
  unsetenv("MILLENAI_DEV");
  unsetenv("MILLENAI_HOME");
  unsetenv("MILLENAI_PORT");
  unsetenv("CONCORDE_PUBLIC_PORT");
  if (!concorde::port_open(8889)) {
    std::printf("SKIP: nothing holds 8889 here; this check runs only beside the desktop app\n");
    return 0;
  }
  concorde::Host h(/*windowed=*/false);
  std::string err;
  bool ok = h.bind_front(&err);
  int p = h.public_port();
  std::printf("8889 taken; the app's front bound %d (%s)\n", p, ok ? "ok" : err.c_str());
  return ok && p >= 18890 && p <= 18898 ? 0 : 1;
}
