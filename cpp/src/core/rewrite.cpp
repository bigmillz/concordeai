#include "rewrite.hpp"

#include <vector>

namespace concorde {

bool py_space(unsigned char c) {
  return c == ' ' || (c >= 0x09 && c <= 0x0d) || (c >= 0x1c && c <= 0x1f) || c == 0x85 || c == 0xa0;
}

namespace {

std::string_view py_strip(std::string_view s) {
  size_t a = 0, b = s.size();
  while (a < b && py_space(static_cast<unsigned char>(s[a]))) ++a;
  while (b > a && py_space(static_cast<unsigned char>(s[b - 1]))) --b;
  return s.substr(a, b - a);
}

std::string lower(std::string_view s) {
  std::string o(s);
  for (char& c : o)
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
  return o;
}

bool ieq(std::string_view a, std::string_view b) {
  if (a.size() != b.size()) return false;
  for (size_t i = 0; i < a.size(); ++i) {
    char x = a[i], y = b[i];
    if (x >= 'A' && x <= 'Z') x = static_cast<char>(x - 'A' + 'a');
    if (y >= 'A' && y <= 'Z') y = static_cast<char>(y - 'A' + 'a');
    if (x != y) return false;
  }
  return true;
}

// One header as it sits in the head: its first line and any obs-fold
// continuation lines, each with its own line ending.
struct Field {
  std::string_view name;  // exactly as written, up to the colon
  std::string_view raw;   // every byte of every line of it, endings included
  size_t value_at = 0;    // offset in raw just past the colon
  std::string_view eol;   // the first line's ending ("\r\n" or "\n")
  bool has_colon = false;
};

struct Split {
  std::string_view start_line;  // with its ending
  std::vector<Field> fields;
  std::string_view tail;  // the blank line, and anything after it
};

Split split_head(std::string_view h) {
  Split s;
  size_t i = 0;
  auto line_end = [&](size_t from) {
    size_t n = h.find('\n', from);
    return n == std::string_view::npos ? h.size() : n + 1;
  };
  size_t e = line_end(0);
  s.start_line = h.substr(0, e);
  i = e;
  while (i < h.size()) {
    e = line_end(i);
    std::string_view line = h.substr(i, e - i);
    if (line == "\r\n" || line == "\n") {
      s.tail = h.substr(i);
      return s;
    }
    bool cont = !line.empty() && (line[0] == ' ' || line[0] == '\t');
    if (cont && !s.fields.empty()) {
      Field& f = s.fields.back();
      f.raw = std::string_view(f.raw.data(), f.raw.size() + line.size());
    } else {
      Field f;
      f.raw = line;
      size_t c = line.find(':');
      size_t body_end = line.size();
      if (body_end && line[body_end - 1] == '\n') --body_end;
      if (body_end && line[body_end - 1] == '\r') --body_end;
      f.eol = line.substr(body_end);
      if (c != std::string_view::npos && c < body_end) {
        f.has_colon = true;
        f.name = line.substr(0, c);
        f.value_at = c + 1;
      }
      s.fields.push_back(f);
    }
    i = e;
  }
  return s;  // no blank line: the caller only hands over whole heads
}

// The value as compat32's header_source_parse gives it: the first line
// after the colon, left-stripped of spaces and tabs, the continuation
// lines joined raw, the final line ending removed.
std::string logical_value(const Field& f) {
  std::string_view v = f.raw.substr(f.value_at);
  size_t a = 0;
  while (a < v.size() && (v[a] == ' ' || v[a] == '\t')) ++a;
  std::string out(v.substr(a));
  while (!out.empty() && (out.back() == '\r' || out.back() == '\n')) out.pop_back();
  return out;
}

std::string emit(std::string_view name, const std::string& value, std::string_view eol) {
  std::string o(name);
  o += ": ";
  o += value;
  o += eol.empty() ? std::string_view("\r\n") : eol;
  return o;
}

}  // namespace

size_t find_head_end(std::string_view buf) {
  size_t i = buf.find('\n');
  if (i == std::string_view::npos) return std::string_view::npos;
  ++i;  // past the start line, whatever it holds
  while (i < buf.size()) {
    size_t n = buf.find('\n', i);
    if (n == std::string_view::npos) return std::string_view::npos;
    std::string_view line = buf.substr(i, n - i);
    if (line.empty() || line == "\r") return n + 1;
    i = n + 1;
  }
  return std::string_view::npos;
}

std::string rewrite_host_value(std::string_view v, const PortMap& m) {
  std::string t = lower(py_strip(v));
  const std::string pub = std::to_string(m.pub), priv = std::to_string(m.priv);
  if (t == "127.0.0.1:" + pub) return "127.0.0.1:" + priv;
  if (t == "localhost:" + pub) return "localhost:" + priv;
  return "0.invalid";
}

std::string rewrite_origin_value(std::string_view v, const PortMap& m) {
  std::string_view t = py_strip(v);
  std::string lo = lower(t);
  if (lo.empty() || lo == "null") return std::string(v);
  size_t s = lo.find("://");
  if (s == std::string::npos) return std::string(v);
  size_t ns = s + 3;
  size_t ne = lo.find_first_of("/?#", ns);
  if (ne == std::string::npos) ne = lo.size();
  std::string_view netloc = std::string_view(lo).substr(ns, ne - ns);
  // the server compares with str.partition(":"): the port is everything
  // after the FIRST colon, so the swap is made on exactly that
  size_t c = netloc.find(':');
  if (c == std::string_view::npos) return std::string(v);
  std::string_view port = netloc.substr(c + 1);
  if (port == std::to_string(m.pub)) {
    std::string o(t.substr(0, ns + c + 1));
    o += std::to_string(m.priv);
    o += t.substr(ne);
    return o;
  }
  if (port == std::to_string(m.priv)) return "http://0.invalid";
  return std::string(v);
}

std::string rewrite_cookie_value(std::string_view v, const PortMap& m) {
  const std::string pub = "millen_key_" + std::to_string(m.pub);
  const std::string priv = "millen_key_" + std::to_string(m.priv);
  std::vector<std::string> segs;
  size_t i = 0;
  while (true) {
    size_t n = v.find(';', i);
    segs.emplace_back(v.substr(i, n == std::string_view::npos ? std::string_view::npos : n - i));
    if (n == std::string_view::npos) break;
    i = n + 1;
  }
  std::vector<std::string> kept;
  for (std::string& sg : segs) {
    size_t a = 0;
    while (a < sg.size() && py_space(static_cast<unsigned char>(sg[a]))) ++a;
    size_t eq = sg.find('=', a);
    if (eq != std::string::npos) {
      std::string_view name = std::string_view(sg).substr(a, eq - a);
      if (name == priv) continue;  // another port's cookie: never this one's key
      if (name == pub) sg = sg.substr(0, a) + priv + sg.substr(eq);
    }
    kept.push_back(sg);
  }
  std::string o;
  for (size_t k = 0; k < kept.size(); ++k) {
    if (k) o += ';';
    o += kept[k];
  }
  return o;
}

std::string rewrite_request_head(std::string_view head, const PortMap& m) {
  Split s = split_head(head);
  std::string o(s.start_line);
  for (const Field& f : s.fields) {
    if (!f.has_colon) {
      o += f.raw;
      continue;
    }
    if (ieq(f.name, "Host")) {
      o += emit(f.name, rewrite_host_value(logical_value(f), m), f.eol);
    } else if (ieq(f.name, "Origin")) {
      std::string lv = logical_value(f);
      std::string nv = rewrite_origin_value(lv, m);
      if (nv == lv) o += f.raw;
      else o += emit(f.name, nv, f.eol);
    } else if (ieq(f.name, "Cookie")) {
      // edited in place, continuation lines and all: the server's regex
      // reads the joined value (\s* spans a fold)
      std::string_view raw = f.raw.substr(f.value_at);
      size_t body = raw.size();
      if (body && raw[body - 1] == '\n') --body;
      if (body && raw[body - 1] == '\r') --body;
      std::string nv = rewrite_cookie_value(raw.substr(0, body), m);
      if (nv == raw.substr(0, body)) {
        o += f.raw;
      } else {
        o += f.name;
        o += ':';
        o += nv;
        o += raw.substr(body);
      }
    } else {
      o += f.raw;
    }
  }
  o += s.tail;
  return o;
}

std::string rewrite_response_head(std::string_view head, const PortMap& m) {
  Split s = split_head(head);
  std::string o(s.start_line);
  const std::string priv_c = "millen_key_" + std::to_string(m.priv) + "=";
  const std::string pub_c = "millen_key_" + std::to_string(m.pub) + "=";
  for (const Field& f : s.fields) {
    if (!f.has_colon) {
      o += f.raw;
      continue;
    }
    if (ieq(f.name, "Set-Cookie")) {
      std::string lv = logical_value(f);
      if (lv.compare(0, priv_c.size(), priv_c) == 0) {
        o += emit(f.name, pub_c + lv.substr(priv_c.size()), f.eol);
        continue;
      }
    } else if (ieq(f.name, "Location")) {
      std::string lv = logical_value(f);
      for (const char* h : {"http://127.0.0.1:", "http://localhost:"}) {
        std::string pre = std::string(h) + std::to_string(m.priv);
        if (lv.compare(0, pre.size(), pre) == 0 &&
            (lv.size() == pre.size() || lv[pre.size()] == '/' || lv[pre.size()] == '?' || lv[pre.size()] == '#')) {
          lv = std::string(h) + std::to_string(m.pub) + lv.substr(pre.size());
          o += emit(f.name, lv, f.eol);
          goto next;
        }
      }
    }
    o += f.raw;
  next:;
  }
  o += s.tail;
  return o;
}

}  // namespace concorde
