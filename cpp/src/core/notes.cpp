#include "notes.hpp"

#include <cctype>
#include <cstdio>
#include <cstdlib>

#include "env.hpp"

namespace concorde {

namespace {

struct P {
  const std::string& s;
  size_t i = 0;
  void ws() {
    while (i < s.size() && (s[i] == ' ' || s[i] == '\t' || s[i] == '\r' || s[i] == '\n')) ++i;
  }
  bool eat(char c) {
    ws();
    if (i < s.size() && s[i] == c) {
      ++i;
      return true;
    }
    return false;
  }
  static void utf8(std::string& out, unsigned cp) {
    if (cp < 0x80) {
      out += static_cast<char>(cp);
    } else if (cp < 0x800) {
      out += static_cast<char>(0xC0 | (cp >> 6));
      out += static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
      out += static_cast<char>(0xE0 | (cp >> 12));
      out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
      out += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
      out += static_cast<char>(0xF0 | (cp >> 18));
      out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
      out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
      out += static_cast<char>(0x80 | (cp & 0x3F));
    }
  }
  bool hex4(unsigned& v) {
    if (i + 4 > s.size()) return false;
    v = 0;
    for (int k = 0; k < 4; ++k) {
      char c = s[i++];
      v <<= 4;
      if (c >= '0' && c <= '9') v |= static_cast<unsigned>(c - '0');
      else if (c >= 'a' && c <= 'f') v |= static_cast<unsigned>(c - 'a' + 10);
      else if (c >= 'A' && c <= 'F') v |= static_cast<unsigned>(c - 'A' + 10);
      else return false;
    }
    return true;
  }
  bool str(std::string& out) {
    ws();
    if (i >= s.size() || s[i] != '"') return false;
    ++i;
    while (i < s.size()) {
      char c = s[i++];
      if (c == '"') return true;
      if (static_cast<unsigned char>(c) < 0x20) return false;
      if (c != '\\') {
        out += c;
        continue;
      }
      if (i >= s.size()) return false;
      char e = s[i++];
      switch (e) {
        case '"': out += '"'; break;
        case '\\': out += '\\'; break;
        case '/': out += '/'; break;
        case 'b': out += '\b'; break;
        case 'f': out += '\f'; break;
        case 'n': out += '\n'; break;
        case 'r': out += '\r'; break;
        case 't': out += '\t'; break;
        case 'u': {
          unsigned cp;
          if (!hex4(cp)) return false;
          if (cp >= 0xD800 && cp < 0xDC00 && i + 1 < s.size() && s[i] == '\\' && s[i + 1] == 'u') {
            i += 2;
            unsigned lo;
            if (!hex4(lo) || lo < 0xDC00 || lo > 0xDFFF) return false;
            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
          }
          utf8(out, cp);
          break;
        }
        default: return false;
      }
    }
    return false;
  }
  bool scalar(std::string& out) {
    ws();
    if (i < s.size() && s[i] == '"') return str(out);
    size_t a = i;
    while (i < s.size() && (std::isalnum(static_cast<unsigned char>(s[i])) || s[i] == '-' || s[i] == '+' ||
                            s[i] == '.')) {
      ++i;
    }
    if (i == a) return false;  // an object or array: not flat
    out = s.substr(a, i - a);
    return true;
  }
};

bool to_long(const std::string& s, long& out) {
  if (s.empty() || s.size() > 18) return false;
  for (char c : s)
    if (c < '0' || c > '9') return false;
  out = std::strtol(s.c_str(), nullptr, 10);
  return true;
}

std::string q(const std::string& s) {
  // the values here are token_urlsafe strings; escape anyway, as json.dump would
  std::string o = "\"";
  for (unsigned char c : s) {
    if (c == '"' || c == '\\') {
      o += '\\';
      o += static_cast<char>(c);
    } else if (c < 0x20) {
      char b[8];
      std::snprintf(b, sizeof b, "\\u%04x", c);
      o += b;
    } else {
      o += static_cast<char>(c);
    }
  }
  return o + "\"";
}

}  // namespace

std::optional<std::map<std::string, std::string>> parse_flat_json(const std::string& s) {
  P p{s};
  std::map<std::string, std::string> m;
  if (!p.eat('{')) return std::nullopt;
  if (p.eat('}')) {
    p.ws();
    return p.i == s.size() ? std::optional(m) : std::nullopt;
  }
  for (;;) {
    std::string k, v;
    if (!p.str(k) || !p.eat(':') || !p.scalar(v)) return std::nullopt;
    m[k] = v;
    if (p.eat(',')) continue;
    if (p.eat('}')) break;
    return std::nullopt;
  }
  p.ws();
  if (p.i != s.size()) return std::nullopt;
  return m;
}

std::optional<InstanceNote> read_instance_note(const std::string& path) {
  auto m = parse_flat_json(read_file(path));
  if (!m) return std::nullopt;
  InstanceNote n;
  long port = 0;
  if (!m->count("port") || !to_long((*m)["port"], port) || port <= 0 || port > 65535) return std::nullopt;
  if (!m->count("key") || !m->count("token") || !m->count("pid")) return std::nullopt;
  if (!to_long((*m)["pid"], n.pid)) return std::nullopt;
  n.port = static_cast<int>(port);
  n.key = (*m)["key"];
  n.token = (*m)["token"];
  return n;
}

std::optional<BootNote> read_boot_note(const std::string& path) {
  auto m = parse_flat_json(read_file(path));
  if (!m || !m->count("code") || !m->count("pid")) return std::nullopt;
  BootNote b;
  if (!to_long((*m)["pid"], b.pid)) return std::nullopt;
  b.code = (*m)["code"];
  return b;
}

std::string instance_note_json(const InstanceNote& n) {
  return "{\"port\": " + std::to_string(n.port) + ", \"key\": " + q(n.key) + ", \"token\": " + q(n.token) +
         ", \"pid\": " + std::to_string(n.pid) + "}";
}

std::string boot_note_json(const BootNote& b) {
  return "{\"code\": " + q(b.code) + ", \"pid\": " + std::to_string(b.pid) + "}";
}

}  // namespace concorde
