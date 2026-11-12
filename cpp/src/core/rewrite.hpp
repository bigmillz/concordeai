// THE FRONT'S ONLY EDITS (CPP_PLAN.md step 1). The front owns the public
// port the page and every native caller see; the Python server listens on
// a private loopback port behind it. To the outside the pair must be
// indistinguishable from Python alone on the public port, so the front
// touches exactly four things in a head and passes every other byte as it
// came:
//
//   request  Host     127.0.0.1:<pub> / localhost:<pub> -> the same name on
//                     <priv>; anything else -> "0.invalid", so the server's
//                     own refusal answers (a Host naming <priv> directly
//                     must not get in through the front).
//            Origin   a netloc on port <pub> -> <priv>; one on <priv> ->
//                     http://0.invalid (a mismatch, as it would be).
//            Cookie   millen_key_<pub> renamed millen_key_<priv>; a cookie
//                     already named millen_key_<priv> is dropped (another
//                     port's cookie never opens this one, 6b310).
//   response Set-Cookie  millen_key_<priv>= -> millen_key_<pub>=
//            Location    http://127.0.0.1:<priv> / localhost:<priv> -> <pub>
//
// Nothing is added: no X-Forwarded-*, no Via (the server refuses any proxy
// header, 6b320). Pure functions, so the unit test and the fuzzer drive
// them without a socket.
#pragma once

#include <cstddef>
#include <string>
#include <string_view>

namespace concorde {

struct PortMap {
  int pub = 0;
  int priv = 0;
};

// Offset just past the blank line that ends a head ("\r\n\r\n" or the
// "\n\n" http.server also accepts), or std::string::npos.
size_t find_head_end(std::string_view buf);

std::string rewrite_request_head(std::string_view head, const PortMap& m);
std::string rewrite_response_head(std::string_view head, const PortMap& m);

// The parts, public for the tests.
std::string rewrite_host_value(std::string_view v, const PortMap& m);
std::string rewrite_origin_value(std::string_view v, const PortMap& m);
std::string rewrite_cookie_value(std::string_view v, const PortMap& m);
// Python's str.strip() / regex \s over a latin-1 decoded header: ASCII
// whitespace, \x1c-\x1f, \x85 and \xa0.
bool py_space(unsigned char c);

}  // namespace concorde
