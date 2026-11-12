// The segment joining dictation relies on (NOTES 6b642).
#include "core/voice_text.hpp"

#include <cstdio>

static int fails = 0;
static void eq(const std::string& got, const std::string& want, const char* what) {
  if (got != want) {
    std::printf("FAIL %s: got [%s] want [%s]\n", what, got.c_str(), want.c_str());
    ++fails;
  }
}

int main() {
  using concorde::voice::Segments;
  using concorde::voice::tidy;
  {  // SpeechTranscriber, measured: two segments, the second with a leading space
    Segments s;
    for (const char* v : {"What", "What'", "What's", "What's up", "What's up?"}) s.result(false, v);
    eq(s.whole(), "What's up?", "volatile replaces");
    s.result(true, "What's up?");
    eq(s.whole(), "What's up?", "first final");
    s.result(false, " Man");
    eq(s.whole(), "What's up? Man", "partial after a final keeps the final");
    s.result(true, " Man.");
    eq(s.whole(), "What's up? Man.", "the whole utterance");
  }
  {  // a segment with no seam space gets one
    Segments s;
    s.result(true, "Hello");
    s.result(true, "world.");
    eq(s.whole(), "Hello world.", "space added at the seam");
  }
  {  // SFSpeechRecognizer: cumulative partials, one final
    Segments s;
    s.result(false, "what");
    s.result(false, "What's up");
    s.result(true, "What's up, man?");
    eq(s.whole(), "What's up, man?", "cumulative fallback");
  }
  {  // nothing heard
    Segments s;
    eq(s.whole(), "", "empty");
    s.result(true, "");
    eq(s.whole(), "", "empty final");
  }
  eq(tidy("  a \n\t b  "), "a b", "tidy");
  eq(tidy("¿Qué tal?  Bien."), "¿Qué tal? Bien.", "utf-8 untouched");
  std::printf("%s\n", fails ? "voice_text: FAILED" : "voice_text: ok");
  return fails ? 1 : 0;
}
