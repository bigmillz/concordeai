// Llama 3's tokenizer ENCODE in C++ (NOTES 6b614), so the spike can take a
// question as text: tokenizer.json's byte-level BPE (vocab, ranked merges,
// ignore_merges), its pre-tokenizer regex written out by hand, and the
// Llama 3.2 chat template's one-user-message shape. Checked against
// transformers' apply_chat_template on the prompts in tok_check.py.
// A spike: the regex's \p{L}, \p{N} and \s are the C library's iswalpha,
// iswnumber and iswspace under a UTF-8 locale, close to Unicode's classes,
// not identical for every script. The real port borrows HF tokenizers.
#pragma once

#include <clocale>
#include <cstdint>
#include <ctime>
#include <cwctype>
#include <wctype.h>
#include <fstream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

namespace llama3 {

inline std::string utf8(char32_t v) {
  std::string o;
  if (v < 0x80) {
    o += static_cast<char>(v);
  } else if (v < 0x800) {
    o += static_cast<char>(0xC0 | (v >> 6));
    o += static_cast<char>(0x80 | (v & 0x3F));
  } else if (v < 0x10000) {
    o += static_cast<char>(0xE0 | (v >> 12));
    o += static_cast<char>(0x80 | ((v >> 6) & 0x3F));
    o += static_cast<char>(0x80 | (v & 0x3F));
  } else {
    o += static_cast<char>(0xF0 | (v >> 18));
    o += static_cast<char>(0x80 | ((v >> 12) & 0x3F));
    o += static_cast<char>(0x80 | ((v >> 6) & 0x3F));
    o += static_cast<char>(0x80 | (v & 0x3F));
  }
  return o;
}

inline std::u32string from_utf8(const std::string& s) {
  std::u32string o;
  for (size_t i = 0; i < s.size();) {
    unsigned char c = static_cast<unsigned char>(s[i]);
    int n = c < 0x80 ? 1 : c < 0xE0 ? 2 : c < 0xF0 ? 3 : 4;
    char32_t v = n == 1 ? c : n == 2 ? (c & 0x1F) : n == 3 ? (c & 0x0F) : (c & 0x07);
    for (int k = 1; k < n && i + k < s.size(); ++k) v = (v << 6) | (static_cast<unsigned char>(s[i + k]) & 0x3F);
    o += v;
    i += static_cast<size_t>(n);
  }
  return o;
}

class Tokenizer {
 public:
  bool load(const std::string& dir) {
    std::setlocale(LC_CTYPE, "en_US.UTF-8");
    std::ifstream f(dir + "/tokenizer.json", std::ios::binary);
    std::ostringstream ss;
    ss << f.rdbuf();
    j_ = ss.str();
    // GPT-2's bytes_to_unicode
    std::vector<int> bs;
    for (int b = '!'; b <= '~'; ++b) bs.push_back(b);
    for (int b = 0xA1; b <= 0xAC; ++b) bs.push_back(b);
    for (int b = 0xAE; b <= 0xFF; ++b) bs.push_back(b);
    std::vector<char32_t> cs(bs.begin(), bs.end());
    int extra = 0;
    for (int b = 0; b < 256; ++b) {
      bool in = false;
      for (int x : bs) in = in || x == b;
      if (!in) {
        bs.push_back(b);
        cs.push_back(static_cast<char32_t>(256 + extra++));
      }
    }
    for (size_t i = 0; i < bs.size(); ++i) byte_char_[bs[i]] = utf8(cs[i]);
    // "vocab": { "tok": id, ... }
    size_t i = j_.find('{', j_.find("\"vocab\"")) + 1;
    for (;;) {
      skip(i);
      if (j_[i] == '}') break;
      ++i;
      std::string t = str(i);
      size_t c = j_.find(':', i);
      vocab_[t] = std::atoi(j_.c_str() + c + 1);
      i = j_.find_first_of(",}", c);
      if (j_[i] == ',') ++i;
    }
    // "merges": [ ["a", "b"], ... ] , ranked by position
    i = j_.find('[', j_.find("\"merges\"")) + 1;
    for (int rank = 0;; ++rank) {
      skip(i);
      if (j_[i] == ']') break;
      ++i;  // the pair's [
      skip(i);
      ++i;  // a's opening quote
      std::string a = str(i);
      skip(i);  // the comma and any spaces
      ++i;      // b's opening quote
      std::string b = str(i);
      skip(i);
      ++i;  // the pair's ]
      merges_[a + '\x01' + b] = rank;
      skip(i);
    }
    j_.clear();
    return !vocab_.empty() && !merges_.empty();
  }

  // the ids transformers' apply_chat_template gives one user message
  // (Llama 3.2's template, no tools, today's date)
  std::vector<int> encode_chat(const std::string& user) const {
    char date[32];
    std::time_t now = std::time(nullptr);
    std::strftime(date, sizeof date, "%d %b %Y", std::localtime(&now));
    std::vector<int> ids{128000, 128006};
    add(ids, "system");
    ids.push_back(128007);
    add(ids, std::string("\n\nCutting Knowledge Date: December 2023\nToday Date: ") + date + "\n\n");
    ids.push_back(128009);
    ids.push_back(128006);
    add(ids, "user");
    ids.push_back(128007);
    add(ids, "\n\n" + trim(user));
    ids.push_back(128009);
    ids.push_back(128006);
    add(ids, "assistant");
    ids.push_back(128007);
    add(ids, "\n\n");
    return ids;
  }

  void add(std::vector<int>& ids, const std::string& text) const {
    for (const std::u32string& piece : pretokenize(from_utf8(text))) {
      std::string bytes;
      for (char32_t c : piece) bytes += utf8(c);
      std::string key;
      for (unsigned char b : bytes) key += byte_char_[b];
      auto it = vocab_.find(key);
      if (it != vocab_.end()) {  // ignore_merges: a whole word in the vocab is that token
        ids.push_back(it->second);
        continue;
      }
      std::vector<std::string> sym;
      for (char32_t c : from_utf8(key)) sym.push_back(utf8(c));
      for (;;) {
        int best = -1;
        size_t at = 0;
        for (size_t k = 0; k + 1 < sym.size(); ++k) {
          auto m = merges_.find(sym[k] + '\x01' + sym[k + 1]);
          if (m != merges_.end() && (best < 0 || m->second < best)) {
            best = m->second;
            at = k;
          }
        }
        if (best < 0) break;
        sym[at] += sym[at + 1];
        sym.erase(sym.begin() + static_cast<long>(at) + 1);
      }
      for (auto& s : sym) {
        auto v = vocab_.find(s);
        if (v != vocab_.end()) ids.push_back(v->second);
      }
    }
  }

 private:
  static bool L(char32_t c) { return std::iswalpha(static_cast<wint_t>(c)); }
  static bool N(char32_t c) { return ::iswnumber(static_cast<wint_t>(c)); }
  static bool S(char32_t c) { return std::iswspace(static_cast<wint_t>(c)); }
  static bool NL(char32_t c) { return c == '\r' || c == '\n'; }
  static char32_t lower(char32_t c) { return (c >= 'A' && c <= 'Z') ? c + 32 : c; }

  // (?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}|
  //  ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+
  static size_t match(const std::u32string& s, size_t i) {
    const size_t n = s.size();
    if (s[i] == '\'' && i + 1 < n) {
      char32_t a = lower(s[i + 1]);
      if (a == 's' || a == 't' || a == 'm' || a == 'd') return i + 2;
      if (i + 2 < n) {
        char32_t b = lower(s[i + 2]);
        if ((a == 'r' && b == 'e') || (a == 'v' && b == 'e') || (a == 'l' && b == 'l')) return i + 3;
      }
    }
    {
      size_t k = i;
      if (!L(s[k]) && !N(s[k]) && !NL(s[k])) ++k;
      if (k < n && L(s[k])) {
        while (k < n && L(s[k])) ++k;
        return k;
      }
    }
    if (N(s[i])) {
      size_t k = i;
      while (k < n && k - i < 3 && N(s[k])) ++k;
      return k;
    }
    {
      size_t k = i;
      if (s[k] == ' ') ++k;
      if (k < n && !S(s[k]) && !L(s[k]) && !N(s[k])) {
        while (k < n && !S(s[k]) && !L(s[k]) && !N(s[k])) ++k;
        while (k < n && NL(s[k])) ++k;
        return k;
      }
    }
    size_t k = i, last = std::u32string::npos;
    while (k < n && S(s[k])) {
      if (NL(s[k])) last = k;
      ++k;
    }
    if (last != std::u32string::npos) return last + 1;
    if (k > i) {
      if (k == n) return k;
      if (k - i >= 2) return k - 1;
      return k;
    }
    return i + 1;
  }

  static std::vector<std::u32string> pretokenize(const std::u32string& s) {
    std::vector<std::u32string> out;
    for (size_t i = 0; i < s.size();) {
      size_t e = match(s, i);
      out.push_back(s.substr(i, e - i));
      i = e;
    }
    return out;
  }

  static std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n"), b = s.find_last_not_of(" \t\r\n");
    return a == std::string::npos ? "" : s.substr(a, b - a + 1);
  }

  void skip(size_t& i) const {
    while (i < j_.size() && (j_[i] == ' ' || j_[i] == '\n' || j_[i] == '\r' || j_[i] == '\t' || j_[i] == ',')) ++i;
  }

  std::string str(size_t& i) const {  // a JSON string just past its quote, to UTF-8
    std::string o;
    while (i < j_.size() && j_[i] != '"') {
      if (j_[i] != '\\') {
        o += j_[i++];
        continue;
      }
      char e = j_[++i];
      if (e == 'u') {
        char32_t v = static_cast<char32_t>(std::stoul(j_.substr(i + 1, 4), nullptr, 16));
        i += 4;
        if (v >= 0xD800 && v < 0xDC00 && j_[i + 1] == '\\') {
          char32_t lo = static_cast<char32_t>(std::stoul(j_.substr(i + 3, 4), nullptr, 16));
          v = 0x10000 + ((v - 0xD800) << 10) + (lo - 0xDC00);
          i += 6;
        }
        o += utf8(v);
      } else {
        o += e == 'n' ? '\n' : e == 't' ? '\t' : e == 'r' ? '\r' : e == 'b' ? '\b' : e == 'f' ? '\f' : e;
      }
      ++i;
    }
    ++i;
    return o;
  }

  std::string j_;
  std::string byte_char_[256];
  std::unordered_map<std::string, int> vocab_;
  std::unordered_map<std::string, int> merges_;
};

}  // namespace llama3
