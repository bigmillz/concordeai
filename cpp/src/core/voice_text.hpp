// Dictation's text, segment by segment (NOTES 6b642). SpeechTranscriber
// finalises one utterance in several segments ("What's up?" then " Man."),
// and its volatile results cover only the segment still open; the page
// must always be shown the WHOLE utterance so far. Header-only, no
// platform code, so ctest checks it (tests/voice_text_test.cpp).
#pragma once
#include <string>

namespace concorde::voice {

inline bool is_space(char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r'; }

// a + b with exactly the space the recognizer meant: none added when either
// side already has one at the seam
inline std::string join(const std::string& a, const std::string& b) {
  if (a.empty()) return b;
  if (b.empty()) return a;
  if (is_space(a.back()) || is_space(b.front())) return a + b;
  return a + " " + b;
}

// runs of whitespace become one space; none at the ends
inline std::string tidy(const std::string& s) {
  std::string o;
  o.reserve(s.size());
  bool sp = false;
  for (char c : s) {
    if (is_space(c)) {
      sp = !o.empty();
      continue;
    }
    if (sp) o += ' ';
    sp = false;
    o += c;
  }
  return o;
}

struct Segments {
  std::string committed;  // finalised segments, in order
  std::string open;       // the volatile text of the segment still open
  // one recognizer result: a final closes the open segment, a volatile one
  // replaces it (the SFSpeechRecognizer fallback reports the whole utterance
  // each time and one final at the end; that fits the same rule)
  void result(bool final, const std::string& text) {
    if (final) {
      committed = join(committed, text);
      open.clear();
    } else {
      open = text;
    }
  }
  std::string whole() const { return tidy(join(committed, open)); }
  void clear() { committed.clear(); open.clear(); }
};

}  // namespace concorde::voice
