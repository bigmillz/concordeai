// The C++ shell's on-device recognizer, macOS 26 and later (NOTES 6b642).
// Apple's SpeechAnalyzer + SpeechTranscriber are Swift-only, so this one
// small file is compiled into the shell's own binary (no helper process,
// no Python) and exposed to voice.mm as an Objective-C class, CCSAEngine
// (declared by hand in voice_sa.h). The module is DictationTranscriber
// (punctuated, sooner), SpeechTranscriber for a language it lacks. Capture,
// levels, the bridge and the
// joining of segments are voice.mm's; this file only recognises.
//
// What it does, measured against concorde-dictate (6b543) on this Mac:
//   * warm(lang:): the locale is resolved (never detected: the page's
//     choice, else the system's preferred languages in order, the first the
//     transcriber supports, else en-US), its assets installed if missing,
//     and the NEXT utterance's transcriber + analyzer made and prepared, so
//     a start finds the model loaded and an analyzer waiting;
//   * begin(): the input stream exists at once, so audio fed before the
//     analyzer has started is held in the stream (the pre-roll) and nothing
//     said at the press is lost;
//   * results: one callback per result, final or volatile, for the
//     CURRENT SEGMENT only (SpeechTranscriber finalises an utterance in
//     several segments: "What's up?" then " Man."); voice.mm joins them.
// NOTHING LEAVES THIS COMPUTER: no network API is used here; the model is
// the OS's own.
import AVFoundation
import Foundation
import Speech

@objc(CCSAEngine)
public final class CCSAEngine: NSObject {
  /// The analyzer can run here: macOS 26+, and SpeechTranscriber says it
  /// can (DictationTranscriber rides the same analyzer).
  @objc public static func available() -> Bool {
    if #available(macOS 26, *) { return SpeechTranscriber.isAvailable }
    return false
  }

  // the prepared pair (macOS 26 types, held untyped so the class itself
  // needs no availability)
  private var nextPair: AnyObject?
  private var live: AnyObject?
  private var localeId = ""
  private var pickDictation = true
  private var warmedLang: String?
  /// the locale the last warm() resolved, BCP-47 ("" before one has)
  @objc public private(set) var locale = ""
  /// the module it chose: "dictation" or "speech" ("" before a warm)
  @objc public private(set) var module = ""
  private let q = DispatchQueue(label: "concorde.voice.sa")
  @objc public private(set) var ready = false

  /// Resolve the locale, install its assets, prepare the next analyzer.
  /// done(localeBCP47, ready, error) on an arbitrary queue.
  @objc public func warm(lang: String, done: @escaping (String, Bool, String?) -> Void) {
    guard #available(macOS 26, *) else { done("", false, "needs macOS 26"); return }
    Task {
      do {
        let pick = await Self.resolve(lang)
        let changed = pick.locale.identifier != localeId || pick.dictation != pickDictation
        localeId = pick.locale.identifier
        pickDictation = pick.dictation
        warmedLang = lang
        locale = pick.locale.identifier(.bcp47)
        module = pick.dictation ? "dictation" : "speech"
        if changed || nextPair == nil {
          let pair = try await Pair.make(pick)
          q.sync { nextPair = pair }
        }
        ready = true
        done(pick.locale.identifier(.bcp47), true, nil)
      } catch {
        ready = false
        done("", false, "\(error)")
      }
    }
  }

  /// The module and locale for a lang: for each wanted tag (lang, else the
  /// system's preferred languages in order) DictationTranscriber's
  /// equivalent first, then SpeechTranscriber's; else en-US dictation.
  @available(macOS 26, *)
  static func resolve(_ lang: String) async -> Pick {
    var asks: [String] = []
    if !lang.isEmpty { asks.append(lang) } else { asks.append(contentsOf: Locale.preferredLanguages) }
    for a in asks {
      if let l = await DictationTranscriber.supportedLocale(equivalentTo: Locale(identifier: a)) {
        return Pick(dictation: true, locale: l)
      }
      if let l = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: a)) {
        return Pick(dictation: false, locale: l)
      }
    }
    return Pick(dictation: true, locale: Locale(identifier: "en-US"))
  }

  /// Start an utterance on the prepared analyzer (or a fresh one if warm
  /// never ran). `input` is the format feed() will be given. onResult(final,
  /// text) per result of the current segment; onEnd(error or nil) once.
  /// A start for a language warm() didn't resolve still begins at once:
  /// the audio is held until that language's analyzer is ready. located()
  /// gives the locale (BCP-47) once known: at once for the warmed language,
  /// after a lookup (a few ms) for another.
  @objc public func begin(lang: String, input: AVAudioFormat,
                          located: @escaping (String) -> Void,
                          onResult: @escaping (Bool, String) -> Void,
                          onEnd: @escaping (String?) -> Void) -> Bool {
    guard #available(macOS 26, *) else { return false }
    let same = warmedLang == lang && !localeId.isEmpty
    let pair: Pair? = same ? q.sync { let p = nextPair as? Pair; nextPair = nil; return p } : nil
    let u = Utterance(input: input, onResult: onResult, onEnd: onEnd)
    q.sync { live = u }
    let known = Pick(dictation: pickDictation, locale: Locale(identifier: localeId))
    if same { located(locale) }
    u.run(pair: pair, resolve: same ? nil : lang, pick: known, located: same ? nil : located)
    if same {
      // the next one is prepared while this one runs
      Task { [weak self] in
        guard let self else { return }
        if let p = try? await Pair.make(known) { self.q.sync { if self.nextPair == nil { self.nextPair = p } } }
      }
    }
    return true
  }

  /// One captured buffer, in the format begin() was given (any thread).
  @objc public func feed(_ buf: AVAudioPCMBuffer) {
    guard #available(macOS 26, *) else { return }
    (q.sync { live } as? Utterance)?.feed(buf)
  }

  /// The end of the audio: what is left is finalised, onEnd follows.
  @objc public func finish() {
    guard #available(macOS 26, *) else { return }
    let u: Utterance? = q.sync { let x = live as? Utterance; live = nil; return x }
    u?.finish()
  }

  /// Dropped: no more callbacks for it.
  @objc public func cancel() {
    guard #available(macOS 26, *) else { return }
    let u: Utterance? = q.sync { let x = live as? Utterance; live = nil; return x }
    u?.cancel()
  }
}

@available(macOS 26, *)
struct Pick {
  let dictation: Bool
  let locale: Locale
}

// A module, its analyzer and its results as (final, text). DictationTranscriber
// (6b642, measured by beta-4-voice on the same clips: the first word ~735 ms
// after the audio starts against ~1050 ms, partials every ~300 ms, "What's up,
// man?" where SpeechTranscriber writes "What's up? Man.", "Montevideo" for
// "Montivity"); SpeechTranscriber for a language dictation doesn't carry.
@available(macOS 26, *)
final class Pair {
  let analyzer: SpeechAnalyzer
  let format: AVAudioFormat
  let results: AsyncThrowingStream<(Bool, String), Error>

  init(_ a: SpeechAnalyzer, _ f: AVAudioFormat, _ r: AsyncThrowingStream<(Bool, String), Error>) {
    analyzer = a; format = f; results = r
  }

  static func make(_ pick: Pick) async throws -> Pair {
    let (rs, c) = AsyncThrowingStream<(Bool, String), Error>.makeStream()
    let t: any SpeechModule
    if pick.dictation {
      let m = DictationTranscriber(locale: pick.locale, contentHints: [.shortForm], transcriptionOptions: [.punctuation],
                                   reportingOptions: [.volatileResults, .frequentFinalization], attributeOptions: [])
      Task {
        do { for try await r in m.results { c.yield((r.isFinal, String(r.text.characters))) }; c.finish() }
        catch { c.finish(throwing: error) }
      }
      t = m
    } else {
      let m = SpeechTranscriber(locale: pick.locale, transcriptionOptions: [],
                                reportingOptions: [.volatileResults, .fastResults], attributeOptions: [])
      Task {
        do { for try await r in m.results { c.yield((r.isFinal, String(r.text.characters))) }; c.finish() }
        catch { c.finish(throwing: error) }
      }
      t = m
    }
    if let r = try await AssetInventory.assetInstallationRequest(supporting: [t]) {
      try await r.downloadAndInstall()   // the OS's own model, installed once
    }
    let opts = SpeechAnalyzer.Options(priority: .userInitiated, modelRetention: .processLifetime)
    let a = SpeechAnalyzer(modules: [t], options: opts)
    guard let f = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [t]) else {
      throw NSError(domain: "concorde.voice", code: 1, userInfo: [NSLocalizedDescriptionKey: "no audio format"])
    }
    try await a.prepareToAnalyze(in: f)   // the model loaded now, not at the press
    return Pair(a, f, rs)
  }
}

@available(macOS 26, *)
final class Utterance {
  let input: AVAudioFormat
  let onResult: (Bool, String) -> Void
  let onEnd: (String?) -> Void
  let (stream, cont) = AsyncStream<AnalyzerInput>.makeStream()
  private var conv: AVAudioConverter?
  private var outFmt: AVAudioFormat?
  private var pending: [AVAudioPCMBuffer] = []   // fed before the format is known
  private var pushedAny = false
  private let lock = NSLock()
  private var cancelled = false
  private var ended = false
  private var task: Task<Void, Never>?

  init(input: AVAudioFormat, onResult: @escaping (Bool, String) -> Void, onEnd: @escaping (String?) -> Void) {
    self.input = input; self.onResult = onResult; self.onEnd = onEnd
  }

  func run(pair: Pair?, resolve lang: String?, pick known: Pick, located: ((String) -> Void)?) {
    task = Task {
      do {
        let p: Pair
        if let pair { p = pair } else {
          let pick = lang != nil ? await CCSAEngine.resolve(lang!) : known
          located?(pick.locale.identifier(.bcp47))
          p = try await Pair.make(pick)
        }
        self.attach(p.format)
        let results = Task {
          do {
            for try await (fin, text) in p.results {
              if self.isCancelled { break }
              self.onResult(fin, text)
            }
            return nil as String?
          } catch { return "\(error)" }
        }
        try await p.analyzer.start(inputSequence: self.stream)
        // the stream ends at finish() (or cancel); everything fed is analysed
        await self.waitEnded()
        if self.isCancelled {
          await p.analyzer.cancelAndFinishNow()
        } else {
          try await p.analyzer.finalizeAndFinishThroughEndOfInput()
        }
        let err = await results.value
        if !self.isCancelled { self.onEnd(err) }
      } catch {
        if !self.isCancelled { self.onEnd("\(error)") }
      }
    }
  }

  // the analyzer's format is known: the converter made and the pre-roll
  // pushed, in order, under the lock (a feed racing this waits its turn);
  // a finish() that came first ends the stream now
  private func attach(_ f: AVAudioFormat) {
    lock.lock(); defer { lock.unlock() }
    outFmt = f
    conv = AVAudioConverter(from: input, to: f)
    pending.forEach { push($0) }
    pending = []
    if ended { endStream() }
  }

  private var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }

  private var endWaiters: [CheckedContinuation<Void, Never>] = []
  private func waitEnded() async {
    await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
      lock.lock()
      if ended { lock.unlock(); c.resume(); return }
      endWaiters.append(c)
      lock.unlock()
    }
  }

  private func markEnded() {
    lock.lock()
    ended = true
    let w = endWaiters
    endWaiters = []
    lock.unlock()
    w.forEach { $0.resume() }
  }

  func feed(_ b: AVAudioPCMBuffer) {
    lock.lock(); defer { lock.unlock() }
    if ended || cancelled { return }
    if conv == nil { pending.append(b); return }   // the pre-roll
    push(b)
  }

  // converted to the analyzer's format and yielded (the stream buffers it
  // until the analyzer reads)
  private func push(_ b: AVAudioPCMBuffer) {
    guard let conv, let outFmt else { return }
    let cap = AVAudioFrameCount(Double(b.frameLength) * outFmt.sampleRate / b.format.sampleRate) + 32
    guard let o = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: cap) else { return }
    var fed = false
    var err: NSError?
    conv.convert(to: o, error: &err) { _, st in
      if fed { st.pointee = .noDataNow; return nil }
      fed = true; st.pointee = .haveData; return b
    }
    if err == nil && o.frameLength > 0 {
      pushedAny = true
      cont.yield(AnalyzerInput(buffer: o))
    }
  }

  // the stream's end (under the lock). An utterance given no audio at all (a
  // stop before the first tap block) would hang the analyzer in
  // finalizeAndFinishThroughEndOfInput (found by beta-4-voice's helper), so
  // it gets 0.1 s of silence first, which finishes as ""
  private func endStream() {
    if !pushedAny, let outFmt,
       let z = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: AVAudioFrameCount(outFmt.sampleRate / 10)) {
      z.frameLength = z.frameCapacity
      let abl = UnsafeMutableAudioBufferListPointer(z.mutableAudioBufferList)
      for b in abl { if let d = b.mData { memset(d, 0, Int(b.mDataByteSize)) } }
      pushedAny = true
      cont.yield(AnalyzerInput(buffer: z))
    }
    cont.finish()
  }

  func finish() {
    lock.lock()
    ended = true
    let ready = conv != nil
    if ready { endStream() }  // else attach() ends the stream once the pre-roll is pushed
    lock.unlock()
    markEnded()
  }

  func cancel() {
    lock.lock(); cancelled = true; lock.unlock()
    cont.finish()
    markEnded()
  }
}
