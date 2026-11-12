// The front's edits and the note reader, without a socket (CPP_PLAN.md
// step 1). `ctest` runs it; every case names the server rule it keeps.
#include <cstdio>
#include <string>

#include "../src/core/notes.hpp"
#include "../src/core/rewrite.hpp"

using namespace concorde;

static int fails = 0, passes = 0;
#define CHECK(name, cond)                                  \
  do {                                                     \
    if (cond) {                                            \
      ++passes;                                            \
    } else {                                               \
      ++fails;                                             \
      std::printf("FAIL %s (line %d)\n", name, __LINE__); \
    }                                                      \
  } while (0)

int main() {
  const PortMap m{9893, 51234};

  // Host: the public name on the private port; anything else refused by the server
  CHECK("host public", rewrite_host_value("127.0.0.1:9893", m) == "127.0.0.1:51234");
  CHECK("host localhost", rewrite_host_value("LocalHost:9893 ", m) == "localhost:51234");
  CHECK("host private is not a way in", rewrite_host_value("127.0.0.1:51234", m) == "0.invalid");
  CHECK("host other", rewrite_host_value("evil.example:9893", m) == "0.invalid");
  CHECK("host empty", rewrite_host_value("", m) == "0.invalid");
  CHECK("host leading zero", rewrite_host_value("127.0.0.1:09893", m) == "0.invalid");
  CHECK("host nbsp stripped as python does", rewrite_host_value("\xa0" "127.0.0.1:9893", m) == "127.0.0.1:51234");

  // Origin: the CSRF rule compares the port after the first colon
  CHECK("origin public", rewrite_origin_value("http://127.0.0.1:9893", m) == "http://127.0.0.1:51234");
  CHECK("origin localhost path", rewrite_origin_value("http://localhost:9893/x", m) == "http://localhost:51234/x");
  CHECK("origin private", rewrite_origin_value("http://127.0.0.1:51234", m) == "http://0.invalid");
  CHECK("origin foreign", rewrite_origin_value("https://evil.example", m) == "https://evil.example");
  CHECK("origin null", rewrite_origin_value("null", m) == "null");
  CHECK("origin other port", rewrite_origin_value("http://127.0.0.1:9894", m) == "http://127.0.0.1:9894");

  // Cookie: the launch key renamed; another port's same-name cookie dropped
  CHECK("cookie rename", rewrite_cookie_value(" millen_key_9893=K", m) == " millen_key_51234=K");
  CHECK("cookie among others", rewrite_cookie_value("a=1; millen_key_9893=K; b=2", m) ==
                                   "a=1; millen_key_51234=K; b=2");
  CHECK("cookie private dropped", rewrite_cookie_value("millen_key_51234=K; a=1", m) == " a=1");
  CHECK("cookie longer name kept", rewrite_cookie_value("millen_key_98930=K", m) == "millen_key_98930=K");
  CHECK("cookie space before = kept", rewrite_cookie_value("millen_key_9893 =K", m) == "millen_key_9893 =K");

  // whole heads
  std::string req =
      "POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:9893\r\nOrigin: http://127.0.0.1:9893\r\n"
      "Cookie: millen_key_9893=KEY\r\nX-Api-Token: T\r\nContent-Length: 2\r\n\r\n";
  size_t e = find_head_end(req);
  CHECK("head end", e == req.size());
  std::string out = rewrite_request_head(req, m);
  CHECK("request rewritten",
        out == "POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:51234\r\nOrigin: http://127.0.0.1:51234\r\n"
               "Cookie: millen_key_51234=KEY\r\nX-Api-Token: T\r\nContent-Length: 2\r\n\r\n");
  std::string proxied = "GET / HTTP/1.1\r\nHost: 127.0.0.1:9893\r\nX-Forwarded-For: 1.2.3.4\r\n\r\n";
  CHECK("proxy header passes, for the server to refuse",
        rewrite_request_head(proxied, m).find("X-Forwarded-For: 1.2.3.4\r\n") != std::string::npos);
  std::string bare = "GET / HTTP/1.0\nHost: localhost:9893\n\n";
  CHECK("bare LF head", find_head_end(bare) == bare.size() &&
                            rewrite_request_head(bare, m) == "GET / HTTP/1.0\nHost: localhost:51234\n\n");
  std::string folded = "GET / HTTP/1.1\r\nHost: 127.0.0.1:9893\r\n x\r\n\r\n";
  CHECK("folded host refused", rewrite_request_head(folded, m) == "GET / HTTP/1.1\r\nHost: 0.invalid\r\n\r\n");
  std::string nohost = "GET / HTTP/1.1\r\nAccept: */*\r\n\r\n";
  CHECK("no host stays no host", rewrite_request_head(nohost, m) == nohost);

  std::string resp =
      "HTTP/1.0 302 Found\r\nServer: BaseHTTP/0.6 Python/3.14.0\r\n"
      "Set-Cookie: millen_key_51234=KEY; Path=/; HttpOnly; SameSite=Strict\r\nLocation: /\r\n\r\n";
  CHECK("set-cookie renamed", rewrite_response_head(resp, m) ==
                                  "HTTP/1.0 302 Found\r\nServer: BaseHTTP/0.6 Python/3.14.0\r\n"
                                  "Set-Cookie: millen_key_9893=KEY; Path=/; HttpOnly; SameSite=Strict\r\n"
                                  "Location: /\r\n\r\n");
  std::string loc = "HTTP/1.0 302 Found\r\nLocation: http://127.0.0.1:51234/account\r\n\r\n";
  CHECK("absolute location", rewrite_response_head(loc, m) ==
                                 "HTTP/1.0 302 Found\r\nLocation: http://127.0.0.1:9893/account\r\n\r\n");

  // the notes, as json.dump writes them
  auto j = parse_flat_json("{\"port\": 9893, \"key\": \"a-b_c\", \"token\": \"t\\u0041\", \"pid\": 42}");
  CHECK("note parse", j && (*j)["port"] == "9893" && (*j)["token"] == "tA" && (*j)["pid"] == "42");
  CHECK("nested refused", !parse_flat_json("{\"a\": {\"b\": 1}}"));
  CHECK("trailing junk refused", !parse_flat_json("{\"a\": 1} x"));
  InstanceNote n{9893, "k", "t", 7};
  CHECK("note write", instance_note_json(n) == "{\"port\": 9893, \"key\": \"k\", \"token\": \"t\", \"pid\": 7}");

  std::printf("%d passed, %d failed\n", passes, fails);
  return fails ? 1 : 0;
}
