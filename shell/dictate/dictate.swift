// concorde-dictate (6b543, 6b641): the on-device speech helper ConcordeAI's
// server runs on macOS 26 and later. Apple's SpeechAnalyzer, the system's own
// recognizer, behind a pipe: NOTHING LEAVES THIS COMPUTER. No network code is
// in this file (the gauntlet lints it), the model is the OS's, and the only
// permission is the microphone's, which the page already holds.
//
// args: --locale <BCP-47> picks the language (6b641, per Patrick: "What's up,
// man?" came out "Hvittar"). Never detected from the audio: the one named,
// else the system's preferred languages in order, the first the recognizer
// has, else en-US. "ready" carries the locale chosen.
// stdin, frames: u32 little-endian length + PCM int16 16 kHz mono; a zero
// length ends the utterance (the final follows). 0xFFFFFFFF followed by a
// u32 is BEGIN: the utterance's number, from the server, so the two can never
// drift apart (6b641: an empty utterance used to be counted by the server and
// not here, and every line after it was dropped as an old one's). The process
// stays for the next one, the model kept loaded.
// stdout, JSON lines: {"ev":..,"text":..,"u":N,"ms":T} with ev one of
// ready, listening, partial, final, done, err. The recognizer finalises an
// utterance in SEGMENTS ("What's up?" then " Man."), and its volatile results
// cover only the segment under way, so every partial here is the WHOLE
// utterance so far (the finalised segments + the volatile one), and the one
// final, at the end, is all of it. A clip with nothing in it still gets a
// final ("") and a done, so the server never waits out a timer.
import Foundation
import AVFoundation
import Speech

let t0 = Date()
let out = FileHandle.standardOutput
let lock = NSLock()
func emit(_ ev: String, _ text: String = "", _ n: Int = 0) {
    let d: [String: Any] = ["ev": ev, "text": text, "u": n, "ms": Int(Date().timeIntervalSince(t0) * 1000)]
    let j = try! JSONSerialization.data(withJSONObject: d)
    lock.lock(); out.write(j); out.write("\n".data(using: .utf8)!); lock.unlock()
}
let inFmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
func readExact(_ n: Int) -> Data? {
    var d = Data()
    while d.count < n {
        let more = FileHandle.standardInput.readData(ofLength: n - d.count)
        if more.isEmpty { return nil }
        d.append(more)
    }
    return d
}
enum Frame { case eof, end, begin(Int), audio(Data) }
func readFrame() -> Frame {
    guard let h = readExact(4) else { return .eof }
    let n = h.withUnsafeBytes { $0.load(as: UInt32.self) }   // host is little-endian
    if n == 0 { return .end }
    if n == 0xFFFF_FFFF {
        guard let u = readExact(4) else { return .eof }
        return .begin(Int(u.withUnsafeBytes { $0.load(as: UInt32.self) }))
    }
    guard let d = readExact(Int(n)) else { return .eof }
    return .audio(d)
}
func pcm(_ data: Data) -> AVAudioPCMBuffer {
    let frames = AVAudioFrameCount(data.count / 2)
    let buf = AVAudioPCMBuffer(pcmFormat: inFmt, frameCapacity: frames)!
    buf.frameLength = frames
    _ = data.withUnsafeBytes { p in memcpy(buf.int16ChannelData![0], p.baseAddress!, data.count) }
    return buf
}
// the finalised segments joined, then the one under way: single spaces, no edges
func joined(_ parts: [String]) -> String {
    parts.joined(separator: " ").split(whereSeparator: { $0 == " " || $0 == "\n" || $0 == "\t" })
        .joined(separator: " ")
}

// The module (6b641): DictationTranscriber, the one keyboard dictation uses,
// with punctuation; measured against SpeechTranscriber on the same clips it
// wrote the first word ~300 ms sooner (730 vs 1050 ms), refined more often,
// punctuated as a person would ("What's up, man?" where SpeechTranscriber
// wrote "What's up? Man.") and got the words right ("Montevideo" where it
// wrote "Montivity"). SpeechTranscriber stays for a language dictation
// doesn't carry.
@available(macOS 26, *)
struct Engine {
    let dictation: Bool
    let locale: Locale
    // a module and its results as (text, isFinal)
    func make() -> (any SpeechModule, AsyncThrowingStream<(String, Bool), Error>) {
        let (st, c) = AsyncThrowingStream<(String, Bool), Error>.makeStream()
        if dictation {
            let m = DictationTranscriber(locale: locale, contentHints: [.shortForm], transcriptionOptions: [.punctuation],
                                         reportingOptions: [.volatileResults, .frequentFinalization], attributeOptions: [])
            Task { do { for try await r in m.results { c.yield((String(r.text.characters), r.isFinal)) }; c.finish() } catch { c.finish(throwing: error) } }
            return (m, st)
        }
        let m = SpeechTranscriber(locale: locale, transcriptionOptions: [],
                                  reportingOptions: [.volatileResults, .fastResults], attributeOptions: [])
        Task { do { for try await r in m.results { c.yield((String(r.text.characters), r.isFinal)) }; c.finish() } catch { c.finish(throwing: error) } }
        return (m, st)
    }
}

@available(macOS 26, *)
func pickEngine(_ asked: String?) async -> Engine {
    var want: [String] = []
    if let a = asked, !a.isEmpty { want.append(a) }
    want += Locale.preferredLanguages
    for w in want {
        if let l = await DictationTranscriber.supportedLocale(equivalentTo: Locale(identifier: w)) { return Engine(dictation: true, locale: l) }
        if let l = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: w)) { return Engine(dictation: false, locale: l) }
    }
    return Engine(dictation: true, locale: Locale(identifier: "en-US"))
}

@available(macOS 26, *)
func run() async {
    var asked: String? = nil
    let args = CommandLine.arguments
    if let i = args.firstIndex(of: "--locale"), i + 1 < args.count { asked = args[i + 1] }
    let engine = await pickEngine(asked)
    let locale = engine.locale
    let (first, _) = engine.make()
    do {
        if let r = try await AssetInventory.assetInstallationRequest(supporting: [first]) {
            emit("info", "installing assets"); try await r.downloadAndInstall()
        }
    } catch { emit("err", "assets: \(error)"); exit(1) }
    guard let fmt = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [first]) else { emit("err", "no format"); exit(1) }
    let conv = AVAudioConverter(from: inFmt, to: fmt)!
    let opts = SpeechAnalyzer.Options(priority: .userInitiated, modelRetention: .processLifetime)
    var n = 0
    // the next utterance's analyzer, made ahead so a BEGIN finds it ready
    var next: (AsyncThrowingStream<(String, Bool), Error>, SpeechAnalyzer)? = nil
    func prepare() -> (AsyncThrowingStream<(String, Bool), Error>, SpeechAnalyzer) {
        let (m, rs) = engine.make()
        return (rs, SpeechAnalyzer(modules: [m], options: opts))
    }
    next = prepare()
    emit("ready", locale.identifier(.bcp47) + (engine.dictation ? "" : " speech"))
    var pending: Frame? = nil
    while true {
        // an utterance starts with BEGIN (its number) or, from an older server, audio
        let f = pending ?? readFrame(); pending = nil
        var u: Int
        var data: Data? = nil
        switch f {
        case .eof: return
        case .end: continue                       // an end with no utterance open: nothing to do
        case .begin(let k): u = k; n = max(n, k)
        case .audio(let d): n += 1; u = n; data = d
        }
        let (rs, analyzer) = next ?? prepare(); next = nil
        let (stream, cont) = AsyncStream<AnalyzerInput>.makeStream()
        let results = Task { () -> String in
            var done: [String] = []
            var last = ""
            do {
                for try await (t, fin) in rs {
                    if fin { done.append(t) }
                    let all = joined(done + (fin ? [] : [t]))
                    if all != last && !all.isEmpty { emit("partial", all, u); last = all }
                }
            } catch { emit("err", "\(error)", u) }
            return joined(done)
        }
        do { try await analyzer.start(inputSequence: stream) } catch { emit("err", "start: \(error)", u); emit("final", "", u); emit("done", "", u); next = prepare(); continue }
        emit("listening", "", u)
        var fedAny = false
        func feed(_ d: Data) {
            fedAny = true
            let b = pcm(d)
            let o = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(Double(b.frameLength) * fmt.sampleRate / 16000) + 16)!
            var err: NSError? = nil; var fed = false
            conv.convert(to: o, error: &err) { _, st in if fed { st.pointee = .noDataNow; return nil }; fed = true; st.pointee = .haveData; return b }
            cont.yield(AnalyzerInput(buffer: o))
        }
        if let d = data { feed(d) }
        var eof = false
        loop: while true {
            switch readFrame() {
            case .audio(let d): feed(d)
            case .end: break loop
            case .begin(let k): pending = .begin(k); break loop   // a new one before this one ended: this one ends here
            case .eof: eof = true; break loop
            }
        }
        // an utterance with no audio at all (a microphone that failed, a slip): the
        // analyzer never finishes one that was given nothing, so it gets 0.1 s of
        // silence, which it finishes as "" (6b641; without this the helper hung there)
        if !fedAny { feed(Data(count: 3200)) }
        cont.finish()
        do { try await analyzer.finalizeAndFinishThroughEndOfInput() } catch { emit("err", "finish: \(error)", u) }
        let text = await results.value
        emit("final", text, u)
        emit("done", "", u)
        if eof { return }
        next = prepare()
    }
}
if #available(macOS 26, *) {
    let g = DispatchGroup(); g.enter()
    Task { await run(); g.leave() }
    g.wait(); exit(0)
} else { emit("err", "needs macOS 26"); exit(2) }
