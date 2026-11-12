// Dictation, native in the C++ shell (NOTES 6b642). See voice.h.
//
// The bridge (the page's side is millenai.py's dictation code):
//   window.pywebview.api.voice({act, lang, u}) -> a promise of the reply
//     warm  {lang}  -> {ok, engine:"analyzer"|"sfspeech"|"none", locale, ready,
//                       module:"dictation"|"speech" (the analyzer's)}
//                      the locale resolved, the model loaded and the next
//                      analyzer prepared; the microphone NOT opened
//     start {lang}  -> {ok:true, u, engine, locale}
//                    | {ok:false, code:"mic-denied"|"speech-denied"|"no-mic"|"no-engine"}
//                      capture starts at once; what is said before the
//                      recognizer is ready is held (the pre-roll)
//     stop  {u}     -> {ok:true}; the final follows as an event
//     cancel{u}     -> {ok:true}; nothing more for u
//   events, window.__concordeVoice(ev):
//     {ev:"listening",u,ms,engine,locale}  {ev:"level",u,rms} (~20 Hz, capture only)
//     {ev:"partial",u,text} (the WHOLE utterance so far)  {ev:"final",u,text}
//     {ev:"err",u,code,msg}  {ev:"done",u}
//     and, in a probe build reading CONCORDE_VOICE_FILE, {ev:"eof",u,ms}
//   ms is milliseconds since the start op arrived.
// The handler that calls -handle:reply: is the window's `concorde` handler,
// which answers only the main frame on the app's own origin (app.mm ownFrame).
//
// The language is NEVER detected: the page's choice (Settings), else the
// system's preferred languages in order, the first one the recognizer
// supports, else en-US. A 1-2 s clip with automatic language ID is how
// "What's up, man?" became "Hvittar" (NOTES 6b642).
#import "voice.h"

#import <AVFoundation/AVFoundation.h>
#import <Speech/Speech.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <map>
#include <mutex>
#include <string>
#include <thread>

#include "core/voice_text.hpp"
#import "voice_sa.h"

namespace {

double now_ms() {
  using namespace std::chrono;
  return duration<double, std::milli>(steady_clock::now().time_since_epoch()).count();
}

// a lang the page sent: BCP-47-ish letters, digits, - and _ only, short
NSString* clean_lang(id v) {
  if (![v isKindOfClass:NSString.class]) return @"";
  NSString* s = (NSString*)v;
  if (s.length > 35) return @"";
  NSCharacterSet* bad =
      [[NSCharacterSet characterSetWithCharactersInString:
                           @"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"] invertedSet];
  return [s rangeOfCharacterFromSet:bad].location == NSNotFound ? s : @"";
}

// a copy the recognizer may hold past the tap's own block
AVAudioPCMBuffer* copy_buf(AVAudioPCMBuffer* b) {
  AVAudioPCMBuffer* c = [[AVAudioPCMBuffer alloc] initWithPCMFormat:b.format frameCapacity:b.frameLength];
  if (!c) return nil;
  c.frameLength = b.frameLength;
  const AudioBufferList* src = b.audioBufferList;
  AudioBufferList* dst = c.mutableAudioBufferList;
  for (UInt32 i = 0; i < src->mNumberBuffers && i < dst->mNumberBuffers; ++i) {
    UInt32 n = std::min(src->mBuffers[i].mDataByteSize, dst->mBuffers[i].mDataByteSize);
    memcpy(dst->mBuffers[i].mData, src->mBuffers[i].mData, n);
    dst->mBuffers[i].mDataByteSize = n;
  }
  return c;
}

bool timing_on() { return getenv("CONCORDE_SHELL_TIMING") != nullptr; }

// SFSpeechRecognizer's "nothing was said" is not an error the page should see
bool no_speech(NSError* e) {
  return e && ([e.domain isEqualToString:@"kAFAssistantErrorDomain"] && (e.code == 1110 || e.code == 203));
}

struct Utt {
  concorde::voice::Segments seg;
  std::string sent;  // the last text sent, so a repeat isn't
  double t0 = 0, tCap = 0, tLevel = 0, tPartial = 0, tStop = 0;
  std::string locale;
  bool ended = false;
  SFSpeechAudioBufferRecognitionRequest* sfReq = nil;
  SFSpeechRecognitionTask* sfTask = nil;
};

}  // namespace

@implementation CCVoice {
  __weak WKWebView* _web;
  NSString* _engine;  // analyzer | sfspeech | none
  CCSAEngine* _sa;
  AVAudioEngine* _audio;
  BOOL _tapped;
  std::atomic<long> _live;  // the utterance capturing now, 0: none
  long _u;                  // the last utterance number given out
  std::mutex _m;            // _utts, and the level sums
  std::map<long, Utt> _utts;
  double _sq;
  long _n, _per;
  NSString* _lang;  // the last start's lang (re-warmed after each final)
  // the probe build's file input
  std::atomic<bool> _fileStop;
  std::thread _fileThread;
  // the fallback
  SFSpeechRecognizer* _sfRec;
  NSString* _sfFor;  // the lang _sfRec was resolved for
}

- (instancetype)initWithWeb:(WKWebView*)web {
  if (!(self = [super init])) return nil;
  _web = web;
  _live = 0;
  _fileStop = false;
  NSString* force = nil;
#ifdef CONCORDE_PROBE
  if (const char* f = getenv("CONCORDE_VOICE_ENGINE")) force = @(f);
#endif
  if ([force isEqualToString:@"sfspeech"]) _engine = @"sfspeech";
  else if ([force isEqualToString:@"none"]) _engine = @"none";
  else if ([CCSAEngine available]) _engine = @"analyzer";
  else if (NSClassFromString(@"SFSpeechRecognizer")) _engine = @"sfspeech";
  else _engine = @"none";
  if ([_engine isEqualToString:@"analyzer"]) _sa = [CCSAEngine new];
  return self;
}

- (NSString*)voiceFile {
#ifdef CONCORDE_PROBE
  if (const char* f = getenv("CONCORDE_VOICE_FILE")) return @(f);
#endif
  return nil;
}

// ------------------------------------------------------------ the bridge

- (void)handle:(NSDictionary*)cmd reply:(void (^)(id _Nullable))reply0 {
  void (^reply)(id) = ^(id o) {
    if ([NSThread isMainThread]) reply0(o);
    else dispatch_async(dispatch_get_main_queue(), ^{ reply0(o); });
  };
  NSString* act = [cmd[@"act"] isKindOfClass:NSString.class] ? cmd[@"act"] : @"";
  NSString* lang = clean_lang(cmd[@"lang"]);
  long u = [cmd[@"u"] isKindOfClass:NSNumber.class] ? [cmd[@"u"] longValue] : 0;
  if ([act isEqualToString:@"warm"]) [self warm:lang reply:reply];
  else if ([act isEqualToString:@"start"]) [self start:lang reply:reply];
  else if ([act isEqualToString:@"stop"]) {
    [self stop:u];
    reply(@{@"ok" : @YES});
  } else if ([act isEqualToString:@"cancel"]) {
    [self cancel:u];
    reply(@{@"ok" : @YES});
  } else reply(@{@"ok" : @NO, @"code" : @"bad-act"});
}

- (void)emit:(NSDictionary*)ev {
  dispatch_async(dispatch_get_main_queue(), ^{
    WKWebView* w = self->_web;
    if (!w) return;
    NSData* j = [NSJSONSerialization dataWithJSONObject:ev options:0 error:nil];
    if (!j) return;
    NSString* js = [NSString stringWithFormat:@"window.__concordeVoice&&window.__concordeVoice(%@)",
                                              [[NSString alloc] initWithData:j encoding:NSUTF8StringEncoding]];
    [w evaluateJavaScript:js completionHandler:nil];
  });
}

// ------------------------------------------------------------ warm

- (NSString*)sfResolve:(NSString*)lang {
  // identifiers compared as ll_RR whichever separator either side uses
  auto key = [](NSString* s) { return [s stringByReplacingOccurrencesOfString:@"-" withString:@"_"]; };
  NSMutableArray<NSString*>* sup = [NSMutableArray new];
  for (NSLocale* l in [SFSpeechRecognizer supportedLocales]) [sup addObject:key(l.localeIdentifier)];
  [sup sortUsingSelector:@selector(compare:)];
  NSMutableArray<NSString*>* asks = [NSMutableArray new];
  if (lang.length) [asks addObject:lang];
  else [asks addObjectsFromArray:NSLocale.preferredLanguages];
  for (NSString* a0 in asks) {
    NSString* a = key(a0);
    for (NSString* l in sup)
      if ([l caseInsensitiveCompare:a] == NSOrderedSame) return l;
    // the language alone: a bare one its home region first (en -> en_US,
    // fr -> fr_FR), else the first ll_* the recognizer has (es-UY -> es_419)
    NSString* base = [a componentsSeparatedByString:@"_"].firstObject;
    if ([a isEqualToString:base]) {
      NSString* home = [base.lowercaseString isEqualToString:@"en"]
                           ? @"en_US"
                           : [NSString stringWithFormat:@"%@_%@", base.lowercaseString, base.uppercaseString];
      for (NSString* l in sup)
        if ([l caseInsensitiveCompare:home] == NSOrderedSame) return l;
    }
    for (NSString* l in sup)
      if ([[l componentsSeparatedByString:@"_"].firstObject caseInsensitiveCompare:base] == NSOrderedSame) return l;
  }
  return @"en_US";
}

- (SFSpeechRecognizer*)sfFor:(NSString*)lang {
  if (_sfRec && [_sfFor isEqualToString:lang]) return _sfRec;
  _sfRec = [[SFSpeechRecognizer alloc] initWithLocale:[NSLocale localeWithLocaleIdentifier:[self sfResolve:lang]]];
  _sfFor = lang;
  return _sfRec;
}

- (void)warm:(NSString*)lang reply:(void (^)(id))reply {
  if (!_audio) _audio = [AVAudioEngine new];  // the object only: no input node, no microphone
  if ([_engine isEqualToString:@"analyzer"]) {
    NSString* eng = _engine;
    [_sa warmWithLang:lang
                 done:^(NSString* loc, BOOL ready, NSString* err) {
                   NSMutableDictionary* o =
                       [@{@"ok" : @YES, @"engine" : eng, @"locale" : loc ?: @"", @"ready" : @(ready)} mutableCopy];
                   if (self->_sa.module.length) o[@"module"] = self->_sa.module;  // dictation | speech
                   if (err) o[@"err"] = err;
                   reply(o);
                 }];
  } else if ([_engine isEqualToString:@"sfspeech"]) {
    SFSpeechRecognizer* r = [self sfFor:lang];
    BOOL ready = r && r.isAvailable;
    if (@available(macOS 10.15, *)) ready = ready && r.supportsOnDeviceRecognition;
    reply(@{@"ok" : @YES,
            @"engine" : _engine,
            @"locale" : [r.locale.localeIdentifier stringByReplacingOccurrencesOfString:@"_" withString:@"-"] ?: @"",
            @"ready" : @(ready)});
  } else {
    reply(@{@"ok" : @YES, @"engine" : @"none", @"locale" : @"", @"ready" : @NO});
  }
}

// ------------------------------------------------------------ start

- (void)start:(NSString*)lang reply:(void (^)(id))reply {
  long prev = _live.load();
  if (prev) [self cancel:prev > 0 ? prev : -prev];  // one microphone: a new start drops the one capturing
  long u = ++_u;
  {
    std::lock_guard<std::mutex> g(_m);
    _utts[u].t0 = now_ms();
  }
  _live = -u;  // claimed, not capturing yet (a cancel during the asks below sees it)
  _lang = lang;
  if ([_engine isEqualToString:@"none"]) {
    [self forget:u];
    reply(@{@"ok" : @NO, @"code" : @"no-engine"});
    return;
  }
  NSString* file = [self voiceFile];
  void (^fail)(NSString*) = ^(NSString* code) {
    [self forget:u];
    reply(@{@"ok" : @NO, @"code" : code});
  };
  void (^go)(void) = ^{
    if (self->_live.load() != -u) {  // cancelled while a prompt was up
      [self forget:u];
      reply(@{@"ok" : @NO, @"code" : @"cancelled"});
      return;
    }
    [self begin:u lang:lang file:file reply:reply fail:fail];
  };
  void (^speech)(void) = ^{
    if (![self->_engine isEqualToString:@"sfspeech"]) return go();
    SFSpeechRecognizerAuthorizationStatus s = [SFSpeechRecognizer authorizationStatus];
    if (s == SFSpeechRecognizerAuthorizationStatusAuthorized) return go();
    if (s != SFSpeechRecognizerAuthorizationStatusNotDetermined) return fail(@"speech-denied");
    [SFSpeechRecognizer requestAuthorization:^(SFSpeechRecognizerAuthorizationStatus st) {
      dispatch_async(dispatch_get_main_queue(), ^{
        if (st == SFSpeechRecognizerAuthorizationStatusAuthorized) go();
        else fail(@"speech-denied");
      });
    }];
  };
  if (file) return speech();
  AVAuthorizationStatus st = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio];
  if (st == AVAuthorizationStatusAuthorized) return speech();
  if (st != AVAuthorizationStatusNotDetermined) return fail(@"mic-denied");
  [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio
                           completionHandler:^(BOOL granted) {
                             dispatch_async(dispatch_get_main_queue(), ^{
                               if (granted) speech();
                               else fail(@"mic-denied");
                             });
                           }];
}

- (void)forget:(long)u {
  std::lock_guard<std::mutex> g(_m);
  _utts.erase(u);
  long want = -u;
  _live.compare_exchange_strong(want, 0);
}

- (void)begin:(long)u
         lang:(NSString*)lang
         file:(NSString*)file
        reply:(void (^)(id))reply
         fail:(void (^)(NSString*))fail {
  AVAudioFormat* fmt = nil;
  AVAudioFile* af = nil;
  AVAudioInputNode* in = nil;
  if (file) {
    af = [[AVAudioFile alloc] initForReading:[NSURL fileURLWithPath:file] error:nil];
    if (!af) return fail(@"no-mic");
    fmt = af.processingFormat;
  } else {
    if (!_audio) _audio = [AVAudioEngine new];
    in = _audio.inputNode;
    fmt = [in outputFormatForBus:0];
  }
  if (!fmt || fmt.sampleRate <= 0 || fmt.channelCount == 0) return fail(@"no-mic");
  {
    std::lock_guard<std::mutex> g(_m);
    _sq = 0;
    _n = 0;
    _per = std::max<long>(1, (long)(fmt.sampleRate * 0.05));
  }
  // the reply waits for two things: the capture running and the locale
  // known (at once for the warmed language; a few ms' lookup for another,
  // while the audio is already being held)
  __block NSString* locale = nil;
  __block BOOL captured = NO, replied = NO;
  NSString* eng = _engine;
  void (^answer)(void) = ^{
    if (replied || !captured || !locale) return;
    replied = YES;
    double ms = 0;
    {
      std::lock_guard<std::mutex> g(self->_m);
      auto it = self->_utts.find(u);
      if (it != self->_utts.end()) {
        ms = it->second.tCap - it->second.t0;
        it->second.locale = locale.UTF8String ?: "";
      }
    }
    reply(@{@"ok" : @YES, @"u" : @(u), @"engine" : eng, @"locale" : locale});
    [self emit:@{@"ev" : @"listening", @"u" : @(u), @"ms" : @(lround(ms)), @"engine" : eng, @"locale" : locale}];
  };
  if (_sa) {
    __weak CCVoice* me = self;
    BOOL ok = [_sa beginWithLang:lang
        input:fmt
        located:^(NSString* loc) {
          dispatch_async(dispatch_get_main_queue(), ^{
            locale = loc ?: @"";
            answer();
          });
        }
        onResult:^(BOOL fin, NSString* text) {
          [me result:u final:fin text:text];
        }
        onEnd:^(NSString* err) {
          [me ended:u err:err];
        }];
    if (!ok) return fail(@"no-engine");
  } else {
    SFSpeechRecognizer* r = [self sfFor:lang];
    if (!r || !r.isAvailable) return fail(@"no-engine");
    SFSpeechAudioBufferRecognitionRequest* req = [SFSpeechAudioBufferRecognitionRequest new];
    req.shouldReportPartialResults = YES;
    req.requiresOnDeviceRecognition = YES;  // never the network
    if (@available(macOS 13.0, *)) req.addsPunctuation = YES;
    __weak CCVoice* me = self;
    SFSpeechRecognitionTask* task =
        [r recognitionTaskWithRequest:req
                        resultHandler:^(SFSpeechRecognitionResult* res, NSError* e) {
                          if (res) [me result:u final:res.isFinal text:res.bestTranscription.formattedString];
                          if ((res && res.isFinal) || e)
                            [me ended:u err:(e && !(res && res.isFinal) && !no_speech(e)) ? e.localizedDescription : nil];
                        }];
    {
      std::lock_guard<std::mutex> g(_m);
      _utts[u].sfReq = req;
      _utts[u].sfTask = task;
    }
    locale = [r.locale.localeIdentifier stringByReplacingOccurrencesOfString:@"_" withString:@"-"];
  }
  _live = u;
  if (file) {
    _fileStop = false;
    if (_fileThread.joinable()) _fileThread.join();
    __weak CCVoice* me = self;
    _fileThread = std::thread([me, af, fmt, u] {
      // real time: each block is delivered when it would have been captured;
      // past the file's end, silence, as a quiet room keeps giving a
      // microphone (so a pause can end a toggle), until the stop
      const AVAudioFrameCount chunk = 1024;
      auto t0 = std::chrono::steady_clock::now();
      long frames = 0;
      bool eof = false;
      for (;;) {
        CCVoice* s = me;
        if (!s || s->_fileStop.load()) return;
        AVAudioPCMBuffer* b = [[AVAudioPCMBuffer alloc] initWithPCMFormat:fmt frameCapacity:chunk];
        if (!eof && (![af readIntoBuffer:b error:nil] || b.frameLength == 0)) {
          eof = true;
          double ms = 0;
          {
            std::lock_guard<std::mutex> g(s->_m);
            auto it = s->_utts.find(u);
            if (it != s->_utts.end()) ms = now_ms() - it->second.t0;
          }
          [s emit:@{@"ev" : @"eof", @"u" : @(u), @"ms" : @(lround(ms))}];
        }
        if (eof) {
          b.frameLength = chunk;
          for (AVAudioChannelCount c = 0; c < fmt.channelCount; ++c)
            if (b.floatChannelData) memset(b.floatChannelData[c], 0, chunk * sizeof(float));
        }
        frames += b.frameLength;
        s = nil;
        std::this_thread::sleep_until(t0 + std::chrono::duration<double>(frames / fmt.sampleRate));
        s = me;
        if (!s || s->_fileStop.load()) return;
        [s audio:b u:u];
      }
    });
  } else {
    __weak CCVoice* me = self;
    [in installTapOnBus:0
             bufferSize:1024
                 format:fmt
                  block:^(AVAudioPCMBuffer* b, AVAudioTime* when) {
                    [me audio:b u:u];
                  }];
    _tapped = YES;
    [_audio prepare];
    NSError* e = nil;
    if (![_audio startAndReturnError:&e]) {
      [self stopCapture];
      [self dropRecognizer:u live:YES];
      return fail(@"no-mic");
    }
  }
  {
    std::lock_guard<std::mutex> g(_m);
    _utts[u].tCap = now_ms();
  }
  captured = YES;
  answer();
}

// one captured block (the audio thread, or the file reader's)
- (void)audio:(AVAudioPCMBuffer*)b u:(long)u {
  if (_live.load() != u || b.frameLength == 0) return;
  AVAudioPCMBuffer* c = copy_buf(b);
  if (!c) return;
  double rms = -1;
  SFSpeechAudioBufferRecognitionRequest* req = nil;
  {
    std::lock_guard<std::mutex> g(_m);
    auto it = _utts.find(u);
    if (it == _utts.end()) return;
    if (const float* d = c.floatChannelData ? c.floatChannelData[0] : nullptr) {
      for (AVAudioFrameCount i = 0; i < c.frameLength; ++i) _sq += (double)d[i] * d[i];
      _n += c.frameLength;
      // the first block's level at once (the meter moves at the press), then every ~50 ms
      if (_n >= _per || !it->second.tLevel) {
        rms = std::sqrt(_sq / _n);
        _sq = 0;
        _n = 0;
        if (!it->second.tLevel) it->second.tLevel = now_ms();
      }
    }
    req = it->second.sfReq;
  }
  if (_sa) [_sa feed:c];
  else [req appendAudioPCMBuffer:c];
  if (rms >= 0)
    [self emit:@{@"ev" : @"level", @"u" : @(u), @"rms" : @(std::round(std::min(1.0, rms) * 10000) / 10000)}];
}

// ------------------------------------------------------------ results

- (void)result:(long)u final:(BOOL)fin text:(NSString*)text {
  std::string whole;
  {
    std::lock_guard<std::mutex> g(_m);
    auto it = _utts.find(u);
    if (it == _utts.end() || it->second.ended) return;
    it->second.seg.result(fin, text.UTF8String ?: "");
    whole = it->second.seg.whole();
    if (whole == it->second.sent) return;
    it->second.sent = whole;
    if (!it->second.tPartial && !whole.empty()) it->second.tPartial = now_ms();
  }
  [self emit:@{@"ev" : @"partial", @"u" : @(u), @"text" : @(whole.c_str())}];
}

- (void)ended:(long)u err:(NSString*)err {
  dispatch_async(dispatch_get_main_queue(), ^{
    std::string whole;
    Utt x;
    {
      std::lock_guard<std::mutex> g(self->_m);
      auto it = self->_utts.find(u);
      if (it == self->_utts.end() || it->second.ended) return;  // cancelled, or already ended
      it->second.ended = true;
      whole = it->second.seg.whole();
      x = it->second;
      self->_utts.erase(it);
    }
    long want = u;
    if (self->_live.compare_exchange_strong(want, 0)) [self stopCapture];  // it ended on its own
    if (err) [self emit:@{@"ev" : @"err", @"u" : @(u), @"code" : @"recognizer", @"msg" : err}];
    [self emit:@{@"ev" : @"final", @"u" : @(u), @"text" : @(whole.c_str())}];
    [self emit:@{@"ev" : @"done", @"u" : @(u)}];
    if (timing_on()) {
      double tf = now_ms();
      auto d = [](double a, double b) { return (a && b) ? (long)lround(a - b) : -1L; };
      fprintf(stderr,
              "[shell-timing] voice: start->capture %ld ms, ->first level %ld ms, ->first partial %ld ms; "
              "stop->final %ld ms; engine %s%s locale %s\n",
              d(x.tCap, x.t0), d(x.tLevel, x.t0), d(x.tPartial, x.t0), d(tf, x.tStop), self->_engine.UTF8String,
              self->_sa ? [@"/" stringByAppendingString:self->_sa.module].UTF8String : "", x.locale.c_str());
    }
    // ready again: the next analyzer prepared for the same language
    if (self->_sa && self->_live.load() == 0) [self->_sa warmWithLang:self->_lang ?: @"" done:^(NSString*, BOOL, NSString*){}];
  });
}

// ------------------------------------------------------------ stop, cancel

- (void)stopCapture {
  _live = 0;
  _fileStop = true;
  if (_fileThread.joinable()) _fileThread.join();
  if (_tapped) {
    [_audio.inputNode removeTapOnBus:0];
    _tapped = NO;
  }
  if (_audio.running) [_audio stop];  // the microphone (and its indicator) off
}

- (void)stop:(long)u {
  if (!u || _live.load() != u) return;  // not capturing (already stopped, or another one)
  [self stopCapture];
  SFSpeechAudioBufferRecognitionRequest* req = nil;
  {
    std::lock_guard<std::mutex> g(_m);
    auto it = _utts.find(u);
    if (it == _utts.end()) return;
    it->second.tStop = now_ms();
    req = it->second.sfReq;
  }
  if (_sa) [_sa finish];
  else [req endAudio];
}

// live: u is the utterance capturing (or claimed), so the analyzer's live
// utterance is its own; a stopped one that is still finishing is only forgotten
- (void)dropRecognizer:(long)u live:(BOOL)live {
  SFSpeechRecognitionTask* task = nil;
  {
    std::lock_guard<std::mutex> g(_m);
    auto it = _utts.find(u);
    if (it != _utts.end()) task = it->second.sfTask;
    _utts.erase(u);
  }
  if (_sa && live) [_sa cancel];
  [task cancel];
}

- (void)cancel:(long)u {
  long l = _live.load();
  BOOL live = u && (l == u || l == -u);
  if (live) [self stopCapture];
  if (u) {
    bool known;
    {
      std::lock_guard<std::mutex> g(_m);
      known = _utts.count(u) > 0;
    }
    if (known) [self dropRecognizer:u live:live];
  }
}

- (void)shutdown {
  long l = _live.load();
  [self stopCapture];
  if (l) [self dropRecognizer:l > 0 ? l : -l live:YES];
}

@end
