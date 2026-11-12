// libFuzzer target for the front's parsers (CPP_PLAN.md, risks): the head
// splitter and both rewriters, and the note reader. Built with
// -DCONCORDE_FUZZ=ON by a clang that ships libFuzzer (Homebrew LLVM; Apple
// clang does not), under UBSan (ASan too where its runtime starts; see CMakeLists):
//   cmake -S cpp -B build-fuzz -DCONCORDE_FUZZ=ON -DCMAKE_CXX_COMPILER=$(brew --prefix llvm)/bin/clang++
//   build-fuzz/fuzz_rewrite -max_total_time=60
// Invariants beyond "no crash": a whole head stays a whole head, and no
// rewritten request ever carries a Host naming the private port.
#include <cstdint>
#include <cstdlib>
#include <string>

#include "../src/core/notes.hpp"
#include "../src/core/rewrite.hpp"

using namespace concorde;

extern "C" int LLVMFuzzerTestOneInput(const uint8_t* data, size_t size) {
  std::string in(reinterpret_cast<const char*>(data), size);
  const PortMap m{9893, 51234};
  parse_flat_json(in);
  size_t e = find_head_end(in);
  if (e == std::string::npos) return 0;
  std::string head = in.substr(0, e);
  std::string req = rewrite_request_head(head, m);
  std::string resp = rewrite_response_head(head, m);
  if (find_head_end(req) != req.size()) std::abort();
  if (find_head_end(resp) != resp.size()) std::abort();
  // the only way to a private Host is a public one: an input that never
  // names the public port must never come out naming the private one
  if (head.find("9893") == std::string::npos) {
    std::string lo = req;
    for (char& c : lo)
      if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    if (lo.find("host: 127.0.0.1:51234") != std::string::npos || lo.find("host: localhost:51234") != std::string::npos)
      if (head.find("51234") == std::string::npos) std::abort();
    // a Host that named the private port itself is never let through
    size_t i = 0;
    while ((i = lo.find("\nhost: ", i)) != std::string::npos) {
      i += 7;
      if (lo.compare(i, 15, "127.0.0.1:51234") == 0 || lo.compare(i, 15, "localhost:51234") == 0) std::abort();
    }
  }
  return 0;
}
